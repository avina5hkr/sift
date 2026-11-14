"""The Apps tab API (spec 10C, 12). Every action re-runs the protection checks (I10)."""

import time
from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

from sift.cleanup import apps
from sift.device.apps import valid_package
from sift.device.transport import DeviceDisconnected, DeviceTimeout
from sift.server.deps import Ctx
from sift.server.errors import ApiError
from sift.server.routes.delete import _no_backup_running, _session

router = APIRouter()


def _phone(ctx: Ctx) -> Any:
    session = _session(ctx)
    if session is None:
        raise ApiError(409, "no_phone", "Connect the phone over USB first.")
    return session


@router.get("/apps")
def list_apps(ctx: Ctx) -> dict[str, Any]:
    rows, probes = apps.list_apps(ctx, _phone(ctx))
    return {
        "apps": [apps.as_dict(r) for r in rows],
        "suggested": [r.package for r in apps.suggestions(rows)],
        "probes": probes,  # which probes answered; a missing one shows "unavailable"
    }


class Unlock(BaseModel):
    backup_done: bool = False
    typed_name: str = ""


class AppsRequest(BaseModel):
    action: Literal["uninstall", "clear_data"]
    packages: list[str] = Field(min_length=1, max_length=500)
    unlock: dict[str, Unlock] = Field(default_factory=dict)
    confirm: bool = False


def _installed(ctx: Ctx, session: Any) -> tuple[list[apps.AppRow], set[str]]:
    """A fresh app list; only packages on it may be acted on (I10). No list, no action."""
    rows, probes = apps.list_apps(ctx, session)
    if not probes.get("packages") or not rows:
        raise ApiError(409, "no_app_list", "Sift could not read the phone's app list. Try again.")
    return rows, {r.package for r in rows}


def _checks(
    ctx: Ctx, session: Any, body: AppsRequest, installed: set[str]
) -> dict[str, str | None]:
    return {
        p: apps.check(
            ctx, session, p, body.action, body.unlock.get(p, Unlock()).model_dump(), installed
        )
        for p in dict.fromkeys(body.packages)
    }


@router.post("/apps/preflight")
def preflight(ctx: Ctx, body: AppsRequest) -> dict[str, Any]:
    session = _phone(ctx)
    _, installed = _installed(ctx, session)
    found = _checks(ctx, session, body, installed)
    return {"allowed": [p for p, why in found.items() if why is None],
            "refused": {p: why for p, why in found.items() if why is not None}}  # fmt: skip


def _act(ctx: Ctx, body: AppsRequest, action: str) -> dict[str, Any]:
    if body.action != action:
        raise ApiError(422, "wrong_action", "The action does not match this request.")
    if not body.confirm:
        raise ApiError(409, "not_confirmed", "Confirm first.")
    _no_backup_running(ctx)
    session = _phone(ctx)
    from sift.device.wifi import delete_blocked

    if why := delete_blocked(ctx.config.phone.allow_delete_over_wifi, session):
        raise ApiError(409, "wifi", why)
    try:
        session.check_identity()
    except Exception as e:
        raise ApiError(409, "wrong_phone", f"Sift could not confirm the phone: {e}") from e
    rows, installed = _installed(ctx, session)
    found = _checks(ctx, session, body, installed)  # the checks again, in this request (I10)
    refused = {p: why for p, why in found.items() if why is not None}
    if refused:
        raise ApiError(409, "protected", "; ".join(f"{p}: {w}" for p, w in refused.items()))
    saved: list[str] = []
    if action == "uninstall" and ctx.config.apps.save_app_list_before_uninstall:
        saved = apps.save_app_list(ctx, rows)
        if not saved:
            raise ApiError(
                409, "no_backup_drive", "Sift saves the app list first; connect a backup folder."
            )
    done: list[str] = []
    failed: dict[str, str] = {}
    run = session.transport.uninstall if action == "uninstall" else session.transport.clear_data
    for i, pkg in enumerate(found):
        try:
            run(pkg)
            done.append(pkg)
        except (DeviceDisconnected, DeviceTimeout) as e:
            for rest in list(found)[i:]:
                failed[rest] = f"The phone stopped answering: {e}"
            break
        except Exception as e:
            failed[pkg] = str(e)
    if action == "uninstall" and done:
        with ctx.db.tx() as conn:  # an Unprotect does not carry over to a reinstall
            conn.executemany("DELETE FROM app_unprotect WHERE package = ?", [(p,) for p in done])
    return {"done": done, "failed": failed, "app_list": saved}


@router.post("/apps/uninstall")
def uninstall(ctx: Ctx, body: AppsRequest) -> dict[str, Any]:
    return _act(ctx, body, "uninstall")


@router.post("/apps/clear-data")
def clear_data(ctx: Ctx, body: AppsRequest) -> dict[str, Any]:
    return _act(ctx, body, "clear_data")


class Confirm(BaseModel):
    confirm: bool = False


@router.post("/apps/trim-caches")
def trim_caches(ctx: Ctx, body: Confirm) -> dict[str, bool]:
    if not body.confirm:
        raise ApiError(409, "not_confirmed", "Confirm first.")
    _no_backup_running(ctx)
    session = _phone(ctx)
    from sift.device.wifi import delete_blocked

    if why := delete_blocked(ctx.config.phone.allow_delete_over_wifi, session):
        raise ApiError(409, "wifi", why)
    try:
        session.check_identity()
    except Exception as e:
        raise ApiError(409, "wrong_phone", f"Sift could not confirm the phone: {e}") from e
    try:
        session.transport.trim_caches()
    except Exception as e:
        raise ApiError(409, "phone_refused", f"The phone did not free its caches: {e}") from e
    return {"done": True}


class ExportRequest(BaseModel):
    apks: list[str] = Field(default_factory=list, max_length=500)


@router.post("/apps/export")
def export(ctx: Ctx, body: ExportRequest) -> dict[str, Any]:
    session = _phone(ctx)
    rows, installed = _installed(ctx, session)
    saved = apps.save_app_list(ctx, rows)
    if not saved:
        raise ApiError(409, "no_backup_drive", "Connect a backup folder first.")
    out: dict[str, Any] = {"app_list": saved}
    if body.apks:
        wanted = [p for p in dict.fromkeys(body.apks) if p in installed]
        out["apks"] = apps.save_apks(ctx, session, wanted, {r.package: r.version for r in rows})
        out["apks"]["failed"] += len(set(body.apks)) - len(wanted)
    return out


class UnprotectRequest(BaseModel):
    package: str
    confirm: bool = False
    protect_again: bool = False


@router.post("/apps/unprotect")
def unprotect(ctx: Ctx, body: UnprotectRequest) -> dict[str, bool]:
    if not valid_package(body.package):
        raise ApiError(422, "bad_package", "Not an app package name.")
    if body.protect_again:  # always allowed, protected or not
        with ctx.db.tx() as conn:
            conn.execute("DELETE FROM app_unprotect WHERE package = ?", (body.package,))
        return {"unprotected": False}
    if body.package in apps.WHATSAPP:
        raise ApiError(409, "whatsapp", "WhatsApp unlocks with its chat backup, not here.")
    if not apps.is_protected(ctx, body.package):
        raise ApiError(422, "not_protected", "This app is not protected.")
    with ctx.db.tx() as conn:
        if not body.confirm:
            raise ApiError(
                409, "not_confirmed", "Confirm that its codes or data may not be recoverable."
            )
        conn.execute(
            "INSERT OR REPLACE INTO app_unprotect (package, at) VALUES (?, ?)",
            (body.package, int(time.time())),
        )
    return {"unprotected": True}
