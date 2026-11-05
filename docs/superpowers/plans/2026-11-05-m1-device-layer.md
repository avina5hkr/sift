# Sift M1 Device Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every phone access goes through one `Transport` interface with a fault-injecting fake and a real adb implementation. The status bar shows the live connection state of the phone within about a second of any change. SSD detection is M3 (owner decision); the SSD chip stays "SSD not connected" until then.

**Architecture:** `sift.device` holds the `Transport` protocol, `FakeTransport`/`FakePhone` (used by every test and by `make dev`), a minimal adb **sync v2** client (64-bit sizes), `AdbTransport` built on adbutils plus that client, `AdbManager` (locate or download adb; attach to the shared server on 5037; never kill it) and `DeviceTracker` (the track-devices stream → `ConnectionState` → `device` events). The server exposes typed device status; the React phone chip renders it. **Nothing in M1 writes to the phone.**

**Tech Stack:** adbutils 2.12.0 (server protocol, shell, RECV streaming, track-devices), own sync-v2 client, `system_profiler -json SPUSBHostDataType` / `ioreg` for link speed, `diskutil info -plist` for volumes; existing FastAPI/React stack from M0.

**Spec:** the maintainer's private product spec. It is kept out of this repository; section numbers in this plan refer to it, and every requirement a task needs is restated in the task itself.

## Global Constraints

- Python 3.14 via uv; mypy strict for `sift.core.*`, `sift.backup.*`, `sift.cleanup.*`. Make `sift.device.*` strict too (it is safety-critical: identity check I3f).
- **No writes to the phone in M1.** `AdbTransport.delete`, `push` and `media_rescan` raise `NotImplementedError("… arrives in M8")`. No `rm`, `mv`, `settings put`, `pm`, or anything else that changes the phone. `FakeTransport` implements them for later milestones' tests.
- **adb server:** use the shared server on port 5037 (`phone.adb_server_port`, default 5037). If a server answers there, attach to it through adbutils (no adb CLI call that could restart it). If none answers, start one with the located adb (`adb -P <port> start-server`). **Never call `kill-server`.** (Spec 6; measured: macOS gives one process exclusive USB access, `kIOReturnExclusiveAccess`.)
- List and stat **only** through the sync protocol, never by parsing `ls` or `find`. Use **sync v2** (`LIS2`/`STA2`: 64-bit size and mtime) because adbutils' v1 `LIST`/`STAT` truncate sizes to 32 bits (the owner's phone has a 4.12 GB file). Shell only for `sha256sum`, `tail`, `getprop`, `df`, `wm size`, `dumpsys battery`, and `true`; quote every path with `shlex.quote`.
- Per-call timeouts: stat 10 s; list one directory 60 s; read stall 30 s without bytes; delete 15 s; one sha256 batch `max(120 s, batch_bytes / 100 MB/s)` with batches capped at 40 files **or** 8 GB. At most 6 adb connections at once.
- Reconnect backoff 1, 2, 4, 8, 16, then every 30 s. A disconnect pauses work (it never fails it).
- Identity: `ro.serialno`, falling back to `android_id` (`settings get secure android_id` is a read). It is mapped to `device.uuid` in the database. A different phone gives `wrong_device`.
- USB 2.0 (480 Mb/s) is the normal case; the "use a USB 3 cable" text is a hint only. Link speed comes from `system_profiler -json SPUSBHostDataType`, falling back to `ioreg -p IOUSB`.
- Plain-words UI text; no stack traces. The token never appears in logs (M0 rule stands).
- No network except the adb download, and only after explicit consent (I8).
- Commits are authored by the owner only, through repo-local git config; no AI attribution trailers. Never push.
- Real-phone work happens only in Task 10. It is read-only, and **the controller asks the owner before any step that needs the owner's hands** (replug, revoke/re-allow USB debugging); the owner may choose fake-phone-only coverage for those states.

## Review Focus

