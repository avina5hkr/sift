"""The first-run wizard's API: where setup stands, adb, the phone picker, the Mac folder, the SSD,
the sets with their sizes, and the reader test.

Everything here is plain words in the API's usual `{code, message, detail}` shape. The phone's
serial number leaves this module only as its last four characters (`last4`); a phone is named in
requests by an opaque `ref` that the server resolves from the phones adb lists right now.
`complete` is remembered in the database (`setting.setup_complete`), not in config.toml.
"""

import hashlib
import json
import logging
import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, StrictStr

from sift.backup.destination import DEFAULT_LABEL, check_mac_root, sanitize_label
from sift.backup.phonepaths import shown
from sift.backup.readers import run_reader_test
from sift.backup.run import mac_target
from sift.core.config import BackupSet, save_config
from sift.core.jobs import TERMINAL, JobError
from sift.device.adb_manager import AdbStatus
from sift.device.tracker import (
    STORED_PHONE_ORDER,
    AttachedPhone,
    DeviceTracker,
    PhonePickError,
)
from sift.device.transport import TransportError
from sift.server.deps import Ctx
from sift.server.errors import ApiError
from sift.server.routes.backup import _job_error, _run_lock, _sets_lock, _unknown_set
from sift.server.routes.devices import AdbManagerDep
from sift.services.plainwords import describe
from sift.services.volumes import Volume, list_external_volumes

router = APIRouter(prefix="/setup")
_log = logging.getLogger("sift.server")
_config_lock = threading.Lock()
_reader_test_lock = threading.Lock()

SETUP_COMPLETE_KEY = "setup_complete"
SWITCH_WORD = "SWITCH"
# What Google's platform-tools package weighs, roughly: shown before anything is downloaded, so
# the owner knows what they are agreeing to. The exact size is not asked of the network first.
ADB_DOWNLOAD_BYTES = 15_000_000
ADB_DOWNLOAD_HOST = "dl.google.com"


def _setting(ctx: Ctx, key: str) -> str | None:
    with ctx.db.read() as conn:
        row = conn.execute("SELECT value FROM setting WHERE key = ?", (key,)).fetchone()
    return str(row[0]) if row is not None else None


def _tracker(ctx: Ctx) -> DeviceTracker:
    if ctx.tracker is None:
        raise ApiError(409, "no_tracker", "Sift is not looking for a phone right now.")
    return ctx.tracker


def _last4(serial: str | None) -> str | None:
    return serial[-4:] if serial else None


def _ref(serial: str) -> str:
    return hashlib.sha256(f"sift-pick:{serial}".encode()).hexdigest()[:16]


# -- where setup stands --------------------------------------------------------------------------


class PhoneOut(BaseModel):
    uuid: str
    model: str | None
    last4: str | None
    label: str | None


class SetupState(BaseModel):
    complete: bool
    phone: PhoneOut | None
    mac_root: str
    device_label: str
    suggested_label: str  # the phone's model, made safe for a folder name
    ssd_volume_uuid: str


def _has_files(ctx: Ctx, device_uuid: str) -> bool:
    """True once any file of this phone has been backed up."""
    with ctx.db.read() as conn:
        return (
            conn.execute(
                "SELECT 1 FROM phone_file p JOIN device d ON d.id = p.device_id "
                "WHERE d.uuid = ? AND p.state = 'done' LIMIT 1",
                (device_uuid,),
            ).fetchone()
            is not None
        )


def _stored_phone(ctx: Ctx) -> PhoneOut | None:
    """The phone Sift backs up. `label` is its folder name: its own, or (for a phone backed up
    before labels were per phone) the default one; null while it has none to keep."""
    with ctx.db.read() as conn:
        row = conn.execute(
            f"SELECT uuid, serial, model, label FROM device {STORED_PHONE_ORDER} LIMIT 1"
        ).fetchone()
    if row is None:
        return None
    label = row[3] or (ctx.config.backup.device_label if _has_files(ctx, row[0]) else None)
    return PhoneOut(uuid=row[0], model=row[2], last4=_last4(row[1]), label=label)


