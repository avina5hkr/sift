"""Property tests of the backup engine: P1 (I5), P2 (I2), P6, and one set ticked.

Each example builds a small fake phone (a sample of `build_fake_phone`, plus generated files
with hostile names, some sharing their bytes) and backs it up through a `JobManager`, as the
app does.

An `App` is one life of the app over the same folders: it opens the database, runs recovery,
and starts backup jobs. A crash is simulated at a crash point (`sift.core.crashpoints`) by
raising `SimulatedCrash`; that `App` is then dropped and a new one is built from what is on
disk and in the database, as after a real crash. Between lives the phone may change (a file
renamed, or copied to a newer file).

P1 crashes with one reader: with several, the files still in flight would go on after a
simulated crash (a real crash stops them too) and could store what a real crash never could.
The SIGKILL tests kill a real child process, with three readers.

The `ci` profile runs 200 examples of each property; a normal run keeps to a few, so the whole
suite stays quick. The SIGKILL tests are marked `slow` (they still run by default).
"""

import hashlib
import json
import os
import random
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, event, example, given, settings
from hypothesis import strategies as st

from sift import __version__
from sift.backup.phonepaths import from_db
from sift.backup.puller import CRASH_POINT, STAGING_DIR
from sift.backup.recovery import recover
from sift.backup.run import run_backup
from sift.backup.scanner import confirm_scan
from sift.backup.sets import resolve_roots, set_for_path
from sift.backup.skiprules import SkipRules, is_skipped
from sift.backup.staging import PART_SUFFIX
from sift.core import crashpoints
from sift.core.clock import SystemClock
from sift.core.config import Config, load_config, phone_path_key
from sift.core.context import AppContext
from sift.core.db import Database
from sift.core.events import EventBus
from sift.core.jobs import JobControl, JobManager, JobSnapshot, JobState
from sift.core.migrations import apply_migrations
from sift.core.paths import AppPaths
from sift.device.fake_phone import (
    BIG_VIDEO,
    NON_UTF8_FILE,
    WA_MEDIA,
    FakePhone,
    build_fake_phone,
)
from sift.device.fake_transport import FakeTransport, FaultPlan
from sift.device.session import DeviceSession, PhoneIdentity
from sift.services.plainwords import describe

ROOT = Path(__file__).resolve().parents[2]
SCAN_STAGED = "scanner.staged"
SCAN_APPLYING = "scanner.applying"
CRASH_POINTS = (CRASH_POINT, SCAN_STAGED, SCAN_APPLYING)
DEVICE_UUID = "dev-1"
OLD = 1_690_000_000  # mtime of the generated files: long settled
RUN_TIMEOUT_S = 30.0
CLEAN_RUNS = 3  # runs with no crash allowed to finish the backup

# The `ci` profile runs 200 examples of each property; a normal run keeps to a few.
EXAMPLES = settings().max_examples if os.environ.get("HYPOTHESIS_PROFILE") == "ci" else 8
PROPERTY = settings(
    max_examples=EXAMPLES,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)

SETS = (
    "Camera",
    "Screenshots",
    "Pictures and Movies",
    "WhatsApp media",
    "WhatsApp Business",
    "Documents",
    "Downloads",
    "Recordings",
    "Loose files",
)
# A folder of each set where generated files go.
EXTRA_FOLDERS = (
    "/sdcard/DCIM/Camera",
    "/sdcard/DCIM/Screenshots",
    f"{WA_MEDIA}/WhatsApp Images",
    "/sdcard/Documents",
    "/sdcard/Download",
    "/sdcard",
)
# Bytes several generated files share, so content is met more than once.
SHARED = (b"shared bytes, one" * 9, b"shared bytes, two" * 31, b"three")

# Crash counts seen across one property's examples, by crash point.
CRASHED: Counter[str] = Counter()


class SimulatedCrash(BaseException):
    """A crash: not an Exception, so nothing on the way treats it as a failed file."""

    def __init__(self, point: str) -> None:
        super().__init__(point)
        self.point = point