1. **Files over 4 GiB and odd names.** v1 sync truncates sizes. Names can be non-UTF-8, contain newlines or a trailing dot, or lead with a space (`" AI Media"`). Expected: exact 64-bit sizes, raw names round-trip, and nothing is skipped silently. → Task 3 `test_lis2_reports_size_over_4gib`, `test_names_are_bytes_safe`; Task 2 fake tree includes all of these.
2. **Another adb server, or a different adb version, already running.** Expected: attach without restarting it, never kill it, and say "another tool may be holding the phone — Retry" when USB shows the phone but adb does not. → Task 4 `test_attach_never_runs_adb_cli_when_server_answers`, Task 7 `test_held_by_other_tool_state`.
3. **The phone vanishes or hangs mid-read.** Expected: a typed `DeviceDisconnected`/`DeviceTimeout` within the stall limit. Never a short read reported as success, never a hung thread. → Task 2 fault contract tests (shared by both transports), Task 5 `test_read_stall_raises_timeout`.
4. **Hash output with hostile file names.** A name with a newline or backslash makes `sha256sum` escape its output. Expected: names never appear in the parsed output, and results map to inputs by position. → Task 5 `test_sha256_maps_by_position_with_hostile_names`.
5. **Wrong phone, or identity probe failure.** Expected: `wrong_device` blocks all jobs, and a failed `ro.serialno` read falls back to `android_id`. If both fail, the state is `offline` with a plain message, never "connected". → Task 7 `test_wrong_device_blocks`, `test_identity_fallback`.

---

### Task 1: Transport interface and types

**Files:** Create `src/sift/device/__init__.py`, `src/sift/device/transport.py`, `tests/device/__init__.py`, `tests/device/test_transport_types.py`. Modify `pyproject.toml` (mypy strict override adds `sift.device.*`).

**Interfaces — Produces:**
```python
@dataclass(frozen=True)
class RemoteEntry:
    path: str                 # absolute, as on the phone (decoded with surrogateescape)
    size: int                 # 64-bit
    mtime: int                # Unix seconds UTC
    is_dir: bool
    is_symlink: bool

@dataclass(frozen=True)
class DeviceInfo:
    serial: str; model: str; android_version: str
    battery_pct: int | None; free_bytes: int | None; total_bytes: int | None
    screen_sizes: tuple[tuple[int, int], ...]   # physical and override from `wm size`

@dataclass(frozen=True)
class LinkHealth:
    kind: Literal["usb", "wifi"]; speed_mbps: int | None; latency_ms: float | None

class TransportError(Exception): ...
class DeviceDisconnected(TransportError): ...
class DeviceTimeout(TransportError): ...
class RemoteError(TransportError): ...          # the phone answered with an error (e.g. ENOENT, EACCES)

class Transport(Protocol):
    def info(self) -> DeviceInfo: ...
    def walk(self, root: str) -> Iterator[RemoteEntry]: ...          # depth-first; never follows symlinks
    def list_dir(self, path: str) -> list[RemoteEntry]: ...          # one level (for shallow_paths)
    def stat(self, path: str) -> RemoteEntry | None: ...
    def open_read(self, path: str, offset: int = 0) -> BinaryIO: ...
    def sha256(self, paths: list[str]) -> dict[str, str | None]: ...
    def delete(self, path: str) -> None: ...
    def push(self, local: Path, remote: str, mtime: int) -> None: ...
    def media_rescan(self, paths: list[str]) -> bool: ...
    def link_health(self) -> LinkHealth: ...
```
`list_dir` is an addition to the spec's interface (needed for `shallow_paths`); record it in the report.

- [ ] Tests: dataclasses are frozen and hashable; `issubclass(DeviceTimeout, TransportError)`; `RemoteError` keeps the remote message. A `typing.Protocol` runtime check (`isinstance(FakeTransport(), Transport)`) moves to Task 2.
- [ ] Implement, run `uv run pytest tests/device -q` and `make lint`, then commit `feat(device): transport interface and types`.

---

### Task 2: FakePhone, FakeTransport with faults, and the shared contract suite