def _other_labels(ctx: Ctx, device_uuid: str | None) -> set[str]:
    """The folder names (case-folded) in use by every phone except `device_uuid`."""
    with ctx.db.read() as conn:
        rows = conn.execute(
            "SELECT label FROM device WHERE uuid IS NOT ?", (device_uuid,)
        ).fetchall()
    return {str(r[0] or ctx.config.backup.device_label).casefold() for r in rows}


def _unique_label(ctx: Ctx, wanted: str, device_uuid: str | None) -> str:
    """`wanted`, or `wanted 2`, `wanted 3`... so no two phones share a folder (ignoring case)."""
    taken = _other_labels(ctx, device_uuid)
    label, n = wanted, 1
    while label.casefold() in taken:
        n += 1
        label = f"{wanted} {n}"
    return label


@router.get("/state")
def get_state(ctx: Ctx) -> SetupState:
    """Whether first-run setup is finished, and the answers given so far."""
    phone = _stored_phone(ctx)
    backup = ctx.config.backup
    return SetupState(
        complete=_setting(ctx, SETUP_COMPLETE_KEY) == "1",
        phone=phone,
        mac_root=backup.mac_root,
        device_label=phone.label if phone and phone.label else backup.device_label,
        suggested_label=sanitize_label(phone.model or "")
        if phone and phone.model
        else DEFAULT_LABEL,
        ssd_volume_uuid=backup.ssd_volume_uuid,
    )


@router.post("/complete", status_code=200)
def complete(ctx: Ctx) -> SetupState:
    """Mark first-run setup as finished (also what "skip setup" does)."""
    with ctx.db.tx() as conn:
        conn.execute(
            "INSERT INTO setting (key, value) VALUES (?, '1') "
            "ON CONFLICT (key) DO UPDATE SET value = '1'",
            (SETUP_COMPLETE_KEY,),
        )
    return get_state(ctx)


# -- adb -----------------------------------------------------------------------------------------


class AdbState(BaseModel):
    status: str  # ready | missing | broken
    version: str | None
    message: str
    download_bytes: int  # about how big the download is
    download_host: str  # where it comes from


@router.get("/adb")
def get_adb(manager: AdbManagerDep) -> AdbState:
    """Is adb usable? When it is not, what the download would be (nothing is downloaded here)."""
    loc = manager.locate()
    return AdbState(
        status=str(loc.status),
        version=loc.version if loc.status is AdbStatus.ready else None,
        message=loc.message,
        download_bytes=ADB_DOWNLOAD_BYTES,
        download_host=ADB_DOWNLOAD_HOST,
    )


# -- the phone picker ----------------------------------------------------------------------------


class AttachedOut(BaseModel):
    ref: str
    model: str | None
    last4: str
    ready: bool
    waiting_for_allow: bool
    stored: bool

    @classmethod
    def of(cls, phone: AttachedPhone) -> AttachedOut:
        return cls(
            ref=_ref(phone.serial),
            model=phone.model,
            last4=phone.serial[-4:],
            ready=phone.ready,
            waiting_for_allow=phone.waiting_for_allow,
            stored=phone.stored,
        )


class AttachedList(BaseModel):
    phones: list[AttachedOut]


@router.get("/devices")
def list_attached(ctx: Ctx) -> AttachedList:
    """The phones connected now (never emulators), for the picker: model and last 4 characters."""
    phones = _tracker(ctx).attached_phones()
    return AttachedList(phones=[AttachedOut.of(p) for p in phones])


class PickRequest(BaseModel):
    ref: StrictStr


class SwitchRequest(PickRequest):
    confirmation: StrictStr = ""


def _attached(ctx: Ctx, ref: str) -> AttachedPhone:
    for phone in _tracker(ctx).attached_phones():
        if _ref(phone.serial) == ref:
            return phone
    raise ApiError(
        404,
        "phone_not_found",
        "That phone is not connected any more. Reconnect it and choose again.",
    )


