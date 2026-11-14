"""Health report, bug report and the pre-reset checklist API (spec 10D, 12). Read-only on the
phone, except the owner-started bug report capture and the contacts push after a reset."""

from pathlib import Path
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from sift.backup.run import _stored_device_id, usable_dests
from sift.cleanup.archive import push_one
from sift.core.files import report_folder, write_new
from sift.health import bugreport, checklist
from sift.health import report as health
from sift.server.deps import Ctx
from sift.server.errors import ApiError
from sift.server.routes.delete import _session
from sift.server.routes.vault import TaskOut, start_task
from sift.services.tasks import TaskControl

router = APIRouter()


def _phone(ctx: Ctx) -> Any:
    session = _session(ctx)
    if session is None:
        raise ApiError(409, "no_phone", "Connect the phone over USB first.")
    return session


def _device_id(ctx: Ctx, session: Any | None) -> int:
    """The connected phone, or else the phone Sift knows best."""
    if session is not None:
        found = _stored_device_id(ctx.db, session.device_uuid)
        if found is not None:
            return found
    from sift.server.routes.teach import _device_id as stored

    return stored(ctx)


def _roots(ctx: Ctx) -> list[Path]:
    return [Path(d.root) for d in usable_dests(ctx)]


@router.post("/health/run")
def run(ctx: Ctx) -> TaskOut:
    session = _phone(ctx)
    device_id = _device_id(ctx, session)

    def work(control: TaskControl) -> dict[str, Any]:
        rep = health.collect(
            session.transport,
            ctx.config,
            should_stop=lambda: control.should_stop,
            on_progress=control.progress,
        )
        return {"report_id": health.save(ctx.db, device_id, rep)}

    return start_task(ctx, "health", work)


@router.get("/health/report")
def get_report(ctx: Ctx) -> dict[str, Any]:
    return {"report": health.latest(ctx.db, _device_id(ctx, _session(ctx)))}


@router.post("/health/export")
def export(ctx: Ctx) -> dict[str, Any]:
    rep = health.latest(ctx.db, _device_id(ctx, _session(ctx)))
    if rep is None:
        raise ApiError(409, "no_report", "Run the health report first.")
    roots = _roots(ctx)
    if not roots:
        raise ApiError(409, "no_backup_drive", "Connect a backup folder first.")
    return {"paths": health.export(rep, roots, ctx.config.health.report_dir),
            "markdown": health.markdown(rep)}  # fmt: skip


@router.post("/health/bugreport")
def capture_bugreport(ctx: Ctx) -> TaskOut:
    session = _phone(ctx)
    roots = _roots(ctx)
    if not roots:
        raise ApiError(409, "no_backup_drive", "Connect a backup folder first.")

    def work(control: TaskControl) -> dict[str, Any]:
        paths = bugreport.capture(
            session.transport,
            roots,
            ctx.config.health.report_dir,
            on_progress=control.progress,
            should_stop=lambda: control.should_stop,
        )
        return {"paths": paths}

    return start_task(ctx, "bugreport", work)


@router.get("/reset-checklist")
def get_checklist(ctx: Ctx) -> dict[str, Any]:
    session = _session(ctx)
    items = checklist.states(ctx, _device_id(ctx, session), session)
    return {"items": [checklist.as_dict(i) for i in items], "phone": session is not None}


class Tick(BaseModel):
    ticked: bool
    done_on: str | None = Field(default=None, max_length=40)
    note: str | None = Field(default=None, max_length=500)


@router.put("/reset-checklist/{item}")
def put_tick(ctx: Ctx, item: int, body: Tick) -> dict[str, Any]:
    found = checklist.BY_ID.get(item)
    if found is None:
        raise ApiError(404, "no_item", "There is no such checklist item.")
    if found.how == "auto":
        raise ApiError(422, "automatic", "Sift checks this item itself.")
    session = _session(ctx)
    checklist.set_tick(ctx, _device_id(ctx, session), item, body.ticked, body.done_on, body.note)
    return get_checklist(ctx)


@router.post("/reset-checklist/export")
def export_checklist(ctx: Ctx) -> dict[str, Any]:
    session = _session(ctx)
    text = checklist.markdown(checklist.states(ctx, _device_id(ctx, session), session))
    roots = _roots(ctx)
    if not roots:
        raise ApiError(409, "no_backup_drive", "Connect a backup folder first.")
    from datetime import datetime

    name = f"pre-reset-checklist-{datetime.now():%Y-%m-%d-%H%M%S}.md"
    paths = []
    for root in roots:
        folder = report_folder(root, ctx.config.health.report_dir)
        folder.mkdir(parents=True, exist_ok=True)
        paths.append(str(write_new(folder / name, text.encode())))
    return {"paths": paths, "markdown": text}


@router.get("/reset-checklist/restore")
def restore_helper(ctx: Ctx) -> dict[str, Any]:
    session = _session(ctx)
    vcf = checklist.newest_vcf(ctx, _device_id(ctx, session))
    return {
        "contacts": None
        if vcf is None
        else {"phone_path": vcf[1], "cards": checklist.count_cards(vcf[0])},
        "reinstall": checklist.reinstall_list(ctx),
    }


class PushContacts(BaseModel):
    confirm: bool = False


@router.post("/reset-checklist/restore/contacts")
def push_contacts(ctx: Ctx, body: PushContacts) -> dict[str, Any]:
    """Push the newest backed-up .vcf to the phone's Download folder for import (never over a
    file there)."""
    if not body.confirm:
        raise ApiError(409, "not_confirmed", "Confirm first.")
    session = _phone(ctx)
    try:
        session.check_identity()
    except Exception as e:
        raise ApiError(409, "wrong_phone", f"Sift could not confirm the phone: {e}") from e
    vcf = checklist.newest_vcf(ctx, _device_id(ctx, session))
    if vcf is None:
        raise ApiError(409, "no_vcf", "No backed-up contacts file was found.")
    local, remote, mtime = vcf
    target = "/sdcard/Download/" + remote.rsplit("/", 1)[-1]
    try:
        where = push_one(session, local, target, mtime)
    except Exception as e:
        raise ApiError(409, "push_failed", f"The phone did not take the file: {e}") from e
    return {"phone_path": where}