**Files:** Create `src/sift/device/fake_transport.py`, `src/sift/device/fake_phone.py`, `tests/contract/__init__.py`, `tests/contract/conftest.py`, `tests/contract/test_transport_contract.py`, `tests/device/test_fake_faults.py`.

**Interfaces — Produces:**
```python
@dataclass
class FakeFile:
    data: bytes | None = None        # None → lazily generated deterministic bytes (allows > 4 GiB sizes cheaply)
    size: int = 0; mtime: int = 0; is_symlink_to: str | None = None

class FakePhone:
    serial: str; model: str; android_version: str; android_id: str
    def add_file(self, path: str, data: bytes | None = None, *, size: int | None = None, mtime: int = 0) -> None: ...
    def add_symlink(self, path: str, target: str) -> None: ...
    def modify(self, path: str, data: bytes, mtime: int) -> None: ...
    def remove(self, path: str) -> None: ...

@dataclass
class FaultPlan:
    disconnect_after_bytes: int | None = None
    hang_on: set[str] = field(default_factory=set)          # paths whose read never yields (until stall timeout)
    slow_read_bps: int | None = None
    size_change_mid_read: set[str] = ...
    wrong_sha256: set[str] = ...
    refuse_delete: set[str] = ...
    offline_for_s: float | None = None                       # all calls raise DeviceDisconnected for T seconds
    identity: str | None = None                              # report a different serial
    garbage_output: bool = False                             # shell-like calls return unparseable text

@dataclass(frozen=True)
class JournalEntry: op: str; path: str | None; mutating: bool

class FakeTransport:                                         # implements Transport
    def __init__(self, phone: FakePhone, faults: FaultPlan | None = None,
                 clock: Callable[[], float] = time.monotonic, stall_timeout_s: float = 30.0) -> None: ...
    journal: list[JournalEntry]                              # every call, mutating ones flagged (M3/M8 "read-only" assertions)

def build_fake_phone(n_files: int = 200, *, seed: int = 0) -> FakePhone: ...
```
`build_fake_phone` makes a tree shaped like a heavily used phone (aggregates in `docs/spikes/pre-m2-inputs.md`, never the private inventory). It includes:
- `DCIM/Camera` with `YYYYMMDD_HHMMSS.jpg/.mp4` names and one WhatsApp-named stray;
- `DCIM/Screenshots`; `Pictures/.thumbnails`;
- WhatsApp Media at both the Android/media path and the legacy `/sdcard/WhatsApp/Media`, with `Sent` and `Private` folders, `.Links`, `.Statuses`, `.wamocache`, `.Wamo` and a random dot-folder;
- `" AI Media"`, `WhatsApp Backup Excluded Stickers`, `WhatsApp Video Notes`;
- names ending in a dot in `WhatsApp Documents/Sent`, non-ASCII names (Hindi, emoji), a non-UTF-8 name, and a name containing a newline;
- zero-byte files, a symlink, one 4.12 GB lazily generated video, and files with mtime 0, a pre-1970 mtime and a post-2038 mtime;
- `Recordings/Call` and loose files directly in `/sdcard`;
- WhatsApp Business media, and `Android/data` (present but never readable).

The contract suite (`tests/contract/`) is parametrised by a `transport` fixture that yields `FakeTransport(build_fake_phone())` always, plus `AdbTransport` (marker `realphone`, skipped by default; a test that needs a known file skips on the real phone unless it is present). It proves these properties:
- `walk` yields every file exactly once, with sizes and mtimes equal to `stat`;
- symlinks are reported as symlinks and never followed;
- `list_dir` is one level only;
- `open_read(p).read()` equals the content and its length equals `stat(p).size`;
- `open_read(p, offset=n)` returns the bytes from `n` on;
- `sha256` is correct and returns `None` for a missing path;
- a missing path's `stat` returns `None`;
- `info()` fields are populated.

Edge-case tests on the fake (owner request):
- `test_fake_size_over_4gib_stat_walk_and_offset_read`: the 4.12 GB lazy file reports its exact size via `stat` and `walk`, and `open_read(p, offset=2**32 + 5)` returns the right bytes without materialising the file;
- `test_fake_odd_and_zero_mtimes`: files with mtime 0, a negative mtime, 2**31 + 1 and 2**40 report them unchanged through `stat`, `walk` and `list_dir`.

