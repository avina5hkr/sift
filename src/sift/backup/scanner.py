"""The backup scan: what is on the phone, staged first and applied only when it looks safe.

`scan` lists the roots of the ticked sets and stages what it finds in `scan_entry`; nothing in
`phone_file` changes while the phone is being listed. Then, in one transaction, it either
applies the staged result or, when the mass-disappearance guard trips, leaves `phone_file`
alone and waits for the owner: `confirm_scan` applies the staged result, `discard_scan` drops it.

- **Settle rule:** a file whose mtime is within `settle_s` of the phone's clock is still being
  written, so it is left for the next scan. New or changed files with an mtime within the last
  hour are looked at again after one shared wait and left if they changed meanwhile. Older
  files are not looked at twice: a file being written has a recent mtime.
- **Unreadable folders:** a folder (or entry) the phone will not list is skipped with a plain
  warning. Rows under it keep their `present` value, because their state is unknown.
- **Seen but left out:** a known file that is settling, or now skipped by a rule, is staged with
  its row's current size and mtime, so applying keeps the row as it was and present.
- **Rows are never deleted.** A row not seen gets `present = 0`, and only when it lies in a
  scanned root, outside any folder the scan could not read.
- **Guard:** (a) a folder or a scanned set that had files at the last applied scan and now has
  none, or (b) a scanned set that lost more than a fifth of its files. A set or folder with no
  history (its first scan) cannot trip it.
"""

import json
import math
import os
import shutil
import tempfile
import time
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from sqlite3 import Connection
from typing import Literal

from sift.backup.sets import ScanRoot, claiming_roots, set_for_path
from sift.backup.skiprules import SkipRules, is_skipped
from sift.core.clock import Clock, SystemClock
from sift.core.config import phone_path_key
from sift.core.crashpoints import hit
from sift.core.db import Database
from sift.core.origin import classify_origin
from sift.device.session import DeviceSession
from sift.device.transport import RemoteEntry, RemoteError, Transport

RECENT_S = 3600  # new or changed files this recent are looked at twice
_GUARD_ADVICE = (
    "Nothing was changed. If you deleted them on purpose, confirm; otherwise check the phone "
    "and scan again."
)
_BATCH = 500


@dataclass(frozen=True)
class Disappearance:
    scope: Literal["folder", "set"]
    name: str  # the set's name, or the folder's phone path
    before: int
    now: int


@dataclass(frozen=True)
class ScanResult:
    scan_id: int
    state: Literal["applied", "awaiting_confirmation"]
    new_or_changed: int
    bytes_needed: int
    unchanged: int
    settling: int
    skipped_by_rule: int
    warnings: tuple[str, ...]  # plain words, e.g. "Could not read /sdcard/X; skipped."
    disappearance: tuple[Disappearance, ...]


class ScanNotPending(Exception):
    """The scan named is not waiting for the owner to confirm or discard it."""


# -- listing the phone ---------------------------------------------------------------------------


def _parent(path: str) -> str:
    return path.rsplit("/", 1)[0] or "/"


@dataclass
class _Listing:
    """What the walk saw: files to consider, known files left out, warnings, unread folders."""

    files: dict[str, tuple[RemoteEntry, str]] = field(default_factory=dict)  # path -> (e, set)
    left_out: dict[str, str] = field(default_factory=dict)  # path -> set (skipped by a rule)
    skipped_by_rule: int = 0
    warnings: list[str] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)  # phone paths whose rows are kept
    _visited: set[str] = field(default_factory=set)

    def cannot_read(self, path: str) -> None:
        self.unreadable.append(path)
        warning = f"Could not read {path}; skipped."
        if warning not in self.warnings:
            self.warnings.append(warning)


