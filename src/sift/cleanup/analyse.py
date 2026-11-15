"""Analyse phone, read-only: how much of each set is already safely backed up.

The phone is only read (a listing and batched SHA-256s); the only database writes are this
run's own `analysis_run` row. Each present file is matched to the backup index by size, then the
phone's own hash decides (names are never proof). Four groups per set, with counts and sizes:

- **verified**: the phone's hash equals content that has a replica on every required destination;
- **not_verified**: same size as an adopted file that is not hashed yet (still being checked);
- **not_backed_up**: no match, or a copy is missing on a required destination;
- **problems**: the phone's hash matches content listed under Problems (damaged copy);
- **unchecked**: same size as stored content, not hashed yet (a run still going, or stopped).
  "Not backed up" always means checked and missing.

A run writes its partial result as it goes (`partial`, with `processed` of `total` files), so
the screen can show "Checking now: 2,200 of 51,106" beside the last complete result. The
result also records its scope (the sets' definitions and the destinations), so a later change
marks it out of date (plan A5).

The result is never trusted later: a delete's preflight rechecks every file.
"""

import json
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from sift.backup.confirm import current_dest_ids, required_kinds
from sift.backup.phonepaths import shown
from sift.backup.scanner import list_roots
from sift.backup.sets import resolve_roots
from sift.backup.skiprules import SkipRules
from sift.core.clock import Clock, SystemClock
from sift.core.config import Config
from sift.core.db import Database
from sift.device.transport import Transport

log = logging.getLogger("sift.cleanup")

SHA_BATCH = 40
HASH_BPS = 40 * 10**6  # measured phone hashing rate (pre-M2 inputs), for the time estimate


@dataclass
class Group:
    files: int = 0
    bytes: int = 0

    def add(self, size: int) -> None:
        self.files += 1
        self.bytes += size

    def sub(self, size: int) -> None:
        self.files -= 1
        self.bytes -= size


@dataclass
class SetResult:
    verified: Group = field(default_factory=Group)
    not_verified: Group = field(default_factory=Group)
    not_backed_up: Group = field(default_factory=Group)
    problems: Group = field(default_factory=Group)
    unchecked: Group = field(default_factory=Group)


@dataclass
class Analysis:
    sets: dict[str, SetResult] = field(default_factory=dict)
    hashed_files: int = 0
    hashed_bytes: int = 0
    estimate_s: float = 0.0  # time for the phone to hash the candidates, before they were asked
    warnings: list[str] = field(default_factory=list)
    total_files: int = 0  # every file listed in the sets (after the skip rules)
    processed_files: int = 0  # files whose group is known
    skipped_by_rule: int = 0  # files the skip rules leave out
    unreadable: list[str] = field(default_factory=list)  # folders that could not be read
    left_out_sets: list[str] = field(default_factory=list)  # backup sets not in this check
    scope_key: str = ""  # the sets' definitions and the destinations, to tell when it is stale


def scope_key(config: Config, set_names: Sequence[str]) -> str:
    """What a result depends on besides the files: the checked sets' definitions and the
    destinations. A different key later means the result is out of date."""
    wanted = {n.casefold() for n in set_names}
    sets = sorted(
        (
            s.model_dump(exclude={"enabled"})
            for s in config.backup.sets
            if s.name.casefold() in wanted
        ),
        key=lambda d: str(d["name"]).casefold(),
    )
    backup = config.backup
    dests = [backup.mac_root, backup.ssd_volume_uuid, backup.ssd_folder]
    return json.dumps({"sets": sets, "dests": dests}, sort_keys=True, default=str)


def _index(
    db: Database, config: Config, required: list[str]
) -> tuple[dict[int, set[str]], set[str], set[int], set[str]]:
    """Sizes -> stored hashes; hashes on every required destination; sizes of provisional
    adopted files; hashes with an open problem."""
    with db.read() as conn:
        kinds = {dest_id: kind for kind, dest_id in current_dest_ids(db, config).items()}
        where: dict[str, set[str]] = {}
        sizes: dict[int, set[str]] = {}
        for digest, dest_id, size in conn.execute(
            "SELECT r.content_hash, r.dest_id, c.size FROM replica r "
            "JOIN content c ON c.hash = r.content_hash"
        ):
            if int(dest_id) in kinds:  # only the current Mac folder and SSD count
                where.setdefault(str(digest), set()).add(kinds[int(dest_id)])
            sizes.setdefault(int(size), set()).add(str(digest))
        provisional = {
            int(r[0])
            for r in conn.execute("SELECT size FROM adopted_file WHERE state = 'provisional'")
        }
        problems = {
            str(r[0])
            for r in conn.execute(
                "SELECT content_hash FROM problem WHERE resolved_at IS NULL "
                "AND content_hash IS NOT NULL"
            )
        }
        for digest, size in conn.execute(  # damaged copies are still worth recognising
            "SELECT c.hash, c.size FROM problem p JOIN content c ON c.hash = p.content_hash "
            "WHERE p.resolved_at IS NULL"
        ):
            sizes.setdefault(int(size), set()).add(str(digest))
    full = {h for h, k in where.items() if all(kind in k for kind in required)}
    return sizes, full, provisional, problems