# -- the phone ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Extra:
    folder: str
    name: str
    shared: int | None  # an index into SHARED, or None for bytes of its own


@dataclass(frozen=True)
class PhoneSpec:
    seed: int
    keep_pct: int  # how much of build_fake_phone's tree is kept
    extras: tuple[Extra, ...] = ()


def _same_name(a: str, b: str) -> bool:
    return phone_path_key(a) == phone_path_key(b)


def make_phone(spec: PhoneSpec) -> FakePhone:
    """A sample of `build_fake_phone(10, seed)` with small files, plus `spec.extras`."""
    phone = build_fake_phone(10, seed=spec.seed)
    phone.remove(BIG_VIDEO)
    rng = random.Random(spec.seed)
    for path in sorted(phone.files):
        file = phone.files[path]
        if file.is_symlink_to is not None:
            continue
        if rng.randrange(100) >= spec.keep_pct:
            phone.remove(path)
        elif file.data is None or file.size > 4096:  # keep the copies small
            seed = hashlib.sha256(path.encode("utf-8", "surrogateescape")).digest()
            phone.modify(path, seed * 40, file.mtime)
    for i, extra in enumerate(spec.extras):
        path = f"{extra.folder.rstrip('/')}/{extra.name}"
        # Phone storage ignores case: two names that differ only by case cannot both exist.
        siblings = phone.children(extra.folder) if phone.is_dir(extra.folder) else []
        taken = [*phone.files, *(e.path for e in siblings)]
        if any(_same_name(path, other) for other in taken):
            continue
        if extra.shared is not None:
            data = SHARED[extra.shared]
        else:
            data = b"bytes of its own: " + path.encode("utf-8", "surrogateescape")
        phone.add_file(path, data, mtime=OLD + i)
    return phone


NAME_CHARS = st.sampled_from(
    [
        *"aAzZ09 ._-~():\\*?",
        *("\u00e9", "\u0301", "\u00df", "\u0130", "\u202e", "\U0001f389"),
        *("\udce9", "\udc80", "\n", "\x7f", "\t"),
    ]
)
names = st.builds(
    str.__add__,
    st.text(NAME_CHARS, min_size=1, max_size=10),
    st.sampled_from(["", ".jpg", ".JPG", ".mp4", ".pdf", ".txt"]),
).filter(lambda n: n not in (".", ".."))
extras = st.builds(
    Extra,
    folder=st.sampled_from(EXTRA_FOLDERS),
    name=names,
    shared=st.none() | st.integers(0, len(SHARED) - 1),
)
phone_specs = st.builds(
    PhoneSpec,
    seed=st.integers(0, 2**16),
    keep_pct=st.integers(5, 25),
    extras=st.lists(extras, max_size=6).map(tuple),
)
set_choices = st.sets(st.sampled_from(SETS), min_size=1).map(lambda s: tuple(sorted(s)))


@dataclass(frozen=True)
class Faults:
    """A FaultPlan to give each life's transport (each one-shot fault fires once per life)."""

    disconnect_after_bytes: int | None = None
    short_reads: bool = False
    wrong_sha256: bool = False  # for every file: a moved file is downloaded again
    offline_s: float | None = None
    # Not transient: a file with these never finishes, so only P2 uses them.
    grows: int = 0  # how many files raise FileChanged at their old end
    hangs: int = 0  # how many files never yield a byte (DeviceTimeout, then failed)

    def plan(self, phone: FakePhone) -> FaultPlan:
        paths = sorted(p for p, f in phone.files.items() if f.is_symlink_to is None)
        rng = random.Random(len(paths))
        return FaultPlan(
            disconnect_after_bytes=self.disconnect_after_bytes,
            short_reads=self.short_reads,
            wrong_sha256=set(paths) if self.wrong_sha256 else set(),
            size_change_mid_read=set(rng.sample(paths, min(self.grows, len(paths)))),
            hang_on=set(rng.sample(paths, min(self.hangs, len(paths)))),
        )