The fault tests (fake only):
- disconnect after N bytes → `DeviceDisconnected`;
- hang → `DeviceTimeout` after the stall timeout (with an injected clock, no real waiting);
- size change mid-read is detectable (the bytes read ≠ the stat size);
- a wrong hash is returned as configured;
- offline for T seconds → `DeviceDisconnected`, then recovery;
- garbage output → `RemoteError`;
- an identity override changes `info().serial`.

The journal is also tested: reads are not mutating, while `delete` and `push` are.

- [ ] Write the contract and fault tests, watch them fail, implement, watch them pass. Run `uv run pytest -q`, then commit `feat(device): fake phone, fault injection, transport contract suite`.

---

### Task 3: adb sync v2 client

**Files:** Create `src/sift/device/sync2.py`, `tests/device/test_sync2.py`, `tests/device/fake_adb_server.py`.

**Interfaces — Produces:**
```python
@dataclass(frozen=True)
class SyncStat: mode: int; size: int; mtime: int; error: int        # error = errno from the phone (0 = ok)

class Sync2:
    def __init__(self, host: str, port: int, serial: str, *, timeout_s: float) -> None: ...  # host:transport:<serial> then "sync:"
    def stat(self, path: bytes) -> SyncStat: ...                               # STA2 (lstat semantics)
    def list(self, path: bytes) -> Iterator[tuple[bytes, SyncStat]]: ...       # LIS2 → DNT2 entries, excludes "." and ".."
    def close(self) -> None: ...
def device_features(host: str, port: int, serial: str) -> set[str]: ...       # host-serial:<serial>:features
```
Decisions:
- Wire format per AOSP `file_sync_protocol.h`: `STA2`/`LIS2` requests; replies `STA2`/`DNT2` with `{id, error, dev, ino, mode, nlink, uid, gid, size(u64), atime(i64), mtime(i64), ctime(i64)}` little-endian, plus `namelen` and the name for `DNT2`; `DONE` ends a list.
- Every read loops until the exact byte count arrives (`_recv_exact`). A short read raises `DeviceDisconnected`.
- Names are bytes end to end, decoded only at the `AdbTransport` boundary with `surrogateescape`.
- If `ls_v2`/`stat_v2` are missing from the features, raise `RemoteError("This phone's adb is too old for Sift (needs Android 11 or later).")`.
- `tests/device/fake_adb_server.py` is a tiny threaded TCP server that speaks just enough of the host protocol (`host:transport:`, `sync:`, `STA2`, `LIS2`, `host-serial:…:features`) from scripted data.

- [ ] Tests:
  - `test_lis2_reports_size_over_4gib` (size 4_420_000_000 round-trips);
  - `test_names_are_bytes_safe` (b"caf\xe9.jpg", b"new\nline", b"trail.", b" AI Media");
  - `test_stat_missing_returns_error_errno` (ENOENT=2);
  - `test_short_read_raises_disconnected`;
  - `test_features_gate`;
  - `test_timeout_raises_device_timeout`;
  - `test_sta2_reports_size_over_4gib` (4_420_000_000 and 2**40 via STA2, not only LIS2);
  - `test_odd_and_zero_mtimes_round_trip` (mtime 0, a pre-1970 negative mtime, 2**31 + 1 i.e. after 2038, and 2**40; all exact Unix seconds, never converted to local time or replaced with "now");
  - `test_v1_truncation_is_not_used` (the client never sends LIST/STAT v1).
- [ ] Implement, run, then commit `feat(device): adb sync v2 client with 64-bit sizes`.

---

### Task 4: AdbManager — locate, consented download, attach-or-start on 5037

**Files:** Create `src/sift/device/adb_manager.py`, `tests/device/test_adb_manager.py`. Modify `src/sift/server/routes/` (new `devices.py`: `POST /api/devices/adb/download` with body `{"consent": true}`).