def _list_roots(transport: Transport, roots: Sequence[ScanRoot], rules: SkipRules) -> _Listing:
    seen = _Listing()
    skipped_folders: dict[str, bool] = {}

    def folder_skipped(folder: str) -> bool:
        """True when `folder` or a folder above it is skipped by a rule (a pruned folder)."""
        if folder in ("", "/"):
            return False
        known = skipped_folders.get(folder)
        if known is None:
            known = is_skipped(
                folder, is_dir=True, size=0, rules=rules
            ) is not None or folder_skipped(_parent(folder))
            skipped_folders[folder] = known
        return known

    def take(entry: RemoteEntry) -> None:
        if entry.is_dir or entry.is_symlink or entry.path in seen._visited:
            return
        seen._visited.add(entry.path)
        set_name = set_for_path(entry.path, roots)
        if set_name is None:  # in a folder the set excludes
            return
        rule = is_skipped(entry.path, is_dir=False, size=entry.size, rules=rules)
        if rule is not None or folder_skipped(_parent(entry.path)):
            seen.skipped_by_rule += 1
            seen.left_out[entry.path] = set_name
            return
        seen.files[entry.path] = (entry, set_name)

    for root in roots:

        def on_error(error: RemoteError, root: ScanRoot = root) -> None:
            # Without a path the folder is unknown, so everything under the root is kept.
            seen.cannot_read(error.path or root.path)

        entries: Iterable[RemoteEntry]
        try:
            if root.shallow:
                entries = transport.list_dir(root.path)
            else:
                entries = transport.walk(root.path, on_error=on_error)
            for entry in entries:
                take(entry)
        except RemoteError:
            seen.cannot_read(root.path)
    return seen


# -- staging -------------------------------------------------------------------------------------


@dataclass
class _Counts:
    new_or_changed: int = 0
    bytes_needed: int = 0
    unchanged: int = 0
    settling: int = 0


def _device_id(conn: Connection, device_uuid: str) -> int:
    row = conn.execute("SELECT id FROM device WHERE uuid = ?", (device_uuid,)).fetchone()
    if row is None:
        raise ValueError("the phone being scanned is not stored")
    return int(row[0])


def _drop_unfinished(conn: Connection, device_id: int, now: int) -> None:
    """Earlier scans of this phone still running (a crash) or waiting are replaced by a new one."""
    stale = [
        r[0]
        for r in conn.execute(
            "SELECT id FROM scan WHERE device_id = ? AND state IN ('running', "
            "'awaiting_confirmation')",
            (device_id,),
        )
    ]
    for scan_id in stale:
        _drop(conn, scan_id, now)


def _drop(conn: Connection, scan_id: int, now: int) -> None:
    conn.execute(
        "UPDATE scan SET state = 'discarded', finished_at = ? WHERE id = ?", (now, scan_id)
    )
    conn.execute("DELETE FROM scan_entry WHERE scan_id = ?", (scan_id,))