transient_faults = st.builds(
    Faults,
    disconnect_after_bytes=st.none() | st.integers(0, 20_000),
    short_reads=st.booleans(),
    wrong_sha256=st.booleans(),
    offline_s=st.none() | st.just(0.02),
)
any_faults = st.builds(
    Faults,
    disconnect_after_bytes=st.none() | st.integers(0, 20_000),
    short_reads=st.booleans(),
    wrong_sha256=st.booleans(),
    offline_s=st.none() | st.just(0.02),
    grows=st.integers(0, 2),
    hangs=st.integers(0, 1),
)


# -- one life of the app ------------------------------------------------------------------------


class Tracker:
    def __init__(self, session: DeviceSession) -> None:
        self.current = session

    def session(self) -> DeviceSession:
        return self.current


@dataclass
class World:
    """What outlives a crash: the folders on the Mac, and the phone."""

    base: Path
    phone: FakePhone
    sets: tuple[str, ...]
    readers: int = 1
    faults: Faults = field(default_factory=Faults)

    @property
    def paths(self) -> AppPaths:
        return AppPaths(support=self.base / "support", cache=self.base / "cache")

    @property
    def root(self) -> Path:
        return self.base / "backup"

    def config(self) -> Config:
        config, _warnings = load_config(self.paths.config)
        config.backup.mac_root = str(self.root)
        config.phone.parallel_reads_usb = self.readers
        return config


class Watched:
    """A JobControl that calls `on_step` before each progress report (one per file, at least)."""

    def __init__(self, control: JobControl, on_step: Callable[[], None]) -> None:
        self._control = control
        self._on_step = on_step

    def __getattr__(self, name: str) -> Any:
        return getattr(self._control, name)

    def report(self, *args: Any, **kwargs: Any) -> None:
        self._on_step()
        self._control.report(*args, **kwargs)


@dataclass
class RunResult:
    snapshot: JobSnapshot
    crashed: str | None
    bytes_read: int
    opened: int


class App:
    """One life of the app over a World: open, recover, then run backups."""

    def __init__(self, world: World) -> None:
        self.world = world
        paths = world.paths
        paths.ensure()
        self.db = Database(paths.db)
        self.db.open()
        apply_migrations(self.db, paths.db_backups)
        recover(self.db)
        with self.db.tx() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO device (uuid, serial) VALUES (?, 'FAKE-0001')",
                (DEVICE_UUID,),
            )
        self.transport = FakeTransport(
            world.phone, world.faults.plan(world.phone), stall_timeout_s=0.005
        )
        session = DeviceSession(
            self.transport, PhoneIdentity.from_info(self.transport.info()), DEVICE_UUID
        )
        if world.faults.offline_s is not None:  # once the phone is known: the job meets it
            self.transport.go_offline(world.faults.offline_s)
        self.ctx = AppContext(
            paths=paths,
            config=world.config(),
            config_warnings=[],
            db=self.db,
            bus=EventBus(),
            token="t0k",
            allowed_hosts=frozenset(),
            version=__version__,
            web_dist=None,
            tracker=Tracker(session),  # type: ignore[arg-type]
        )
        self.jobs = JobManager(
            self.ctx,
            clock=SystemClock(),
            runner=self._runner,
            describe=describe,
            sleep=lambda _s: None,
            poll_s=0.005,
        )
        self.on_step: Callable[[], None] = lambda: None
        self._crashed: str | None = None

    def close(self) -> None:
        crashpoints.install(None)
        self.db.close()

    def _runner(self, control: JobControl) -> None:
        try:
            run_backup(
                self.ctx,
                Watched(control, self.on_step),  # type: ignore[arg-type]
                control.sets,
                control.dry_run,
                sleep=lambda _s: None,
                reconnect_pause_s=0.0,
            )
        except SimulatedCrash as crash:  # the process "died": nothing more runs in this life
            self._crashed = crash.point

    def backup(
        self,
        crash: tuple[str, int] | None = None,
        on_hit: Callable[[str], None] | None = None,
    ) -> RunResult:
        """One backup job of the world's sets; with `crash`, die at that point's nth hit."""
        hits: Counter[str] = Counter()
        lock = threading.Lock()

        def hook(name: str) -> None:
            if on_hit is not None:
                on_hit(name)
            with lock:
                hits[name] += 1
                due = crash is not None and crash == (name, hits[name])
            if due:
                raise SimulatedCrash(name)

        crashpoints.install(hook)
        served, journal = self.transport.bytes_served, len(self.transport.journal)
        job_id = self.jobs.start_backup(self.world.sets)
        deadline = time.monotonic() + RUN_TIMEOUT_S
        try:
            while not self.jobs.wait_finished(job_id, 0.01):
                assert time.monotonic() < deadline, "the backup did not finish"
                if self._snapshot(job_id).state is JobState.awaiting_confirmation:
                    try:
                        self._confirm()  # the owner confirms what the guard stopped
                    except SimulatedCrash as crash:  # died applying it: the job goes too
                        self._crashed = crash.point
                        self.jobs.stop(job_id)
        finally:
            crashpoints.install(None)
        opened = sum(1 for e in self.transport.journal[journal:] if e.op == "open_read")
        return RunResult(
            self._snapshot(job_id),
            self._crashed,
            self.transport.bytes_served - served,
            opened,
        )

    def _snapshot(self, job_id: int) -> JobSnapshot:
        return next(s for s in self.jobs.snapshot() if s.id == job_id)

    def _confirm(self) -> None:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT id FROM scan WHERE state = 'awaiting_confirmation'"
            ).fetchone()
        if row is not None:
            confirm_scan(self.db, int(row[0]))