**Interfaces — Produces:**
```python
class AdbStatus(StrEnum): ready = "ready"; missing = "missing"; broken = "broken"
@dataclass(frozen=True)
class AdbLocation: path: Path | None; version: str | None; status: AdbStatus; message: str

class AdbManager:
    def __init__(self, paths: AppPaths, config: PhoneConfig, *, runner: Runner = subprocess_runner,
                 server_probe: Callable[[str, int], str | None] = adbutils_server_version) -> None: ...
    def locate(self) -> AdbLocation: ...          # phone.adb_path → <support>/platform-tools/adb → missing; verifies `adb version`
    def ensure_server(self) -> int: ...          # returns the port; attach if a server answers, else `adb -P port start-server`
    def download(self, *, consent: bool, opener: Opener = urlopen) -> AdbLocation: ...
```
Decisions:
- Download URL: `https://dl.google.com/android/repository/platform-tools-latest-darwin.zip`, the spec's URL. Google publishes no stable hash for "latest", so verification is: the zip contains `platform-tools/adb`, it unpacks only under `<support>/platform-tools/` (reject any member whose resolved path escapes, which blocks zip-slip), it is marked executable, and `adb version` runs.
- `consent=False` raises before any network call. The API needs `consent: true` in the body.
- `ensure_server`: if `server_probe` answers on the port, never run any adb CLI command. Otherwise run `adb -P <port> start-server` once, with a 10 s timeout. Never `kill-server`.
- Tests inject `runner` and `opener` and use a locally built zip, so no network and no real adb are needed.

- [ ] Tests:
  - `test_locate_prefers_configured_path`;
  - `test_locate_reports_missing_in_plain_words`;
  - `test_download_requires_consent` (opener never called);
  - `test_download_rejects_zip_slip`;
  - `test_download_installs_and_verifies` (fake zip, runner returns a version);
  - `test_attach_never_runs_adb_cli_when_server_answers` (runner records zero calls);
  - `test_starts_server_once_when_none_answers`;
  - `test_never_calls_kill_server` (scan every runner invocation);
  - API: `POST /api/devices/adb/download` without consent → 400 `consent_required`.
- [ ] Implement, run, then commit `feat(device): adb manager — locate, consented download, shared server`.

---

### Task 5: AdbTransport (read-only)

**Files:** Create `src/sift/device/adb_transport.py`, `src/sift/device/shell.py` (quoting, command builders and parsers), `tests/device/test_adb_shell_parsing.py`, `tests/device/test_adb_transport_unit.py`.

**Interfaces — Produces:** `class AdbTransport` implements `Transport` and takes `(serial: str, port: int, *, limits: Limits = Limits())`.
```python
@dataclass(frozen=True)
class Limits:
    stat_s: float = 10; list_s: float = 60; read_stall_s: float = 30; delete_s: float = 15
    sha_batch_files: int = 40; sha_batch_bytes: int = 8 * 10**9; sha_min_s: float = 120; sha_assumed_bps: int = 100 * 10**6
    max_connections: int = 6
def build_sha256_script(paths: list[str]) -> str: ...      # see decision below
def parse_sha256_output(out: str, n: int) -> list[str | None]: ...
def plan_sha_batches(sizes: dict[str, int], limits: Limits) -> list[list[str]]: ...
def tail_command(path: str, offset: int) -> str: ...        # "tail -c +{offset+1} {quoted}" (1-based, measured)
def parse_df(out: str) -> tuple[int, int] | None: ...       # (free, total) bytes for /sdcard
def parse_wm_size(out: str) -> tuple[tuple[int, int], ...]: ...
def parse_battery(out: str) -> int | None: ...
```
Decisions:
- **Hashing never puts file names in the output.** The script is `for each path: sha256sum < 'path' 2>/dev/null || echo '!'`, one line per path, in order. Each line is a 64-hex hash followed by `  -`, or `!`. So names with newlines or backslashes cannot break parsing. If the line count is not `n`, raise `RemoteError`.
- Batches hold at most 40 files and 8 GB. Timeout per batch is `max(120 s, bytes / 100 MB/s)`.
- `open_read(p, 0)` streams through adbutils `sync.iter_content` (RECV) behind a reader that raises `DeviceTimeout` when no bytes arrive for `read_stall_s` (a watchdog thread closes the connection) and `DeviceDisconnected` on early EOF. `open_read(p, n>0)` uses `exec-out tail -c +{n+1}` (verified on the phone: 1-based).
- `walk` and `list_dir` use `Sync2` (Task 3); `info()` uses `getprop` / `df /sdcard` / `wm size` / `dumpsys battery` (all reads).
- A `threading.BoundedSemaphore(6)` guards connections.
- `delete`, `push` and `media_rescan` raise `NotImplementedError("Deleting and restoring arrive in M8.")`.
- Every adbutils / socket error maps to the typed errors from Task 1.

