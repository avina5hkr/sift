"""Validated settings model, loaded from and saved to `config.toml`.

Every key and default comes from spec section 13. Loading never raises on bad content: a bad
value falls back to its default, an unknown key is ignored, and either one yields a
human-readable warning string for the caller to show or log.
"""

import os
import tempfile
import time
import tomllib
from pathlib import Path
from typing import Annotated, Any, Literal, cast

import tomlkit
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from tomlkit.exceptions import TOMLKitError

SDCARD_PREFIX = "/sdcard/"

Port = Annotated[int, Field(ge=1024, le=65535)]
Parallel = Annotated[int, Field(ge=1, le=6)]
Pct = Annotated[int, Field(ge=0, le=100)]
Days = Annotated[int, Field(ge=0)]
Count = Annotated[int, Field(ge=0)]
Positive = Annotated[int, Field(ge=1)]
Unit = Annotated[float, Field(ge=0.0, le=1.0)]
Layout = Literal["mirror", "by_date"]
AdoptMode = Literal["hash", "fast"]
Disposition = Literal["archive", "quarantine"]


class _Section(BaseModel):
    # strict: a TOML string "yes" or integer 1 must not silently become a bool, and so on.
    model_config = ConfigDict(strict=True, validate_assignment=True)


STORAGE_PREFIX = "/storage/emulated/0/"


def normalize_set_path(value: str) -> str:
    """Return the stored form of a set path, or raise ValueError saying why it is not allowed.

    Rules: under /sdcard/ (a /storage/emulated/0/ prefix is rewritten to /sdcard/), at least one
    segment after it, no trailing slash, and no empty, "." or ".." segment.
    """
    if value.startswith(STORAGE_PREFIX):
        value = SDCARD_PREFIX + value[len(STORAGE_PREFIX) :]
    if value.rstrip("/") == "/sdcard":
        raise ValueError("no folder after /sdcard/")
    if not value.startswith(SDCARD_PREFIX):
        raise ValueError("outside /sdcard")
    if value.endswith("/"):
        raise ValueError("trailing slash")
    for seg in value[len(SDCARD_PREFIX) :].split("/"):
        if seg == "":
            raise ValueError("empty segment")
        if seg in ("..", "."):
            raise ValueError(f'"{seg}" segment')
    return value


def _is_inside(path: str, root: str) -> bool:
    """True when `path` is below `root` by whole segments (so /sdcard/DCIMX is not inside DCIM)."""
    return path.startswith(root + "/")


