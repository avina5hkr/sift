"""The six-check preflight (spec 10, invariant I3), run before any delete, in the same run.

For each planned phone file: (f) the connected phone is the one the file was backed up from;
(e) the file is present and was selected; (a) every required destination has a verified replica
whose file is there with the right size (an unplugged SSD: "waiting for the SSD"); (d) the path
is inside a delete root and outside the never-delete list; not Protected (plan F1), not a locked
keeper, and a keeper still present and not selected; at least min_age_days old; (c) the phone's
size and mtime are unchanged (a fresh stat); then (b) the phone's own SHA-256 equals the stored
hash, in batches of 40. A failed check takes the file off the run with a reason; nothing here
writes to the phone or to a backup folder.
"""

import hashlib
import json
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sift.backup.confirm import current_dest_ids, required_kinds
from sift.backup.phonepaths import from_db
from sift.backup.run import usable_dests
from sift.backup.sets import resolve_roots, set_for_path
from sift.cleanup.errors import RunBlocked
from sift.cleanup.junk import Item, is_protected
from sift.cleanup.lexicons import load
from sift.core.config import NEVER_DELETE_FLOOR, Config, phone_path_key
from sift.core.context import AppContext
from sift.device.session import DeviceSession
from sift.device.transport import TransportError

log = logging.getLogger("sift.cleanup")

SOURCES = ("cleanup", "browse", "slate", "analysis")
TYPED_ABOVE = 200  # plan F2
REASONS = {
    "gone": "No longer on the phone",
    "not_selected": "Not ticked any more",
    "not_backed_up": "Not backed up yet on every required drive",
    "waiting_for_ssd": "Waiting for the SSD",
    "backup_missing": "The backup copy is missing or the wrong size",
    "changed": "Changed on the phone since the backup",
    "outside_roots": "Outside the folders Sift may delete from",
    "protected": "Protected",
    "keeper": "The copy that is kept",
    "keeper_missing": "The kept copy is not on the phone any more",
    "too_new": "Too new",
    "hash_mismatch": "The phone's copy does not match the backup",
    "unreadable": "The phone could not read it",
    "over_limit": "More than one run may delete; run it again for the rest",
}


def selection_hash(ids: Sequence[int]) -> str:
    return hashlib.sha256(",".join(str(i) for i in sorted(set(ids))).encode()).hexdigest()


def create_run(
    ctx: AppContext, device_id: int, source: str, ids: Sequence[int], disposition: str
) -> int:
    """Record the planned files (present phone files of this phone only)."""
    if source not in SOURCES:
        raise ValueError(source)
    unique = sorted(set(ids))
    with ctx.db.tx() as conn:
        cur = conn.execute(
            "INSERT INTO delete_run (source, device_id, selection_hash, state, disposition, "
            "created_at) VALUES (?, ?, ?, 'preflight', ?, ?)",
            (source, device_id, selection_hash(unique), disposition, int(time.time())),
        )
        run_id = int(cur.lastrowid or 0)
        for start in range(0, len(unique), 500):
            part = unique[start : start + 500]
            marks = ",".join("?" for _ in part)
            conn.execute(
                "INSERT INTO delete_item (run_id, phone_file_id, remote_path, content_hash, size, "
                f"mtime) SELECT ?, id, remote_path, content_hash, size, mtime FROM phone_file "
                f"WHERE device_id = ? AND id IN ({marks})",
                (run_id, device_id, *part),
            )
    return run_id