def analyse(
    db: Database,
    config: Config,
    transport: Transport,
    set_names: Sequence[str],
    *,
    should_stop: Callable[[], bool] = lambda: False,
    on_progress: Callable[[int, int], None] | None = None,
    on_partial: Callable[[Analysis], None] | None = None,
) -> Analysis:
    """Walk the sets on the phone, match by size then by the phone's hash, and group.
    `on_partial` gets the result so far after each batch (the same object, still filling)."""
    result = Analysis(scope_key=scope_key(config, set_names))
    roots = resolve_roots(config.backup.sets, list(set_names))
    listing = list_roots(transport, roots, SkipRules.from_config(config))
    result.warnings = list(listing.warnings)
    result.skipped_by_rule = listing.skipped_by_rule
    result.unreadable = [shown(p) for p in listing.unreadable]
    picked = {n.casefold() for n in set_names}
    result.left_out_sets = [s.name for s in config.backup.sets if s.name.casefold() not in picked]
    result.total_files = len(listing.files)
    sizes, full, provisional, problem_hashes = _index(db, config, required_kinds(config))
    candidates: list[tuple[str, int, str]] = []  # (path, size, set)
    for path, (entry, set_name) in sorted(listing.files.items()):
        res = result.sets.setdefault(set_name, SetResult())
        if entry.size in sizes:
            candidates.append((path, entry.size, set_name))
            res.unchecked.add(entry.size)
        elif entry.size in provisional:
            res.not_verified.add(entry.size)
            result.processed_files += 1
        else:
            res.not_backed_up.add(entry.size)
            result.processed_files += 1
    for name in set_names:
        result.sets.setdefault(name, SetResult())
    result.estimate_s = sum(size for _p, size, _s in candidates) / HASH_BPS
    if on_partial is not None:
        on_partial(result)
    for start in range(0, len(candidates), SHA_BATCH):
        if should_stop():
            break
        batch = candidates[start : start + SHA_BATCH]
        hashes = transport.sha256([path for path, _size, _set in batch])
        for path, size, set_name in batch:
            res = result.sets[set_name]
            digest = hashes.get(path)
            result.hashed_files += 1
            result.hashed_bytes += size
            result.processed_files += 1
            res.unchecked.sub(size)
            if digest is not None and digest in problem_hashes and digest not in full:
                res.problems.add(size)
            elif digest is not None and digest in full:
                res.verified.add(size)
            elif size in provisional:
                res.not_verified.add(size)
            else:
                res.not_backed_up.add(size)
        if on_progress is not None:
            on_progress(min(start + SHA_BATCH, len(candidates)), len(candidates))
        if on_partial is not None:
            on_partial(result)
    return result


PARTIAL_EVERY_S = 2.0  # how often a running analysis saves its result so far


