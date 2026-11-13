"""What a backup scan leaves out, and why.

`SkipRules.from_config` takes the owner's editable lists and toggles from the settings;
`is_skipped` names the first rule that applies to a path. The scanner prunes a skipped folder
and never lists inside it. Phone paths are compared ignoring case.

Rule names returned: hidden_folder, whatsapp_hidden_folder, whatsapp_statuses, whatsapp_db,
name_prefix, name_suffix, zero_byte.
"""

import functools
from dataclasses import dataclass

from sift.core.config import Config, phone_path_key
from sift.core.origin import WHATSAPP_MEDIA_ROOTS

_STATUSES = ".statuses"
_DB_FOLDERS = ("Databases", "Backups")


def _parent(path: str) -> str:
    return path.rsplit("/", 1)[0]


@dataclass(frozen=True)
class SkipRules:
    hidden_folders: tuple[str, ...]
    name_prefixes: tuple[str, ...]
    name_suffixes: tuple[str, ...]
    skip_zero_byte: bool
    include_whatsapp_db: bool
    include_whatsapp_statuses: bool
    whatsapp_media_roots: tuple[str, ...]

    @classmethod
    def from_config(cls, cfg: Config) -> SkipRules:
        b = cfg.backup
        return cls(
            hidden_folders=tuple(b.skip_hidden_folders),
            name_prefixes=tuple(b.skip_name_prefixes),
            name_suffixes=tuple(b.skip_name_suffixes),
            skip_zero_byte=b.skip_zero_byte,
            include_whatsapp_db=b.include_whatsapp_db,
            include_whatsapp_statuses=b.include_whatsapp_statuses,
            whatsapp_media_roots=WHATSAPP_MEDIA_ROOTS,
        )

    @functools.cached_property
    def _hidden(self) -> frozenset[str]:
        return frozenset(n.casefold() for n in self.hidden_folders)

    @functools.cached_property
    def _prefixes(self) -> tuple[str, ...]:
        return tuple(n.casefold() for n in self.name_prefixes)

    @functools.cached_property
    def _suffixes(self) -> tuple[str, ...]:
        return tuple(n.casefold() for n in self.name_suffixes)

    @functools.cached_property
    def _media_keys(self) -> tuple[tuple[str, int], ...]:
        """Each WhatsApp Media root as (key, number of folder names in it)."""
        return tuple((k, k.count("/")) for k in map(phone_path_key, self.whatsapp_media_roots))

    @functools.cached_property
    def _db_keys(self) -> tuple[str, ...]:
        """The Databases and Backups folders beside each Media root."""
        return tuple(
            f"{_parent(key)}/{name.casefold()}"
            for key, _ in self._media_keys
            for name in _DB_FOLDERS
        )


def is_skipped(path: str, *, is_dir: bool, size: int, rules: SkipRules) -> str | None:
    """The name of the first rule that skips `path`, or None. `size` only matters for files."""
    key = phone_path_key(path)
    if not rules.include_whatsapp_db:
        for db in rules._db_keys:
            if key == db or key.startswith(db + "/"):
                return "whatsapp_db"
    names = key.split("/")[1:]
    folders = names if is_dir else names[:-1]
    media_depth = -1  # how many names the WhatsApp Media root has, when the path is in one
    for root, depth in rules._media_keys:
        if key.startswith(root + "/"):
            media_depth = depth
            break
    for i, folder in enumerate(folders):
        in_whatsapp = i >= media_depth >= 0
        if in_whatsapp and folder == _STATUSES:
            if not rules.include_whatsapp_statuses:
                return "whatsapp_statuses"
            continue
        if in_whatsapp and folder.startswith("."):
            return "whatsapp_hidden_folder"
        if folder in rules._hidden:
            return "hidden_folder"
    name = names[-1] if names else ""
    if name.startswith(rules._prefixes):
        return "name_prefix"
    if name.endswith(rules._suffixes):
        return "name_suffix"
    if rules.skip_zero_byte and not is_dir and size == 0:
        return "zero_byte"
    return None
