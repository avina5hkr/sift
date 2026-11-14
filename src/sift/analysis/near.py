"""Stage D (images) and the grouping half of stage E (videos): near-duplicate groups (spec 8).

Images: candidates share at least one of nine dHash bands (pigeonhole: two hashes within
distance 8 share a band), have the same kind and aspect ratios within the tolerance; a pair is
confirmed when dHash ≤ 6 and pHash ≤ 8 (no embeddings yet, plan C3). Confirmed pairs are joined
with union-find. Videos: durations, frame fingerprints and aspect (see `video.similar`).

Each group gets a type from its members' origins (wa_forward, mixed, camera_burst; groups with
no WhatsApp member and no camera majority are "other") and a keeper by score: +1000 camera
origin, +500 camera EXIF, then pixels, then file size, then the earlier mtime.
"""

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from sift.analysis.phash import distance, to_unsigned
from sift.analysis.video import similar
from sift.core.config import AnalysisConfig
from sift.core.db import Database
from sift.core.origin import classify_origin

ALGO_VERSION = 1
BANDS = 9


@dataclass
class Item:
    hash: str
    kind: str
    width: int
    height: int
    size: int
    dhash: int
    phash: int | None
    has_camera_exif: bool
    duration_ms: int = 0
    frames: tuple[int, ...] = ()
    origins: set[str] = field(default_factory=set)
    mtime: int = 0

    @property
    def aspect(self) -> float:
        return self.width / self.height if self.width and self.height else 0.0

    @property
    def camera(self) -> bool:
        return "camera" in self.origins


class UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def bands(value: int) -> list[tuple[int, int]]:
    """(band index, band value): 64 bits in nine bands of 7 or 8 bits."""
    v = to_unsigned(value)
    out, start = [], 0
    for i in range(BANDS):
        width = 8 if i < 64 - 7 * BANDS else 7  # 1 band of 8 bits, 8 bands of 7 bits
        out.append((i, (v >> start) & ((1 << width) - 1)))
        start += width
    return out


def _aspect_ok(a: Item, b: Item, tol: float) -> bool:
    if not a.aspect or not b.aspect:
        return False
    return abs(a.aspect - b.aspect) <= tol * max(a.aspect, b.aspect)


def _image_match(a: Item, b: Item, cfg: AnalysisConfig) -> bool:
    if a.hash == b.hash or a.kind != b.kind or not _aspect_ok(a, b, cfg.neardup_aspect_tol):
        return False
    if a.phash is None or b.phash is None or distance(a.phash, b.phash) > cfg.neardup_phash_max:
        return False
    return distance(a.dhash, b.dhash) <= cfg.neardup_dhash_max


def _video_match(a: Item, b: Item, cfg: AnalysisConfig) -> bool:
    return similar(
        (a.duration_ms, a.width, a.height, a.frames),
        (b.duration_ms, b.width, b.height, b.frames),
        frame_max=cfg.video_frame_hash_max,
        frames_min=cfg.video_frame_match_min,
        aspect_tol=cfg.neardup_aspect_tol,
    )


def _connect_images(
    items: Sequence[Item], cfg: AnalysisConfig, uf: UnionFind, should_stop: Callable[[], bool]
) -> None:
    """Join every matching pair of images that share a hash band (pigeonhole: a dHash within
    the limit shares at least one band). Two images already joined are not compared again: it
    cannot change the groups, and it keeps repetitive libraries (many near-identical memes) from
    storing or re-testing millions of pairs (external review, P3.13)."""
    buckets: dict[tuple[int, int], list[Item]] = {}
    for item in items:
        uf.find(item.hash)
        for band in bands(item.dhash):
            buckets.setdefault(band, []).append(item)
    for members in buckets.values():
        if should_stop():
            raise NearStopped
        for i, a in enumerate(members):
            for b in members[i + 1 :]:
                if uf.find(a.hash) != uf.find(b.hash) and _image_match(a, b, cfg):
                    uf.union(a.hash, b.hash)


def _connect_videos(
    items: Sequence[Item], cfg: AnalysisConfig, uf: UnionFind, should_stop: Callable[[], bool]
) -> None:
    ordered = sorted(items, key=lambda v: v.duration_ms)
    for i, a in enumerate(ordered):
        uf.find(a.hash)
        if i % 500 == 0 and should_stop():
            raise NearStopped
        for b in ordered[i + 1 :]:
            if b.duration_ms - a.duration_ms > max(1000, 0.02 * b.duration_ms):
                break  # sorted by duration: nothing later can be close enough
            if uf.find(a.hash) != uf.find(b.hash) and _video_match(a, b, cfg):
                uf.union(a.hash, b.hash)


def image_pairs(items: Sequence[Item], cfg: AnalysisConfig) -> set[tuple[str, str]]:
    """Every matching image pair (for tests and small sets; grouping does not build this)."""
    out = set()
    for i, a in enumerate(items):
        for b in items[i + 1 :]:
            if _image_match(a, b, cfg):
                out.add(_pair(a.hash, b.hash))
    return out


