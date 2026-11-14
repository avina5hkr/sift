"""The Apps tab (spec 10C, invariant I10): the app list, protection, and the app actions.

Protection is enforced here, on the server. A protected package can be uninstalled or cleared
only when its unlock conditions hold in the same request: WhatsApp and WhatsApp Business need a
local chat backup under 24 hours old, the owner's "my chat backup is complete" and the typed app
name; every other protected package (authenticators, the owner's additions) needs an explicit,
recorded Unprotect. Before any uninstall the app list is saved to every mounted destination.
"""

import csv
import io
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from sift.backup.run import usable_dests
from sift.core.context import AppContext
from sift.device.apps import (
    PLAY_STORE,
    parse_diskstats,
    parse_packages,
    parse_usagestats,
    parse_versions,
    valid_package,
)
from sift.device.session import DeviceSession

DAY = 86_400
WHATSAPP = {
    "com.whatsapp": ("WhatsApp", "/sdcard/Android/media/com.whatsapp/WhatsApp/Databases",
                     "/sdcard/WhatsApp/Databases"),
    "com.whatsapp.w4b": ("WhatsApp Business",
                         "/sdcard/Android/media/com.whatsapp.w4b/WhatsApp Business/Databases",
                         "/sdcard/WhatsApp Business/Databases"),
}  # fmt: skip


@dataclass
class AppRow:
    package: str
    name: str
    installer: str | None
    from_play_store: bool
    version: str | None
    app_bytes: int | None
    data_bytes: int | None
    cache_bytes: int | None
    last_used: int | None
    protected: bool
    unprotected: bool
    unused: bool


def list_apps(ctx: AppContext, session: DeviceSession) -> tuple[list[AppRow], dict[str, bool]]:
    """Every third-party app with what the probes could read; second value: which probes worked."""
    t = session.transport
    texts = {k: t.apps_probe(k) for k in ("packages", "diskstats", "usagestats", "versions")}
    packages = parse_packages(texts["packages"] or "")
    sizes = parse_diskstats(texts["diskstats"] or "")
    used = parse_usagestats(texts["usagestats"] or "")
    versions = parse_versions(texts["versions"] or "")
    protected = set(ctx.config.apps.protected_packages)
    with ctx.db.read() as conn:
        unprot = {str(r[0]) for r in conn.execute("SELECT package FROM app_unprotect")}
    cutoff = time.time() - ctx.config.apps.unused_days * DAY
    rows = []
    for pkg, installer in sorted(packages.items()):
        app_b, data_b, cache_b = sizes.get(pkg, (None, None, None))
        last = used.get(pkg)
        rows.append(
            AppRow(
                pkg, WHATSAPP[pkg][0] if pkg in WHATSAPP else pkg, installer,
                installer == PLAY_STORE, versions.get(pkg), app_b, data_b, cache_b, last,
                pkg in protected, pkg in unprot, last is not None and last < cutoff,
            )
        )  # fmt: skip
    probes = {k: bool(v) for k, v in texts.items()}
    return rows, probes


def suggestions(rows: list[AppRow]) -> list[AppRow]:
    """Unused for 90+ days, largest first; never pre-ticked, never protected."""
    picked = [r for r in rows if r.unused and not r.protected]
    return sorted(picked, key=lambda r: -((r.app_bytes or 0) + (r.data_bytes or 0)))


def whatsapp_backup_age_h(session: DeviceSession, package: str) -> float | None:
    """Hours since the newest local chat backup (msgstore*.db.crypt*), or None if none."""
    newest = 0
    for folder in WHATSAPP[package][1:]:
        try:
            entries = session.transport.list_dir(folder)
        except Exception:
            continue
        for e in entries:
            name = e.path.rsplit("/", 1)[-1]
            if not e.is_dir and name.startswith("msgstore") and ".db.crypt" in name:
                newest = max(newest, e.mtime)
    return None if not newest else (time.time() - newest) / 3600


def check(
    ctx: AppContext, session: DeviceSession, package: str, action: str, unlock: dict[str, Any]
) -> str | None:
    """Why `action` on `package` is refused now, or None when it may go ahead (I10)."""
    if not valid_package(package):
        return "Not an app package name."
    if action in ("uninstall", "clear_data") and package in ctx.config.apps.protected_packages:
        if package in WHATSAPP:
            name = WHATSAPP[package][0]
            age = whatsapp_backup_age_h(session, package)
            limit = ctx.config.apps.whatsapp_backup_max_age_hours
            if age is None or age > limit:
                return f"{name}'s chat backup on the phone is not from the last {limit} hours."
            if not unlock.get("backup_done"):
                return f"Confirm that your {name} chat backup is complete."
            if str(unlock.get("typed_name", "")).strip().casefold() != name.casefold():
                return f"Type {name} to confirm."
            return None
        with ctx.db.read() as conn:
            if (
                conn.execute("SELECT 1 FROM app_unprotect WHERE package = ?", (package,)).fetchone()
                is None
            ):
                return "This app is protected. Unprotect it first."
    return None


def save_app_list(ctx: AppContext, rows: list[AppRow]) -> list[str]:
    """DEST_ROOT/.sift/apps/DATE.csv on every mounted destination; never replaces a file."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["name", "package", "version", "installer", "play_store_link"])
    for r in rows:
        link = (
            f"https://play.google.com/store/apps/details?id={r.package}"
            if r.from_play_store
            else ""
        )
        w.writerow([r.name, r.package, r.version or "", r.installer or "", link])
    body = buf.getvalue().encode()
    stamp = time.strftime("%Y-%m-%d-%H%M%S")
    written = []
    for dest in usable_dests(ctx):
        folder = Path(dest.root) / ".sift" / "apps"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{stamp}.csv"
        n = 1
        while path.exists():
            path = folder / f"{stamp}-{n}.csv"
            n += 1
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        try:
            os.write(fd, body)
            os.fsync(fd)
        finally:
            os.close(fd)
        written.append(str(path))
    return written


def save_apks(ctx: AppContext, session: DeviceSession, packages: list[str]) -> dict[str, int]:
    """Pull each package's APKs (splits too) to .sift/apps/apks/PACKAGE/ on the first mounted
    destination; a file already there is kept, never replaced."""
    dests = usable_dests(ctx)
    if not dests:
        return {"saved": 0, "failed": len(packages)}
    saved = failed = 0
    for pkg in packages:
        if not valid_package(pkg):
            failed += 1
            continue
        folder = Path(dests[0].root) / ".sift" / "apps" / "apks" / pkg
        try:
            folder.mkdir(parents=True, exist_ok=True)
            for remote in session.transport.package_paths(pkg):
                target = folder / remote.rsplit("/", 1)[-1]
                if target.exists():
                    continue
                with session.transport.open_read(remote) as src, open(target, "xb") as out:
                    while chunk := src.read(1 << 20):
                        out.write(chunk)
            saved += 1
        except Exception:
            failed += 1
    return {"saved": saved, "failed": failed}


def as_dict(r: AppRow) -> dict[str, Any]:
    return asdict(r)