def _running_job(ctx: Ctx) -> bool:
    return ctx.jobs is not None and any(s.state not in TERMINAL for s in ctx.jobs.snapshot())


def _refuse_while_running(ctx: Ctx) -> None:
    if _running_job(ctx):
        raise ApiError(
            409,
            "job_conflict",
            "A backup or a look through the phone is running. Wait for it to finish, or stop "
            "it, before changing the phone.",
        )


def _store(ctx: Ctx, phone: AttachedPhone) -> PhoneOut:
    try:
        uuid = _tracker(ctx).choose_phone(phone.serial)
    except PhonePickError as e:
        raise ApiError(409, "phone_not_ready", e.message) from e
    stored = _stored_phone(ctx)
    assert stored is not None and stored.uuid == uuid
    return stored


@router.post("/phone")
def choose_phone(ctx: Ctx, body: PickRequest) -> PhoneOut:
    """The owner's choice becomes the stored phone, but only while none is stored yet. Once a
    phone is stored, a different one can only be chosen with Switch phone (typed SWITCH), whatever
    state setup is in. Choosing the stored phone again changes nothing.

    The check and the change happen under the lock that starts backups, and nothing changes
    while one runs, so no job can start or lose its phone in between.
    """
    with _run_lock:
        chosen = _attached(ctx, body.ref)
        stored = _stored_phone(ctx)
        if stored is not None:
            if chosen.stored:
                return stored
            raise ApiError(
                409,
                "phone_already_chosen",
                "Sift already backs up another phone. To back up this one instead, use Switch "
                f"phone in Settings and type {SWITCH_WORD}.",
            )
        _refuse_while_running(ctx)
        return _store(ctx, chosen)


@router.post("/switch-phone")
def switch_phone(ctx: Ctx, body: SwitchRequest) -> PhoneOut:
    """Back up a different phone from now on.

    The old phone's rows and backed-up files stay exactly as they are. The new phone gets a
    folder of its own under the backup folder, named after its model (a number is added if
    another phone already uses that name), unless it was backed up before and has one.

    Needs the word SWITCH typed (any case); refused while a backup or scan runs. The check and the
    change are made under the lock that starts backups.
    """
    if body.confirmation.strip().casefold() != SWITCH_WORD.casefold():
        raise ApiError(
            400,
            "confirmation_required",
            f"Type {SWITCH_WORD} to confirm that you want to back up a different phone.",
        )
    with _run_lock:
        chosen = _attached(ctx, body.ref)
        stored = _stored_phone(ctx)
        if stored is not None and chosen.stored:
            return stored
        _refuse_while_running(ctx)
        # A phone backed up before labels existed uses the default folder name: pin that name on
        # it now, so its folder stays the same when it is chosen again later.
        with ctx.db.tx() as conn:
            conn.execute(
                "UPDATE device SET label = ? WHERE (label IS NULL OR label = '') AND id IN "
                "(SELECT DISTINCT device_id FROM phone_file WHERE state = 'done')",
                (ctx.config.backup.device_label,),
            )
        phone = _store(ctx, chosen)
        if not _has_own_label(ctx, phone.uuid):
            label = _unique_label(ctx, sanitize_label(phone.model or ""), phone.uuid)
            with ctx.db.tx() as conn:
                conn.execute("UPDATE device SET label = ? WHERE uuid = ?", (label, phone.uuid))
            phone = phone.model_copy(update={"label": label})
        return phone


def _has_own_label(ctx: Ctx, device_uuid: str) -> bool:
    with ctx.db.read() as conn:
        row = conn.execute("SELECT label FROM device WHERE uuid = ?", (device_uuid,)).fetchone()
    return bool(row and row[0])


# -- the Mac folder ------------------------------------------------------------------------------


class FolderOut(BaseModel):
    path: str
    ok: bool
    problem: str | None
    exists: bool
    free_bytes: int | None


