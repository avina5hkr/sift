"""A fake phone: an in-memory file tree for tests and `make dev`. No personal data."""

import functools
import hashlib
import posixpath
import random
import time
from dataclasses import dataclass

from sift.device.transport import RemoteEntry

_PATTERN_LEN = 65_521  # prime, so a repeating pattern never lines up with power-of-two offsets
_HASH_CHUNK = 1 << 20


@dataclass
class FakeFile:
    data: bytes | None = None  # None: bytes are generated lazily from (path, size)
    size: int = 0
    mtime: int = 0
    is_symlink_to: str | None = None


@functools.lru_cache(maxsize=256)
def lazy_pattern(path: str, size: int) -> bytes:
    """The period of the repeating byte pattern of a lazy file: byte i is pattern[i % len]."""
    seed = path.encode("utf-8", "surrogateescape") + b"\0" + str(size).encode()
    return hashlib.shake_128(seed).digest(_PATTERN_LEN)


def lazy_bytes(path: str, size: int, offset: int, length: int) -> bytes:
    """Bytes [offset, offset + length) of a lazy file, clamped to its size. O(length)."""
    end = min(offset + length, size)
    if offset >= end:
        return b""
    count = end - offset
    pattern = lazy_pattern(path, size)
    start = offset % _PATTERN_LEN
    repeats = (start + count) // _PATTERN_LEN + 1
    return (pattern * repeats)[start : start + count]


# On a real phone /sdcard is a symlink (lstat says so; reads and listings follow it) to the
# storage folder. Files are kept under the /sdcard spelling; the storage spelling is an alias.
SDCARD = "/sdcard"
STORAGE_ROOT = "/storage/emulated/0"
_STORAGE_PARENTS = ("/storage", "/storage/emulated")


def _norm(path: str) -> str:
    return path if path == "/" else path.rstrip("/")


def _canon(path: str) -> str:
    """The key a path is stored under: the storage spelling becomes the /sdcard one."""
    path = _norm(path)
    if path == STORAGE_ROOT or path.startswith(STORAGE_ROOT + "/"):
        return SDCARD + path[len(STORAGE_ROOT) :]
    return path


def derived_android_id(serial: str | None) -> str:
    """A stable pretend Android ID for a phone with this serial (16 hex digits)."""
    return hashlib.sha1(f"fake:{serial}".encode()).hexdigest()[:16]


@dataclass
class FakeApp:
    """An installed app on the fake phone (spec 10C probes and actions)."""

    installer: str | None = "com.android.vending"
    app_bytes: int = 50_000_000
    data_bytes: int = 10_000_000
    cache_bytes: int = 5_000_000
    last_used: int = 0  # Unix seconds; 0 = never
    version: str = "1.0"
    apk_paths: tuple[str, ...] = ()


