"""Backup confirmation: a dated, per-set snapshot that every file on the phone is safely backed up.

A set is Confirmed only when every present phone file in it (1) has a replica on every required
destination, (2) that replica was verified, and (3) the phone's own SHA-256 equals the backed-up
hash. These are checks 1 to 3 of the delete preflight, run read-only over the whole set. The
phone is asked only about files never checked or changed since their last check (batched). The
report is kept in the database and written as Markdown to `DEST_ROOT/.sift/reports/` on every
plugged-in destination. A confirmation goes Stale when the set changes or after
`backup.confirmation_valid_days`. It never allows a delete by itself: the preflight rechecks.
"""

import json
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

from sift.backup.replicator import load_dest
from sift.core.clock import Clock, SystemClock
from sift.core.config import Config
from sift.core.db import Database
from sift.device.transport import Transport

log = logging.getLogger("sift.backup")

DAY_S = 86_400


def required_kinds(config: Config) -> list[str]:
    """Assumption A2: with no SSD ever chosen, the Mac alone is required."""
    if not config.backup.ssd_volume_uuid:
        return ["mac"]
    return list(config.backup.required_destinations)


@dataclass
class SetReport:
    set_name: str
    on_phone: int = 0
    bytes: int = 0
    verified: dict[str, int] = field(default_factory=dict)  # destination kind -> files
    provisional: int = 0  # adopted files not hashed yet (whole destination)
    not_backed_up: int = 0  # new or changed since the last backup
    failed: int = 0
    phone_hash_mismatch: int = 0
    phone_unchecked: int = 0  # the phone was not asked (not connected)
    required: list[str] = field(default_factory=list)
    confirmed: bool = False
    missing: dict[str, int] = field(default_factory=dict)  # destination kind -> files lacking


@dataclass(frozen=True)
class Confirmation:
    id: int
    set_name: str
    created_at: int
    files: int
    bytes: int
    confirmed: bool
    report: dict[str, object]
    stale_reason: str | None


def _rows(
    db: Database, device_id: int, set_name: str
) -> list[tuple[int, str, int, int, str, str | None, int | None, int | None]]:
    with db.read() as conn:
        return conn.execute(
            "SELECT id, remote_path, size, mtime, state, content_hash, phone_hash_at, "
            "phone_hash_mtime FROM phone_file WHERE device_id = ? AND set_name = ? AND present = 1",
            (device_id, set_name),
        ).fetchall()


def build_report(
    db: Database,
    config: Config,
    device_id: int,
    set_name: str,
    *,
    transport: Transport | None = None,
    clock: Clock | None = None,
    should_stop: Callable[[], bool] = lambda: False,
) -> SetReport:
    """Checks 1 to 3 over the set; with `transport`, unchecked files are hashed on the phone."""
    clock = clock or SystemClock()
    report = SetReport(set_name, required=required_kinds(config))
    rows = _rows(db, device_id, set_name)
    with db.read() as conn:
        kinds = {int(r[0]): str(r[1]) for r in conn.execute("SELECT id, kind FROM destination")}
        held: dict[str, set[str]] = {}
        for digest, dest_id in conn.execute(
            "SELECT r.content_hash, r.dest_id FROM replica r JOIN phone_file p "
            "ON p.content_hash = r.content_hash WHERE p.device_id = ? AND p.set_name = ? "
            "AND p.present = 1",
            (device_id, set_name),
        ):
            held.setdefault(str(digest), set()).add(kinds.get(int(dest_id), "?"))
        report.provisional = int(
            conn.execute(
                "SELECT count(*) FROM adopted_file WHERE state = 'provisional'"
            ).fetchone()[0]
        )
    to_ask: list[tuple[int, str, int, str]] = []
    for row_id, path, size, mtime, state, digest, checked_at, checked_mtime in rows:
        report.on_phone += 1
        report.bytes += int(size)
        if state == "failed":
            report.failed += 1
            continue
        if state != "done" or digest is None:
            report.not_backed_up += 1
            continue
        where = held.get(digest, set())
        for kind in where:
            report.verified[kind] = report.verified.get(kind, 0) + 1
        for kind in report.required:
            if kind not in where:
                report.missing[kind] = report.missing.get(kind, 0) + 1
        if checked_at is None or checked_mtime != mtime:
            to_ask.append((int(row_id), str(path), int(mtime), str(digest)))
    asked = 0
    if transport is not None:
        now = clock.now()
        for start in range(0, len(to_ask), 40):
            if should_stop():
                break
            batch = to_ask[start : start + 40]
            hashes = transport.sha256([path for _id, path, _mtime, _digest in batch])
            good = [(now, m, i) for i, path, m, digest in batch if hashes.get(path) == digest]
            report.phone_hash_mismatch += len(batch) - len(good)
            asked += len(batch)
            with db.tx() as conn:
                conn.executemany(
                    "UPDATE phone_file SET phone_hash_at = ?, phone_hash_mtime = ? WHERE id = ?",
                    good,
                )
    report.phone_unchecked = len(to_ask) - asked
    report.confirmed = (
        report.on_phone > 0
        and report.failed == 0
        and report.not_backed_up == 0
        and not report.missing
        and report.phone_hash_mismatch == 0
        and report.phone_unchecked == 0
    )
    return report