- [ ] Tests (no phone needed):
  - parsers on captured output from this phone (Android 16), stored in `tests/device/fixtures/` after anonymising (serials, android_id, user names, volume names and UUIDs replaced; no file or app names);
  - `test_sha256_maps_by_position_with_hostile_names`;
  - `test_sha_batches_respect_count_and_bytes` (40 small files, then one 8 GB batch boundary, then a single 9 GB file alone);
  - `test_tail_command_is_one_based`;
  - `test_read_stall_raises_timeout` (fake stream that stops yielding; injected stall);
  - `test_adb_transport_cannot_write_in_m1` (`delete`/`push`/`media_rescan` raise, and nothing reaches adbutils);
  - `test_every_shell_path_is_quoted` (property test with hypothesis over random names).
- [ ] Implement, run, then commit `feat(device): read-only adb transport`.

---

### Task 6: USB link speed

**Files:** Create `src/sift/services/__init__.py`, `src/sift/services/usblink.py`, `tests/services/test_usblink.py`, `tests/services/fixtures/{sphost.json,ioreg.txt}` (captured on this Mac with serials replaced by placeholders).

**Interfaces — Produces:** `def link_speed_mbps(serial: str | None = None, *, run: Runner = subprocess_runner) -> int | None` and `def android_usb_present(run: Runner = subprocess_runner) -> bool`. The first returns 480/5000/10000 or `None`, from `system_profiler -json SPUSBHostDataType` (`Link Speed`), falling back to `ioreg -p IOUSB -l -w0` (`Device Speed` 1=12, 2=480, 3=5000, 4=10000). It finds the phone by its USB serial number (equal to the adb serial) and never by vendor, so it works for any Android phone. `android_usb_present` reports whether any USB device exposes the standard adb interface (class 0xFF, subclass 0x42, protocol 0x01, read from `ioreg`); it feeds the "held by another tool" state.