class FakePhone:
    """The files and identity of a pretend phone. FakeTransport serves it over the Transport API."""

    def __init__(
        self,
        *,
        serial: str | None = "FAKE-0001",
        model: str = "TestPhone",
        android_version: str = "14",
        android_id: str | None = None,
        time_offset_s: int = 0,
        battery_pct: int | None = 80,
        total_bytes: int | None = 128 * 10**9,
        screen_sizes: tuple[tuple[int, int], ...] = ((1080, 2400),),
    ) -> None:
        self.serial = serial
        self.apps: dict[str, FakeApp] = {}
        self.settings: dict[str, str] = {}  # global settings (stay_on_while_plugged_in)
        self.health: dict[str, str | None] = {}  # probe kind -> text (None = cannot answer)
        self.contacts: int | None = 0  # the contacts provider's count (None = cannot answer)
        self.bugreport: bytes | None = b"PK\x03\x04 fake bug report"  # None: FAIL
        self.model = model
        self.android_version = android_version
        # Unless given, the Android ID follows the serial, so two phones with different serials
        # are different phones under either identifier.
        if android_id is None:
            android_id = derived_android_id(serial)
        self.android_id = android_id
        self.time_offset_s = time_offset_s  # the phone's clock minus the Mac's, in seconds
        self.battery_pct = battery_pct
        self.total_bytes = total_bytes
        self.screen_sizes = screen_sizes
        self.files: dict[str, FakeFile] = {}  # files and symlinks by absolute path
        self.unreadable: set[str] = set()  # folders that exist but cannot be listed
        # folder path -> child paths. /sdcard holds the storage; it is a symlink in `entry()`.
        self._dirs: dict[str, set[str]] = {
            "/": {SDCARD, "/storage"},
            "/storage": {"/storage/emulated"},
            "/storage/emulated": {STORAGE_ROOT},
            SDCARD: set(),
        }

    # -- building -------------------------------------------------------------------------

    def add_dir(self, path: str, *, readable: bool = True) -> None:
        path = _canon(path)
        if path in self.files:
            raise ValueError(f"{path} is a file")
        self._ensure_dir(path)
        if not readable:
            self.unreadable.add(path)

    def add_file(
        self, path: str, data: bytes | None = None, *, size: int | None = None, mtime: int = 0
    ) -> None:
        """Add a file. With `data=None` and a `size`, its bytes are generated lazily."""
        path = _canon(path)
        if data is not None and size is not None and size != len(data):
            raise ValueError(f"size {size} does not match len(data) {len(data)}")
        if data is None and size is None:
            data = b""
        file = FakeFile(data=data, size=len(data) if data is not None else size or 0, mtime=mtime)
        self._put(path, file)

    def add_symlink(self, path: str, target: str, *, mtime: int = 0) -> None:
        path = _canon(path)
        size = len(target.encode("utf-8", "surrogateescape"))
        self._put(path, FakeFile(data=None, size=size, mtime=mtime, is_symlink_to=target))

    def modify(self, path: str, data: bytes, mtime: int) -> None:
        file = self.files[_canon(path)]
        file.data = data
        file.size = len(data)
        file.mtime = mtime

    def remove(self, path: str) -> None:
        """Remove a file or symlink, or a folder with everything in it."""
        path = _canon(path)
        if path in self.files:
            del self.files[path]
        elif path in self._dirs and path not in ("/", SDCARD, *_STORAGE_PARENTS, STORAGE_ROOT):
            for child in list(self._dirs[path]):
                self.remove(child)
            del self._dirs[path]
            self.unreadable.discard(path)
        else:
            raise KeyError(path)
        self._dirs[posixpath.dirname(path) or "/"].discard(path)

    def _ensure_dir(self, path: str) -> None:
        if path in self._dirs:
            return
        parent = posixpath.dirname(path) or "/"
        if parent in self.files:
            raise ValueError(f"{parent} is a file")
        self._ensure_dir(parent)
        self._dirs[path] = set()
        self._dirs[parent].add(path)

    def _put(self, path: str, file: FakeFile) -> None:
        if path in self._dirs:
            raise ValueError(f"{path} is a folder")
        parent = posixpath.dirname(path) or "/"
        self._ensure_dir(parent)
        self.files[path] = file
        self._dirs[parent].add(path)

    # -- reading --------------------------------------------------------------------------

    def is_unreadable(self, path: str) -> bool:
        """True for a folder that exists but cannot be listed."""
        return _canon(path) in self.unreadable

    def is_dir(self, path: str) -> bool:
        """True for a folder, or a symlink to one (/sdcard): what a listing would follow."""
        return _canon(path) in self._dirs

    def entry(self, path: str) -> RemoteEntry | None:
        """The entry for `path`; a symlink (including /sdcard) is reported as itself (lstat)."""
        shown = _norm(path)
        path = _canon(shown)
        if shown == SDCARD:
            return RemoteEntry(
                path=shown,
                size=len(STORAGE_ROOT),
                mtime=0,
                is_dir=False,
                is_symlink=True,
            )
        file = self.files.get(path)
        if file is not None:
            return RemoteEntry(
                path=shown,
                size=file.size,
                mtime=file.mtime,
                is_dir=False,
                is_symlink=file.is_symlink_to is not None,
            )
        if path in self._dirs:
            return RemoteEntry(path=shown, size=0, mtime=0, is_dir=True, is_symlink=False)
        return None

    def children(self, path: str) -> list[RemoteEntry]:
        """Entries one level below a folder, sorted by path. Ignores `unreadable`.

        Entries are named under the spelling of `path` that was asked for.
        """
        shown = _norm(path)
        canon = _canon(shown)
        entries = [
            self.entry(shown + child[len(canon) :] if canon != shown else child)
            for child in sorted(self._dirs[canon])
        ]
        return [e for e in entries if e is not None]

    def read_range(self, path: str, offset: int, length: int) -> bytes:
        """Up to `length` bytes of a file from `offset`, clamped to its size."""
        path = _canon(path)
        file = self.files[path]
        if file.data is not None:
            return file.data[offset : offset + length]
        return lazy_bytes(path, file.size, offset, length)

    def sha256_of(self, path: str) -> str:
        path = _canon(path)
        file = self.files[path]
        digest = hashlib.sha256()
        if file.data is not None:
            digest.update(file.data)
        else:
            for offset in range(0, file.size, _HASH_CHUNK):
                digest.update(lazy_bytes(path, file.size, offset, _HASH_CHUNK))
        return digest.hexdigest()

    def used_bytes(self) -> int:
        return sum(f.size for f in self.files.values() if f.is_symlink_to is None)


