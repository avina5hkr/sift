"""Validated settings model, loaded from and saved to `config.toml`.

Every key and default comes from spec section 13. Loading never raises on bad content or on a
file it cannot open: a bad value falls back to its default, an unknown key is ignored, and either
one yields a human-readable warning string for the caller to show or log. A config that cannot be
used at all (unparseable, unreadable, or a `[backup]` that is not a table) gets the fallback
backup sets, none of them deletable.
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
from tomlkit.items import Table

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


STORAGE_ROOT = "/storage/emulated/0"
STORAGE_PREFIX = STORAGE_ROOT + "/"


def normalize_set_path(value: str, *, allow_root: bool = False) -> str:
    """Return the stored form of a set path, or raise ValueError saying why it is not allowed.

    Rules: under /sdcard/ (a /storage/emulated/0 prefix is rewritten to /sdcard), at least one
    segment after it, no trailing slash, and no empty, "." or ".." segment. `allow_root` (only
    for shallow paths) also accepts the bare root `/sdcard` itself.
    """
    if value == STORAGE_ROOT:
        value = "/sdcard"
    elif value.startswith(STORAGE_PREFIX):
        value = SDCARD_PREFIX + value[len(STORAGE_PREFIX) :]
    if value.rstrip("/") == "/sdcard":
        if allow_root and value == "/sdcard":
            return value
        raise ValueError("trailing slash" if allow_root else "no folder after /sdcard/")
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


def phone_path_key(path: str) -> str:
    """The form phone paths are compared in: storage spelling as /sdcard, no trailing slash, folded.

    Shared storage on the phone is case-insensitive, so every comparison of phone paths (sets,
    excludes, skip names, origin) goes through this.
    """
    if path == STORAGE_ROOT or path.startswith(STORAGE_PREFIX):
        path = SDCARD_PREFIX[:-1] + path[len(STORAGE_ROOT) :]
    return path.rstrip("/").casefold() if path != "/" else path


def _is_inside(path: str, root: str) -> bool:
    """True when `path` is below `root` by whole segments (so /sdcard/DCIMX is not inside DCIM).

    Phone paths are compared ignoring case: shared storage on the phone is case-insensitive.
    """
    return path.casefold().startswith(root.casefold() + "/")


def _normalize_list(values: list[str], *, allow_root: bool = False) -> list[str]:
    """Normalise every entry with the set-path rules; the first bad one raises ValueError."""
    out: list[str] = []
    for item in values:
        try:
            out.append(normalize_set_path(item, allow_root=allow_root))
        except ValueError as err:
            raise ValueError(f"{item!r} is not allowed ({err})") from err
    return out


def _name_key(name: str) -> str:
    """The form of a set name that must be unique: trimmed and case-folded."""
    return name.strip().casefold()


class BackupSet(_Section):
    name: Annotated[str, Field(min_length=1)]
    paths: list[str] = Field(default_factory=list)  # whole folders, subfolders included
    shallow_paths: list[str] = Field(default_factory=list)  # direct child files only
    exclude: list[str] = Field(default_factory=list)
    enabled: bool
    deletable: bool
    adopt_mode: AdoptMode = "hash"  # "fast" is reserved: load warns, M3 treats it as hash

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name must not be blank")
        return v.strip()

    @field_validator("paths", "exclude")
    @classmethod
    def _normalized(cls, v: list[str]) -> list[str]:
        return _normalize_list(v)

    @field_validator("shallow_paths")
    @classmethod
    def _normalized_shallow(cls, v: list[str]) -> list[str]:
        return _normalize_list(v, allow_root=True)

    @model_validator(mode="after")
    def _has_something_to_back_up(self) -> BackupSet:
        if not self.paths and not self.shallow_paths:
            raise ValueError("needs at least one entry in paths or shallow_paths")
        return self

    @model_validator(mode="after")
    def _exclude_inside_paths(self) -> BackupSet:
        for ex in self.exclude:
            if not any(_is_inside(ex, root) for root in self.paths):
                raise ValueError(f"exclude {ex!r} is not inside any of the set's paths")
        return self


class DefaultSet(BaseModel):
    """A built-in backup set, frozen: tuples and no assignment. `to_backup_set` gives a copy."""

    model_config = ConfigDict(frozen=True)

    name: str
    paths: tuple[str, ...]
    shallow_paths: tuple[str, ...]
    exclude: tuple[str, ...]
    enabled: bool
    deletable: bool
    adopt_mode: AdoptMode

    def to_backup_set(self, **update: Any) -> BackupSet:
        fields = {k: list(v) if isinstance(v, tuple) else v for k, v in self.model_dump().items()}
        return BackupSet.model_validate({**fields, **update})


def _freeze(item: BackupSet) -> DefaultSet:
    return DefaultSet.model_validate(item.model_dump())


# Spec section 7, "Backup sets" table, in table order. "Custom" has no paths so it is not here.
# Each entry is validated as a BackupSet, then frozen.
_DEFAULT_SET_LIST: list[BackupSet] = [
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
        paths=[
            "/sdcard/Android/media/com.whatsapp/WhatsApp/Media",
            "/sdcard/WhatsApp/Media",  # the pre-scoped-storage location
        ],
        enabled=True,
        deletable=True,
    ),
    BackupSet(
        name="WhatsApp Business",
        paths=["/sdcard/Android/media/com.whatsapp.w4b/WhatsApp Business/Media"],
        enabled=True,
        deletable=True,
    ),
    BackupSet(
        name="WhatsApp chat backups",
        paths=[
            "/sdcard/Android/media/com.whatsapp/WhatsApp/Databases",
            "/sdcard/Android/media/com.whatsapp/WhatsApp/Backups",
            "/sdcard/Android/media/com.whatsapp.w4b/WhatsApp Business/Databases",
            "/sdcard/Android/media/com.whatsapp.w4b/WhatsApp Business/Backups",
            "/sdcard/WhatsApp/Databases",
            "/sdcard/WhatsApp/Backups",
            "/sdcard/WhatsApp Business/Databases",
            "/sdcard/WhatsApp Business/Backups",
        ],
        enabled=True,
        deletable=False,  # never freed from the phone (NEVER_DELETE_FLOOR); kept in the backup
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
    BackupSet(
        name="Recordings",
        paths=["/sdcard/Recordings"],
        enabled=True,
        deletable=True,
    ),
    BackupSet(
        name="Loose files",
        paths=[],
        shallow_paths=["/sdcard"],  # the root's own files only, no subfolders
        enabled=True,
        deletable=False,
    ),
]


DEFAULT_SETS: tuple[DefaultSet, ...] = tuple(_freeze(s) for s in _DEFAULT_SET_LIST)


def _default_sets() -> list[BackupSet]:
    return [s.to_backup_set() for s in DEFAULT_SETS]


def _fallback_sets() -> list[BackupSet]:
    """Default sets for a broken config: same sets, but nothing may be deleted."""
    return [s.to_backup_set(deletable=False) for s in DEFAULT_SETS]


class PhoneConfig(_Section):
    adb_path: str = ""  # empty = managed platform-tools
    adb_server_port: Port = 5037
    keep_awake: bool = False  # stay_on_while_plugged_in during jobs
    allow_delete_over_wifi: bool = False
    parallel_reads_usb: Parallel = 3
    parallel_reads_wifi: Parallel = 2
    heartbeat_seconds: Positive = 10
    # What the first-run reader test measured, in bytes per second (0: not measured yet). The
    # backup's time estimate uses it until the running job has measured its own speed.
    measured_read_bps: Count = 0


def _both_destinations() -> list[Literal["mac", "ssd"]]:
    return ["mac", "ssd"]


def _is_absolute_mac_path(value: str) -> bool:
    """True when `value` is an absolute Mac path once a leading `~` is expanded."""
    return os.path.isabs(os.path.expanduser(value))


DEFAULT_SKIP_HIDDEN_FOLDERS: tuple[str, ...] = (
    ".thumbnails",
    ".trash",
    ".shared",
    ".cache",
    ".Statuses",
    ".Links",
    ".wamocache",
    ".Wamo",
)
DEFAULT_SKIP_NAME_PREFIXES: tuple[str, ...] = (".pending-", ".trashed-", ".tmp", ".nomedia")
DEFAULT_SKIP_NAME_SUFFIXES: tuple[str, ...] = (".tmp", ".crdownload", ".part")


def _name_problem(name: str) -> str | None:
    """Why `name` cannot be a skip name (a folder name, or the start or end of a file name)."""
    if not name.strip():
        return "is blank"
    if "/" in name:
        return "is a path; give a name without a slash"
    if "\0" in name:
        return "has an invalid character"
    return None


def _check_names(values: list[str]) -> list[str]:
    for item in values:
        problem = _name_problem(item)
        if problem:
            raise ValueError(f"{item!r} {problem}")
    return values


class BackupConfig(_Section):
    mac_root: str = "~/PhoneBackup"
    ssd_volume_uuid: str = ""
    ssd_folder: str = (
        ""  # the backup folder on the SSD, relative to the drive; "" = same name as mac_root
    )
    device_label: str = "Phone"
    layout: Layout = "mirror"
    settle_seconds: Count = 120
    max_retries: Count = 3
    include_whatsapp_db: bool = False
    include_whatsapp_statuses: bool = False
    skip_hidden_folders: list[str] = Field(
        default_factory=lambda: list(DEFAULT_SKIP_HIDDEN_FOLDERS)
    )
    skip_name_prefixes: list[str] = Field(default_factory=lambda: list(DEFAULT_SKIP_NAME_PREFIXES))
    skip_name_suffixes: list[str] = Field(default_factory=lambda: list(DEFAULT_SKIP_NAME_SUFFIXES))
    skip_zero_byte: bool = True
    required_destinations: Annotated[list[Literal["mac", "ssd"]], Field(min_length=1)] = Field(
        default_factory=_both_destinations
    )
    free_space_headroom_pct: Pct = 2
    verify_reminder_days: Days = 30
    adopt_mode: AdoptMode = "hash"  # "fast" is reserved and falls back to hash
    confirmation_valid_days: Days = 30
    sets: list[BackupSet] = Field(default_factory=_default_sets)

    @field_validator("mac_root")
    @classmethod
    def _mac_root_absolute(cls, v: str) -> str:
        if not _is_absolute_mac_path(v):
            raise ValueError("must be a full folder path (starting with / or ~)")
        return v

    @field_validator("skip_hidden_folders", "skip_name_prefixes", "skip_name_suffixes")
    @classmethod
    def _skip_names(cls, v: list[str]) -> list[str]:
        return _check_names(v)

    @field_validator("sets")
    @classmethod
    def _unique_names(cls, v: list[BackupSet]) -> list[BackupSet]:
        names = [_name_key(s.name) for s in v]
        if len(set(names)) != len(names):
            raise ValueError("set names must be unique")
        return v


class AnalysisConfig(_Section):
    # Hard limits, not preferences: looser hash distances were not validated against false
    # matches before image embeddings refine the groups, so a bigger value is refused (review P3.9).
    neardup_dhash_max: Annotated[int, Field(ge=0, le=6)] = 6
    neardup_phash_max: Annotated[int, Field(ge=0, le=8)] = 8
    neardup_aspect_tol: Unit = 0.06
    video_frame_match_min: Positive = 4
    video_frame_hash_max: Count = 10
    cluster_cosine: Unit = 0.88
    cluster_split_cosine: Unit = 0.92
    cluster_max_size: Positive = 2000
    # Vision's language codes, in order (Hindi first: Vision reads Devanagari only then)
    ocr_languages: list[str] = Field(default_factory=lambda: ["hi-IN", "en-US"])
    workers: Count = 0  # text reading threads; 0 = half the cores (2 to 8)


# Settings that older versions wrote but that never changed anything (fixed by the model or the
# code). An old config.toml that still holds them loads without a warning.
RETIRED = {
    "cleanup": frozenset({"never_precheck"}),  # replaced by precheck_suggestions (opt-in)
    "analysis": frozenset(
        {"thumb_px", "neardup_cosine_min", "video_frames", "ocr_provider", "embedding_model",
         "embedding_batch"}
    ),
}  # fmt: skip


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
    protected_folders: list[str] = Field(default_factory=list)  # never suggested as junk


# Folders the app never deletes from, whatever the config says: app data, and the WhatsApp
# databases and backups (current and legacy locations). Spec 13, after the 10A-10D block.
NEVER_DELETE_FLOOR: tuple[str, ...] = (
    "/sdcard/Android/data",
    "/sdcard/Android/obb",
    "/sdcard/Android/media/com.whatsapp/WhatsApp/Databases",
    "/sdcard/Android/media/com.whatsapp/WhatsApp/Backups",
    "/sdcard/Android/media/com.whatsapp.w4b/WhatsApp Business/Databases",
    "/sdcard/Android/media/com.whatsapp.w4b/WhatsApp Business/Backups",
    "/sdcard/WhatsApp/Databases",
    "/sdcard/WhatsApp/Backups",
    "/sdcard/WhatsApp Business/Databases",
    "/sdcard/WhatsApp Business/Backups",
)


class CleanupConfig(_Section):
    min_age_days: Days = 7
    quarantine_days: Annotated[int, Field(ge=1)] = 30
    # Model suggestions are ticked for you only when this is on and a calibration passed on
    # the current pipeline. Off by default while the thresholds are provisional (beta).
    precheck_suggestions: bool = False
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
            # the pre-Android 11 location of the same folders (plan assumption F6)
            "/sdcard/WhatsApp/Media/WhatsApp Images",
            "/sdcard/WhatsApp/Media/WhatsApp Video",
            "/sdcard/WhatsApp/Media/WhatsApp Animated Gifs",
            "/sdcard/WhatsApp/Media/WhatsApp Stickers",
        ]
    )
    never_delete: list[str] = Field(
        default_factory=lambda: [*NEVER_DELETE_FLOOR, "/sdcard/Documents", "/sdcard/Download"]
    )
    delete_reminder_days: Days = 0  # 0 = no reminder

    @field_validator("delete_roots", "never_delete")
    @classmethod
    def _normalized_paths(cls, v: list[str]) -> list[str]:
        return _normalize_list(v)


class SlateConfig(_Section):
    keep_folders: list[str] = Field(default_factory=lambda: ["/sdcard/Documents"])
    keep_newer_than_days: Days = 30
    disposition_default: Disposition = "archive"
    typed_confirmation: bool = True
    chunk_size: Positive = 500
    manifest_dir: str = ".sift/manifests"
    remove_empty_folders: bool = True

    @field_validator("keep_folders")
    @classmethod
    def _normalized_paths(cls, v: list[str]) -> list[str]:
        return _normalize_list(v)


class BrowseConfig(_Section):
    disposition_default: Disposition = "quarantine"
    large_video_mb: Count = 100
    old_days: Days = 90
    typed_confirmation_over_files: Count = 200
    never_delete_extra: list[str] = Field(default_factory=list)  # added to cleanup.never_delete

    @field_validator("never_delete_extra")
    @classmethod
    def _normalized_paths(cls, v: list[str]) -> list[str]:
        return _normalize_list(v)


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

    def effective_never_delete(self) -> tuple[str, ...]:
        """Every folder deleting must avoid: the built-in floor plus both user lists.

        The floor comes first and cannot be removed by editing the config. Entries that differ
        only in case count once.
        """
        out: dict[str, str] = {}
        user = (*self.cleanup.never_delete, *self.browse.never_delete_extra)
        for path in (*NEVER_DELETE_FLOOR, *user):
            out.setdefault(path.casefold(), path)
        return tuple(out.values())


_FAST_NOTE = "adopt_mode fast is not built yet; hash is used"


_PLAIN_TYPES = {
    "string_type": "must be text",
    "list_type": "must be a list",
    "bool_type": "must be true or false",
    "missing": "is required",
}


def _plain(e: Any) -> str:
    """One pydantic error as plain words: no "Value error, " or "Input should be ..." wording."""
    kind: str = e["type"]
    msg: str = e["msg"]
    if kind in _PLAIN_TYPES:
        return _PLAIN_TYPES[kind]
    if kind == "value_error":
        return msg.removeprefix("Value error, ")
    return "must be " + msg.removeprefix("Input should be ") if kind == "literal_error" else msg


def plain_error_message(error: Any) -> str:
    """One entry of `ValidationError.errors()` in plain words (for the settings API)."""
    return _plain(error)


def _describe(err: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in e['loc'])}: {_plain(e)}" if e["loc"] else _plain(e)
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
    raw: object,
    where: str,
    warnings: list[str],
    inside: list[str] | None = None,
    allow_root: bool = False,
) -> object:
    """Drop each invalid entry of a paths/exclude/shallow_paths list with its own warning.

    `inside` (for exclude) is the set's cleaned paths; an entry outside all of them is dropped.
    `allow_root` (for shallow_paths) lets the bare `/sdcard` through.
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
            norm = normalize_set_path(item, allow_root=allow_root)
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
    if "shallow_paths" in known:
        known["shallow_paths"] = _clean_paths(
            known["shallow_paths"], f"{label}.shallow_paths", warnings, allow_root=True
        )
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
        if _name_key(item.name) in seen:
            warnings.append(f"{label}: duplicate name, dropped")
            continue
        seen.add(_name_key(item.name))
        if item.adopt_mode == "fast":
            warnings.append(f"{label}.{_FAST_NOTE}")
        sets.append(item)
    if not sets:
        warnings.append(_FALLBACK_WARNING)
        return _fallback_sets()
    return sets


