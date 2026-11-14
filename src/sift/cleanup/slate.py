"""Clean slate (spec 10A, invariant I11): keep what the owner lists, remove the rest after a
verified backup.

`decide` is pure: for one file, the first matching rule wins (never-delete list, Protected,
manual keep, keep folders, newer than N days, labelled keep, camera originals, path patterns),
else `remove`. A removal not backed up on every required destination is a gap. The plan never
deletes; its removal set goes through the normal delete run (source `slate`).
"""

import fnmatch
import json
import time
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from sift.backup.sets import resolve_roots, set_for_path
from sift.cleanup.browse import FileRow, backed_up, load_rows
from sift.cleanup.lexicons import Lexicon, load
from sift.core.config import NEVER_DELETE_FLOOR, Config, phone_path_key
from sift.core.context import AppContext

DAY = 86_400
CATEGORIES = ("camera", "screenshots", "whatsapp", "documents", "junk", "other")


@dataclass
class Rules:
    keep_folders: list[str] = field(default_factory=lambda: ["/sdcard/Documents"])
    keep_newer_than_days: int = 30  # 0 = off
    keep_labelled: bool = True
    keep_camera: bool = False
    patterns: list[str] = field(default_factory=list)  # e.g. /sdcard/Download/*.pdf

    @classmethod
    def defaults(cls, cfg: Config) -> Rules:
        return cls(list(cfg.slate.keep_folders), cfg.slate.keep_newer_than_days)


@dataclass
class Info:
    label: str | None = None
    text: str | None = None
    screenshot: bool = False


@dataclass(frozen=True)
class Decision:
    action: str  # never | protected | keep | remove
    reason: str


def _under(key: str, folders: Iterable[str]) -> bool:
    return any(key == f or key.startswith(f.rstrip("/") + "/") for f in folders)


def decide(
    row: FileRow,
    info: Info,
    rules: Rules,
    cfg: Config,
    doc: Lexicon,
    *,
    kept_ids: set[int],
    kept_folders: Sequence[str],
    now: float,
) -> Decision:
    key = phone_path_key(row.path)
    never = [phone_path_key(p) for p in (*NEVER_DELETE_FLOOR, *cfg.browse.never_delete_extra)]
    if not key.startswith("/sdcard/") or _under(key, never):
        return Decision("never", "On the never-delete list")
    shot = info.screenshot or row.origin == "screenshot" or "/screenshots/" in key
    protected_folders = [phone_path_key(p) for p in cfg.scoring.protected_folders]
    if (
        info.label == "protected"
        or doc.hit(info.text) is not None
        or (cfg.scoring.protect_screenshots and shot)
        or _under(key, protected_folders)
    ):
        return Decision("protected", "Protected")
    if row.id in kept_ids or _under(key, [phone_path_key(f) for f in kept_folders]):
        return Decision("keep", "You kept it")
    if _under(key, [phone_path_key(f) for f in rules.keep_folders]):
        return Decision("keep", "In a kept folder")
    if rules.keep_newer_than_days and now - row.mtime < rules.keep_newer_than_days * DAY:
        return Decision("keep", f"Newer than {rules.keep_newer_than_days} days")
    if rules.keep_labelled and info.label == "keep":
        return Decision("keep", "You marked it keep")
    if rules.keep_camera and row.origin == "camera":
        return Decision("keep", "Camera original")
    if any(fnmatch.fnmatch(key, phone_path_key(p)) for p in rules.patterns):
        return Decision("keep", "Matches a keep pattern")
    return Decision("remove", "No keep rule matches")


def category(row: FileRow, info: Info) -> str:
    key = phone_path_key(row.path)
    if info.label == "junk":
        return "junk"
    if row.origin == "camera" or "/dcim/camera/" in key:
        return "camera"
    if row.origin == "screenshot" or "/screenshots/" in key:
        return "screenshots"
    if "whatsapp" in key:
        return "whatsapp"
    if row.kind == "doc" or key.startswith(("/sdcard/documents/", "/sdcard/download/")):
        return "documents"
    return "other"


@dataclass
class Planned:
    row: FileRow
    decision: Decision
    category: str
    gap: str | None  # not_backed_up | no_set | None


def _infos(ctx: AppContext, hashes: Sequence[str]) -> dict[str, Info]:
    out: dict[str, Info] = {}
    with ctx.db.read() as conn:
        for start in range(0, len(hashes), 900):
            part = hashes[start : start + 900]
            marks = ",".join("?" for _ in part)
            for h, label, text, shot in conn.execute(
                "SELECT c.hash, l.label, c.ocr_text, c.is_screenshot FROM content c "
                f"LEFT JOIN label l ON l.content_hash = c.hash WHERE c.hash IN ({marks})",
                part,
            ):
                out[str(h)] = Info(label, text, bool(shot))
    return out