def scan(
    session: DeviceSession,
    db: Database,
    roots: Sequence[ScanRoot],
    rules: SkipRules,
    *,
    clock: Clock,
    settle_s: int = 120,
    recheck_s: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
) -> ScanResult:
    """Scan the phone's `roots`, stage the result, and apply it unless the guard trips.

    Raises `WrongDevice` if another phone has taken the session's place, and lets transport
    failures other than an unreadable folder (a disconnect, say) through: the scan row is then
    left `running` and the next scan replaces it.
    """
    info = session.read_info()
    phone_now = info.phone_time if info.phone_time is not None else clock.now()
    set_names = list(dict.fromkeys(r.set_name for r in roots))
    with db.tx() as conn:
        device_id = _device_id(conn, session.device_uuid)
        _drop_unfinished(conn, device_id, clock.now())
        cursor = conn.execute(
            "INSERT INTO scan (device_id, started_at, sets, state) VALUES (?, ?, ?, 'running')",
            (device_id, clock.now(), json.dumps(set_names)),
        )
        scan_id = int(cursor.lastrowid or 0)
    with db.read() as conn:
        known: dict[str, tuple[int, int]] = {
            r[0]: (r[1], r[2])
            for r in conn.execute(
                "SELECT remote_path, size, mtime FROM phone_file WHERE device_id = ?",
                (device_id,),
            )
        }

    listing = _list_roots(session.transport, roots, rules)
    counts = _Counts()
    staged: list[tuple[str, int, int, str]] = []  # (path, size, mtime, set)

    def hold(path: str, set_name: str) -> None:
        """A seen file left for later: keep its row (if any) as it is."""
        if path in known:
            size, mtime = known[path]
            staged.append((path, size, mtime, set_name))

    def accept(entry: RemoteEntry, set_name: str) -> None:
        counts.new_or_changed += 1
        counts.bytes_needed += entry.size
        staged.append((entry.path, entry.size, entry.mtime, set_name))

    for path, set_name in listing.left_out.items():
        hold(path, set_name)
    recheck: list[tuple[RemoteEntry, str]] = []
    for path, (entry, set_name) in listing.files.items():
        if entry.mtime >= phone_now - settle_s:
            counts.settling += 1
            hold(path, set_name)
        elif known.get(path) == (entry.size, entry.mtime):
            counts.unchanged += 1
            staged.append((path, entry.size, entry.mtime, set_name))
        elif entry.mtime >= phone_now - RECENT_S:
            recheck.append((entry, set_name))
        else:
            accept(entry, set_name)
    if recheck:
        sleep(recheck_s)
        for entry, set_name in recheck:
            try:
                again = session.transport.stat(entry.path)
            except RemoteError:
                again = None
            if again is None or (again.size, again.mtime) != (entry.size, entry.mtime):
                counts.settling += 1
                hold(entry.path, set_name)
            else:
                accept(entry, set_name)

    stats = {
        "roots": [[r.path, r.set_name, r.shallow, list(r.excludes)] for r in roots],
        "unreadable": listing.unreadable,
        "counts": {**counts.__dict__, "skipped_by_rule": listing.skipped_by_rule},
    }
    with db.tx() as conn:
        for chunk in _chunks(staged):
            conn.executemany(
                "INSERT INTO scan_entry (scan_id, path, size, mtime, set_name, folder) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [(scan_id, p, s, m, n, phone_path_key(_parent(p))) for p, s, m, n in chunk],
            )
        conn.execute(
            "UPDATE scan SET stats = ?, warnings = ? WHERE id = ?",
            (json.dumps(stats), json.dumps(listing.warnings), scan_id),
        )
    hit("scanner.staged")

    with db.tx() as conn:
        plan = _plan(conn, scan_id)
        gone = _guard(conn, plan)
        warnings = [*listing.warnings, *(_warning(d) for d in gone)]
        if gone:
            stats["disappearance"] = [d.__dict__ for d in gone]
            conn.execute(
                "UPDATE scan SET state = 'awaiting_confirmation', stats = ?, warnings = ? "
                "WHERE id = ?",
                (json.dumps(stats), json.dumps(warnings), scan_id),
            )
            state: Literal["applied", "awaiting_confirmation"] = "awaiting_confirmation"
        else:
            _apply(conn, plan, clock.now())
            state = "applied"
    return ScanResult(
        scan_id=scan_id,
        state=state,
        new_or_changed=counts.new_or_changed,
        bytes_needed=counts.bytes_needed,
        unchanged=counts.unchanged,
        settling=counts.settling,
        skipped_by_rule=listing.skipped_by_rule,
        warnings=tuple(warnings),
        disappearance=tuple(gone),
    )


def _chunks[T](items: Sequence[T]) -> Iterator[Sequence[T]]:
    for start in range(0, len(items), _BATCH):
        yield items[start : start + _BATCH]


# -- the guard and applying ----------------------------------------------------------------------


@dataclass
class _Plan:
    """What applying a staged scan would do, worked out from the staged rows and phone_file."""

    scan_id: int
    device_id: int
    sets: list[str]
    staged: list[tuple[str, int, int, str]]  # (path, size, mtime, set)
    to_absent: list[int]  # phone_file ids that get present = 0
    sets_before: Counter[str]
    sets_after: Counter[str]
    folders_after: dict[str, Counter[str]]  # folder key -> set -> present files
    folder_names: dict[str, str]  # folder key -> the folder's path as the phone spells it


