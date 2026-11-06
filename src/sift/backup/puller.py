"""Copying the scanned files from the phone to the Mac backup folder (invariants I1, I2, I5).

`pending_items` lists what a run must copy, in pull order: camera files newest first, then
WhatsApp, then everything else. `Puller.run` copies them with `readers` files in flight at
once, each read through its own stream, so the job's share of the phone's connection slots is
`readers`.

Copying one file:

1. **Moved file:** a file with no copy yet whose size and mtime match content already backed
   up (or a row whose content is known) is hashed on the phone; the same SHA-256 links the row
   to that content and nothing is transferred.
2. The bytes stream into a `StagedFile` (`.staging/<uuid>.part` under the backup folder),
   hashed as they arrive. A byte count that does not match the scanned size, or `FileChanged`,
   looks at the file again: if it changed the row goes back to `discovered` for the next run,
   with a warning; otherwise the attempt failed and is retried (2 s, 5 s, 15 s), then `failed`.
3. **Duplicate:** content that already has a copy in the backup folder is not stored again:
   the `.part` is dropped and the row is aliased to that content (`duplicates_skipped`).
4. The destination path is chosen once (layout, then collision against the paths already
   recorded) and stored on the row before the rename (the `_HASH8` form too, when the
   collision gives one); `StagedFile.commit` flushes, renames without replacing anything, and
   reads the file back. Only then are the `content` and `replica` rows written, in one
   transaction (crash point `puller.after_rename_before_rows` sits just before it; a test's
   simulated crash there must raise a `BaseException` that is not an `Exception`, or it counts
   as a failed file instead of ending the run).
5. **`TargetExists`:** a file is already at that path with no row (a crash at that crash
   point, say). Its SHA-256 is read: the same bytes means it is the copy: it is flushed to the
   disk with its folder (it may never have been), the `.part` is dropped and the rows are
   written for the existing file. Different bytes, or anything that is not a regular file (a
   symlink, a FIFO, a folder: never opened or followed), send the copy to the `_HASH8` path;
   if that is taken too by anything else, the file is `failed`. An existing file is never
   modified (I1).
6. **A copy a crash left with no rows:** before committing, a file at a path stored on a
   row, that no replica row owns and that holds exactly these bytes (verified by SHA-256), is
   used instead of storing the bytes again; it is flushed to the disk first, as in step 5.
   This is what keeps one content stored once when, after such a crash, the phone file was
   renamed or a newer copy of it is pulled first. Such paths are listed once per run: only a
   crash makes one, and a crash ends the run.

Deciding on a copy (steps 3 to 6) is serialised, so two readers can never both store the same
content or claim the same path; the transfers themselves run in parallel.

Row states: `discovered → pulling → done | failed | discovered`. Pause and Stop let the files in
flight finish first. A disconnect puts the files it cut short back in the queue, waits for the
phone and goes on; it is never a failed file.
"""

import logging
import mimetypes
import os
import queue
import stat
import threading
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol

from sift.backup.layout import (
    by_date_rel_path,
    exif_taken,
    mirror_rel_path,
    rel_key,
    resolve_collision,
)
from sift.backup.phonepaths import from_db, shown
from sift.backup.staging import (
    ChecksumMismatch,
    IncompleteCopy,
    StagedFile,
    TargetExists,
    make_durable,
    regular_file_sha256,
)
from sift.core.clock import Clock
from sift.core.crashpoints import hit
from sift.core.db import Database
from sift.core.jobs import JobStopped
from sift.device.session import DeviceSession
from sift.device.transport import (
    DeviceDisconnected,
    DeviceTimeout,
    FileChanged,
    RemoteEntry,
    RemoteError,
    Transport,
)

log = logging.getLogger(__name__)

CHUNK = 1 << 20  # 1 MiB reads, hashed as they arrive
STAGING_DIR = ".staging"
CRASH_POINT = "puller.after_rename_before_rows"
# The sets the `by_date` layout applies to (compared case-folded); every other set is mirrored.
BY_DATE_SETS = frozenset({"camera", "pictures and movies"})
# Errors worth another attempt at the same file. A disconnect is not one (the job waits for the
# phone instead), nor is FileChanged (the file goes back to the queue for the next run).
RETRYABLE: tuple[type[BaseException], ...] = (
    DeviceTimeout,
    RemoteError,
    IncompleteCopy,
    ChecksumMismatch,
    OSError,
)
RECONNECT_PAUSE_S = 1.0
# Stands for "something other than a regular file" at a path: never this copy's content.
NOT_A_FILE = "not-a-regular-file"
_COLLECT_S = 0.25  # how often the coordinator wakes to report progress