def ids_for_sets(ctx: AppContext, device_id: int, sets: Sequence[str]) -> list[int]:
    """Delete only (spec 10E): present files of the chosen deletable sets whose content has a
    verified replica on every required destination. Never a file that is not backed up."""
    cfg = ctx.config
    allowed = [s.name for s in cfg.backup.sets if s.deletable and s.name in sets]
    roots = resolve_roots(cfg.backup.sets, allowed)
    dests = current_dest_ids(ctx.db, cfg)
    need = [dests[k] for k in required_kinds(cfg)]
    if any(d < 0 for d in need):
        return []
    marks = ",".join("?" for _ in need)
    with ctx.db.read() as conn:
        rows = conn.execute(
            "SELECT p.id, p.remote_path FROM phone_file p WHERE p.device_id = ? AND p.present = 1 "
            "AND p.state = 'done' AND p.content_hash IS NOT NULL AND "
            "(SELECT count(DISTINCT r.dest_id) FROM replica r "
            "WHERE r.content_hash = p.content_hash AND r.rel_path NOT LIKE '.quarantine/%' "
            f"AND r.verified_at IS NOT NULL AND r.dest_id IN ({marks})) = ?",
            (device_id, *need, len(need)),
        ).fetchall()
    return [int(i) for i, path in rows if set_for_path(from_db(str(path)), roots) in allowed]


@dataclass
class _File:
    pid: int
    path: str  # as the phone names it
    stored: str  # as the database stores it
    digest: str | None
    size: int
    mtime: int
    result: str = "ok"


def _under(key: str, roots: Sequence[str]) -> bool:
    return any(key == r or key.startswith(r.rstrip("/") + "/") for r in roots)


def in_delete_roots(cfg: Config, path: str) -> bool:
    key = phone_path_key(path)
    parts = key.split("/")[1:]
    if any(part in ("", ".", "..") for part in parts) or "\n" in key:
        return False  # never trust a path that could climb out of a root
    never = [phone_path_key(p) for p in (*NEVER_DELETE_FLOOR, *cfg.cleanup.never_delete)]
    roots = [phone_path_key(p) for p in cfg.cleanup.delete_roots]
    return key.startswith("/sdcard/") and _under(key, roots) and not _under(key, never)


def _protected(
    ctx: AppContext, files: list[_File], source: str, selected: set[int], device_id: int
) -> set[int]:
    """Phone file ids blocked as Protected (plan F1)."""
    cfg = ctx.config
    doc = load("document", ctx.paths.lexicons)
    hashes = sorted({f.digest for f in files if f.digest})
    if not hashes:
        return set()
    out: set[int] = set()
    with ctx.db.read() as conn:
        info: dict[str, Any] = {}
        paths: dict[str, list[tuple[int, str, str, int]]] = {}
        for start in range(0, len(hashes), 500):
            part = hashes[start : start + 500]
            marks = ",".join("?" for _ in part)
            for row in conn.execute(
                "SELECT c.hash, c.kind, c.size, c.width, c.height, c.duration_ms, "
                "c.has_camera_exif, c.is_screenshot, c.unreadable, c.ocr_text, l.label "
                "FROM content c LEFT JOIN label l ON l.content_hash = c.hash "
                f"WHERE c.hash IN ({marks})",
                part,
            ):
                info[str(row[0])] = row
            for pid, path, origin, mtime, digest in conn.execute(
                "SELECT id, remote_path, origin, mtime, content_hash FROM phone_file "
                f"WHERE device_id = ? AND present = 1 AND content_hash IN ({marks})",
                (device_id, *part),
            ):
                paths.setdefault(str(digest), []).append(
                    (int(pid), from_db(str(path)), str(origin), int(mtime))
                )
    for f in files:
        row = info.get(str(f.digest))
        if row is None:  # nothing known about it: fail closed
            out.add(f.pid)
            continue
        item = Item(
            str(row[0]), str(row[1]), int(row[2]), row[3], row[4], row[5], bool(row[6]),
            bool(row[7]), bool(row[8]), row[9], None, None, paths.get(str(f.digest), []),
        )  # fmt: skip
        item.label = row[10]
        if source == "cleanup":
            blocked = is_protected(item, cfg, doc, -1.0)
            stays = any(p[0] not in selected for p in item.paths)  # an exact copy stays behind
            if blocked and not stays:
                out.add(f.pid)
        else:
            folders = tuple(
                phone_path_key(x).rstrip("/") + "/" for x in cfg.scoring.protected_folders
            )
            if (
                item.label == "protected"
                or doc.hit(item.text) is not None
                or (folders and any(phone_path_key(p[1]).startswith(folders) for p in item.paths))
            ):
                out.add(f.pid)
    return out