@contextmanager
def app(world: World) -> Iterator[App]:
    life = App(world)
    try:
        yield life
    finally:
        life.close()


@contextmanager
def world_in(tmp: Path, **kwargs: Any) -> Iterator[World]:
    with tempfile.TemporaryDirectory(dir=tmp) as base:
        yield World(Path(base), **kwargs)


# -- what is on disk and in the database (read independently of the app) ------------------------


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def tree(root: Path) -> dict[str, bytes]:
    """Every file of the backup folder but the staging folder, by relative path."""
    out: dict[str, bytes] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != STAGING_DIR]
        for name in filenames:
            path = Path(dirpath) / name
            out[path.relative_to(root).as_posix()] = path.read_bytes()
    return out


def part_files(root: Path) -> list[str]:
    return [
        str(Path(d) / n)
        for d, _dirs, files in os.walk(root)
        for n in files
        if n.endswith(PART_SUFFIX)
    ]


@contextmanager
def read_db(world: World) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(world.paths.db)  # read-write, so it can recover a killed writer's WAL
    try:
        conn.execute("PRAGMA query_only = ON")
        yield conn
    finally:
        conn.close()


def replica_problems(world: World) -> list[str]:
    """I2: each replica row's file exists, is a regular file, and has the content's SHA-256."""
    if not world.paths.db.exists():
        return []
    with read_db(world) as conn:
        rows = conn.execute(
            "SELECT d.root_path, r.rel_path, r.content_hash, c.hash FROM replica r "
            "JOIN destination d ON d.id = r.dest_id LEFT JOIN content c ON c.hash = r.content_hash"
        ).fetchall()
    problems = []
    for root, rel, digest, content in rows:
        path = Path(root) / rel
        if content is None:
            problems.append(f"{rel}: no content row")
        elif path.is_symlink() or not path.is_file():
            problems.append(f"{rel}: no file")
        elif sha(path.read_bytes()) != digest:
            problems.append(f"{rel}: other bytes than {digest[:8]}")
    return problems