# Built-in sets added after the first release; `join_added_sets` brings them into an owner's
# saved config once, at app start.
ADDED_LATER = ("WhatsApp chat backups",)


def join_added_sets(cfg: Config, already: set[str]) -> list[str]:
    """Append each ADDED_LATER set missing from `cfg` and not in `already` (the names joined
    before, so a set the owner removed stays removed). Returns the names added."""
    have = {_name_key(s.name) for s in cfg.backup.sets}
    added = []
    for d in DEFAULT_SETS:
        if d.name in ADDED_LATER and d.name not in already and _name_key(d.name) not in have:
            cfg.backup.sets.append(d.to_backup_set())
            added.append(d.name)
    return added


_SKIP_NAME_LISTS = ("skip_hidden_folders", "skip_name_prefixes", "skip_name_suffixes")


def _clean_names(raw: object, where: str, warnings: list[str]) -> object:
    """Drop each unusable entry of a skip-name list with its own warning, keep the rest."""
    if not isinstance(raw, list):
        return raw
    kept: list[Any] = []
    for item in raw:
        problem = _name_problem(item) if isinstance(item, str) else None
        if problem:
            warnings.append(f'{where}: "{item}" {problem}; dropped')
        else:
            kept.append(item)
    return kept


def _load_backup(table: dict[str, Any], warnings: list[str]) -> BackupConfig:
    for list_key in _SKIP_NAME_LISTS:
        if list_key in table:
            table = {
                **table,
                list_key: _clean_names(table[list_key], f"backup.{list_key}", warnings),
            }
    mac_root = table.get("mac_root")
    if isinstance(mac_root, str) and not _is_absolute_mac_path(mac_root):
        warnings.append(
            f'backup.mac_root: "{mac_root}" is not a full folder path (it must start with / or ~); '
            f"using {BackupConfig().mac_root}"
        )
        table = {k: v for k, v in table.items() if k != "mac_root"}
    kept = _valid_items(BackupConfig, "backup", table, warnings, skip=frozenset({"sets", "roots"}))
    if "sets" in table:
        kept["sets"] = _load_sets(table["sets"], warnings)
        if "roots" in table:
            warnings.append("backup.roots is ignored because backup.sets is present")
    elif "roots" in table:
        legacy = {"name": "Custom", "paths": table["roots"], "enabled": True, "deletable": False}
        custom = _build_set(legacy, "backup.roots", "backup.roots", warnings)
        if custom is None:
            warnings.append("backup.roots: no valid paths")
            warnings.append(_FALLBACK_WARNING)
            kept["sets"] = _fallback_sets()
        else:
            kept["sets"] = [custom]
            warnings.append("backup.roots is replaced by backup.sets; loaded as one Custom set")
    cfg = BackupConfig.model_validate(kept)
    if cfg.adopt_mode == "fast":
        warnings.append(f"backup.{_FAST_NOTE}")
    return cfg