def _plan(conn: Connection, scan_id: int) -> _Plan:
    device_id, sets_json, stats_json = conn.execute(
        "SELECT device_id, sets, stats FROM scan WHERE id = ?", (scan_id,)
    ).fetchone()
    stats = json.loads(stats_json)
    roots = [ScanRoot(p, s, bool(sh), tuple(ex)) for p, s, sh, ex in stats["roots"]]
    unread = [phone_path_key(u) for u in stats["unreadable"]]
    staged = [
        (r[0], int(r[1]), int(r[2]), str(r[3]))
        for r in conn.execute(
            "SELECT path, size, mtime, set_name FROM scan_entry WHERE scan_id = ?", (scan_id,)
        )
    ]
    staged_paths = {s[0] for s in staged}
    sets_before: Counter[str] = Counter()
    sets_after: Counter[str] = Counter()
    folders_after: dict[str, Counter[str]] = {}
    folder_names: dict[str, str] = {}
    to_absent: list[int] = []

    def count(path: str, set_name: str | None) -> None:
        folder = _parent(path)
        key = phone_path_key(folder)
        folder_names.setdefault(key, folder)
        if set_name is not None:
            sets_after[set_name] += 1
            folders_after.setdefault(key, Counter())[set_name] += 1

    for row_id, path, set_name in conn.execute(
        "SELECT id, remote_path, set_name FROM phone_file WHERE device_id = ? AND present = 1",
        (device_id,),
    ):
        folder_names.setdefault(phone_path_key(_parent(path)), _parent(path))
        if set_name is not None:
            sets_before[set_name] += 1
        if path in staged_paths:
            continue
        key = phone_path_key(path)
        kept = any(key == u or key.startswith(u + "/") for u in unread)
        if not kept and claiming_roots(path, roots):
            to_absent.append(row_id)
        else:
            count(path, set_name)
    for path, _size, _mtime, set_name in staged:
        count(path, set_name)
    return _Plan(
        scan_id,
        device_id,
        json.loads(sets_json),
        staged,
        to_absent,
        sets_before,
        sets_after,
        folders_after,
        folder_names,
    )


def _guard(conn: Connection, plan: _Plan) -> list[Disappearance]:
    gone: list[Disappearance] = []
    for set_name in plan.sets:
        before, now = plan.sets_before[set_name], plan.sets_after[set_name]
        if before >= 1 and (now == 0 or (before - now) * 5 > before):
            gone.append(Disappearance("set", set_name, before, now))
    for folder, before in conn.execute(
        "SELECT folder, present_files FROM folder_count WHERE device_id = ? ORDER BY folder",
        (plan.device_id,),
    ):
        now = sum(plan.folders_after.get(folder, Counter()).values())
        if before >= 1 and now == 0:
            gone.append(Disappearance("folder", plan.folder_names.get(folder, folder), before, 0))
    return gone


def _warning(d: Disappearance) -> str:
    label = d.name if d.scope == "set" else f"The folder {d.name}"
    files = "file" if d.before == 1 else "files"
    return f"{label} had {d.before:,} {files} last time and now shows {d.now:,}. {_GUARD_ADVICE}"


