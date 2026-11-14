"""Whether two backup folders are really two copies (review M8 C1).

Two folders on the same drive, or one inside the other, are one copy as far as invariant I3a is
concerned: losing that drive loses both. Paths that do not exist yet are judged by their nearest
existing parent folder.
"""

import os
from pathlib import Path


def _existing(path: Path) -> Path:
    p = path
    while not p.exists() and p != p.parent:
        p = p.parent
    return p


def one_place(a: Path, b: Path) -> bool:
    """True when `a` and `b` are the same folder, nested, or on the same drive."""
    ra, rb = Path(os.path.realpath(a.expanduser())), Path(os.path.realpath(b.expanduser()))
    if ra == rb or ra in rb.parents or rb in ra.parents:
        return True
    return same_drive(ra, rb)


def same_drive(a: Path, b: Path) -> bool:
    """Both on one volume (tests replace this: their "drives" are temp folders)."""
    try:
        return os.stat(_existing(a)).st_dev == os.stat(_existing(b)).st_dev
    except OSError:
        return False