def _set_aside(path: Path) -> str:
    stamp = time.time_ns()
    target = path.with_name(f"{path.name}.invalid-{stamp}")
    while target.exists():  # never write over an earlier set-aside copy
        stamp += 1
        target = path.with_name(f"{path.name}.invalid-{stamp}")
    os.replace(path, target)
    return target.name


def _fallback_config() -> Config:
    return Config(backup=BackupConfig(sets=_fallback_sets()))


def load_config(path: Path) -> tuple[Config, list[str]]:
    """Read `path`; a missing file gives the spec defaults. Never raises.

    A file that cannot be used (unparseable, unreadable) gives the fallback sets with deleting
    turned off. An unparseable file is set aside and replaced by a fresh one holding that safe
    state, so it also holds on the next launch.
    """
    warnings: list[str] = []
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Config(), warnings
    except tomllib.TOMLDecodeError, UnicodeDecodeError:
        cfg = _fallback_config()
        try:
            where = f" and was set aside as {_set_aside(path)}"
        except OSError:
            # Still in place: do not write over what the owner may want to repair.
            return cfg, [f"{path.name} could not be read; defaults are in use.", _FALLBACK_WARNING]
        warnings = [
            f"{path.name} could not be read{where}; defaults are in use.",
            _FALLBACK_WARNING,
        ]
        try:
            save_config(cfg, path)
        except OSError as err:
            warnings.append(f"a new {path.name} could not be written ({err.strerror or err}).")
        return cfg, warnings
    except OSError as err:
        reason = err.strerror or type(err).__name__
        return _fallback_config(), [
            f"{path.name} could not be opened ({reason}); "
            "defaults are in use with deleting turned off.",
            _FALLBACK_WARNING,
        ]

    sections: dict[str, BaseModel] = {}
    for key, value in data.items():
        field = Config.model_fields.get(key)
        model = field.annotation if field else None
        if not (isinstance(model, type) and issubclass(model, _Section)):
            warnings.append(f"{key}: unknown setting, ignored")
        elif not isinstance(value, dict):
            warnings.append(f"{key}: invalid value, using the defaults")
            if model is BackupConfig:
                warnings.append(_FALLBACK_WARNING)
                sections[key] = BackupConfig(sets=_fallback_sets())
        elif model is BackupConfig:
            sections[key] = _load_backup(value, warnings)
        else:
            for list_key in _PATH_LISTS.get(key, ()):
                if list_key in value:
                    value = {
                        **value,
                        list_key: _clean_paths(value[list_key], f"{key}.{list_key}", warnings),
                    }
            sections[key] = model.model_validate(
                _valid_items(model, key, value, warnings, skip=RETIRED.get(key, frozenset()))
            )
            if key == "cleanup" and "never_delete" in value:
                _warn_missing_floor(cast("CleanupConfig", sections[key]), warnings)
    return Config.model_validate(sections), warnings


