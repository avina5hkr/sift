"""The pre-reset checklist and restore helper (spec 10D). Sift never resets the phone.

Ten items; each turns green when its automatic check passes and, where it needs one, the owner
has ticked it. Automatic checks read the database and the backup folders; the WhatsApp file and
the phone's contact count are read from the phone when it is connected (read-only).
"""

import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from sift.backup import confirm
from sift.backup.run import usable_dests
from sift.core.context import AppContext
from sift.device.session import DeviceSession

How = Literal["auto", "tick", "both"]


@dataclass(frozen=True)
class Item:
    id: int
    title: str
    how: How
    help: str


ITEMS = [
    Item(1, "Full backup verified on both destinations", "auto",
         "Every backup set is Confirmed and not stale."),
    Item(2, "WhatsApp: local backup under 24 hours old; Google Drive backup done and its key or "
         "password saved", "both",
         "Sift checks the local backup file; tick when the encrypted Drive backup is done and "
         "its key or password is saved somewhere safe."),
    Item(3, "Contacts exported as .vcf and backed up", "auto",
         "Export contacts to a .vcf file on the phone (Contacts, Manage contacts, Export), then "
         "back it up. Sift counts the cards and compares with the phone's contact count."),
    Item(4, "Samsung Smart Switch backup to the Mac done", "tick",
         "Sift cannot read Smart Switch's backup; tick with the date you made it."),
    Item(5, "Authenticators: cloud backup on and recovery account noted; other authenticators "
         "exported; recovery codes saved", "tick",
         "Authenticator backups restore on the same platform only. Work or school accounts "
         "need a new sign-in or an admin reset."),
    Item(6, "Secure Folder emptied or backed up", "tick", "Files in Secure Folder are erased."),
    Item(7, "Google and Samsung accounts removed, screen-lock PIN known", "tick",
         "Removing accounts first avoids the reset protection lock."),
    Item(8, "SIM tested in another phone for SMS and calls; eSIM profiles noted", "tick",
         "A reset erases eSIM profiles; note them so the carrier can reissue them."),
    Item(9, "App reinstall list saved", "auto", "Save it from the Apps page (Save app list)."),
    Item(10, "Notes, health data and password-manager exports done", "tick",
         "Apps with their own export (notes, health, password managers) are not in Sift's backup."),
]  # fmt: skip
BY_ID = {i.id: i for i in ITEMS}


@dataclass
class ItemState:
    id: int
    title: str
    how: How
    help: str
    auto_ok: bool | None  # None: no automatic part, or it cannot be judged now
    auto_detail: str | None
    ticked: bool
    done_on: str | None
    note: str | None
    green: bool


def _ticks(ctx: AppContext, device_id: int) -> dict[int, tuple[bool, str | None, str | None]]:
    with ctx.db.read() as conn:
        rows = conn.execute(
            "SELECT item, ticked, done_on, note FROM reset_check WHERE device_id = ?", (device_id,)
        ).fetchall()
    return {int(r[0]): (bool(r[1]), r[2], r[3]) for r in rows}


def set_tick(
    ctx: AppContext, device_id: int, item: int, ticked: bool, done_on: str | None, note: str | None
) -> None:
    with ctx.db.tx() as conn:
        conn.execute(
            "INSERT INTO reset_check (device_id, item, ticked, done_on, note, at) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (device_id, item) DO UPDATE SET "
            "ticked = excluded.ticked, done_on = excluded.done_on, note = excluded.note, "
            "at = excluded.at",
            (device_id, item, int(ticked), done_on, note, int(time.time())),
        )


def _backup_verified(ctx: AppContext, device_id: int) -> tuple[bool, str]:
    names = [s.name for s in ctx.config.backup.sets if s.enabled]
    if not names:
        return False, "No backup sets are turned on."
    missing = []
    for name in names:
        c = confirm.latest(ctx.db, ctx.config, device_id, name)
        if c is None or not c.confirmed:
            missing.append(f"{name} (not confirmed)")
        elif c.stale_reason:
            missing.append(f"{name} (stale)")
    if missing:
        return False, "Confirm on the Backup page: " + ", ".join(missing) + "."
    return True, f"All {len(names)} sets confirmed."


NOT_INSTALLED = "WhatsApp is not installed; nothing to back up."


def _whatsapp(ctx: AppContext, session: DeviceSession | None) -> tuple[bool | None, str]:
    from sift.cleanup.apps import WHATSAPP, whatsapp_backup_age_h
    from sift.device.apps import parse_packages

    if session is None:
        return None, "Connect the phone to check the WhatsApp backup file."
    try:
        installed = set(parse_packages(session.transport.apps_probe("packages") or ""))
    except Exception:
        installed = set()
    apps = [p for p in WHATSAPP if p in installed] if installed else ["com.whatsapp"]
    if not apps:
        return True, NOT_INSTALLED
    limit = ctx.config.apps.whatsapp_backup_max_age_hours
    lines, ok = [], True
    for pkg in apps:
        name = WHATSAPP[pkg][0]
        age = whatsapp_backup_age_h(session, pkg)
        if age is None:
            ok = False
            lines.append(f"No local {name} backup found ({name}, Settings, Chats, Chat backup).")
        elif age > limit:
            ok = False
            lines.append(f"The local {name} backup is {age:.0f} hours old; back up again.")
        else:
            lines.append(f"Local {name} backup is {age:.0f} hours old.")
    return ok, " ".join(lines)