def _apply(conn: Connection, plan: _Plan, now: int) -> None:
    """Write the planned result into phone_file and folder_count, in the caller's transaction."""
    for chunk in _chunks(plan.staged):
        conn.executemany(
            "INSERT INTO phone_file (device_id, remote_path, origin, size, mtime, state, "
            "present, last_scan_id, set_name) VALUES (?, ?, ?, ?, ?, 'discovered', 1, ?, ?) "
            "ON CONFLICT (device_id, remote_path) DO UPDATE SET "
            "state = CASE WHEN size != excluded.size OR mtime != excluded.mtime "
            "  THEN 'discovered' ELSE state END, "
            "content_hash = CASE WHEN size != excluded.size OR mtime != excluded.mtime "
            "  THEN NULL ELSE content_hash END, "
            "attempts = CASE WHEN size != excluded.size OR mtime != excluded.mtime "
            "  THEN 0 ELSE attempts END, "
            "error = CASE WHEN size != excluded.size OR mtime != excluded.mtime "
            "  THEN NULL ELSE error END, "
            "size = excluded.size, mtime = excluded.mtime, origin = excluded.origin, "
            "set_name = excluded.set_name, present = 1, last_scan_id = excluded.last_scan_id",
            [
                (plan.device_id, path, classify_origin(path), size, mtime, plan.scan_id, name)
                for path, size, mtime, name in chunk
            ],
        )
    for ids in _chunks(plan.to_absent):
        conn.executemany("UPDATE phone_file SET present = 0 WHERE id = ?", [(i,) for i in ids])
    hit("scanner.applying")
    counted = set()
    for folder, by_set in plan.folders_after.items():
        counted.add(folder)
        conn.execute(
            "INSERT INTO folder_count (device_id, folder, set_name, present_files, scan_id) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (device_id, folder) DO UPDATE SET "
            "set_name = excluded.set_name, present_files = excluded.present_files, "
            "scan_id = excluded.scan_id",
            (
                plan.device_id,
                folder,
                by_set.most_common(1)[0][0],
                sum(by_set.values()),
                plan.scan_id,
            ),
        )
    emptied = [
        r[0]
        for r in conn.execute(
            "SELECT folder FROM folder_count WHERE device_id = ? AND present_files > 0",
            (plan.device_id,),
        )
        if r[0] not in counted
    ]
    for folder in emptied:
        conn.execute(
            "UPDATE folder_count SET present_files = 0, scan_id = ? "
            "WHERE device_id = ? AND folder = ?",
            (plan.scan_id, plan.device_id, folder),
        )
    conn.execute(
        "UPDATE scan SET state = 'applied', finished_at = ? WHERE id = ?", (now, plan.scan_id)
    )
    conn.execute("DELETE FROM scan_entry WHERE scan_id = ?", (plan.scan_id,))


def _pending(conn: Connection, scan_id: int) -> None:
    row = conn.execute("SELECT state FROM scan WHERE id = ?", (scan_id,)).fetchone()
    if row is None or row[0] != "awaiting_confirmation":
        raise ScanNotPending(f"scan {scan_id} is not waiting for confirmation")


def confirm_scan(db: Database, scan_id: int, *, clock: Clock | None = None) -> None:
    """The owner confirmed: apply the staged result of a scan the guard stopped."""
    now = (clock or SystemClock()).now()
    with db.tx() as conn:
        _pending(conn, scan_id)
        _apply(conn, _plan(conn, scan_id), now)


def discard_scan(db: Database, scan_id: int, *, clock: Clock | None = None) -> None:
    """The owner declined: drop the staged result; phone_file stays as it was."""
    now = (clock or SystemClock()).now()
    with db.tx() as conn:
        _pending(conn, scan_id)
        _drop(conn, scan_id, now)


# -- space on the Mac ----------------------------------------------------------------------------


def _size(n: int) -> str:
    if n < 1000:
        return f"{n} byte" if n == 1 else f"{n} bytes"
    value = float(n)
    for unit in ("KB", "MB", "GB", "TB"):
        value /= 1000
        if value < 1000 or unit == "TB":
            break
    return f"{value:.1f} {unit}"


def _writable(folder: Path) -> bool:
    if not os.access(folder, os.W_OK | os.X_OK):
        return False
    try:
        with tempfile.NamedTemporaryFile(dir=folder, prefix=".sift-probe-"):
            pass
    except OSError:
        return False
    return True


def space_check(bytes_needed: int, dest: Path, headroom_pct: float) -> str | None:
    """A plain message when `dest` cannot take `bytes_needed` plus headroom; None when it can.

    A destination folder not made yet is judged by the nearest folder above it that exists.
    """
    folder = dest
    while not folder.exists() and folder != folder.parent:
        folder = folder.parent
    if not folder.is_dir() or not _writable(folder):
        return (
            "Sift cannot save files in the backup folder on the Mac. "
            "Check that you can write to it."
        )
    need = bytes_needed + math.ceil(bytes_needed * headroom_pct / 100)
    free = shutil.disk_usage(folder).free
    if free >= need:
        return None
    return (
        f"The Mac needs {_size(need - free)} more free space for this backup: it needs "
        f"{_size(need)}, with {headroom_pct:g}% to spare, and {_size(free)} is free."
    )
