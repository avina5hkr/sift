"""Summarise the fake phone that `make dev` serves, and optionally dump its tree as JSON.

Usage: uv run python scripts/fake_phone.py [--files N] [--seed S] [--json PATH]

Only paths, sizes and times are listed. File contents are never read, so the large lazily
generated files are never materialised.
"""

import argparse
import json
import posixpath
import sys
from collections.abc import Callable
from pathlib import Path

from sift.device.fake_phone import SD, WA_BUSINESS, WA_LEGACY, WA_MEDIA, FakePhone, build_fake_phone

_FOUR_GIB = 4 * 2**30
_Y2038 = 2**31 - 1
_WA_ROOTS = (WA_MEDIA, WA_LEGACY, WA_BUSINESS)


def _human(n: int) -> str:
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")


def _is_non_utf8(path: str) -> bool:
    try:
        path.encode("utf-8")
    except UnicodeEncodeError:
        return True
    return False


def _folders(path: str) -> list[str]:
    return posixpath.dirname(path).split("/")


def _in_hidden_whatsapp_folder(path: str) -> bool:
    for root in _WA_ROOTS:
        if path.startswith(root + "/"):
            below = path[len(root) + 1 :].split("/")[:-1]
            return any(part.startswith(".") for part in below)
    return False


def _edge_cases(phone: FakePhone) -> list[tuple[str, Callable[[str], bool]]]:
    files = phone.files
    return [
        ("non-UTF-8 names", _is_non_utf8),
        ("names with newline", lambda p: "\n" in p),
        ("trailing-dot names", lambda p: posixpath.basename(p).endswith(".")),
        ("leading-space folders", lambda p: any(f.startswith(" ") for f in _folders(p))),
        (
            "zero-byte files",
            lambda p: files[p].size == 0 and files[p].is_symlink_to is None,
        ),
        ("symlinks", lambda p: files[p].is_symlink_to is not None),
        ("files over 4 GiB", lambda p: files[p].size > _FOUR_GIB),
        ("mtime 0", lambda p: files[p].mtime == 0),
        ("mtime negative", lambda p: files[p].mtime < 0),
        ("mtime after 2038", lambda p: files[p].mtime > _Y2038),
        ("hidden WhatsApp folders", _in_hidden_whatsapp_folder),
        ("legacy WhatsApp folder files", lambda p: p.startswith(WA_LEGACY + "/")),
        ("loose files in /sdcard", lambda p: posixpath.dirname(p) == SD),
    ]


def _dump(phone: FakePhone, target: Path) -> None:
    entries = [
        {
            "path": path,
            "size": file.size,
            "mtime": file.mtime,
            "is_symlink": file.is_symlink_to is not None,
        }
        for path, file in sorted(phone.files.items())
    ]
    # ensure_ascii keeps non-UTF-8 (surrogate-escaped) names representable.
    target.write_text(json.dumps({"files": entries}, indent=1), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Summarise the fake phone used by `make dev`.")
    parser.add_argument("--files", type=int, default=200, metavar="N", help="bulk size (200)")
    parser.add_argument("--seed", type=int, default=0, metavar="S", help="random seed (0)")
    parser.add_argument("--json", type=Path, metavar="PATH", help="also dump the tree as JSON")
    args = parser.parse_args(argv)

    phone = build_fake_phone(args.files, seed=args.seed)
    total = sum(f.size for f in phone.files.values())  # sizes only; nothing is generated
    print(f"files: {len(phone.files)}")
    print(f"total bytes: {total:,} ({_human(total)})")
    print("edge cases:")
    paths = sorted(phone.files)
    for label, test in _edge_cases(phone):
        matches = [p for p in paths if test(p)]
        print(f"  {label}: {len(matches)}")
        if matches:
            print(f"    e.g. {matches[0]!a}")
    if args.json is not None:
        _dump(phone, args.json)
        print(f"tree written to {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