# Path-list settings that get the same rules as set paths, by section.
_PATH_LISTS: dict[str, tuple[str, ...]] = {
    "cleanup": ("delete_roots", "never_delete"),
    "slate": ("keep_folders",),
    "browse": ("never_delete_extra",),
}


def _warn_missing_floor(cleanup: CleanupConfig, warnings: list[str]) -> None:
    listed = {p.casefold() for p in cleanup.never_delete}
    for path in NEVER_DELETE_FLOOR:
        if path.casefold() not in listed:
            warnings.append(
                f'cleanup.never_delete: "{path}" is missing from the list; '
                "it stays protected and cannot be removed"
            )


def _apply(table: Any, values: dict[str, Any]) -> None:
    """Set each key in a tomlkit table, touching only values that changed (keeps comments)."""
    for key, value in values.items():
        if key in table and table[key] == value:
            continue
        table[key] = value


def _same_sets(current: Any, sets: list[dict[str, Any]]) -> bool:
    unwrap = getattr(current, "unwrap", None)
    return unwrap is not None and bool(unwrap() == sets)


def save_config(cfg: Config, path: Path) -> None:
    """Write `cfg` to `path` atomically, keeping the comments and layout of an existing file."""
    try:
        doc = tomlkit.parse(path.read_text(encoding="utf-8"))
    except FileNotFoundError, TOMLKitError, UnicodeDecodeError:
        doc = tomlkit.document()

    for name, values in cfg.model_dump().items():
        if not isinstance(doc.get(name), Table):  # missing, or a stray value like `phone = 3`
            doc[name] = tomlkit.table()
        table = cast("Any", doc[name])
        if name == "backup":
            sets = values.pop("sets")
            table.pop("roots", None)  # replaced by sets; never written back
        _apply(table, values)
        if name == "backup" and not _same_sets(table.get("sets"), sets):
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
