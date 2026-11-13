"""Review candidates. M4: the Exact duplicates tab (stage B, spec 8 and 10).

Phone paths of one phone that share a content hash form a group. One path is the keeper, by
priority DCIM/Camera, Pictures, WhatsApp received, WhatsApp Sent, other folders, and then the
earlier mtime; the others are copies, pre-checked because they hold identical bytes. Only phone
paths would be deleted (M8); the backup keeps the bytes. The owner's own ticks (`selection`)
survive every rebuild and are never overwritten by defaults.
"""

import json
from dataclasses import dataclass

from sift.core.config import phone_path_key
from sift.core.db import Database
from sift.core.origin import classify_origin

_PRIORITY = {"camera": 0, "pictures": 1, "whatsapp_received": 2, "whatsapp_sent": 3}
_OTHER = 4


def keeper_rank(path: str, mtime: int) -> tuple[int, int, str]:
    """Lower is kept first: DCIM/Camera, then Pictures (and other DCIM folders), WhatsApp
    received, WhatsApp Sent, other folders; ties to the earlier mtime."""
    key = phone_path_key(path)
    origin = classify_origin(path)
    if key.startswith("/sdcard/dcim/camera/"):
        rank = _PRIORITY["camera"]
    elif key.startswith(("/sdcard/pictures/", "/sdcard/dcim/")):
        rank = _PRIORITY["pictures"]
    else:
        rank = _PRIORITY.get(origin, _OTHER)
    return rank, mtime, path


@dataclass(frozen=True)
class ExactGroup:
    content_hash: str
    keeper: int  # phone_file id
    copies: tuple[int, ...]


def exact_groups(db: Database, device_id: int) -> list[ExactGroup]:
    """Groups of present phone paths with identical bytes on this phone (stage B)."""
    with db.read() as conn:
        rows = conn.execute(
            "SELECT id, content_hash, remote_path, mtime FROM phone_file "
            "WHERE device_id = ? AND present = 1 AND content_hash IS NOT NULL AND content_hash IN "
            "(SELECT content_hash FROM phone_file WHERE device_id = ? AND present = 1 "
            "AND content_hash IS NOT NULL GROUP BY content_hash HAVING count(*) > 1) "
            "ORDER BY content_hash",
            (device_id, device_id),
        ).fetchall()
    by_hash: dict[str, list[tuple[int, str, int]]] = {}
    for row_id, digest, path, mtime in rows:
        by_hash.setdefault(str(digest), []).append((int(row_id), str(path), int(mtime)))
    groups: list[ExactGroup] = []
    for digest, members in by_hash.items():
        ordered = sorted(members, key=lambda m: keeper_rank(m[1], m[2]))
        groups.append(ExactGroup(digest, ordered[0][0], tuple(m[0] for m in ordered[1:])))
    return groups


def rebuild_exact(db: Database, device_id: int) -> int:
    """Replace this phone's `exact` candidates; returns the number of groups."""
    groups = exact_groups(db, device_id)
    rows: list[tuple[int, str, str, str, int, str, int]] = []
    reason = json.dumps(
        [{"code": "exact_copy", "text": "Identical bytes are kept in another place"}]
    )
    for n, group in enumerate(groups, 1):
        rows.append((group.keeper, "exact", "manual", "[]", n, "keeper", 0))
        rows.extend((c, "exact", "suggested", reason, n, "copy", 1) for c in group.copies)
    with db.tx() as conn:
        conn.execute(
            "DELETE FROM candidate WHERE tab = 'exact' AND phone_file_id IN "
            "(SELECT id FROM phone_file WHERE device_id = ?)",
            (device_id,),
        )
        conn.executemany(
            "INSERT INTO candidate (phone_file_id, tab, tier, reasons, group_id, role, "
            "default_checked) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (phone_file_id) DO UPDATE SET tab = excluded.tab, tier = excluded.tier, "
            "reasons = excluded.reasons, group_id = excluded.group_id, role = excluded.role, "
            "default_checked = excluded.default_checked",
            rows,
        )
    return len(groups)


def is_checked(default_checked: int, chosen: int | None) -> bool:
    """The owner's choice wins; the default applies only when there is none."""
    return bool(default_checked) if chosen is None else bool(chosen)


def set_selection(db: Database, ids: list[int], checked: bool) -> None:
    with db.tx() as conn:
        conn.executemany(
            "INSERT INTO selection (phone_file_id, checked) VALUES (?, ?) "
            "ON CONFLICT (phone_file_id) DO UPDATE SET checked = excluded.checked",
            [(i, int(checked)) for i in ids],
        )


def select_all_in_tab(db: Database, device_id: int, tab: str, checked: bool) -> int:
    """Tick or untick every selectable item (never a keeper) of a tab; returns how many."""
    with db.read() as conn:
        ids = [
            int(r[0])
            for r in conn.execute(
                "SELECT c.phone_file_id FROM candidate c "
                "JOIN phone_file p ON p.id = c.phone_file_id "
                "WHERE c.tab = ? AND p.device_id = ? AND p.present = 1 "
                "AND IFNULL(c.role, '') != 'keeper'",
                (tab, device_id),
            )
        ]
    set_selection(db, ids, checked)
    return len(ids)