class NearStopped(Exception):
    """The owner pressed Stop while near-duplicates were being found."""


def group_type(members: Sequence[Item]) -> str:
    whatsapp = [m for m in members if any(o.startswith("whatsapp") for o in m.origins)]
    cameraish = [m for m in members if m.camera or "pictures" in m.origins]
    if whatsapp and len(whatsapp) == len(members):
        return "mixed" if any(m.camera for m in members) else "wa_forward"
    if whatsapp and cameraish:
        return "mixed"
    if all(m.camera for m in members):
        return "camera_burst"
    return "other"


def keeper_score(m: Item) -> tuple[int, int, int, int]:
    return (
        (1000 if m.camera else 0) + (500 if m.has_camera_exif else 0),
        m.width * m.height,
        m.size,
        -m.mtime,
    )


def load_items(db: Database, device_id: int) -> list[Item]:
    """Every analysed image or video that this phone holds now (present), with its origins."""
    with db.read() as conn:
        rows = conn.execute(
            "SELECT c.hash, c.kind, c.width, c.height, c.size, c.dhash, c.phash, "
            "c.has_camera_exif, c.duration_ms, c.vframes, p.remote_path, p.mtime "
            "FROM content c JOIN phone_file p ON p.content_hash = c.hash "
            "WHERE p.device_id = ? AND p.present = 1 AND c.unreadable = 0 "
            "AND c.kind IN ('image', 'video') AND c.dhash IS NOT NULL",
            (device_id,),
        ).fetchall()
    items: dict[str, Item] = {}
    for digest, kind, w, h, size, dh, ph, exif, dur, vframes, path, mtime in rows:
        item = items.get(digest)
        if item is None:
            item = items[digest] = Item(
                str(digest),
                str(kind),
                int(w or 0),
                int(h or 0),
                int(size),
                int(dh),
                None if ph is None else int(ph),
                bool(exif),
                int(dur or 0),
                tuple(json.loads(vframes)) if vframes else (),
                set(),
                int(mtime),
            )
        item.origins.add(origin_of(str(path)))
        item.mtime = min(item.mtime, int(mtime))
    return list(items.values())


def origin_of(path: str) -> str:
    origin = classify_origin(path)
    if origin == "other" and path.casefold().startswith("/sdcard/pictures/"):
        return "pictures"
    return origin


@dataclass(frozen=True)
class NearGroup:
    kind: str
    group_type: str
    keeper: str
    copies: tuple[str, ...]


def find_groups(
    items: Sequence[Item], cfg: AnalysisConfig, should_stop: Callable[[], bool] = lambda: False
) -> list[NearGroup]:
    by_hash = {i.hash: i for i in items}
    images = [i for i in items if i.kind == "image"]
    videos = [i for i in items if i.kind == "video" and i.frames]
    uf = UnionFind()
    _connect_images(images, cfg, uf, should_stop)
    _connect_videos(videos, cfg, uf, should_stop)
    clusters: dict[str, list[Item]] = {}
    for digest in list(uf.parent):
        clusters.setdefault(uf.find(digest), []).append(by_hash[digest])
    groups = []
    for members in clusters.values():
        if len(members) < 2:
            continue
        # A star around each keeper: a member joins only when it matches the keeper itself, so
        # a chain A~B~C never puts two unlike pictures (A and C) in one group.
        match = _video_match if members[0].kind == "video" else _image_match
        left = sorted(members, key=lambda m: (keeper_score(m), m.hash), reverse=True)
        while len(left) >= 2:
            if should_stop():
                raise NearStopped
            keeper, rest = left[0], left[1:]
            close = [m for m in rest if m.kind == keeper.kind and match(keeper, m, cfg)]
            if close:
                group = [keeper, *close]
                groups.append(
                    NearGroup(
                        keeper.kind,
                        group_type(group),
                        keeper.hash,
                        tuple(m.hash for m in close),
                    )
                )
            taken = {keeper.hash, *(m.hash for m in close)}
            left = [m for m in rest if m.hash not in taken]
    return groups


def _pair(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


def store_groups(db: Database, groups: Sequence[NearGroup]) -> list[int]:
    """Replace every near group; returns their ids in the order given."""
    ids: list[int] = []
    with db.tx() as conn:
        conn.execute("DELETE FROM near_member")
        conn.execute("DELETE FROM near_group")
        for g in groups:
            cur = conn.execute(
                "INSERT INTO near_group (kind, group_type, algo_version) VALUES (?, ?, ?)",
                (g.kind, g.group_type, ALGO_VERSION),
            )
            gid = int(cur.lastrowid or 0)
            ids.append(gid)
            conn.executemany(
                "INSERT INTO near_member (group_id, content_hash, role) VALUES (?, ?, ?)",
                [(gid, g.keeper, "keeper"), *((gid, c, "copy") for c in g.copies)],
            )
    return ids