def _keepers(
    ctx: AppContext, files: list[_File], selected: set[int], device_id: int
) -> dict[int, str]:
    """Suggestion tabs: a keeper is never deleted, and a copy needs its keeper still on the
    phone and not selected (spec 10 check 5)."""
    out: dict[int, str] = {}
    ids = [f.pid for f in files]
    with ctx.db.read() as conn:
        exact = {
            int(pid): (int(gid), str(role))
            for pid, gid, role in conn.execute(
                "SELECT c.phone_file_id, c.group_id, c.role FROM candidate c JOIN phone_file p "
                "ON p.id = c.phone_file_id WHERE c.tab = 'exact' AND p.device_id = ?",
                (device_id,),
            )
        }
        keeper_of_group = {gid: pid for pid, (gid, role) in exact.items() if role == "keeper"}
        present = {
            int(r[0])
            for r in conn.execute(
                "SELECT id FROM phone_file WHERE device_id = ? AND present = 1", (device_id,)
            )
        }
        copy_groups: dict[str, set[int]] = {}  # content -> near groups it is a copy in
        keeper_groups: dict[str, set[int]] = {}  # content -> near groups it keeps
        for gid, digest, role in conn.execute(
            "SELECT group_id, content_hash, role FROM near_member"
        ):
            target = keeper_groups if role == "keeper" else copy_groups
            target.setdefault(str(digest), set()).add(int(gid))
        keeper_paths: dict[int, set[int]] = {}  # near group -> its keeper's present paths
        for gid, kpid in conn.execute(
            "SELECT k.group_id, kp.id FROM near_member k JOIN phone_file kp "
            "ON kp.content_hash = k.content_hash AND kp.device_id = ? AND kp.present = 1 "
            "WHERE k.role = 'keeper'",
            (device_id,),
        ):
            keeper_paths.setdefault(int(gid), set()).add(int(kpid))
    del ids
    selected_copy_groups = {
        g for f in files if f.pid in selected for g in copy_groups.get(str(f.digest), set())
    }
    for f in files:
        role = exact.get(f.pid)
        if role is not None and role[1] == "keeper":
            out[f.pid] = "keeper"
            continue
        if role is not None:
            keeper = keeper_of_group.get(role[0])
            if keeper is None or keeper not in present or keeper in selected:
                out[f.pid] = "keeper_missing"
            continue
        if keeper_groups.get(str(f.digest), set()) & selected_copy_groups:
            out[f.pid] = "keeper"  # its copies are being removed because it stays
            continue
        groups = copy_groups.get(str(f.digest), set())
        if any(not (keeper_paths.get(g, set()) - selected) for g in groups):
            out[f.pid] = "keeper_missing"  # a near copy needs its keeper still on the phone
    return out


def _replicas_ok(ctx: AppContext, files: list[_File]) -> dict[int, str]:
    """(a) every required destination: a verified replica whose file is there, right size."""
    cfg = ctx.config
    dests = current_dest_ids(ctx.db, cfg)
    usable = {d.id: d for d in usable_dests(ctx)}
    out: dict[int, str] = {}
    need = [(k, dests[k]) for k in required_kinds(cfg)]
    with ctx.db.read() as conn:
        for f in files:
            if f.digest is None:
                out[f.pid] = "not_backed_up"
                continue
            for kind, dest_id in need:
                row = conn.execute(
                    "SELECT rel_path, size FROM replica WHERE content_hash = ? AND dest_id = ? "
                    "AND verified_at IS NOT NULL AND rel_path NOT LIKE '.quarantine/%'",
                    (f.digest, dest_id),
                ).fetchone()
                if row is None:
                    out[f.pid] = "not_backed_up"
                    break
                dest = usable.get(dest_id)
                if dest is None:
                    out[f.pid] = "waiting_for_ssd" if kind == "ssd" else "backup_missing"
                    break
                local = Path(dest.root) / str(row[0])
                try:
                    if local.stat().st_size != int(row[1]) or int(row[1]) != f.size:
                        out[f.pid] = "backup_missing"
                        break
                except OSError:
                    out[f.pid] = "backup_missing"
                    break
    return out