# -- the standard tree --------------------------------------------------------------------------

SD = "/sdcard"
WA_MEDIA = f"{SD}/Android/media/com.whatsapp/WhatsApp/Media"
WA_LEGACY = f"{SD}/WhatsApp/Media"
WA_BUSINESS = f"{SD}/Android/media/com.whatsapp.w4b/WhatsApp Business/Media"

KNOWN_FILE = f"{SD}/DCIM/Camera/20240101_120000.jpg"
KNOWN_DATA = bytes(range(256)) * 8
BIG_VIDEO = f"{SD}/DCIM/Camera/20240615_183000.mp4"
BIG_VIDEO_SIZE = int(4.12 * 2**30)  # 4.12 GiB, over the 32-bit limit
SYMLINK = f"{SD}/Download/camera-link"
SYMLINK_TARGET = f"{SD}/DCIM/Camera"
ZERO_BYTE_FILE = f"{SD}/Download/empty.dat"
UNREADABLE_DIR = f"{SD}/Android/data"
NON_UTF8_FILE = f"{SD}/Download/caf\udce9.jpg"  # one raw byte 0xE9, surrogate-escaped
NEWLINE_FILE = f"{SD}/Download/two\nlines.txt"
# Devanagari ("photo_1"-like) and emoji names, written as escapes.
HINDI_FILE = f"{SD}/Download/फोटो_१.jpg"
EMOJI_FILE = f"{WA_MEDIA}/WhatsApp Images/party_\U0001f389.jpg"
ODD_NAME_FILES = (NON_UTF8_FILE, NEWLINE_FILE, HINDI_FILE, ZERO_BYTE_FILE, SYMLINK)
ODD_MTIME_FILES = {
    f"{SD}/Download/mtime_zero.dat": 0,
    f"{SD}/Download/mtime_before_1970.dat": -31_536_000,
    f"{SD}/Download/mtime_after_2038.dat": 2**31 + 1,
    f"{SD}/Download/mtime_far_future.dat": 2**40,
}

_BASE_TS = 1_672_531_200  # 2023-01-01 00:00:00 UTC


def _stamp(ts: int, fmt: str) -> str:
    return time.strftime(fmt, time.gmtime(ts))