def _app_dirs(ctx: Ctx) -> tuple[Path, ...]:
    return (ctx.paths.support, ctx.paths.cache)


@router.get("/folder")
def check_folder(ctx: Ctx, path: str = "") -> FolderOut:
    """Whether `path` can be the backup folder, and its free space. Looks only; writes nothing."""
    return FolderOut(**check_mac_root(path, app_dirs=_app_dirs(ctx)).__dict__)


class FolderRequest(BaseModel):
    path: StrictStr
    label: StrictStr = ""


class FolderSaved(BaseModel):
    mac_root: str
    device_label: str
    free_bytes: int | None


def _has_backups(ctx: Ctx) -> bool:
    with ctx.db.read() as conn:
        return conn.execute("SELECT 1 FROM replica LIMIT 1").fetchone() is not None


@router.post("/mac-folder")
def save_mac_folder(ctx: Ctx, body: FolderRequest) -> FolderSaved:
    """Save the backup folder and the phone's folder name, make both folders, and record the
    Mac destination. The phone's name is made safe for any volume, so what is saved can differ
    a little from what was typed."""
    check = check_mac_root(body.path, app_dirs=_app_dirs(ctx))
    if not check.ok:
        raise ApiError(422, "invalid_folder", check.problem or "Choose another folder.")
    phone = _stored_phone(ctx)
    label = sanitize_label(body.label or (phone.model if phone and phone.model else ""))
    backup = ctx.config.backup
    current = os.path.expanduser(backup.mac_root)
    if _has_backups(ctx) and check.path != os.path.normpath(current):
        raise ApiError(
            409,
            "backups_exist",
            "Sift has already backed up files, so the backup folder can no longer change here. "
            "Move the existing backup first.",
        )
    keeps = phone.label if phone is not None and phone.label else backup.device_label
    own_files = _has_files(ctx, phone.uuid) if phone is not None else _has_backups(ctx)
    if label != keeps and own_files:
        raise ApiError(
            409,
            "backups_exist",
            "Sift has already backed up this phone's files, so its folder name can no longer "
            "change here.",
        )
    if label.casefold() in _other_labels(ctx, phone.uuid if phone else None):
        raise ApiError(
            409,
            "label_taken",
            "Another phone already uses that folder name. Choose a different name.",
        )
    try:
        (Path(check.path) / label).mkdir(parents=True, exist_ok=True)
    except OSError as e:
        _log.warning("Could not create the backup folder: %s", e)
        raise ApiError(
            422, "invalid_folder", "Sift could not create that folder. Choose another one."
        ) from e
    with _config_lock:
        previous = (backup.mac_root, backup.device_label)
        with ctx.db.read() as conn:
            only_phone = conn.execute("SELECT COUNT(*) FROM device").fetchone()[0] <= 1
        # The default name moves only with the one phone that has used it; with several phones
        # each keeps its own, so changing the default would rename another phone's folder.
        backup.mac_root = body.path.strip()
        if only_phone:
            backup.device_label = label
        try:
            save_config(ctx.config, ctx.paths.config)
            mac_target(ctx.db, ctx.config, phone.uuid if phone else None)  # the destination row
        except OSError as e:
            backup.mac_root, backup.device_label = previous
            _log.exception("Could not save the backup folder")
            raise ApiError(
                500, "save_failed", "Sift could not save the settings. Nothing was changed."
            ) from e
        if phone is not None:
            with ctx.db.tx() as conn:
                conn.execute("UPDATE device SET label = ? WHERE uuid = ?", (label, phone.uuid))
    return FolderSaved(mac_root=backup.mac_root, device_label=label, free_bytes=check.free_bytes)


# -- the SSD -------------------------------------------------------------------------------------


def get_volume_lister() -> Callable[[], list[Volume]]:
    """Lists mounted external volumes; tests override it so no real diskutil runs."""
    return list_external_volumes