def run_preflight(
    ctx: AppContext,
    run_id: int,
    session: DeviceSession | None,
    *,
    should_stop: Callable[[], bool] = lambda: False,
    on_progress: Callable[[int, int], None] | None = None,
    now: Callable[[], float] = time.time,
) -> dict[str, int]:
    """Check every planned file; returns counts per result. The run ends 'ready' when at least
    one file passed, 'blocked' otherwise, 'stopped' when asked to stop."""
    cfg = ctx.config
    with ctx.db.read() as conn:
        source, device_id = conn.execute(
            "SELECT source, device_id FROM delete_run WHERE id = ?", (run_id,)
        ).fetchone()
        uuid = conn.execute("SELECT uuid FROM device WHERE id = ?", (device_id,)).fetchone()[0]
        rows = conn.execute(
            "SELECT i.phone_file_id, i.remote_path, i.content_hash, i.size, i.mtime, p.present, "
            "p.state, IFNULL(s.checked, -1) FROM delete_item i "
            "JOIN phone_file p ON p.id = i.phone_file_id "
            "LEFT JOIN selection s ON s.phone_file_id = i.phone_file_id WHERE i.run_id = ?",
            (run_id,),
        ).fetchall()
    files = [
        _File(int(r[0]), from_db(str(r[1])), str(r[1]), r[2], int(r[3]), int(r[4])) for r in rows
    ]
    from sift.cleanup.quarantine import revive

    revive(ctx)  # content back on a phone leaves quarantine first
    try:
        if session is None:
            raise RunBlocked("Connect the phone over USB first.")
        if session.device_uuid != uuid:
            raise RunBlocked("This is not the phone these files were backed up from.")  # I3f
        session.check_identity()
    except RunBlocked as e:
        _finish(ctx, run_id, files, blocked=str(e))
        return {"blocked": len(files)}
    except Exception as e:  # WrongDevice or a lost connection
        _finish(ctx, run_id, files, blocked=f"Sift could not confirm the phone: {e}")
        return {"blocked": len(files)}
    selected = {int(r[0]) for r in rows}
    by_id = {int(r[0]): r for r in rows}
    total = len(files) * 2
    done = 0

    def tick(n: int = 1) -> None:
        nonlocal done
        done += n
        if on_progress is not None:
            on_progress(done, total)

    def alive() -> list[_File]:
        return [f for f in files if f.result == "ok"]

    for f in files:  # (e) present and selected; (d) roots; min age is checked on the fresh stat
        r = by_id[f.pid]
        if not r[5] or r[6] != "done":
            f.result = "gone"
        elif source == "cleanup" and r[7] != 1:
            f.result = "not_selected"
        elif not in_delete_roots(cfg, f.path):
            f.result = "outside_roots"
    for pid, why in _replicas_ok(ctx, alive()).items():  # (a)
        next(f for f in files if f.pid == pid).result = why
    blocked = _protected(ctx, alive(), source, selected, device_id)
    for f in alive():
        if f.pid in blocked:
            f.result = "protected"
    if source == "cleanup":
        for pid, why in _keepers(ctx, alive(), selected, device_id).items():
            next(f for f in files if f.pid == pid).result = why
    oldest = now() - cfg.cleanup.min_age_days * 86_400
    transport = session.transport
    try:
        for f in files:  # (c) a fresh stat, and the minimum age
            if should_stop():
                _finish(ctx, run_id, files, stopped=True)
                return {"stopped": len(files)}
            if f.result == "ok":
                entry = transport.stat(f.path)
                if entry is None:
                    f.result = "gone"
                elif entry.size != f.size or entry.mtime != f.mtime:
                    f.result = "changed"
                elif entry.mtime > oldest:
                    f.result = "too_new"
            tick()
        batch = cfg.cleanup.preflight_hash_batch
        todo = alive()
        for start in range(0, len(todo), batch):  # (b) the phone's own hash, in this run
            if should_stop():
                _finish(ctx, run_id, files, stopped=True)
                return {"stopped": len(files)}
            part = todo[start : start + batch]
            got = transport.sha256([f.path for f in part])
            for f in part:
                digest = got.get(f.path)
                if digest is None:
                    f.result = "unreadable"
                elif digest != f.digest:
                    f.result = "hash_mismatch"
            tick(len(part))
    except TransportError as e:
        _finish(ctx, run_id, files, blocked=f"The phone stopped answering: {e}")
        return {"blocked": len(files)}
    _finish(ctx, run_id, files)
    counts: dict[str, int] = {}
    for f in files:
        counts[f.result] = counts.get(f.result, 0) + 1
    return counts


