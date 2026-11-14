"""The delete manifest (invariant I9): written before the first delete of a run.

One JSON file per run on every required destination, `DEST_ROOT/.sift/manifests/`, listing every
planned phone path with its size and hash. Created exclusively (never replaces a file) and
flushed to disk with F_FULLFSYNC. A required destination that is not mounted blocks the run.
"""

import fcntl
import json
import os
import time
from pathlib import Path

from sift.backup.confirm import current_dest_ids, required_kinds
from sift.backup.phonepaths import from_db
from sift.backup.run import usable_dests
from sift.cleanup.errors import ManifestBlocked
from sift.core.context import AppContext


def write_manifests(ctx: AppContext, run_id: int) -> list[str]:
    cfg = ctx.config
    dests = current_dest_ids(ctx.db, cfg)
    usable = {d.id: d for d in usable_dests(ctx)}
    targets = []
    for kind in required_kinds(cfg):
        dest = usable.get(dests[kind])
        if dest is None:
            what = "the SSD" if kind == "ssd" else "the backup folder on this Mac"
            raise ManifestBlocked(f"Waiting for {what}: the delete list is saved there first.")
        targets.append(Path(dest.root))
    with ctx.db.read() as conn:
        rows = conn.execute(
            "SELECT remote_path, size, content_hash FROM delete_item "
            "WHERE run_id = ? AND check_result = 'ok' ORDER BY remote_path",
            (run_id,),
        ).fetchall()
        source = conn.execute("SELECT source FROM delete_run WHERE id = ?", (run_id,)).fetchone()[0]
    body = json.dumps(
        {
            "run": run_id,
            "source": source,
            "written_at": int(time.time()),
            "files": [{"path": from_db(str(p)), "size": int(s), "sha256": h} for p, s, h in rows],
        },
        ensure_ascii=False,
        indent=1,
    ).encode()
    stamp = time.strftime("%Y-%m-%d-%H%M%S")
    written = []
    for root in targets:
        folder = root / ".sift" / "manifests"
        path = folder / f"{stamp}-run{run_id}.json"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            try:
                os.write(fd, body)
                fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
            finally:
                os.close(fd)
        except OSError as e:
            raise ManifestBlocked(f"Sift could not save the delete list in {root}: {e}") from e
        written.append(str(path))
    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE delete_run SET manifests = ? WHERE id = ?", (json.dumps(written), run_id)
        )
    return written