VolumeLister = Annotated[Callable[[], list[Volume]], Depends(get_volume_lister)]


class VolumeOut(BaseModel):
    name: str
    file_system: str
    uuid: str
    free_bytes: int | None
    total_bytes: int | None
    suitable: bool
    warning: str | None


class VolumesOut(BaseModel):
    volumes: list[VolumeOut]
    selected_uuid: str


@router.get("/volumes")
def list_volumes(ctx: Ctx, lister: VolumeLister) -> VolumesOut:
    """Mounted external drives (read-only look), each with its file system and free space."""
    return VolumesOut(
        volumes=[VolumeOut(**v.__dict__) for v in lister()],
        selected_uuid=ctx.config.backup.ssd_volume_uuid,
    )


class SsdRequest(BaseModel):
    volume_uuid: StrictStr | None = None  # null skips the SSD


@router.post("/ssd")
def save_ssd(ctx: Ctx, body: SsdRequest, lister: VolumeLister) -> VolumesOut:
    """Remember which drive is the SSD (or none). Copying to it arrives later; this only stores
    the choice."""
    wanted = body.volume_uuid or ""
    if wanted and wanted not in {v.uuid for v in lister()}:
        raise ApiError(
            422,
            "unknown_volume",
            "That drive is not connected any more. Plug it in and choose again.",
        )
    with _config_lock:
        previous = ctx.config.backup.ssd_volume_uuid
        ctx.config.backup.ssd_volume_uuid = wanted
        try:
            save_config(ctx.config, ctx.paths.config)
        except OSError as e:
            ctx.config.backup.ssd_volume_uuid = previous
            _log.exception("Could not save the SSD choice")
            raise ApiError(
                500, "save_failed", "Sift could not save the settings. Nothing was changed."
            ) from e
    return list_volumes(ctx, lister)


# -- what to back up -----------------------------------------------------------------------------


class SetNode(BaseModel):
    name: str
    enabled: bool
    paths: list[str]
    files: int | None  # null until a scan has seen this set
    bytes: int | None


class SetsTree(BaseModel):
    phone_connected: bool
    scanning: bool
    scan_job_id: int | None
    sets: list[SetNode]


def _display_paths(item: BackupSet) -> list[str]:
    shallow = [f"files directly in {p}" for p in item.shallow_paths]
    return [*item.paths, *shallow]


def _tree(ctx: Ctx) -> SetsTree:
    device_id = None
    session = ctx.tracker.session() if ctx.tracker is not None else None
    counts: dict[str, tuple[int, int]] = {}
    scanned: set[str] = set()
    if session is not None:
        with ctx.db.read() as conn:
            row = conn.execute(
                "SELECT id FROM device WHERE uuid = ?", (session.device_uuid,)
            ).fetchone()
            device_id = int(row[0]) if row is not None else None
            if device_id is not None:
                counts = {
                    str(r[0]).casefold(): (int(r[1]), int(r[2] or 0))
                    for r in conn.execute(
                        "SELECT set_name, COUNT(*), SUM(size) FROM phone_file "
                        "WHERE device_id = ? AND present = 1 "
                        "AND state NOT IN ('skipped', 'deleted') AND set_name IS NOT NULL "
                        "GROUP BY set_name",
                        (device_id,),
                    )
                }
                for (names,) in conn.execute(
                    "SELECT sets FROM scan WHERE device_id = ? AND state = 'applied'",
                    (device_id,),
                ):
                    scanned.update(_names(names))
    scanning = [
        s
        for s in (ctx.jobs.snapshot() if ctx.jobs is not None else [])
        if s.state not in TERMINAL and s.type == "backup" and s.dry_run
    ]
    nodes = []
    for item in ctx.config.backup.sets:
        seen = item.name.casefold() in scanned
        files, size = counts.get(item.name.casefold(), (0, 0))
        nodes.append(
            SetNode(
                name=item.name,
                enabled=item.enabled,
                paths=[shown(p) if p.startswith("/") else p for p in _display_paths(item)],
                files=files if seen else None,
                bytes=size if seen else None,
            )
        )
    return SetsTree(
        phone_connected=device_id is not None,
        scanning=bool(scanning),
        scan_job_id=scanning[-1].id if scanning else None,
        sets=nodes,
    )