Layout = Literal["mirror", "by_date"]

_KINDS: dict[str, frozenset[str]] = {
    kind: frozenset(exts.split(" "))
    for kind, exts in {
        "image": "jpg jpeg png gif webp heic heif bmp dng tif tiff avif raw cr2 nef arw",
        "video": "mp4 mov 3gp 3g2 mkv webm avi m4v mts ts wmv",
        "audio": "opus mp3 m4a aac wav ogg oga amr flac wma mid midi",
        "doc": "pdf doc docx xls xlsx ppt pptx txt rtf odt ods odp csv md epub vcf",
    }.items()
}


def content_kind(path: str) -> str:
    """image, video, audio, doc or other, from the file name's extension."""
    name = path.rsplit("/", 1)[-1]
    _, dot, ext = name.rpartition(".")
    ext = ext.casefold() if dot else ""
    return next((kind for kind, exts in _KINDS.items() if ext in exts), "other")


@dataclass(frozen=True)
class Target:
    """Where copies go: one Mac destination row and its folder."""

    dest_id: int
    root: Path
    device_label: str
    layout: Layout = "mirror"

    @property
    def staging(self) -> Path:
        return self.root / STAGING_DIR


@dataclass(frozen=True)
class PullItem:
    """One phone_file row to copy, as the scan left it."""

    id: int
    path: str
    size: int
    mtime: int
    set_name: str | None
    origin: str
    content_hash: str | None
    rel_path: str | None


class Outcome(StrEnum):
    copied = "copied"  # a new copy (or an existing file with the same bytes) is recorded
    duplicate = "duplicate"  # downloaded, but that content already had a copy: aliased
    linked = "linked"  # a moved file: matched on the phone, nothing transferred
    changed = "changed"  # the file changed on the phone: back to discovered
    failed = "failed"
    disconnected = "disconnected"  # back in the queue once the phone returns
    stopped = "stopped"  # the job stopped before this file began
    fatal = "fatal"  # an error that ends the run (re-raised by `run`)


@dataclass
class PullStats:
    files_total: int = 0
    bytes_total: int = 0
    files_done: int = 0
    bytes_done: int = 0
    copied: int = 0
    duplicates_skipped: int = 0
    linked: int = 0
    changed: int = 0
    failed: int = 0
    bytes_copied: int = 0  # bytes of the files stored as new copies
    bytes_not_transferred: int = 0  # bytes of files counted in bytes_done that never crossed
    warnings: list[str] = field(default_factory=list)


class PullControl(Protocol):
    """What the puller needs from its job (`sift.core.jobs.JobControl` provides it).

    Only the coordinating thread calls `checkpoint`, `report`, `wait_for_phone` and
    `wait_until`; the reader threads call `retry` alone.
    """

    @property
    def stop_requested(self) -> bool: ...

    @property
    def pause_requested(self) -> bool: ...

    def checkpoint(self) -> None: ...

    def report(
        self,
        files_done: int,
        bytes_done: int,
        files_total: int | None = None,
        bytes_total: int | None = None,
        *,
        current: str | None = None,
        duplicates_skipped: int = 0,
        failed: int = 0,
        transferred_bytes: int | None = None,
    ) -> None: ...

    def wait_for_phone(self) -> DeviceSession: ...

    def wait_until(self, done: Callable[[], bool], *, timeout_s: float | None = None) -> bool: ...

    def retry[T](self, fn: Callable[[], T], *, retry_on: tuple[type[BaseException], ...]) -> T: ...


def _order_key(item: PullItem) -> tuple[int, int, str]:
    group = 0 if item.origin == "camera" else 1 if item.origin.startswith("whatsapp") else 2
    return (group, -item.mtime, item.path)