def _markdown(report: SetReport, when: datetime) -> str:
    gb = report.bytes / 1e9
    lines = [
        f"# {report.set_name}: backup confirmation",
        "",
        f"{when:%d %b %Y %H:%M} UTC",
        "",
        f"- Files on the phone: {report.on_phone} ({gb:.1f} GB)",
    ]
    lines += [f"- Verified on the {k.upper()}: {n}" for k, n in sorted(report.verified.items())]
    lines += [
        f"- Not backed up yet: {report.not_backed_up}",
        f"- Failed: {report.failed}",
        f"- Phone's checksum differs: {report.phone_hash_mismatch}",
        f"- Adopted files still being checked: {report.provisional}",
        "",
        f"**{'Confirmed' if report.confirmed else 'Not confirmed'}.**",
    ]
    if report.missing:
        lines.append(
            "Missing on: "
            + ", ".join(f"{k.upper()} ({n} files)" for k, n in sorted(report.missing.items()))
        )
    return "\n".join(lines) + "\n"


def confirm(
    db: Database,
    config: Config,
    device_id: int,
    set_name: str,
    transport: Transport,
    *,
    clock: Clock | None = None,
    should_stop: Callable[[], bool] = lambda: False,
) -> Confirmation:
    """Run the checks with the phone, store the snapshot and write the Markdown report."""
    clock = clock or SystemClock()
    report = build_report(
        db, config, device_id, set_name, transport=transport, clock=clock, should_stop=should_stop
    )
    now = clock.now()
    when = datetime.fromtimestamp(now, UTC)
    text = _markdown(report, when)
    written: str | None = None
    with db.read() as conn:
        dest_ids = [int(r[0]) for r in conn.execute("SELECT id FROM destination")]
    for dest_id in dest_ids:
        dest = load_dest(db, dest_id)
        if dest is None or not dest.root.is_dir():
            continue
        folder = dest.root / ".sift" / "reports"
        safe = "".join(c if c.isalnum() or c in " -_" else "_" for c in set_name).strip() or "set"
        path = folder / f"{when:%Y-%m-%d-%H%M%S}-{safe}.md"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            with open(path, "x", encoding="utf-8") as f:  # a new file; never overwrites (I1)
                f.write(text)
            written = written or str(path)
        except OSError as e:
            log.warning("Could not write the confirmation report on the %s: %s", dest.kind, e)
    with db.tx() as conn:
        cur = conn.execute(
            "INSERT INTO confirmation (set_name, device_id, created_at, files, bytes, confirmed, "
            "report, report_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                set_name,
                device_id,
                now,
                report.on_phone,
                report.bytes,
                int(report.confirmed),
                json.dumps(asdict(report)),
                written,
            ),
        )
        row_id = int(cur.lastrowid or 0)
    return Confirmation(
        row_id, set_name, now, report.on_phone, report.bytes, report.confirmed, asdict(report), None
    )


def latest(
    db: Database, config: Config, device_id: int, set_name: str, *, clock: Clock | None = None
) -> Confirmation | None:
    """The newest confirmation of the set, with why it is stale (None when still valid)."""
    clock = clock or SystemClock()
    with db.read() as conn:
        row = conn.execute(
            "SELECT id, created_at, files, bytes, confirmed, report FROM confirmation "
            "WHERE device_id = ? AND set_name = ? ORDER BY created_at DESC, id DESC LIMIT 1",
            (device_id, set_name),
        ).fetchone()
    if row is None:
        return None
    row_id, created, files, size, confirmed, report = row
    stale: str | None = None
    if clock.now() - int(created) > config.backup.confirmation_valid_days * DAY_S:
        stale = f"Older than {config.backup.confirmation_valid_days} days."
    else:
        now_rows = _rows(db, device_id, set_name)
        changed = any(r[4] != "done" for r in now_rows)
        if changed or len(now_rows) != files or sum(int(r[2]) for r in now_rows) != size:
            stale = "Files in this set changed since it was confirmed."
    return Confirmation(
        int(row_id),
        set_name,
        int(created),
        int(files),
        int(size),
        bool(confirmed),
        json.loads(report or "{}"),
        stale,
    )