def newest_vcf(ctx: AppContext, device_id: int) -> tuple[Path, str, int] | None:
    """The newest backed-up .vcf of this phone: (local file, phone path, phone mtime)."""
    dests = {d.id: d for d in usable_dests(ctx)}
    if not dests:
        return None
    with ctx.db.read() as conn:
        rows = conn.execute(
            "SELECT r.dest_id, r.rel_path, r.size, p.remote_path, p.mtime FROM phone_file p "
            "JOIN replica r ON r.content_hash = p.content_hash "
            "WHERE p.device_id = ? AND lower(p.remote_path) LIKE '%.vcf' AND r.verified_at > 0 "
            "ORDER BY p.mtime DESC, p.id DESC",
            (device_id,),
        ).fetchall()
    for dest_id, rel, size, remote, mtime in rows:
        dest = dests.get(int(dest_id))
        if dest is None:
            continue
        local = Path(dest.root) / str(rel)
        try:
            if local.stat().st_size == int(size):
                return local, str(remote), int(mtime)
        except OSError:
            continue
    return None


def count_cards(path: Path) -> int:
    n = 0
    with open(path, "rb") as f:
        for line in f:
            if line.strip().upper() == b"BEGIN:VCARD":
                n += 1
    return n


def _contacts(
    ctx: AppContext, device_id: int, session: DeviceSession | None
) -> tuple[bool | None, str]:
    from sift.device.health import parse_count

    found = newest_vcf(ctx, device_id)
    if found is None:
        return False, "No backed-up .vcf found. Export contacts on the phone, then back up."
    local, remote, _ = found
    cards = count_cards(local)
    if cards == 0:
        return False, f"{remote.rsplit('/', 1)[-1]} has no contacts in it."
    phone_count = None
    if session is not None:
        try:
            phone_count = parse_count(session.transport.health_probe("contacts") or "")
        except Exception:
            phone_count = None
    name = remote.rsplit("/", 1)[-1]
    if phone_count is None or phone_count == 0:
        return True, f"{name}: {cards} contacts (the phone's own count is unavailable)."
    if cards < phone_count:
        return False, f"{name} has {cards} contacts; the phone has {phone_count}. Export again."
    return True, f"{name}: {cards} contacts; the phone has {phone_count}."


def newest_app_list(ctx: AppContext) -> Path | None:
    found = [p for d in usable_dests(ctx) for p in (Path(d.root) / ".sift" / "apps").glob("*.csv")]
    return max(found, key=lambda p: p.stat().st_mtime) if found else None


def _app_list(ctx: AppContext) -> tuple[bool, str]:
    path = newest_app_list(ctx)
    if path is None:
        return False, "No app list saved yet."
    when = datetime.fromtimestamp(path.stat().st_mtime)
    return True, f"Saved {when:%d %b %Y}."


def states(ctx: AppContext, device_id: int, session: DeviceSession | None) -> list[ItemState]:
    ticks = _ticks(ctx, device_id)
    auto: dict[int, tuple[bool | None, str]] = {
        1: _backup_verified(ctx, device_id),
        2: _whatsapp(ctx, session),
        3: _contacts(ctx, device_id, session),
        9: _app_list(ctx),
    }
    out = []
    for item in ITEMS:
        ok, detail = auto.get(item.id, (None, None))
        ticked, done_on, note = ticks.get(item.id, (False, None, None))
        green = (item.how == "tick" or ok is True) and (
            item.how == "auto" or ticked or detail == NOT_INSTALLED
        )
        out.append(ItemState(item.id, item.title, item.how, item.help, ok, detail, ticked,
                             done_on, note, green))  # fmt: skip
    return out


def markdown(items: list[ItemState]) -> str:
    done = sum(i.green for i in items)
    lines = [f"# Pre-reset checklist\n\n{datetime.now():%d %b %Y, %H:%M}. {done} of "
             f"{len(items)} done. Sift never resets the phone.\n"]  # fmt: skip
    for i in items:
        lines.append(f"- [{'x' if i.green else ' '}] {i.id}. {i.title}")
        extra = [x for x in (i.auto_detail, i.done_on and f"Done {i.done_on}", i.note) if x]
        lines += [f"  - {x}" for x in extra]
    return "\n".join(lines) + "\n"


def reinstall_list(ctx: AppContext) -> dict[str, Any]:
    import csv

    path = newest_app_list(ctx)
    if path is None:
        return {"path": None, "apps": []}
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    apps = [{"name": r.get("name", "").lstrip("'"), "package": r.get("package", ""),
             "link": r.get("play_store_link", "")} for r in rows]  # fmt: skip
    return {"path": str(path), "apps": apps}


def as_dict(s: ItemState) -> dict[str, Any]:
    return asdict(s)