- [ ] Tests:
  - JSON fixture → 480;
  - empty JSON → ioreg fallback → 480;
  - both fail → `None`;
  - an SSD at 5 Gb/s is not mistaken for the phone;
  - `android_usb_present` true/false (fixtures from two different vendors' interface descriptors, anonymised).
- [ ] Implement, run, then commit `feat(services): USB link speed from SPUSBHostDataType with ioreg fallback`.

---

### Task 7: DeviceTracker — connection states and identity

**Files:** Create `src/sift/device/tracker.py`, `tests/device/test_tracker.py`. Modify `src/sift/core/context.py` (optional `tracker`).

**Interfaces — Produces:**
```python
class ConnectionState(StrEnum):
    no_adb = "no_adb"; no_device = "no_device"; unauthorized = "unauthorized"; offline = "offline"
    wrong_device = "wrong_device"; held_by_other = "held_by_other"
    connected_usb = "connected_usb"; connected_wifi = "connected_wifi"

@dataclass(frozen=True)
class DeviceStatus:
    state: ConnectionState; message: str            # plain words, e.g. "Unlock the phone and tap Allow"
    serial: str | None = None; model: str | None = None; android_version: str | None = None
    battery_pct: int | None = None; free_bytes: int | None = None; total_bytes: int | None = None
    link_mbps: int | None = None; usb2_hint: bool = False; device_uuid: str | None = None

class DeviceTracker:
    def __init__(self, ctx: AppContext, *, events: Callable[[], Iterator[DeviceEvent]], connect: Callable[[str], Transport],
                 usb_present: Callable[[], bool], link_speed: Callable[[str], int | None], clock: Clock, sleep: Callable[[float], None]) -> None: ...
    def start(self) -> None: ...     # daemon thread
    def stop(self) -> None: ...
    def status(self) -> DeviceStatus: ...
    def retry(self) -> None: ...     # resets backoff, reconnects now
```
Decisions:
- Events come from adbutils `track_devices()`: `status` is "device", "unauthorized" or "offline", or absent. Every state change publishes a `device` event carrying `asdict(status)`.
- Identity: `getprop ro.serialno`, falling back to `settings get secure android_id` (a read). It is upserted into the `device` table (`uuid` app-generated, `serial`, `model`, `android_version`, `last_seen_at`).
- **M1 rule for "wrong device"** (until the M2 wizard picks a phone): the first phone ever seen becomes the stored device. A different identity while one is stored gives `wrong_device`. A config or setting to switch phones arrives with the wizard.
- `held_by_other`: no adb device for 3 s while `usb_present()` is true. The message is "Another tool may be using the phone (for example Android Studio). Close it, then press Retry."
- `unauthorized`: "Unlock the phone and tap Allow on the USB debugging prompt. If nothing appears, set USB mode to File transfer; on Samsung phones also check that Auto Blocker is off." (spec 6 rule 8).
- `offline`: the message spec 6 gives.
- Backoff after the stream drops: 1, 2, 4, 8, 16, then 30 s, using the injected sleep and clock.
- `usb2_hint` is true when `link_mbps == 480`.

- [ ] Tests (fake event iterator, `FakeTransport`, fake clock and sleep — no threads waiting in real time):
  - `test_states_follow_events` (absent → `no_device`; unauthorized; offline; device → `connected_usb` with info filled);
  - `test_first_phone_is_stored_and_identity_reused`;
  - `test_wrong_device_blocks`;
  - `test_identity_fallback` (no serialno → android_id; both fail → offline with a message);
  - `test_held_by_other_tool_state`;
  - `test_backoff_schedule` (1, 2, 4, 8, 16, 30, 30);
  - `test_publishes_device_event_on_change_only`;
  - `test_retry_resets_backoff`;
  - `test_usb2_hint`.
- [ ] Implement, run, then commit `feat(device): connection tracker with identity and backoff`.

---

### Task 8: Device API, live phone chip, and the fake phone for dev and e2e

**Files:** Modify `src/sift/server/routes/status.py` (`device: DeviceStatusModel | None` from the tracker), `src/sift/server/routes/devices.py` (`GET /api/devices`, `POST /api/devices/retry`), `src/sift/app.py` (start/stop the tracker; `--fake-phone` flag), `web/src/lib/types.ts`, `web/src/components/StatusBar.tsx`, `web/e2e/smoke.spec.ts`, `Makefile`/`scripts/dev.sh`. Tests: `tests/server/test_devices_api.py`, `web/src/components/StatusBar.test.tsx`.

Decisions:
- `--fake-phone` (honoured only together with `--no-window`, like `SIFT_DEV_TOKEN`) wires the tracker to `FakeTransport(build_fake_phone())` and a scripted event source that announces the fake phone after 0.5 s. `make dev` and the Playwright `webServer` use it. Spec 14: "make dev runs … against FakeTransport and a generated fake phone".
- Chip text by state:

  | State | Chip text |
  | --- | --- |
  | `connected_usb` | `<model> · USB · 87% · 12 GB free`. A small "USB 2" tag with a tooltip when `usb2_hint` is set: "Use a USB 3 cable in a direct port for faster backups." |
  | `connected_wifi` | Same layout as USB |
  | `no_device` | `No phone` |
  | `unauthorized` | `Tap Allow on the phone` |
  | `offline` | `Phone not responding` |
  | `held_by_other` | `Phone busy in another tool` plus a **Retry** button (`POST /api/devices/retry`) |
  | `wrong_device` | `A different phone is connected` |
  | `no_adb` | `adb not installed` |

- Blocking states also show the spec 11 banner, with the tracker's message and one action. Retry is the only action in M1. The adb download consent dialog is part of the M2 wizard; until then `no_adb` explains how to set `phone.adb_path`.
- Events: the UI already invalidates `["status"]` on `device` events (M0). Add invalidation on `hello` too (M0 deferred minor), so a reconnect heals immediately.

- [ ] Tests:
  - API: status shows the fake phone once announced;
  - retry endpoint calls `tracker.retry` and needs the token;
  - `GET /api/devices` lists the stored device.
  - vitest: each state's chip text, the USB 2 tag, and the Retry button.
  - e2e (fake phone): the chip changes from `No phone` to the fake model within 3 s, with no page reload.
- [ ] Run `make test web-test e2e`, then commit `feat: live phone status from the device tracker`.

---

### Task 9: `make fake-phone` and docs

**Files:** Create `scripts/fake_phone.py` (prints a summary of `build_fake_phone(n)`, and optionally dumps the tree as JSON for inspection). Modify `Makefile` (`fake-phone: uv run python scripts/fake_phone.py --files $(N)`), `README.md` (a short "Develop without a phone" note).

- [ ] Test `tests/test_fake_phone_script.py`: run the script with `--files 50`; it prints file count, total bytes, and the presence of the edge cases. Commit `chore: make fake-phone`.

---

### Task 10: Real-phone contract run and device spike (read-only, owner present)

**Files:** Create `scripts/spike_device.py` and `docs/spikes/m1-device.md`. Modify `tests/contract/conftest.py` only if the real-phone fixture needs a known-file override (environment variable `SIFT_REALPHONE_SAMPLE_DIR`, default `/sdcard/DCIM/Camera`).

**The controller runs this task, not a subagent.** It is interactive and touches the owner's phone.

- [ ] **Ask the owner to connect the phone** (phone's own cable, screen unlocked) and confirm.
- [ ] `uv run pytest -m realphone tests/contract -q`: the same contract suite against `AdbTransport`. Expected: all pass.
- [ ] `uv run python scripts/spike_device.py --sample 50`, read-only, prints aggregates only:
  - `adb devices -l`, identity, Android version, `df`, `wm size`, `dumpsys battery` level;
  - sync v2 vs v1 on the 4.12 GB file (proves the truncation fix);
  - LIS2 walk speed over `/sdcard/DCIM` and the WhatsApp Media folders;
  - cold sha256 rate over ≥ 50 files with the planned batching;
  - stream 10 files into `build/m1-spike/` and compare each hash with the phone's, then delete only that folder;
  - `tail -c +N` resume on one large video (bytes match the full read from offset N);
  - link speed from `usblink`.
- [ ] **Ask the owner before each hands-on step**, and skip it if the owner says so (those states stay covered by the fake-phone tests from Task 7):
  - (a) unplug then replug: expect `no_device` then `connected_usb` within about 1 s each;
  - (b) revoke USB debugging authorisations on the phone, then re-allow: expect `unauthorized` then `connected_usb`.
- [ ] Write `docs/spikes/m1-device.md` with numbers and outcomes only: no file names, no inventory. Commit `docs(spikes): M1 real-phone results`.
- [ ] Confirm nothing was written to the phone: the `AdbTransport` journal or log shows no mutating calls, and `df` free space is unchanged to the MB, allowing for the phone's own activity.

---

## M1 exit check

- [ ] `uv run pytest -q`, `make lint`, `make web-test` and `make e2e` all pass. The `realphone` contract run is recorded.
- [ ] With the real phone plugged in, `uv run sift` shows the phone's model, `USB` and the `USB 2` tag in the status bar. The owner does this check.
- [ ] No code path in `sift.device` or `sift.services` writes to the phone; `test_adb_transport_cannot_write_in_m1` guards this.
- [ ] Next: the M2 plan, starting with the quit-path task (roadmap M2, Task 1).