def _names(raw: object) -> list[str]:
    try:
        data = json.loads(str(raw)) if raw else []
    except ValueError:
        return []
    return [str(n).casefold() for n in data] if isinstance(data, list) else []


@router.get("/sets")
def get_sets_tree(ctx: Ctx) -> SetsTree:
    """Each backup set with its folders and, once scanned, its files and size."""
    return _tree(ctx)


@router.post("/scan")
def start_scan(ctx: Ctx) -> SetsTree:
    """Look through the phone for the size of every set. A dry run: nothing is copied and
    nothing is written to the backup folder. 409 with no phone or while a backup runs."""
    if ctx.jobs is None:
        raise ApiError(409, "no_jobs", "Backups are not available right now.")
    names = [s.name for s in ctx.config.backup.sets]
    try:
        with _run_lock:
            ctx.jobs.start_backup(names, dry_run=True)
    except JobError as e:
        raise _job_error(e) from e
    return _tree(ctx)


class TickRequest(BaseModel):
    enabled: Annotated[list[StrictStr], Field(min_length=1)]


@router.post("/sets")
def save_ticks(ctx: Ctx, body: TickRequest) -> SetsTree:
    """Tick exactly these sets for backing up. Only their on/off switch changes."""
    known = {s.name.casefold() for s in ctx.config.backup.sets}
    unknown = [n for n in body.enabled if n.strip().casefold() not in known]
    if unknown:
        raise _unknown_set(
            ValueError("no backup set named " + ", ".join(f'"{n}"' for n in unknown))
        )
    wanted = {n.strip().casefold() for n in body.enabled}
    with _sets_lock:
        previous = [s.enabled for s in ctx.config.backup.sets]
        for item in ctx.config.backup.sets:
            item.enabled = item.name.casefold() in wanted
        try:
            save_config(ctx.config, ctx.paths.config)
        except OSError as e:
            for item, was in zip(ctx.config.backup.sets, previous, strict=True):
                item.enabled = was
            _log.exception("Could not save the backup sets")
            raise ApiError(
                500, "save_failed", "Sift could not save the settings. Nothing was changed."
            ) from e
    return _tree(ctx)


# -- the reader test -----------------------------------------------------------------------------


class ReaderTestOut(BaseModel):
    tested: bool
    readers: int | None
    speed_bps: int | None
    message: str


@router.post("/reader-test")
def reader_test(ctx: Ctx) -> ReaderTestOut:
    """Read some of the phone's biggest files (and throw the bytes away) to learn how fast this
    phone and cable are. About ten seconds. Needs a connected phone and a finished scan."""
    session = ctx.tracker.session() if ctx.tracker is not None else None
    if session is None:
        raise ApiError(409, "no_phone", "Connect your phone first.")
    if _running_job(ctx):
        raise ApiError(409, "job_conflict", "A backup is running. Try the speed test afterwards.")
    if not _reader_test_lock.acquire(blocking=False):
        raise ApiError(409, "test_running", "The speed test is already running.")
    try:
        result: Any = run_reader_test(ctx, session)
    except TransportError as e:
        _log.warning("The reader test failed: %s", e)
        raise ApiError(502, "reader_test_failed", describe(e)) from e
    finally:
        _reader_test_lock.release()
    if result is None:
        return ReaderTestOut(
            tested=False,
            readers=None,
            speed_bps=None,
            message="Sift has not seen your files yet, so there is nothing to time. "
            "Look through the phone first.",
        )
    return ReaderTestOut(
        tested=True,
        readers=result.readers,
        speed_bps=int(result.speed_bps),
        message="Done. Sift will use this to estimate how long backups take.",
    )