def expected_files(world: World) -> dict[str, str]:
    """Phone path -> SHA-256 of each phone file the ticked sets back up (skip rules applied)."""
    config = world.config()
    roots = resolve_roots(config.backup.sets, world.sets)
    rules = SkipRules.from_config(config)
    out = {}
    for path, file in world.phone.files.items():
        if file.is_symlink_to is not None or set_for_path(path, roots) is None:
            continue
        if is_skipped(path, is_dir=False, size=file.size, rules=rules) is None:
            out[path] = world.phone.sha256_of(path)
    return out


def rows_of(world: World, where: str = "1") -> list[tuple[str, str, str | None, str | None]]:
    """(phone path, state, content hash, set) of the phone_file rows `where` selects."""
    with read_db(world) as conn:
        rows = conn.execute(
            "SELECT remote_path, state, content_hash, set_name FROM phone_file WHERE " + where
        ).fetchall()
    return [(from_db(r[0]), r[1], r[2], r[3]) for r in rows]


def unrecorded_files(world: World) -> set[str]:
    """Final files in the backup folder that no replica row records."""
    with read_db(world) as conn:
        recorded = {r[0] for r in conn.execute("SELECT rel_path FROM replica")}
    return set(tree(world.root)) - recorded


def unfinished(world: World) -> int:
    with read_db(world) as conn:
        (count,) = conn.execute(
            "SELECT COUNT(*) FROM phone_file WHERE present = 1 "
            "AND state IN ('discovered', 'pulling', 'failed')"
        ).fetchone()
    return int(count)


def _hash8_base(name: str, digest: str) -> str | None:
    """The base name a `_HASH8` (or `_HASH8_N`) name was made from, or None if it is not one."""
    match = re.search(rf"_{digest[:8]}(?:_\d+)?(?=\.[^.]*$|$)", name)
    return None if match is None else name[: match.start()] + name[match.end() :]


def assert_backed_up(world: World) -> None:
    """P1: the backup holds exactly the phone's content for the ticked sets, each once."""
    expected = expected_files(world)
    files = tree(world.root) if world.root.exists() else {}
    hashes = {rel: sha(data) for rel, data in files.items()}
    assert part_files(world.root) == []
    stored = Counter(hashes.values())
    assert [h for h, n in stored.items() if n > 1] == [], "content stored twice"
    for rel, digest in hashes.items():
        parent, _, name = rel.rpartition("/")
        base = _hash8_base(name, digest)
        if base is not None:
            sibling = f"{parent}/{base}" if parent else base
            assert hashes.get(sibling) != digest, f"{rel} holds the bytes of {sibling}"
    assert set(stored) == set(expected.values())
    rows = rows_of(world, "present = 1 AND state != 'skipped'")
    assert {r[0] for r in rows} == set(expected)
    for path, state, digest, _set in rows:
        assert (state, digest) == ("done", expected[path]), path
    assert replica_problems(world) == []


# -- mutations between lives --------------------------------------------------------------------


def _in_flight(world: World) -> list[str]:
    """The phone files a crash left `pulling` (read before the next life's recovery)."""
    return sorted(r[0] for r in rows_of(world, "state = 'pulling'"))


def mutate(world: World, kind: str, pick: int, in_flight: Sequence[str]) -> None:
    """Rename a file (in its folder), or copy it to a newer file beside it.

    `pick` 0 means the file the crash cut short, when there is one; otherwise a backed-up file.
    """
    candidates = sorted(expected_files(world))
    if not candidates:
        return
    path = in_flight[0] if pick == 0 and in_flight else candidates[pick % len(candidates)]
    if path not in world.phone.files:
        return
    file = world.phone.files[path]
    folder, _, name = path.rpartition("/")
    new = f"{folder}/{'moved' if kind == 'move' else 'copy of'} {name}"
    if any(_same_name(new, p) for p in world.phone.files):
        return
    data = file.data or b""
    if kind == "move":
        world.phone.remove(path)
        world.phone.add_file(new, data, mtime=file.mtime)
    else:
        world.phone.add_file(new, data, mtime=file.mtime + 1)  # newer: copied first