def run_analysis(
    db: Database,
    config: Config,
    transport: Transport,
    device_id: int,
    set_names: Sequence[str],
    *,
    clock: Clock | None = None,
    should_stop: Callable[[], bool] = lambda: False,
    on_progress: Callable[[int, int], None] | None = None,
    on_start: Callable[[int], None] | None = None,
) -> int:
    """Run and store one analysis; returns its id. Its partial result is saved as it goes."""
    clock = clock or SystemClock()
    with db.tx() as conn:
        cur = conn.execute(
            "INSERT INTO analysis_run (device_id, sets, state, started_at) VALUES (?, ?, ?, ?)",
            (device_id, json.dumps(list(set_names)), "running", clock.now()),
        )
        run_id = int(cur.lastrowid or 0)
    if on_start is not None:
        on_start(run_id)
    last = [0.0]

    def save_partial(found: Analysis) -> None:
        now = time.monotonic()
        if now - last[0] < PARTIAL_EVERY_S:
            return
        last[0] = now
        with db.tx() as conn:
            conn.execute(
                "UPDATE analysis_run SET partial = ?, processed = ?, total = ? "
                "WHERE id = ? AND state = 'running'",
                (json.dumps(asdict(found)), found.processed_files, found.total_files, run_id),
            )

    state, error, payload, done, total = "failed", None, None, None, None
    try:
        found = analyse(
            db,
            config,
            transport,
            set_names,
            should_stop=should_stop,
            on_progress=on_progress,
            on_partial=save_partial,
        )
        payload = json.dumps(asdict(found))
        done, total = found.processed_files, found.total_files
        state = "stopped" if should_stop() else "done"
    except Exception as e:
        error = str(e)
        raise
    finally:
        with db.tx() as conn:
            if state == "done":
                conn.execute(
                    "UPDATE analysis_run SET state = ?, finished_at = ?, result = ?, error = ?, "
                    "processed = ?, total = ?, partial = NULL WHERE id = ?",
                    (state, clock.now(), payload, error, done, total, run_id),
                )
            else:  # a stopped or failed run keeps only what it got so far, as partial
                conn.execute(
                    "UPDATE analysis_run SET state = ?, finished_at = ?, error = ?, "
                    "partial = coalesce(?, partial), processed = coalesce(?, processed), "
                    "total = coalesce(?, total) WHERE id = ?",
                    (state, clock.now(), error, payload, done, total, run_id),
                )
    return run_id


_RUN_COLUMNS = "id, sets, state, started_at, finished_at, result, error, processed, total, partial"


def _row(row: Any) -> dict[str, Any]:
    return {
        "id": int(row[0]),
        "sets": json.loads(row[1]),
        "state": row[2],
        "started_at": row[3],
        "finished_at": row[4],
        "result": json.loads(row[5]) if row[5] else None,
        "error": row[6],
        "processed": row[7],
        "total": row[8],
        "partial": json.loads(row[9]) if row[9] else None,
    }


def load_analysis(db: Database, run_id: int) -> dict[str, Any] | None:
    with db.read() as conn:
        row = conn.execute(
            f"SELECT {_RUN_COLUMNS} FROM analysis_run WHERE id = ?", (run_id,)
        ).fetchone()
    return None if row is None else _row(row)


def latest_analyses(db: Database, device_id: int | None) -> dict[str, Any]:
    """For the screen: the newest completed result, a run going on now, and a stopped or failed
    run newer than the completed one (shown apart: it never replaces the complete result)."""
    where, args = ("WHERE device_id = ?", [device_id]) if device_id is not None else ("", [])
    with db.read() as conn:

        def newest(extra: str) -> dict[str, Any] | None:
            joiner = " AND " if where else " WHERE "
            row = conn.execute(
                f"SELECT {_RUN_COLUMNS} FROM analysis_run {where}{joiner}{extra} "
                "ORDER BY id DESC LIMIT 1",
                args,
            ).fetchone()
            return None if row is None else _row(row)

        completed = newest("state = 'done'")
        active = newest("state = 'running'")
        ended = newest("state IN ('stopped', 'failed')")
        # a backup of this phone that copied something (not a failed or discarded run)
        last_backup = conn.execute(
            "SELECT max(finished_at) FROM run_summary WHERE outcome IN ('done', 'stopped') "
            "AND discarded = 0 AND new_files > 0"
            + (" AND device_id = ?" if device_id is not None else ""),
            args,
        ).fetchone()[0]
    if ended is not None and completed is not None and ended["id"] < completed["id"]:
        ended = None
    newest_end = max((r["id"] for r in (completed, ended) if r is not None), default=-1)
    if active is not None and active["id"] < newest_end:
        active = None  # a newer run has finished: this row is left over
    return {"completed": completed, "active": active, "ended": ended, "last_backup": last_backup}


def stale_reason(completed: dict[str, Any], config: Config, last_backup: int | None) -> str | None:
    """Why a completed result may be out of date, in plain words, or None."""
    result = completed.get("result") or {}
    if (
        last_backup is not None
        and completed.get("finished_at")
        and (last_backup > completed["finished_at"])
    ):
        return "A backup ran after this check, so it may be out of date. Analyse again."
    key = result.get("scope_key")
    if key is not None and key != scope_key(config, completed.get("sets") or []):
        return "Your backup folders or destinations changed after this check. Analyse again."
    return None
