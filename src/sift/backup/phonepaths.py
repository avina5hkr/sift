"""Phone paths in the database, and in plain words, whatever bytes the phone's names hold.

The transport gives phone paths as `str`. A byte that is not part of valid UTF-8 comes through
as a lone surrogate U+DC80..U+DCFF (`surrogateescape`), and must go back to the phone exactly
as it came. SQLite text must be valid UTF-8, so such a path cannot be stored as it is:

- `to_db` leaves a valid UTF-8 path unchanged (every row written before this module, too).
  In any other path, each escaped byte is written as NUL followed by the byte in three decimal
  digits ("caf\\udce9.jpg" -> "caf\\x00233.jpg"). A phone path never holds NUL (no phone file
  system allows it in a name), so an escaped path can never equal a stored valid path, and
  `from_db` gives back exactly the original.
- The escape commutes with `phone_path_key` (it only replaces the storage prefix, strips a
  trailing slash and casefolds: NUL and digits casefold to themselves, a surrogate is left as
  it is). So a key stored with `to_db` matches the key of a path read back with `from_db`.
- `shown` is the path for plain words (a warning, the job's current file, an error): each
  escaped byte as `%XX`, as the Mac file name has it (`sift.backup.layout`).

A path that `to_db` cannot store (a NUL, or a surrogate `surrogateescape` never makes) raises
`UnstorablePath`; the scan leaves that one file out with a plain warning.
"""

import re

_ESCAPE = "\x00"
_ESCAPED = re.compile("\x00([0-9]{3})")
_BYTE_FIRST, _BYTE_LAST = 0xDC80, 0xDCFF  # what surrogateescape makes of bytes 0x80..0xFF


class UnstorablePath(ValueError):
    """A path with a NUL, or a surrogate that is not an escaped byte."""


def to_db(path: str) -> str:
    """The text stored for a phone path: the path itself when it is valid UTF-8."""
    if _ESCAPE in path:
        raise UnstorablePath(f"a phone path cannot hold NUL: {shown(path)!r}")
    try:
        path.encode("utf-8")
    except UnicodeEncodeError:
        pass
    else:
        return path
    out: list[str] = []
    for ch in path:
        code = ord(ch)
        if _BYTE_FIRST <= code <= _BYTE_LAST:
            out.append(f"{_ESCAPE}{code - 0xDC00:03d}")
        elif 0xD800 <= code <= 0xDFFF:
            raise UnstorablePath(f"not a name the phone gave: {shown(path)!r}")
        else:
            out.append(ch)
    return "".join(out)


def from_db(text: str) -> str:
    """The phone path `to_db` stored as `text`, with its original bytes."""
    if _ESCAPE not in text:
        return text
    return _ESCAPED.sub(lambda m: chr(0xDC00 + int(m.group(1))), text)


def storable(path: str) -> bool:
    try:
        to_db(path)
    except UnstorablePath:
        return False
    return True


def shown(text: str) -> str:
    """`text` for plain words: each escaped byte as %XX, any other surrogate as U+FFFD."""
    if text.isascii():
        return text
    out: list[str] = []
    for ch in text:
        code = ord(ch)
        if _BYTE_FIRST <= code <= _BYTE_LAST:
            out.append(f"%{code - 0xDC00:02X}")
        elif 0xD800 <= code <= 0xDFFF:
            out.append("�")
        else:
            out.append(ch)
    return "".join(out)