def _finish(
    ctx: AppContext,
    run_id: int,
    files: list[_File],
    *,
    blocked: str | None = None,
    stopped: bool = False,
) -> None:
    if blocked is None and not stopped:  # one run deletes at most max_delete_per_run files
        cap = ctx.config.cleanup.max_delete_per_run
        for f in [f for f in files if f.result == "ok"][cap:]:
            f.result = "over_limit"
    passed = [f for f in files if f.result == "ok"] if blocked is None and not stopped else []
    state = "stopped" if stopped else "blocked" if blocked or not passed else "ready"
    reason = blocked if blocked else (None if passed or stopped else "No file passed the checks.")
    with ctx.db.tx() as conn:
        conn.executemany(
            "UPDATE delete_item SET check_result = ? WHERE run_id = ? AND phone_file_id = ?",
            [
                ("blocked" if blocked else "stopped" if stopped else f.result, run_id, f.pid)
                for f in files
            ],
        )
        conn.execute(
            "UPDATE delete_run SET state = ?, blocked_reason = ?, preflight_done_at = ?, "
            "passed = ?, typed_required = ?, selection_hash = ? WHERE id = ?",
            (
                state,
                reason,
                int(time.time()),
                len(passed),
                int(len(passed) > TYPED_ABOVE),
                selection_hash([f.pid for f in passed]),
                run_id,
            ),
        )


def summary(ctx: AppContext, run_id: int) -> dict[str, Any] | None:
    with ctx.db.read() as conn:
        row = conn.execute(
            "SELECT id, source, state, blocked_reason, disposition, typed_required, created_at, "
            "preflight_done_at, started_at, finished_at, manifests, passed, deleted, failed, "
            "bytes_freed FROM delete_run WHERE id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        items = conn.execute(
            "SELECT phone_file_id, remote_path, size, check_result, state, error "
            "FROM delete_item WHERE run_id = ? ORDER BY remote_path",
            (run_id,),
        ).fetchall()
    keys = (
        "id", "source", "state", "blocked_reason", "disposition", "typed_required", "created_at",
        "preflight_done_at", "started_at", "finished_at", "manifests", "passed", "deleted",
        "failed", "bytes_freed",
    )  # fmt: skip
    out: dict[str, Any] = dict(zip(keys, row, strict=True))
    out["typed_required"] = bool(out["typed_required"])
    out["manifests"] = json.loads(out["manifests"] or "[]")
    out["items"] = [
        {"id": i, "path": p, "size": s, "check": c, "reason": REASONS.get(c), "state": st,
         "error": e}
        for i, p, s, c, st, e in items
    ]  # fmt: skip
    out["passed_bytes"] = sum(int(s) for _i, _p, s, c, _st, _e in items if c == "ok")
    return out