# -- P1 (I5): crash and resume until done -------------------------------------------------------


crash_plans = st.lists(
    st.tuples(
        st.sampled_from(CRASH_POINTS),
        st.integers(1, 4),
        st.none() | st.tuples(st.sampled_from(["move", "copy"]), st.integers(0, 40)),
    ),
    max_size=3,
)

PINNED_SPEC = PhoneSpec(
    seed=7,
    keep_pct=10,
    extras=(
        Extra("/sdcard/DCIM/Camera", "IMG_1.jpg", 0),
        Extra("/sdcard/DCIM/Camera", "img_1.JPG.", 1),
        Extra("/sdcard/Documents", "IMG_1.jpg", 0),
        Extra("/sdcard/DCIM/Camera", "IMG_\udce9\udc80.jpg", None),  # not UTF-8
    ),
)


def _crash_and_resume(
    tmp: Path,
    spec: PhoneSpec,
    sets: tuple[str, ...],
    faults: Faults,
    plan: list[tuple[str, int, tuple[str, int] | None]],
) -> None:
    with world_in(tmp, phone=make_phone(spec), sets=sets, faults=faults) as world:
        for point, nth, mutation in plan:
            with app(world) as life:
                result = life.backup(crash=(point, nth))
            if result.crashed is None:
                continue
            CRASHED[result.crashed] += 1
            event(f"crashed at {result.crashed}")
            if mutation is not None:
                mutate(world, *mutation, _in_flight(world))
        for _ in range(CLEAN_RUNS):
            with app(world) as life:
                result = life.backup()
            assert result.crashed is None
            if result.snapshot.state is JobState.done and unfinished(world) == 0:
                break
        assert result.snapshot.state is JobState.done, result.snapshot.error
        assert_backed_up(world)
        assert not any(e.mutating for e in life.transport.journal)  # I7


def test_p1_crash_and_resume_until_done_stores_each_content_once(tmp_path: Path) -> None:
    CRASHED.clear()

    @PROPERTY
    @given(
        spec=phone_specs,
        sets=set_choices,
        faults=transient_faults,
        plan=crash_plans,
    )
    @example(  # the crash between the rename and the rows, then that file is renamed
        spec=PINNED_SPEC,
        sets=("Camera", "Documents"),
        faults=Faults(),
        plan=[(CRASH_POINT, 1, ("move", 0))],
    )
    @example(  # the same crash, then that file is copied to a newer one (copied first)
        spec=PINNED_SPEC,
        sets=("Camera", "Documents"),
        faults=Faults(),
        plan=[(CRASH_POINT, 1, ("copy", 0)), (CRASH_POINT, 2, None)],
    )
    def p1(
        spec: PhoneSpec,
        sets: tuple[str, ...],
        faults: Faults,
        plan: list[tuple[str, int, tuple[str, int] | None]],
    ) -> None:
        _crash_and_resume(tmp_path, spec, sets, faults, plan)

    p1()
    assert CRASHED[CRASH_POINT] >= 1


# -- P2 (I2): after every step, every replica row is backed by its file -------------------------


def test_p2_every_replica_row_points_to_its_bytes_at_every_step(tmp_path: Path) -> None:
    @PROPERTY
    @given(
        spec=phone_specs,
        sets=set_choices,
        readers=st.integers(1, 3),
        faults=any_faults,
        plan=st.lists(st.tuples(st.sampled_from(CRASH_POINTS), st.integers(1, 3)), max_size=2),
    )
    def p2(
        spec: PhoneSpec,
        sets: tuple[str, ...],
        readers: int,
        faults: Faults,
        plan: list[tuple[str, int]],
    ) -> None:
        with world_in(
            tmp_path, phone=make_phone(spec), sets=sets, readers=readers, faults=faults
        ) as world:
            problems: list[str] = []
            steps = 0

            def check() -> None:
                nonlocal steps
                steps += 1
                problems.extend(replica_problems(world))

            def at_hit(name: str) -> None:
                if name == CRASH_POINT:  # outside any transaction: the database can be read
                    check()

            for crash in [*plan, None]:
                with app(world) as life:
                    check()  # after recovery
                    life.on_step = check
                    life.backup(crash=crash, on_hit=at_hit)
                check()
            assert problems == []
            event(f"steps checked: {min(steps // 10 * 10, 50)}+")

    p2()


