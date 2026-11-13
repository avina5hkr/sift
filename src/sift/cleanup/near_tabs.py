"""The Near-duplicates and Similar shots tabs, built from the near groups (spec 8 D-E, 10).

Near-duplicates: wa_forward and mixed groups. The keeper is locked; the copies are suggested,
and pre-checked only when their dHash is within 4 of the keeper's (cosine arrives in M6).
Similar shots: camera_burst groups, for the owner to choose; never pre-checked. Camera-origin
videos are never suggested. Each content is shown by its phone paths that are present now; the
keeper content is shown by its best path (the exact-duplicate keeper rule).
"""

from dataclasses import dataclass
from typing import Any

from sift.analysis.phash import distance
from sift.cleanup.candidates import keeper_rank
from sift.core.db import Database

TAB_TYPES = {"near": ("wa_forward", "mixed"), "similar": ("camera_burst",)}
PRECHECK_DHASH = 4


@dataclass
class TabItem:
    phone_file_id: int
    path: str
    size: int
    mtime: int
    content_hash: str
    kind: str
    width: int | None
    height: int | None
    default_checked: bool


@dataclass
class TabGroup:
    group_id: int
    keeper: TabItem
    copies: list[TabItem]


def tab_groups(db: Database, device_id: int, tab: str) -> list[TabGroup]:
    types = TAB_TYPES.get(tab)
    if not types:
        return []
    marks = ",".join("?" for _ in types)
    with db.read() as conn:
        rows = conn.execute(
            "SELECT m.group_id, m.role, c.hash, c.kind, c.width, c.height, c.dhash, "
            "p.id, p.remote_path, p.size, p.mtime "
            "FROM near_group g JOIN near_member m ON m.group_id = g.id "
            "JOIN content c ON c.hash = m.content_hash "
            "JOIN phone_file p ON p.content_hash = c.hash AND p.device_id = ? AND p.present = 1 "
            f"WHERE g.group_type IN ({marks}) ORDER BY m.group_id",
            (device_id, *types),
        ).fetchall()
    keepers: dict[int, list[tuple[Any, ...]]] = {}
    copies: dict[int, list[tuple[Any, ...]]] = {}
    for row in rows:
        (keepers if row[1] == "keeper" else copies).setdefault(int(row[0]), []).append(row)
    groups: list[TabGroup] = []
    for gid, krows in keepers.items():
        best = min(krows, key=lambda r: keeper_rank(str(r[8]), int(r[10])))
        keeper = _item(best, checked=False)
        items = []
        for r in copies.get(gid, []):
            camera_video = r[3] == "video" and keeper_rank(str(r[8]), 0)[0] == 0
            if camera_video:
                continue  # camera-origin videos are never suggested
            close = (
                tab == "near"
                and r[6] is not None
                and best[6] is not None
                and distance(int(r[6]), int(best[6])) <= PRECHECK_DHASH
            )
            items.append(_item(r, checked=close))
        if items:
            groups.append(TabGroup(gid, keeper, items))
    return groups


def _item(row: tuple[Any, ...], *, checked: bool) -> TabItem:
    _gid, _role, digest, kind, width, height, _dh, pid, path, size, mtime = row
    return TabItem(
        int(pid),
        str(path),
        int(size),
        int(mtime),
        str(digest),
        str(kind),
        width,
        height,
        checked,
    )


def selectable_ids(db: Database, device_id: int, tab: str) -> list[int]:
    return [c.phone_file_id for g in tab_groups(db, device_id, tab) for c in g.copies]
