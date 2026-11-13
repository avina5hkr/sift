"""Backup sets as scan roots, and which set a phone path belongs to.

`resolve_roots` turns the ticked sets into roots for the scanner. `set_for_path` answers which
ticked set a path belongs to: the root with the longest path that claims it, so a narrow set
inside a wide one takes its own files. Phone paths are compared ignoring case.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from sift.core.config import BackupSet, phone_path_key


@dataclass(frozen=True)
class ScanRoot:
    path: str
    set_name: str
    shallow: bool  # direct child files only, no subfolders
    excludes: tuple[str, ...]  # folders under `path` that this set leaves out


def resolve_roots(sets: Sequence[BackupSet], ticked: Sequence[str]) -> list[ScanRoot]:
    """One root per path of each ticked set, in the order the sets are listed.

    A ticked set runs even if it is turned off in the settings; a set not ticked never does.
    Names match ignoring case. A ticked name that is no set raises ValueError, so a typo cannot
    quietly back up less than was asked for.
    """
    wanted = {name.strip().casefold() for name in ticked}
    known = {s.name.casefold() for s in sets}
    missing = sorted(name for name in ticked if name.strip().casefold() not in known)
    if missing:
        raise ValueError("no backup set named " + ", ".join(f'"{n}"' for n in missing))
    roots: list[ScanRoot] = []
    for item in sets:
        if item.name.casefold() not in wanted:
            continue
        for path in item.paths:
            inside = tuple(
                ex
                for ex in item.exclude
                if phone_path_key(ex).startswith(phone_path_key(path) + "/")
            )
            roots.append(ScanRoot(path, item.name, False, inside))
        for path in item.shallow_paths:
            roots.append(ScanRoot(path, item.name, True, ()))
    return roots


def _claims(key: str, root: ScanRoot) -> bool:
    base = phone_path_key(root.path)
    if root.shallow:
        parent, _, name = key.rpartition("/")
        return bool(name) and (parent or "/") == base
    if not key.startswith(base + "/"):
        return False
    return not any(
        key == (ex := phone_path_key(e)) or key.startswith(ex + "/") for e in root.excludes
    )


def claiming_roots(path: str, roots: Sequence[ScanRoot]) -> list[ScanRoot]:
    """Every root that takes `path`: below it (a direct child for a shallow root), not excluded."""
    key = phone_path_key(path)
    return [r for r in roots if _claims(key, r)]


def set_for_path(path: str, roots: Sequence[ScanRoot]) -> str | None:
    """The set `path` belongs to, or None. The most specific root wins; a tie goes to the first."""
    claims = claiming_roots(path, roots)
    if not claims:
        return None
    return max(claims, key=lambda r: len(phone_path_key(r.path))).set_name
