"""Settings pages (spec 11): a whitelisted set of config fields per page, validated by the
config model and saved to config.toml. Backup folders, the SSD and backup sets keep their own
screens (Setup), so they are not editable here."""

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, ValidationError

from sift.core.config import NEVER_DELETE_FLOOR, Config, save_config
from sift.server.deps import Ctx
from sift.server.errors import ApiError

router = APIRouter()

PAGES: dict[str, list[tuple[str, str]]] = {
    "general": [("cleanup", "precheck_suggestions"), ("phone", "keep_awake")],
    "phone": [
        ("phone", "keep_awake"),
        ("phone", "allow_delete_over_wifi"),
        ("phone", "parallel_reads_usb"),
        ("phone", "parallel_reads_wifi"),
        ("phone", "heartbeat_seconds"),
        ("phone", "adb_path"),
        ("phone", "adb_server_port"),
    ],
    "destinations": [
        ("backup", "required_destinations"),
        ("backup", "layout"),
        ("backup", "free_space_headroom_pct"),
    ],
    "backup": [
        ("backup", "include_whatsapp_db"),
        ("backup", "include_whatsapp_statuses"),
        ("backup", "skip_hidden_folders"),
        ("backup", "skip_name_prefixes"),
        ("backup", "skip_name_suffixes"),
        ("backup", "skip_zero_byte"),
        ("backup", "settle_seconds"),
        ("backup", "max_retries"),
        ("backup", "confirmation_valid_days"),
        ("backup", "verify_reminder_days"),
    ],
    "cleanup": [
        ("cleanup", "delete_roots"),
        ("cleanup", "never_delete"),
        ("cleanup", "min_age_days"),
        ("cleanup", "quarantine_days"),
        ("scoring", "protect_screenshots"),
        ("scoring", "protected_folders"),
        ("browse", "never_delete_extra"),
        ("browse", "large_video_mb"),
        ("browse", "old_days"),
        ("slate", "keep_folders"),
        ("slate", "keep_newer_than_days"),
        ("apps", "unused_days"),
        ("apps", "protected_packages"),
    ],
    "models": [
        ("analysis", "ocr_languages"),
        ("analysis", "workers"),
    ],
    "advanced": [
        ("scoring", "t_suggest"),
        ("scoring", "t_suggest_video"),
        ("scoring", "t_maybe"),
        ("scoring", "min_labels_each"),
        ("analysis", "cluster_cosine"),
        ("cleanup", "max_delete_per_run"),
        ("health", "storage_warn_pct"),
        ("health", "battery_temp_warn_c"),
    ],
}
EDITABLE = {f for fields in PAGES.values() for f in fields}


def _field(cfg: Config, section: str, key: str) -> dict[str, Any]:
    model = type(getattr(cfg, section))
    schema = model.model_json_schema()["properties"].get(key, {})
    value = getattr(getattr(cfg, section), key)
    return {"section": section, "key": key, "value": value, "schema": schema,
            "default": getattr(model(), key)}  # fmt: skip


@router.get("/settings")
def get_settings(ctx: Ctx) -> dict[str, Any]:
    return {
        "pages": {
            page: [_field(ctx.config, s, k) for s, k in fields] for page, fields in PAGES.items()
        },
        "warnings": list(ctx.config_warnings),
    }


class Change(BaseModel):
    section: str
    key: str
    value: Any


class SettingsPut(BaseModel):
    changes: list[Change]


@router.put("/settings")
def put_settings(ctx: Ctx, body: SettingsPut) -> dict[str, Any]:
    """Apply the changes together, or none: the whole config is validated before anything is
    changed or saved."""
    for ch in body.changes:
        if (ch.section, ch.key) not in EDITABLE:
            raise ApiError(422, "not_editable", f"{ch.section}.{ch.key} is not changed here.")
    from sift.server.routes.setup import _config_lock

    with _config_lock:  # one writer of config.toml at a time (setup, speed test, sets)
        return _apply(ctx, body)


def _apply(ctx: Ctx, body: SettingsPut) -> dict[str, Any]:
    data = ctx.config.model_dump()
    for ch in body.changes:
        value = ch.value
        if (ch.section, ch.key) == ("cleanup", "never_delete") and isinstance(value, list):
            value = [*NEVER_DELETE_FLOOR, *[v for v in value if v not in NEVER_DELETE_FLOOR]]
        data[ch.section][ch.key] = value
    try:
        fresh = Config.model_validate(data)
    except ValidationError as e:
        first = e.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ()))
        raise ApiError(422, "invalid", f"{where}: {first.get('msg', 'not valid')}") from e
    updated = ctx.config.model_copy(deep=True)
    for ch in body.changes:
        new = getattr(getattr(fresh, ch.section), ch.key)
        setattr(getattr(updated, ch.section), ch.key, new)
    try:
        save_config(updated, ctx.paths.config)  # saved first; the running app changes after
    except OSError as e:
        raise ApiError(500, "not_saved", f"Settings could not be saved: {e.strerror or e}") from e
    for ch in body.changes:
        setattr(
            getattr(ctx.config, ch.section), ch.key, getattr(getattr(updated, ch.section), ch.key)
        )
    return get_settings(ctx)