def build_fake_phone(n_files: int = 200, *, seed: int = 0) -> FakePhone:
    """A tree shaped like a heavily used phone. `n_files` scales the bulk; edge cases are fixed.

    Every name is invented; the shape follows the aggregates in docs/spikes/pre-m2-inputs.md.
    """
    rng = random.Random(seed)
    phone = FakePhone()
    ts = _BASE_TS

    def tick() -> int:
        nonlocal ts
        ts += rng.randrange(600, 200_000)
        return ts

    def blob() -> bytes:
        return rng.randbytes(rng.randrange(16, 2048))

    def lazy_size() -> int:
        return rng.randrange(5_000_000, 80_000_000)

    def put(path: str, *, mtime: int, video: bool = False) -> None:
        if path in phone.files:
            return
        if video:
            phone.add_file(path, size=lazy_size(), mtime=mtime)
        else:
            phone.add_file(path, blob(), mtime=mtime)

    n_camera = n_files * 40 // 100
    n_shots = n_files * 10 // 100
    n_thumbs = n_files * 15 // 100
    n_whatsapp = n_files * 25 // 100
    n_rest = n_files - n_camera - n_shots - n_thumbs - n_whatsapp

    for _ in range(n_camera):
        t = tick()
        video = rng.random() < 0.25
        put(
            f"{SD}/DCIM/Camera/{_stamp(t, '%Y%m%d_%H%M%S')}.{'mp4' if video else 'jpg'}",
            mtime=t,
            video=video,
        )
    for _ in range(n_shots):
        t = tick()
        put(f"{SD}/DCIM/Screenshots/Screenshot_{_stamp(t, '%Y%m%d-%H%M%S')}.png", mtime=t)
    for _ in range(n_thumbs):
        t = tick()
        put(f"{SD}/Pictures/.thumbnails/{t}{rng.randrange(1000)}.jpg", mtime=t)

    wa_folders = [
        ("WhatsApp Images", "IMG", "jpg", False),
        ("WhatsApp Images/Sent", "IMG", "jpg", False),
        ("WhatsApp Images/Private", "IMG", "jpg", False),
        ("WhatsApp Video", "VID", "mp4", True),
        ("WhatsApp Video/Sent", "VID", "mp4", True),
        ("WhatsApp Video/Private", "VID", "mp4", True),
        ("WhatsApp Audio", "AUD", "opus", False),
        ("WhatsApp Voice Notes/202401", "PTT", "opus", False),
        ("WhatsApp Documents", "DOC", "pdf", False),
        ("WhatsApp Documents/Sent", "DOC", "pdf", False),
        ("WhatsApp Animated Gifs", "VID", "mp4", False),
        ("WhatsApp Stickers", "STK", "webp", False),
        (".Statuses", "VID", "mp4", False),
        (".Links", "IMG", "jpg", False),
        (".wamocache", "IMG", "jpg", False),
        (".Wamo", "IMG", "jpg", False),
        (".c0ffee0123456789", "IMG", "jpg", False),
        (" AI Media", "IMG", "jpg", False),
        ("WhatsApp Backup Excluded Stickers", "STK", "webp", False),
        ("WhatsApp Video Notes", "VID", "mp4", False),
    ]
    roots = (WA_MEDIA, WA_LEGACY)
    for i in range(n_whatsapp):
        folder, prefix, ext, video = wa_folders[i % len(wa_folders)]
        t = tick()
        root = roots[rng.randrange(len(roots))]
        name = f"{prefix}-{_stamp(t, '%Y%m%d')}-WA{i:04d}.{ext}"
        put(f"{root}/{folder}/{name}", mtime=t, video=video)
    # Every hidden and special folder always has at least one file, whatever n_files is.
    for root in roots:
        for j, (folder, prefix, ext, _) in enumerate(wa_folders):
            t = _BASE_TS + 1_000 + j
            put(f"{root}/{folder}/{prefix}-20230101-WA9{j:03d}.{ext}", mtime=t)
    for j in range(3):
        put(
            f"{WA_BUSINESS}/WhatsApp Business Images/IMG-20230102-WA{j:04d}.jpg", mtime=_BASE_TS + j
        )
    put(f"{SD}/DCIM/Camera/IMG-20230103-WA0001.jpg", mtime=_BASE_TS + 100)  # a stray
    for name in ("Quarterly summary.", "meeting notes.", "ticket scan."):
        put(f"{WA_MEDIA}/WhatsApp Documents/Sent/{name}", mtime=_BASE_TS + 7)

    for _ in range(max(n_rest, 0) // 2):
        t = tick()
        put(f"{SD}/Recordings/Call/Call_{_stamp(t, '%Y%m%d_%H%M%S')}.m4a", mtime=t)
    for i in range(max(n_rest, 0) - max(n_rest, 0) // 2):
        put(f"{SD}/Download/file_{i:04d}.bin", mtime=tick())
    put(f"{SD}/Recordings/Call/Call_20230104_090000.m4a", mtime=_BASE_TS + 200)

    # Fixed edge cases.
    phone.add_file(KNOWN_FILE, KNOWN_DATA, mtime=1_704_110_400)
    phone.add_file(BIG_VIDEO, size=BIG_VIDEO_SIZE, mtime=1_718_476_200)
    phone.add_symlink(SYMLINK, SYMLINK_TARGET, mtime=1_700_000_000)
    phone.add_file(ZERO_BYTE_FILE, b"", mtime=1_700_000_000)
    phone.add_file(f"{SD}/Pictures/.thumbnails/.nomedia", b"", mtime=1_700_000_000)
    phone.add_file(NON_UTF8_FILE, b"non-utf8 name", mtime=1_700_000_001)
    phone.add_file(NEWLINE_FILE, b"newline in name", mtime=1_700_000_002)
    phone.add_file(HINDI_FILE, b"hindi name", mtime=1_700_000_003)
    phone.add_file(EMOJI_FILE, b"emoji name", mtime=1_700_000_004)
    for path, mtime in ODD_MTIME_FILES.items():
        phone.add_file(path, b"odd mtime", mtime=mtime)
    for name in ("notes.txt", "backup.dat", ".hidden-cache"):
        phone.add_file(f"{SD}/{name}", rng.randbytes(64), mtime=_BASE_TS + 300)
    phone.add_dir(UNREADABLE_DIR, readable=False)

    return phone