class BackupSet(_Section):
    name: Annotated[str, Field(min_length=1)]
    paths: Annotated[list[str], Field(min_length=1)]
    exclude: list[str] = Field(default_factory=list)
    enabled: bool
    deletable: bool
    adopt_mode: AdoptMode = "hash"  # "fast" is reserved: load warns, M3 treats it as hash

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name must not be blank")
        return v

    @field_validator("paths", "exclude")
    @classmethod
    def _normalized(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for item in v:
            try:
                out.append(normalize_set_path(item))
            except ValueError as err:
                raise ValueError(f"{item!r} is not allowed ({err})") from err
        return out

    @model_validator(mode="after")
    def _exclude_inside_paths(self) -> BackupSet:
        for ex in self.exclude:
            if not any(_is_inside(ex, root) for root in self.paths):
                raise ValueError(f"exclude {ex!r} is not inside any of the set's paths")
        return self


# Spec section 7, "Backup sets" table, in table order. "Custom" has no paths so it is not here.
DEFAULT_SETS: list[BackupSet] = [
    BackupSet(
        name="Camera",
        paths=["/sdcard/DCIM"],
        exclude=["/sdcard/DCIM/Screenshots"],
        enabled=True,
        deletable=True,
    ),
    BackupSet(
        name="Screenshots",
        paths=["/sdcard/DCIM/Screenshots", "/sdcard/Pictures/Screenshots"],
        enabled=True,
        deletable=True,
    ),
    BackupSet(
        name="Pictures and Movies",
        paths=["/sdcard/Pictures", "/sdcard/Movies"],
        exclude=["/sdcard/Pictures/Screenshots"],  # keeps one file out of two sets
        enabled=True,
        deletable=True,
    ),
    BackupSet(
        name="WhatsApp media",
        paths=["/sdcard/Android/media/com.whatsapp/WhatsApp/Media"],
        enabled=True,
        deletable=True,
    ),
    BackupSet(
        name="WhatsApp Business",
        paths=["/sdcard/Android/media/com.whatsapp.w4b/WhatsApp Business/Media"],
        enabled=False,
        deletable=True,
    ),
    BackupSet(
        name="Documents",
        paths=["/sdcard/Documents"],
        enabled=True,
        deletable=False,
    ),
    BackupSet(
        name="Downloads",
        paths=["/sdcard/Download"],
        enabled=False,
        deletable=True,
    ),
]


def _default_sets() -> list[BackupSet]:
    return [s.model_copy(deep=True) for s in DEFAULT_SETS]


def _fallback_sets() -> list[BackupSet]:
    """Default sets for a broken config: same sets, but nothing may be deleted."""
    return [s.model_copy(deep=True, update={"deletable": False}) for s in DEFAULT_SETS]


class PhoneConfig(_Section):
    adb_path: str = ""  # empty = managed platform-tools
    adb_server_port: Port = 5037
    keep_awake: bool = False  # stay_on_while_plugged_in during jobs
    allow_delete_over_wifi: bool = False
    parallel_reads_usb: Parallel = 3
    parallel_reads_wifi: Parallel = 2
    heartbeat_seconds: Positive = 10


def _both_destinations() -> list[Literal["mac", "ssd"]]:
    return ["mac", "ssd"]


class BackupConfig(_Section):
    mac_root: str = "~/PhoneBackup"
    ssd_volume_uuid: str = ""
    device_label: str = "Phone"
    layout: Layout = "mirror"
    settle_seconds: Count = 120
    max_retries: Count = 3
    include_whatsapp_db: bool = False
    include_whatsapp_statuses: bool = False
    required_destinations: Annotated[list[Literal["mac", "ssd"]], Field(min_length=1)] = Field(
        default_factory=_both_destinations
    )
    free_space_headroom_pct: Pct = 2
    verify_reminder_days: Days = 30
    adopt_mode: AdoptMode = "hash"  # "fast" is reserved and falls back to hash
    confirmation_valid_days: Days = 30
    sets: list[BackupSet] = Field(default_factory=_default_sets)

    @field_validator("sets")
    @classmethod
    def _unique_names(cls, v: list[BackupSet]) -> list[BackupSet]:
        names = [s.name for s in v]
        if len(set(names)) != len(names):
            raise ValueError("set names must be unique")
        return v


class AnalysisConfig(_Section):
    thumb_px: Positive = 256
    neardup_dhash_max: Count = 8
    neardup_phash_max: Count = 10
    neardup_cosine_min: Unit = 0.93
    neardup_aspect_tol: Unit = 0.06
    video_frames: Positive = 5
    video_frame_match_min: Positive = 4
    video_frame_hash_max: Count = 10
    cluster_cosine: Unit = 0.88
    cluster_split_cosine: Unit = 0.92
    cluster_max_size: Positive = 2000
    ocr_provider: Literal["auto", "vision", "tesseract"] = "auto"
    ocr_languages: list[str] = Field(default_factory=lambda: ["en-US", "hi-IN"])
    embedding_model: str = "clip-vit-b32-onnx"
    embedding_batch: Positive = 32
    workers: Count = 0  # 0 = CPU cores minus 2


class ScoringConfig(_Section):
    t_suggest: Unit = 0.97  # replaced by the calibrated value
    t_suggest_video: Unit = 0.985
    t_maybe: Unit = 0.60
    min_support: Count = 3
    knn_k: Positive = 15
    knn_min_cosine: Unit = 0.80
    protect_cosine: Unit = 0.90
    keep_cosine: Unit = 0.92
    protect_screenshots: bool = True
    min_labels_each: Count = 30
    calibration_sample: Positive = 300


class CleanupConfig(_Section):
    min_age_days: Days = 7
    quarantine_days: Annotated[int, Field(ge=1)] = 30
    never_precheck: bool = False
    max_delete_per_run: Positive = 5000
    preflight_hash_batch: Positive = 40
    delete_roots: list[str] = Field(
        default_factory=lambda: [
            "/sdcard/DCIM",
            "/sdcard/Pictures",
            "/sdcard/Movies",
            "/sdcard/Android/media/com.whatsapp/WhatsApp/Media/WhatsApp Images",
            "/sdcard/Android/media/com.whatsapp/WhatsApp/Media/WhatsApp Video",
            "/sdcard/Android/media/com.whatsapp/WhatsApp/Media/WhatsApp Animated Gifs",
            "/sdcard/Android/media/com.whatsapp/WhatsApp/Media/WhatsApp Stickers",
        ]
    )
    never_delete: list[str] = Field(
        default_factory=lambda: [
            "/sdcard/Android/data",
            "/sdcard/Documents",
            "/sdcard/Download",
            # Spec 13, last paragraph after the 10A-10D block.
            "/sdcard/Android/obb",
            "/sdcard/Android/media/com.whatsapp/WhatsApp/Databases",
            "/sdcard/Android/media/com.whatsapp/WhatsApp/Backups",
            "/sdcard/Android/media/com.whatsapp.w4b/WhatsApp Business/Databases",
            "/sdcard/Android/media/com.whatsapp.w4b/WhatsApp Business/Backups",
        ]
    )
    delete_reminder_days: Days = 0  # 0 = no reminder


class SlateConfig(_Section):
    keep_folders: list[str] = Field(default_factory=lambda: ["/sdcard/Documents"])
    keep_newer_than_days: Days = 30
    disposition_default: Disposition = "archive"
    typed_confirmation: bool = True
    chunk_size: Positive = 500
    manifest_dir: str = ".sift/manifests"
    remove_empty_folders: bool = True


class BrowseConfig(_Section):
    disposition_default: Disposition = "quarantine"
    large_video_mb: Count = 100
    old_days: Days = 90
    typed_confirmation_over_files: Count = 200
    never_delete_extra: list[str] = Field(default_factory=list)  # added to cleanup.never_delete


class AppsConfig(_Section):
    protected_packages: list[str] = Field(
        default_factory=lambda: [
            "com.whatsapp",
            "com.whatsapp.w4b",
            "com.azure.authenticator",
            "com.google.android.apps.authenticator2",
            "com.authy.authy",
        ]
    )
    unused_days: Days = 90
    whatsapp_backup_max_age_hours: Count = 24
    save_app_list_before_uninstall: bool = True


class HealthConfig(_Section):
    storage_warn_pct: Pct = 90
    battery_temp_warn_c: Count = 40
    report_dir: str = ".sift/reports"


class Config(_Section):
    phone: PhoneConfig = Field(default_factory=PhoneConfig)
    backup: BackupConfig = Field(default_factory=BackupConfig)
    analysis: AnalysisConfig = Field(default_factory=AnalysisConfig)
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    cleanup: CleanupConfig = Field(default_factory=CleanupConfig)
    slate: SlateConfig = Field(default_factory=SlateConfig)
    browse: BrowseConfig = Field(default_factory=BrowseConfig)
    apps: AppsConfig = Field(default_factory=AppsConfig)
    health: HealthConfig = Field(default_factory=HealthConfig)


_FAST_NOTE = "adopt_mode fast is not built yet; hash is used"


def _describe(err: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" if e["loc"] else e["msg"]
        for e in err.errors()
    )


def _valid_items(
    model: type[BaseModel],
    section: str,
    table: dict[str, Any],
    warnings: list[str],
    skip: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Keep each key that validates on its own; warn about the rest."""
    kept: dict[str, Any] = {}
    for key, value in table.items():
        if key in skip:
            continue
        if key not in model.model_fields:
            warnings.append(f"{section}.{key}: unknown setting, ignored")
            continue
        try:
            model.model_validate({key: value})
        except ValidationError:
            warnings.append(f"{section}.{key}: invalid value, using the default")
            continue
        kept[key] = value
    return kept


_FALLBACK_WARNING = (
    "no valid backup sets; using the default sets with deleting turned off "
    "until the settings are fixed"
)


def _clean_paths(
    raw: object, where: str, warnings: list[str], inside: list[str] | None = None
) -> object:
    """Drop each invalid entry of a paths/exclude list with its own warning.

    `inside` (for exclude) is the set's cleaned paths; an entry outside all of them is dropped.
    Anything that is not a list of strings is returned untouched for the model to reject.
    """
    if not isinstance(raw, list):
        return raw
    kept: list[Any] = []
    for item in raw:
        if not isinstance(item, str):
            kept.append(item)
            continue
        try:
            norm = normalize_set_path(item)
        except ValueError as err:
            warnings.append(f'{where}: "{item}" is not allowed ({err}); dropped')
            continue
        if inside is not None and not any(_is_inside(norm, root) for root in inside):
            warnings.append(f'{where}: "{item}" is not inside any of the set\'s paths; dropped')
            continue
        kept.append(norm)
    return kept


def _build_set(
    entry: dict[str, Any], label: str, paths_where: str, warnings: list[str]
) -> BackupSet | None:
    known: dict[str, Any] = {}
    for key, value in entry.items():
        if key in BackupSet.model_fields:
            known[key] = value
        else:
            warnings.append(f"{label}.{key}: unknown setting, ignored")
    if "paths" in known:
        known["paths"] = _clean_paths(known["paths"], paths_where, warnings)
    if "exclude" in known:
        roots = known.get("paths")
        inside = [p for p in roots if isinstance(p, str)] if isinstance(roots, list) else []
        known["exclude"] = _clean_paths(known["exclude"], f"{label}.exclude", warnings, inside)
    try:
        item = BackupSet.model_validate(known)
    except ValidationError as err:
        warnings.append(f"{label}: {_describe(err)}; dropped")
        return None
    return item


def _load_sets(raw: object, warnings: list[str]) -> list[BackupSet]:
    if not isinstance(raw, list):
        warnings.append("backup.sets: invalid value, not a list of tables")
        warnings.append(_FALLBACK_WARNING)
        return _fallback_sets()
    sets: list[BackupSet] = []
    seen: set[str] = set()
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            warnings.append(f"backup.sets[{i}]: not a table, dropped")
            continue
        shown = entry.get("name")
        label = f'backup.sets[{i}] ("{shown if isinstance(shown, str) else "?"}")'
        item = _build_set(entry, label, f"{label}.paths", warnings)
        if item is None:
            continue
        if item.name in seen:
            warnings.append(f"{label}: duplicate name, dropped")
            continue
        seen.add(item.name)
        if item.adopt_mode == "fast":
            warnings.append(f"{label}.{_FAST_NOTE}")
        sets.append(item)
    if not sets:
        warnings.append(_FALLBACK_WARNING)
        return _fallback_sets()
    return sets


def _load_backup(table: dict[str, Any], warnings: list[str]) -> BackupConfig:
    kept = _valid_items(BackupConfig, "backup", table, warnings, skip=frozenset({"sets", "roots"}))
    if "sets" in table:
        kept["sets"] = _load_sets(table["sets"], warnings)
        if "roots" in table:
            warnings.append("backup.roots is ignored because backup.sets is present")
    elif "roots" in table:
        legacy = {"name": "Custom", "paths": table["roots"], "enabled": True, "deletable": False}
        custom = _build_set(legacy, "backup.roots", "backup.roots", warnings)
        if custom is None:
            warnings.append("backup.roots: invalid value, ignored")
        else:
            kept["sets"] = [custom]
            warnings.append("backup.roots is replaced by backup.sets; loaded as one Custom set")
    cfg = BackupConfig.model_validate(kept)
    if cfg.adopt_mode == "fast":
        warnings.append(f"backup.{_FAST_NOTE}")
    return cfg


def _set_aside(path: Path) -> str:
    target = path.with_name(f"{path.name}.invalid-{int(time.time())}")
    os.replace(path, target)
    return target.name


def load_config(path: Path) -> tuple[Config, list[str]]:
    """Read `path`; a missing file gives the defaults. Never raises on bad content."""
    warnings: list[str] = []
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Config(), warnings
    except tomllib.TOMLDecodeError, UnicodeDecodeError:
        try:
            where = f" and was set aside as {_set_aside(path)}"
        except OSError:
            where = ""
        return Config(), [f"{path.name} could not be read{where}; defaults are in use."]

    sections: dict[str, BaseModel] = {}
    for key, value in data.items():
        field = Config.model_fields.get(key)
        model = field.annotation if field else None
        if not (isinstance(model, type) and issubclass(model, _Section)):
            warnings.append(f"{key}: unknown setting, ignored")
        elif not isinstance(value, dict):
            warnings.append(f"{key}: invalid value, using the defaults")
        elif model is BackupConfig:
            sections[key] = _load_backup(value, warnings)
        else:
            sections[key] = model.model_validate(_valid_items(model, key, value, warnings))
    return Config.model_validate(sections), warnings


def _apply(table: Any, values: dict[str, Any]) -> None:
    """Set each key in a tomlkit table, touching only values that changed (keeps comments)."""
    for key, value in values.items():
        if key in table and table[key] == value:
            continue
        table[key] = value


def save_config(cfg: Config, path: Path) -> None:
    """Write `cfg` to `path` atomically, keeping the comments and layout of an existing file."""
    try:
        doc = tomlkit.parse(path.read_text(encoding="utf-8"))
    except FileNotFoundError, TOMLKitError, UnicodeDecodeError:
        doc = tomlkit.document()

    for name, values in cfg.model_dump().items():
        if name not in doc:
            doc[name] = tomlkit.table()
        table = cast("Any", doc[name])
        if name == "backup":
            sets = values.pop("sets")
            table.pop("roots", None)  # replaced by sets; never written back
        _apply(table, values)
        if name == "backup" and ("sets" not in table or table["sets"].unwrap() != sets):
            table.pop("sets", None)
            aot = tomlkit.aot()
            for entry in sets:
                item = tomlkit.table()
                item.update(entry)
                aot.append(item)
            table["sets"] = aot

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(tomlkit.dumps(doc))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