def load_plan(ctx: AppContext, plan_id: int) -> tuple[int, Rules, set[int], list[str]]:
    with ctx.db.read() as conn:
        row = conn.execute(
            "SELECT device_id, rules FROM slate_plan WHERE id = ?", (plan_id,)
        ).fetchone()
        if row is None:
            raise KeyError(plan_id)
        keeps = conn.execute(
            "SELECT phone_file_id, folder FROM slate_keep WHERE plan_id = ?", (plan_id,)
        ).fetchall()
    rules = Rules(**json.loads(row[1]))
    return (
        int(row[0]),
        rules,
        {int(k[0]) for k in keeps if k[0] is not None},
        [str(k[1]) for k in keeps if k[1] is not None],
    )


def create(ctx: AppContext, device_id: int, rules: Rules) -> int:
    with ctx.db.tx() as conn:
        cur = conn.execute(
            "INSERT INTO slate_plan (device_id, rules, created_at) VALUES (?, ?, ?)",
            (device_id, json.dumps(asdict(rules)), int(time.time())),
        )
    return int(cur.lastrowid or 0)


def plan(ctx: AppContext, plan_id: int, *, now: float | None = None) -> list[Planned]:
    """Decide every present file of the plan's phone (recomputed each time: resume-safe)."""
    device_id, rules, kept_ids, kept_folders = load_plan(ctx, plan_id)
    cfg = ctx.config
    at = now if now is not None else time.time()
    rows = load_rows(ctx.db, cfg, device_id)
    infos = _infos(ctx, sorted({r.content_hash for r in rows if r.content_hash}))
    doc = load("document", ctx.paths.lexicons)
    roots = resolve_roots(cfg.backup.sets, [s.name for s in cfg.backup.sets if s.enabled])
    out = []
    for r in rows:
        info = infos.get(r.content_hash or "", Info())
        d = decide(r, info, rules, cfg, doc, kept_ids=kept_ids, kept_folders=kept_folders, now=at)
        gap = None
        if d.action == "remove" and not backed_up(cfg, r.status):
            gap = "not_backed_up" if set_for_path(r.path, roots) else "no_set"
        out.append(Planned(r, d, category(r, info), gap))
    return out


def summary(items: Sequence[Planned]) -> dict[str, Any]:
    def total(sel: Iterable[Planned]) -> dict[str, int]:
        sel = list(sel)
        return {"files": len(sel), "bytes": sum(p.row.size for p in sel)}

    remove = [p for p in items if p.decision.action == "remove"]
    return {
        "remove": total(p for p in remove if p.gap is None),
        "keep": total(p for p in items if p.decision.action == "keep"),
        "protected": total(p for p in items if p.decision.action == "protected"),
        "never": total(p for p in items if p.decision.action == "never"),
        "gaps": total(p for p in remove if p.gap is not None),
        "gaps_no_set": total(p for p in remove if p.gap == "no_set"),
        "categories": {
            c: total(p for p in remove if p.gap is None and p.category == c) for c in CATEGORIES
        },
    }


def tree(items: Sequence[Planned], side: str) -> list[dict[str, Any]]:
    """Groups by folder (two levels under /sdcard) for one side: remove, keep or protected."""
    groups: dict[str, dict[str, Any]] = {}
    for p in items:
        action = p.decision.action
        if (side == "remove" and not (action == "remove" and p.gap is None)) or (
            side != "remove" and action != side
        ):
            continue
        parts = p.row.path.split("/")[2:4]
        folder = "/sdcard/" + "/".join(parts[:-1] if len(parts) > 1 else parts)
        g = groups.setdefault(
            folder, {"folder": folder, "files": 0, "bytes": 0, "sample_ids": [], "reasons": {}}
        )
        g["files"] += 1
        g["bytes"] += p.row.size
        if len(g["sample_ids"]) < 6 and p.row.kind in ("image", "video"):
            g["sample_ids"].append(p.row.id)
        g["reasons"][p.decision.reason] = g["reasons"].get(p.decision.reason, 0) + 1
    return sorted(groups.values(), key=lambda g: -g["bytes"])


def keep(ctx: AppContext, plan_id: int, ids: Sequence[int], folders: Sequence[str]) -> None:
    with ctx.db.tx() as conn:
        conn.executemany(
            "INSERT INTO slate_keep (plan_id, phone_file_id) VALUES (?, ?)",
            [(plan_id, int(i)) for i in ids],
        )
        conn.executemany(
            "INSERT INTO slate_keep (plan_id, folder) VALUES (?, ?)",
            [(plan_id, f) for f in folders if f.startswith("/sdcard/")],
        )


def gap_sets(ctx: AppContext, items: Sequence[Planned]) -> list[str]:
    """The backup sets that cover the gap files (what Back up now starts)."""
    cfg = ctx.config
    roots = resolve_roots(cfg.backup.sets, [s.name for s in cfg.backup.sets if s.enabled])
    names = {set_for_path(p.row.path, roots) for p in items if p.gap == "not_backed_up"}
    return sorted(n for n in names if n)


def removal(items: Sequence[Planned]) -> list[Planned]:
    return [p for p in items if p.decision.action == "remove" and p.gap is None]