# -- P6: a second backup with no phone changes transfers nothing --------------------------------


def test_p6_a_second_backup_with_no_changes_transfers_nothing(tmp_path: Path) -> None:
    @PROPERTY
    @given(spec=phone_specs, sets=set_choices, readers=st.integers(1, 3))
    def p6(spec: PhoneSpec, sets: tuple[str, ...], readers: int) -> None:
        with world_in(tmp_path, phone=make_phone(spec), sets=sets, readers=readers) as world:
            with app(world) as life:
                first = life.backup()
                assert first.snapshot.state is JobState.done
                assert_backed_up(world)
                second = life.backup()
            with app(world) as life:  # and after a restart
                third = life.backup()
            for again in (second, third):
                assert again.snapshot.state is JobState.done
                assert (again.bytes_read, again.opened) == (0, 0)
                assert again.snapshot.files_total == 0
            assert_backed_up(world)

    p6()


# -- one set ticked -----------------------------------------------------------------------------


def test_a_run_with_one_set_ticked_writes_rows_only_for_that_set(tmp_path: Path) -> None:
    @PROPERTY
    @given(spec=phone_specs, ticked=st.sampled_from(SETS))
    def one_set(spec: PhoneSpec, ticked: str) -> None:
        with world_in(tmp_path, phone=make_phone(spec), sets=(ticked,)) as world:
            with app(world) as life:
                assert life.backup().snapshot.state is JobState.done
            rows = rows_of(world)
            assert {r[3] for r in rows} <= {ticked}
            with read_db(world) as conn:
                stored = {r[0] for r in conn.execute("SELECT content_hash FROM replica")}
                contents = {r[0] for r in conn.execute("SELECT hash FROM content")}
            assert stored == contents == set(expected_files(world).values())
            assert_backed_up(world)

    one_set()


def test_a_file_name_that_is_not_utf8_is_backed_up(tmp_path: Path) -> None:
    phone = make_phone(PhoneSpec(seed=1, keep_pct=0))
    phone.add_file(NON_UTF8_FILE, b"non-utf8 name", mtime=OLD)
    with world_in(tmp_path, phone=phone, sets=("Downloads",)) as world:
        with app(world) as life:
            result = life.backup()
        assert result.snapshot.state is JobState.done, result.snapshot.error
        assert_backed_up(world)
        assert tree(world.root)["Phone/Download/caf%E9.jpg"] == b"non-utf8 name"


# -- the same, with a real SIGKILL --------------------------------------------------------------

KILL_SPEC = PhoneSpec(
    seed=11,
    keep_pct=40,
    extras=(
        Extra("/sdcard/DCIM/Camera", "IMG_1.jpg", 0),
        Extra("/sdcard/Documents", "img_1.jpg", 0),
        Extra("/sdcard/DCIM/Camera", "a:b.jpg", 1),
        Extra("/sdcard/DCIM/Camera", "a_b.jpg", None),
        Extra("/sdcard/DCIM/Camera", "caf\udce9.jpg", 2),
    ),
)
KILL_SETS = ("Camera", "Documents", "WhatsApp media", "Loose files")


def _kill_phone(moves: Sequence[Sequence[str]]) -> FakePhone:
    """The SIGKILL tests' phone, with `moves` ([old, new] renames) applied in order."""
    phone = make_phone(KILL_SPEC)
    for old, new in moves:
        file = phone.files[old]
        phone.remove(old)
        phone.add_file(new, file.data or b"", mtime=file.mtime)
    return phone