def pending_items(db: Database, device_id: int, set_names: Sequence[str]) -> list[PullItem]:
    """The rows of `set_names` still to copy, in pull order.

    Camera files newest first, then WhatsApp (newest first), then the rest (newest first).
    """
    wanted = {name.casefold() for name in set_names}
    with db.read() as conn:
        rows = conn.execute(
            "SELECT id, remote_path, size, mtime, set_name, origin, content_hash, rel_path "
            "FROM phone_file WHERE device_id = ? AND state = 'discovered' AND present = 1",
            (device_id,),
        ).fetchall()
    items = [
        PullItem(int(r[0]), from_db(r[1]), int(r[2]), int(r[3]), r[4], r[5], r[6], r[7])
        for r in rows
        if r[4] is not None and str(r[4]).casefold() in wanted
    ]
    return sorted(items, key=_order_key)


def _mtime_date(mtime: int) -> datetime | None:
    try:
        return datetime.fromtimestamp(mtime)
    except OverflowError, OSError, ValueError:
        return None


class Puller:
    def __init__(
        self,
        db: Database,
        target: Target,
        *,
        device_id: int,
        clock: Clock,
        describe: Callable[[BaseException], str],
        readers: int = 1,
        chunk: int = CHUNK,
        reconnect_pause_s: float = RECONNECT_PAUSE_S,
    ) -> None:
        self._db = db
        self._target = target
        self._device_id = device_id
        self._clock = clock
        self._describe = describe
        self._readers = max(1, readers)
        self._chunk = chunk
        self._reconnect_pause_s = reconnect_pause_s
        # Held from "is this content already here?" to the rows being written, so two readers
        # never both store one content or claim one path.
        self._decide = threading.Lock()
        self._copied_by_stamp: dict[tuple[int, int], str] = {}
        self._unrecorded: dict[int, list[str]] = {}  # file size -> paths with no replica row

    # -- the coordinator (the job's own thread) -----------------------------------------------

    def run(
        self, ctl: PullControl, items: Sequence[PullItem], stats: PullStats | None = None
    ) -> PullStats:
        """Copy `items`; returns the counts. Raises `JobStopped` when the job is stopped.

        Files in flight always finish first, whatever ends the run. A caller that passes
        `stats` still holds the counts when the run ends by an exception (a stop, say).
        """
        stats = stats if stats is not None else PullStats()
        stats.files_total = len(items)
        stats.bytes_total = sum(i.size for i in items)
        self._target.staging.mkdir(parents=True, exist_ok=True)
        self._load_copied_by_stamp()
        self._load_unrecorded()
        pending = deque(items)
        inflight: dict[int, PullItem] = {}
        results: queue.Queue[tuple[PullItem, Outcome, BaseException | None]] = queue.Queue()
        live: dict[int, int] = {}  # bytes read so far of each file in flight
        live_lock = threading.Lock()
        session: DeviceSession | None = None
        lost_phone = False
        fatal: BaseException | None = None
        current: str | None = None

        def progress(item_id: int) -> Callable[[int], None]:
            def update(received: int) -> None:
                with live_lock:
                    live[item_id] = received

            return update

        def report() -> None:
            with live_lock:
                in_flight = sum(live.values())
            ctl.report(
                stats.files_done,
                stats.bytes_done + in_flight,
                stats.files_total,
                stats.bytes_total,
                current=None if current is None else shown(current),
                duplicates_skipped=stats.duplicates_skipped,
                failed=stats.failed,
                transferred_bytes=stats.bytes_done - stats.bytes_not_transferred + in_flight,
            )

        def collect(timeout: float | None) -> None:
            nonlocal lost_phone, fatal
            try:
                item, outcome, error = results.get(timeout=timeout)
            except queue.Empty:
                return
            del inflight[item.id]
            with live_lock:
                live.pop(item.id, None)
            if outcome is Outcome.disconnected:
                lost_phone = True
                pending.appendleft(item)
                return
            if outcome is Outcome.stopped:
                return
            if outcome is Outcome.fatal:
                fatal = fatal or error
                return
            stats.files_done += 1
            stats.bytes_done += item.size
            if outcome is Outcome.copied:
                stats.copied += 1
                stats.bytes_copied += item.size
            elif outcome is Outcome.duplicate:
                stats.duplicates_skipped += 1  # its bytes were read, then dropped
            elif outcome is Outcome.linked:
                stats.linked += 1
                stats.bytes_not_transferred += item.size
            elif outcome is Outcome.changed:
                stats.changed += 1
                stats.bytes_not_transferred += item.size
                stats.warnings.append(
                    f"{item.path} changed while it was being copied; it will be copied next time."
                )
            elif outcome is Outcome.failed:
                stats.failed += 1
                stats.bytes_not_transferred += item.size

        try:
            report()
            while (pending or inflight) and fatal is None:
                if inflight and (lost_phone or len(inflight) >= self._readers or not pending):
                    collect(_COLLECT_S)
                    report()
                    continue
                if inflight and (ctl.stop_requested or ctl.pause_requested):
                    collect(_COLLECT_S)  # let every file in flight finish first
                    report()
                    continue
                if lost_phone:  # nothing in flight now: wait for the phone, then go on
                    ctl.wait_until(lambda: False, timeout_s=self._reconnect_pause_s)
                    session, lost_phone = None, False
                    continue
                ctl.checkpoint()  # blocks while paused, raises JobStopped once stopped
                if session is None:
                    session = ctl.wait_for_phone()
                item = pending.popleft()
                inflight[item.id] = item
                current = item.path
                threading.Thread(
                    target=self._worker,
                    args=(ctl, session, item, results, progress(item.id)),
                    name=f"sift-pull-{item.id}",
                    daemon=True,
                ).start()
        finally:
            while inflight:  # however the run ends, the files in flight finish first
                collect(None)
        report()
        if fatal is not None:
            raise fatal
        return stats

    # -- one file (a reader thread) ------------------------------------------------------------

    def _worker(
        self,
        ctl: PullControl,
        session: DeviceSession,
        item: PullItem,
        results: queue.Queue[tuple[PullItem, Outcome, BaseException | None]],
        progress: Callable[[int], None],
    ) -> None:
        outcome, error = Outcome.fatal, None
        try:
            outcome = self._pull_one(ctl, session, item, progress)
        except BaseException as exc:  # a simulated crash, or the database gone at shutdown
            outcome, error = Outcome.fatal, exc
        finally:
            try:
                self._db.release_thread()
            finally:
                results.put((item, outcome, error))

    def _pull_one(
        self,
        ctl: PullControl,
        session: DeviceSession,
        item: PullItem,
        progress: Callable[[int], None],
    ) -> Outcome:
        self._set_state(item, "pulling", count_attempt=True)
        try:
            outcome = ctl.retry(
                lambda: self._attempt(session.transport, item, progress), retry_on=RETRYABLE
            )
        except JobStopped:
            self._set_state(item, "discovered")
            return Outcome.stopped
        except DeviceDisconnected:
            self._set_state(item, "discovered")
            return Outcome.disconnected
        except Exception as exc:
            if isinstance(exc, (*RETRYABLE, TargetExists)):
                log.warning("Could not copy %r: %s", item.path, shown(str(exc)))
            else:  # not a failure the puller expects: keep the traceback
                log.exception("Could not copy %r", item.path)
            self._set_state(item, "failed", error=self._describe(exc))
            return Outcome.failed
        if outcome is Outcome.changed:
            log.warning("%r changed while it was being copied; it is left for later", item.path)
            self._set_state(item, "discovered", error=self._describe(FileChanged("", item.path)))
        return outcome

    def _attempt(
        self, transport: Transport, item: PullItem, progress: Callable[[int], None]
    ) -> Outcome:
        """One try at one file. Raises a `RETRYABLE` error for a failed attempt."""
        known = self._known_content(item)
        if known is not None:
            on_phone = transport.sha256([item.path]).get(item.path)
            if on_phone == known:
                with self._decide:
                    if self._has_copy(known):
                        self._record_alias(item, known)
                        return Outcome.linked
        progress(0)
        with StagedFile(self._target.staging, expected_size=item.size) as staged:
            try:
                received = self._stream_into(transport, item, staged, progress)
            except FileChanged:
                return Outcome.changed
            except RemoteError:
                if self._changed_on_phone(transport, item):
                    return Outcome.changed
                raise
            if received != item.size:
                if self._changed_on_phone(transport, item):
                    return Outcome.changed
                raise IncompleteCopy(f"Received {received} of {item.size} bytes.")
            digest = staged.sha256
            with self._decide:
                if self._has_copy(digest):
                    self._record_alias(item, digest)
                    return Outcome.duplicate  # leaving the `with` drops the .part
                rel_path = self._unrecorded_copy(digest, staged.size) or self._commit(
                    staged, item, digest
                )
                hit(CRASH_POINT)
                self._record_copy(item, digest, rel_path)
        return Outcome.copied

    def _stream_into(
        self,
        transport: Transport,
        item: PullItem,
        staged: StagedFile,
        progress: Callable[[int], None],
    ) -> int:
        entry = RemoteEntry(item.path, item.size, item.mtime, is_dir=False, is_symlink=False)
        stream = transport.open_read(item.path, entry=entry)
        try:
            while chunk := stream.read(self._chunk):
                staged.write(chunk)
                progress(staged.size)
                if staged.size > item.size:  # it grew: no point reading the rest
                    break
        finally:
            stream.close()
        return staged.size

    @staticmethod
    def _changed_on_phone(transport: Transport, item: PullItem) -> bool:
        try:
            now = transport.stat(item.path)
        except FileChanged:
            return True
        return now is None or (now.size, now.mtime) != (item.size, item.mtime)

    # -- choosing the path and committing (under `_decide`) ------------------------------------

    def _layout_path(self, item: PullItem, digest: str, part: Path) -> str:
        target, hash8 = self._target, digest[:8]
        by_date = target.layout == "by_date" and (item.set_name or "").casefold() in BY_DATE_SETS
        if by_date:
            taken = exif_taken(part) or _mtime_date(item.mtime)
            if taken is not None:
                return by_date_rel_path(target.device_label, item.path, taken=taken, hash8=hash8)
        return mirror_rel_path(target.device_label, item.path, hash8=hash8)

    def _commit(self, staged: StagedFile, item: PullItem, digest: str) -> str:
        """Make the copy final and return its path, or the path of an identical file found there.

        Raises `TargetExists` when both the chosen path and its `_HASH8` form hold other bytes.
        """
        base = item.rel_path
        if base is None:
            # Stored before the commit, so a run after a crash picks the same path even if the
            # layout setting changed meanwhile, and finds the file it left there.
            base = self._layout_path(item, digest, staged.part_path)
            self._save_rel_path(item, base)
        on_disk: dict[str, str] = {}  # rel_key -> SHA-256 of a file found with no row

        def owner_of(key: str) -> str | None:
            return on_disk.get(key) or self._owner_of(key)

        for attempt in range(2):
            rel_path = resolve_collision(
                base, hash8=digest[:8], owner_of=owner_of, content_hash=digest
            )
            final = self._target.root / rel_path
            final.parent.mkdir(parents=True, exist_ok=True)
            if rel_path != base:  # stored too, so a crash after the rename leaves it findable
                self._save_rel_path(item, rel_path)
            try:
                staged.commit(final, mtime=item.mtime)
                return rel_path
            except TargetExists:
                found = regular_file_sha256(final)  # None: not a regular file, never opened
                if found == digest:
                    log.info("Reusing an identical file already at %r", rel_path)
                    make_durable(final)  # as durable as a commit before its rows are written
                    return rel_path  # the .part is dropped when the stage is left
                if attempt == 1:
                    raise
                on_disk[rel_key(rel_path)] = found or NOT_A_FILE
        raise AssertionError("unreachable")

    # -- the database --------------------------------------------------------------------------

    def _set_state(
        self, item: PullItem, state: str, *, error: str | None = None, count_attempt: bool = False
    ) -> None:
        with self._db.tx() as conn:
            conn.execute(
                "UPDATE phone_file SET state = ?, error = ?, attempts = attempts + ? WHERE id = ?",
                (
                    state,
                    None if error is None else shown(error),
                    1 if count_attempt else 0,
                    item.id,
                ),
            )

    def _save_rel_path(self, item: PullItem, rel_path: str) -> None:
        with self._db.tx() as conn:
            conn.execute("UPDATE phone_file SET rel_path = ? WHERE id = ?", (rel_path, item.id))

    def _load_copied_by_stamp(self) -> None:
        """(size, mtime) -> content, for this phone's files already copied here (one query).

        Built once per run: a file copied during the run is found by its hash instead.
        """
        with self._db.read() as conn:
            rows = conn.execute(
                "SELECT p.size, p.mtime, p.content_hash FROM phone_file p JOIN replica r "
                "ON r.content_hash = p.content_hash AND r.dest_id = ? WHERE p.device_id = ?",
                (self._target.dest_id, self._device_id),
            ).fetchall()
        self._copied_by_stamp = {(int(r[0]), int(r[1])): str(r[2]) for r in rows}

    def _load_unrecorded(self) -> None:
        """The paths stored on rows that no replica row here owns, by the size of their file.

        After a crash between a rename and its rows, the copy's path is one of them. Built once
        per run (one query each for rows and replicas; a stat for each such path).
        """
        with self._db.read() as conn:
            owned = {
                str(r[0])
                for r in conn.execute(
                    "SELECT rel_key FROM replica WHERE dest_id = ?", (self._target.dest_id,)
                )
            }
            stored = [
                str(r[0])
                for r in conn.execute(
                    "SELECT DISTINCT rel_path FROM phone_file WHERE rel_path IS NOT NULL"
                )
            ]
        self._unrecorded = {}
        for rel_path in stored:
            if rel_key(rel_path) in owned:
                continue
            try:
                st = os.lstat(self._target.root / rel_path)
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                self._unrecorded.setdefault(st.st_size, []).append(rel_path)

    def _unrecorded_copy(self, digest: str, size: int) -> str | None:
        """The path of a file with no replica row holding exactly `digest`, made durable."""
        for rel_path in self._unrecorded.get(size, []):
            if self._owner_of(rel_key(rel_path)) is not None:
                continue  # recorded since the run began
            final = self._target.root / rel_path
            if regular_file_sha256(final) == digest:  # None: gone, or not a regular file
                log.info("Using the copy a crash left at %r", rel_path)
                make_durable(final)
                self._unrecorded[size].remove(rel_path)
                return rel_path
        return None

    def _known_content(self, item: PullItem) -> str | None:
        """Content this file probably is, already copied here: worth asking the phone's hash."""
        if item.content_hash is not None and self._has_copy(item.content_hash):
            return item.content_hash
        return self._copied_by_stamp.get((item.size, item.mtime))

    def _has_copy(self, digest: str) -> bool:
        with self._db.read() as conn:
            row = conn.execute(
                "SELECT 1 FROM replica WHERE content_hash = ? AND dest_id = ?",
                (digest, self._target.dest_id),
            ).fetchone()
        return row is not None

    def _owner_of(self, key: str) -> str | None:
        with self._db.read() as conn:
            row = conn.execute(
                "SELECT content_hash FROM replica WHERE dest_id = ? AND rel_key = ?",
                (self._target.dest_id, key),
            ).fetchone()
        return None if row is None else str(row[0])

    def _record_alias(self, item: PullItem, digest: str) -> None:
        with self._db.tx() as conn:
            conn.execute(
                "UPDATE phone_file SET state = 'done', content_hash = ?, error = NULL WHERE id = ?",
                (digest, item.id),
            )

    def _record_copy(self, item: PullItem, digest: str, rel_path: str) -> None:
        now = self._clock.now()
        mime, _ = mimetypes.guess_type(item.path.rsplit("/", 1)[-1], strict=False)
        with self._db.tx() as conn:
            conn.execute(
                "INSERT INTO content (hash, size, kind, mime, created_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (hash) DO NOTHING",
                (digest, item.size, content_kind(item.path), mime, now),
            )
            conn.execute(
                "INSERT INTO replica (content_hash, dest_id, rel_path, rel_key, size, "
                "verified_at) VALUES (?, ?, ?, ?, ?, ?)",
                (digest, self._target.dest_id, rel_path, rel_key(rel_path), item.size, now),
            )
            conn.execute(
                "UPDATE phone_file SET state = 'done', content_hash = ?, rel_path = ?, "
                "error = NULL WHERE id = ?",
                (digest, rel_path, item.id),
            )
