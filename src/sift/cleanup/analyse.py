"""Analyse phone, read-only: how much of each set is already safely backed up.

The phone is only read (a listing and batched SHA-256s); the only database writes are this
run's own `analysis_run` row. Each present file is matched to the backup index by size, then the
phone's own hash decides (names are never proof). Four groups per set, with counts and sizes:

- **verified**: the phone's hash equals content that has a replica on every required destination;
- **not_verified**: same size as an adopted file that is not hashed yet (still being checked);
- **not_backed_up**: no match, or a copy is missing on a required destination;
- **problems**: the phone's hash matches content listed under Problems (damaged copy).

The result is never trusted later: a delete's preflight rechecks every file.
"""

import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from sift.backup.confirm import required_kinds
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


@dataclass
class SetResult:
    verified: Group = field(default_factory=Group)
    not_verified: Group = field(default_factory=Group)
    not_backed_up: Group = field(default_factory=Group)
    problems: Group = field(default_factory=Group)


@dataclass
class Analysis:
    sets: dict[str, SetResult] = field(default_factory=dict)
    hashed_files: int = 0
    hashed_bytes: int = 0
    estimate_s: float = 0.0  # time for the phone to hash the candidates, before they were asked
    warnings: list[str] = field(default_factory=list)


def _index(
    db: Database, required: list[str]
) -> tuple[dict[int, set[str]], set[str], set[int], set[str]]:
    """Sizes -> stored hashes; hashes on every required destination; sizes of provisional
    adopted files; hashes with an open problem."""
    with db.read() as conn:
        kinds = {int(r[0]): str(r[1]) for r in conn.execute("SELECT id, kind FROM destination")}
        where: dict[str, set[str]] = {}
        sizes: dict[int, set[str]] = {}
        for digest, dest_id, size in conn.execute(
            "SELECT r.content_hash, r.dest_id, c.size FROM replica r "
            "JOIN content c ON c.hash = r.content_hash"
        ):
            where.setdefault(str(digest), set()).add(kinds.get(int(dest_id), "?"))
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
) -> Analysis:
    """Walk the sets on the phone, match by size then by the phone's hash, and group."""
    result = Analysis()
    roots = resolve_roots(config.backup.sets, list(set_names))
    listing = list_roots(transport, roots, SkipRules.from_config(config))
    result.warnings = list(listing.warnings)
    sizes, full, provisional, problem_hashes = _index(db, required_kinds(config))
    candidates: list[tuple[str, int, str]] = []  # (path, size, set)
    for path, (entry, set_name) in sorted(listing.files.items()):
        res = result.sets.setdefault(set_name, SetResult())
        if entry.size in sizes:
            candidates.append((path, entry.size, set_name))
        elif entry.size in provisional:
            res.not_verified.add(entry.size)
        else:
            res.not_backed_up.add(entry.size)
    for name in set_names:
        result.sets.setdefault(name, SetResult())
    result.estimate_s = sum(size for _p, size, _s in candidates) / HASH_BPS
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
    return result


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
) -> int:
    """Run and store one analysis; returns its id."""
    clock = clock or SystemClock()
    with db.tx() as conn:
        cur = conn.execute(
            "INSERT INTO analysis_run (device_id, sets, state, started_at) VALUES (?, ?, ?, ?)",
            (device_id, json.dumps(list(set_names)), "running", clock.now()),
        )
        run_id = int(cur.lastrowid or 0)
    state, error, payload = "failed", None, None
    try:
        found = analyse(
            db, config, transport, set_names, should_stop=should_stop, on_progress=on_progress
        )
        payload = json.dumps(asdict(found))
        state = "stopped" if should_stop() else "done"
    except Exception as e:
        error = str(e)
        raise
    finally:
        with db.tx() as conn:
            conn.execute(
                "UPDATE analysis_run SET state = ?, finished_at = ?, result = ?, error = ? "
                "WHERE id = ?",
                (state, clock.now(), payload, error, run_id),
            )
    return run_id


def load_analysis(db: Database, run_id: int) -> dict[str, Any] | None:
    with db.read() as conn:
        row = conn.execute(
            "SELECT id, sets, state, started_at, finished_at, result, error FROM analysis_run "
            "WHERE id = ?",
            (run_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "id": int(row[0]),
        "sets": json.loads(row[1]),
        "state": row[2],
        "started_at": row[3],
        "finished_at": row[4],
        "result": json.loads(row[5]) if row[5] else None,
        "error": row[6],
    }