def _kill_world(base: Path, moves: Sequence[Sequence[str]]) -> World:
    return World(base, _kill_phone(moves), KILL_SETS, readers=3)


def child_main() -> None:
    """One life of the app in a child process; `sys.argv[1]` is the JSON `_child` writes.

    With `kill_at` [point, n], the process SIGKILLs itself at that crash point's nth hit.
    """
    args = json.loads(sys.argv[1])
    world = _kill_world(Path(args["base"]), args["moves"])
    kill_at = args["kill_at"]
    hits: Counter[str] = Counter()
    lock = threading.Lock()

    def die(name: str) -> None:
        with lock:
            hits[name] += 1
            if kill_at is not None and [name, hits[name]] == kill_at:
                os.kill(os.getpid(), signal.SIGKILL)

    with app(world) as life:
        life.transport.faults.slow_read_bps = args["slow_bps"]
        life.backup(on_hit=die)


def _child(
    base: Path,
    moves: Sequence[Sequence[str]] = (),
    *,
    kill_at: list[Any] | None = None,
    slow_bps: int | None = None,
) -> subprocess.Popen[bytes]:
    args = {"base": str(base), "moves": list(moves), "kill_at": kill_at, "slow_bps": slow_bps}
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from tests.backup.test_properties import child_main; child_main()",
            json.dumps(args),
        ],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def _killed(child: subprocess.Popen[bytes]) -> None:
    _out, err = child.communicate(timeout=RUN_TIMEOUT_S)
    assert child.returncode == -signal.SIGKILL, err.decode(errors="replace")


@pytest.mark.slow
def test_sigkill_and_resume_until_done_stores_each_content_once(tmp_path: Path) -> None:
    """P1 with real kills, three readers, and the files cut short renamed before the resume."""
    moves: list[list[str]] = []
    for kill_at in ([CRASH_POINT, 2], [SCAN_STAGED, 1], [CRASH_POINT, 1]):
        _killed(_child(tmp_path, moves, kill_at=kill_at))
        world = _kill_world(tmp_path, moves)
        assert replica_problems(world) == []  # I2 holds at the moment of the kill
        if kill_at[0] == CRASH_POINT:  # killed between a rename and its rows
            assert unrecorded_files(world)
        for path in _in_flight(world):
            folder, _, name = path.rpartition("/")
            moves.append([path, f"{folder}/moved {name}"])
    world = _kill_world(tmp_path, moves)
    with app(world) as life:
        result = life.backup()
    assert result.snapshot.state is JobState.done, result.snapshot.error
    assert_backed_up(world)


@pytest.mark.slow
def test_sigkill_at_any_moment_leaves_every_replica_row_backed_by_its_bytes(
    tmp_path: Path,
) -> None:
    """P2 with real kills at moments the kernel picks, mid-copy, then a resume to the end."""
    world = _kill_world(tmp_path, [])
    stored = kills = 0
    for _round in range(3):
        child = _child(tmp_path, slow_bps=20_000)
        deadline = time.monotonic() + RUN_TIMEOUT_S
        while child.poll() is None and time.monotonic() < deadline:
            if world.paths.db.exists():
                with read_db(world) as conn:
                    try:
                        (count,) = conn.execute("SELECT COUNT(*) FROM replica").fetchone()
                    except sqlite3.OperationalError:  # the schema is not there yet
                        count = 0
                if count >= stored + 3:
                    child.send_signal(signal.SIGKILL)
                    kills += 1
                    break
            time.sleep(0.005)
        child.communicate(timeout=RUN_TIMEOUT_S)
        assert child.returncode in (0, -signal.SIGKILL)
        assert replica_problems(world) == []
        with read_db(world) as conn:
            (stored,) = conn.execute("SELECT COUNT(*) FROM replica").fetchone()
    assert kills >= 1
    with app(world) as life:
        assert life.backup().snapshot.state is JobState.done
    assert replica_problems(world) == []
    assert_backed_up(world)
