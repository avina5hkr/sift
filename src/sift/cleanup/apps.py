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
import re
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
# Always protected, whatever the config says (I10); the config list only adds to these.
BUILT_IN_PROTECTED = frozenset(
    {
        "com.whatsapp",
        "com.whatsapp.w4b",
        "com.azure.authenticator",
        "com.google.android.apps.authenticator2",
        "com.authy.authy",
    }
)
MSGSTORE = re.compile(r"msgstore(-\d{4}-\d{2}-\d{2}(\.\d+)?)?\.db\.crypt\d+")


def protected_set(ctx: AppContext) -> set[str]:
    """Casefolded names of every protected package: the built-in ones plus the owner's."""
    return {p.casefold() for p in BUILT_IN_PROTECTED | set(ctx.config.apps.protected_packages)}


def is_protected(ctx: AppContext, package: str) -> bool:
    return package.casefold() in protected_set(ctx)


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
    protected = protected_set(ctx)
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
                pkg.casefold() in protected, pkg in unprot, last is not None and last < cutoff,
            )
        )  # fmt: skip
    probes = {k: bool(v) for k, v in texts.items()}
    return rows, probes


def suggestions(rows: list[AppRow]) -> list[AppRow]:
    """Unused for 90+ days, largest first; never pre-ticked, never protected."""
    picked = [r for r in rows if r.unused and not r.protected]
    return sorted(picked, key=lambda r: -((r.app_bytes or 0) + (r.data_bytes or 0)))


def whatsapp_backup_age_h(session: DeviceSession, package: str) -> float | None:
    """Hours since the newest local chat backup, by the phone's own clock, or None if there is
    none or its age cannot be judged. Only regular, non-empty msgstore files count, and a file
    dated in the future is not a fresh backup."""
    try:
        now = session.transport.info().phone_time
    except Exception:
        return None
    if now is None:
        return None
    newest = 0
    for folder in WHATSAPP[package][1:]:
        try:
            entries = session.transport.list_dir(folder)
        except Exception:
            continue
        for e in entries:
            name = e.path.rsplit("/", 1)[-1]
            if e.is_dir or e.is_symlink or e.size <= 0 or not MSGSTORE.fullmatch(name):
                continue
            if e.mtime <= now:
                newest = max(newest, e.mtime)
    return None if not newest else (now - newest) / 3600


def check(
    ctx: AppContext,
    session: DeviceSession,
    package: str,
    action: str,
    unlock: dict[str, Any],
    installed: set[str] | None = None,
) -> str | None:
    """Why `action` on `package` is refused now, or None when it may go ahead (I10)."""
    if not valid_package(package):
        return "Not an app package name."
    if installed is not None and package not in installed:
        return "Not an installed app on this phone's app list."
    if action in ("uninstall", "clear_data") and is_protected(ctx, package):
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


def _cell(value: str) -> str:
    """A CSV cell that a spreadsheet will not run as a formula."""
    return "'" + value if value[:1] in ("=", "+", "-", "@") else value


def _write_new(path: Path, body: bytes) -> Path:
    """Write to a temp file, then link it into place under a name nothing uses yet (I1)."""
    tmp = path.with_name(f".{path.name}.part-{os.getpid()}-{time.monotonic_ns()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        os.write(fd, body)
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        stem, suffix, n = path.stem, path.suffix, 1
        while True:
            try:
                os.link(tmp, path)
                return path
            except FileExistsError:
                path = path.with_name(f"{stem}-{n}{suffix}")
                n += 1
    finally:
        tmp.unlink(missing_ok=True)


def save_app_list(ctx: AppContext, rows: list[AppRow]) -> list[str]:
    """DEST_ROOT/.sift/apps/DATE.csv on every mounted destination; never replaces a file.
    An empty list (the phone did not answer) is not saved: it would not be a record."""
    if not rows:
        return []
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["name", "package", "version", "installer", "play_store_link"])
    for r in rows:
        link = (
            f"https://play.google.com/store/apps/details?id={r.package}"
            if r.from_play_store
            else ""
        )
        w.writerow(
            [_cell(r.name), r.package, _cell(r.version or ""), _cell(r.installer or ""), link]
        )
    body = buf.getvalue().encode()
    stamp = time.strftime("%Y-%m-%d-%H%M%S")
    written = []
    for dest in usable_dests(ctx):
        folder = Path(dest.root) / ".sift" / "apps"
        folder.mkdir(parents=True, exist_ok=True)
        written.append(str(_write_new(folder / f"{stamp}.csv", body)))
    return written


def save_apks(
    ctx: AppContext,
    session: DeviceSession,
    packages: list[str],
    versions: dict[str, str | None] | None = None,
) -> dict[str, int]:
    """Pull each package's APKs (splits too) to .sift/apps/apks/PACKAGE/VERSION/ on the first
    mounted destination. Each file is pulled to a temp name and linked into place only when
    complete; a file already there with the phone's size counts as saved, any other is kept and
    the new copy gets its own name (never replaced, I1)."""
    dests = usable_dests(ctx)
    if not dests:
        return {"saved": 0, "failed": len(packages)}
    saved = failed = 0
    for pkg in packages:
        if not valid_package(pkg):
            failed += 1
            continue
        version = (versions or {}).get(pkg) or "unknown"
        version = re.sub(r"[^A-Za-z0-9._-]", "_", version)[:64] or "unknown"
        folder = Path(dests[0].root) / ".sift" / "apps" / "apks" / pkg / version
        try:
            remotes = session.transport.package_paths(pkg)
            if not remotes:
                raise ValueError("no APK paths")
            folder.mkdir(parents=True, exist_ok=True)
            for remote in remotes:
                _pull_apk(session, remote, folder / remote.rsplit("/", 1)[-1])
            saved += 1
        except Exception:
            failed += 1
    return {"saved": saved, "failed": failed}


def _pull_apk(session: DeviceSession, remote: str, target: Path) -> None:
    entry = session.transport.stat(remote)
    if entry is None or entry.is_dir or entry.size <= 0:
        raise ValueError(f"cannot read {remote}")
    if target.exists() and target.stat().st_size == entry.size:
        return
    tmp = target.with_name(f".{target.name}.part-{os.getpid()}-{time.monotonic_ns()}")
    try:
        with session.transport.open_read(remote) as src, open(tmp, "xb") as out:
            while chunk := src.read(1 << 20):
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        if tmp.stat().st_size != entry.size:
            raise ValueError(f"short read of {remote}")
        path, n = target, 1
        while True:
            try:
                os.link(tmp, path)
                return
            except FileExistsError:
                path = target.with_name(f"{target.stem}-{n}{target.suffix}")
                n += 1
    finally:
        tmp.unlink(missing_ok=True)


def as_dict(r: AppRow) -> dict[str, Any]:
    return asdict(r)
