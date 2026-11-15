import importlib
import pkgutil
import re
from collections.abc import Callable

import pytest
from hypothesis import given
from hypothesis import strategies as st

import sift
from sift.analysis.cluster import Stopped as ClusteringStopped
from sift.analysis.models import ConsentNeeded, DownloadStopped, ModelDownloadError
from sift.analysis.near import NearStopped
from sift.backup.lease import SsdBusy
from sift.backup.phonepaths import UnstorablePath
from sift.backup.readers import ReaderTestCancelled
from sift.backup.replicator import SourceCorrupt
from sift.backup.scanner import ScanNotPending
from sift.backup.ssdcopy import SsdCopyEnded, SsdCopyStopped
from sift.backup.staging import ChecksumMismatch, IncompleteCopy, TargetExists
from sift.backup.verifier import NothingToRepairFrom
from sift.cleanup.calibration import NothingToCalibrate
from sift.cleanup.errors import (
    ChatBackupKept,
    ExecuteRefused,
    ManifestBlocked,
    RestoreRefused,
    RunBlocked,
)
from sift.core.db import (
    DatabaseBusyError,
    DatabaseClosedError,
    DatabaseCorruptError,
    DatabaseTooNewError,
    MigrationError,
)
from sift.core.events import BusClosed
from sift.core.instance import AlreadyRunningError
from sift.core.jobs import JobConflict, JobError, JobNotFound, JobRefused, JobStopped, NoPhone
from sift.device.adb_manager import (
    AdbDownloadError,
    AdbError,
    AdbPortBusy,
    AdbServerError,
    ConsentRequired,
)
from sift.device.session import WrongDevice
from sift.device.tracker import PhoneNotAttached, PhoneNotReady, PhonePickError
from sift.device.transport import (
    DeviceDisconnected,
    DeviceTimeout,
    FileChanged,
    RemoteError,
    TransportError,
)
from sift.device.wifi import WifiError
from sift.health.bugreport import BugReportStopped
from sift.server.errors import ApiError
from sift.services import plainwords
from sift.services.plainwords import describe
from sift.services.tasks import TaskBusy

# One way to build each Sift error type from a message.
MAKERS: dict[str, Callable[[str], BaseException]] = {
    "DeviceDisconnected": DeviceDisconnected,
    "DeviceTimeout": DeviceTimeout,
    "WrongDevice": WrongDevice,
    "FileChanged": lambda m: FileChanged(m, m),
    "RemoteError": lambda m: RemoteError(m, m),
    "RemoteError(ENOENT)": lambda m: RemoteError(m, m, errno=2),
    "RemoteError(EACCES)": lambda m: RemoteError(m, m, errno=13),
    "TransportError": TransportError,
    "IncompleteCopy": IncompleteCopy,
    "ChecksumMismatch": ChecksumMismatch,
    "TargetExists": TargetExists,
    "SourceCorrupt": SourceCorrupt,
    "ConsentNeeded": ConsentNeeded,
    "ModelDownloadError": ModelDownloadError,
    "DownloadStopped": DownloadStopped,
    "ClusteringStopped": ClusteringStopped,
    "NothingToCalibrate": NothingToCalibrate,
    "ExecuteRefused": lambda m: ExecuteRefused("refused", m),
    "ManifestBlocked": ManifestBlocked,
    "RunBlocked": RunBlocked,
    "RestoreRefused": RestoreRefused,
    "BugReportStopped": BugReportStopped,
    "ChatBackupKept": ChatBackupKept,
    "WifiError": WifiError,
    "NearStopped": NearStopped,
    "NothingToRepairFrom": NothingToRepairFrom,
    "TaskBusy": TaskBusy,
    "SsdBusy": SsdBusy,
    "SsdCopyStopped": SsdCopyStopped,
    "SsdCopyEnded": lambda m: SsdCopyEnded("failed", m),
    "ScanNotPending": ScanNotPending,
    "UnstorablePath": UnstorablePath,
    "ReaderTestCancelled": ReaderTestCancelled,
    "ConsentRequired": ConsentRequired,
    "AdbDownloadError": AdbDownloadError,
    "AdbServerError": AdbServerError,
    "AdbError": AdbError,
    "AdbPortBusy": AdbPortBusy,
    "AlreadyRunningError": AlreadyRunningError,
    "BusClosed": BusClosed,
    "PhoneNotAttached": PhoneNotAttached,
    "PhoneNotReady": PhoneNotReady,
    "PhonePickError": PhonePickError,
    "NoPhone": NoPhone,
    "JobNotFound": JobNotFound,
    "JobConflict": JobConflict,
    "JobError": JobError,
    "JobRefused": JobRefused,
    "JobStopped": JobStopped,
    "DatabaseCorruptError": DatabaseCorruptError,
    "DatabaseTooNewError": DatabaseTooNewError,
    "DatabaseBusyError": DatabaseBusyError,
    "DatabaseClosedError": DatabaseClosedError,
    "MigrationError": MigrationError,
    "ApiError": lambda m: ApiError(500, "x", m, m),
    "unknown": ValueError,
    "oserror": lambda m: OSError(2, m, m),
}

LEAKY = re.compile(r"\d|/|\\|errno|ENOENT|EACCES|No such file|Permission denied", re.IGNORECASE)


def _sift_exception_types() -> set[type[BaseException]]:
    found: set[type[BaseException]] = set()
    for info in pkgutil.walk_packages(sift.__path__, "sift."):
        if info.name.endswith("__main__"):
            continue
        try:
            module = importlib.import_module(info.name)
        except ImportError:
            continue
        for value in vars(module).values():
            if (
                isinstance(value, type)
                and issubclass(value, BaseException)
                and value.__module__.startswith("sift.")
            ):
                found.add(value)
    return found


def test_the_known_sentences_are_plain() -> None:
    assert describe(DeviceDisconnected("x")) == "The phone disconnected."
    assert describe(FileChanged("x", "/p")) == "A file changed while it was being copied."
    assert describe(ValueError("x")) == plainwords.GENERIC


def test_every_sift_error_type_has_its_own_sentence() -> None:
    types = _sift_exception_types()
    assert {DeviceDisconnected, AdbPortBusy, ApiError, TargetExists} <= types  # the walk works
    covered = tuple(kind for kind, _ in plainwords._SENTENCES)
    missing = sorted(t.__name__ for t in types if not issubclass(t, covered))
    assert missing == []


def test_subclasses_get_their_own_sentence_not_their_parents() -> None:
    assert describe(FileChanged("x")) != describe(RemoteError("x"))
    assert describe(DeviceDisconnected("x")) != describe(TransportError("x"))
    assert describe(WrongDevice("x")) != describe(TransportError("x"))
    assert describe(ConsentRequired("x")) != describe(AdbError("x"))


def test_remote_errors_are_told_apart_by_errno_without_showing_it() -> None:
    assert describe(RemoteError("m", "/p", errno=2)) == plainwords.FILE_NOT_FOUND
    assert describe(RemoteError("m", "/p", errno=13)) == plainwords.FILE_NOT_ALLOWED
    assert describe(RemoteError("m", "/p", errno=5)) == plainwords.FILE_PROBLEM
    assert describe(RemoteError("m", "/p")) == plainwords.FILE_PROBLEM


def test_every_sentence_is_leak_free() -> None:
    for sentence in plainwords.SENTENCES:
        assert LEAKY.search(sentence) is None, sentence


path_text = st.builds(
    lambda parts, tail: "/" + "/".join(parts) + tail,
    st.lists(st.text(st.characters(blacklist_categories=["Cs"]), min_size=1, max_size=8)),
    st.sampled_from(
        [
            "",
            ": [Errno 2] No such file or directory",
            " (errno 13: Permission denied)",
            ": ENOENT",
            "\nEACCES",
            " 'C:\\Users\\x'",
        ]
    ),
)


@given(
    kind=st.sampled_from(sorted(MAKERS)),
    message=st.one_of(
        path_text,
        st.text(max_size=40),
        st.builds(lambda n: f"[Errno {n}] failed: '/Users/someone/x'", st.integers(0, 200)),
    ),
)
def test_plainwords_never_leaks_errno_or_paths(kind: str, message: str) -> None:
    exc = MAKERS[kind](message)
    out = describe(exc)
    assert out in plainwords.SENTENCES
    assert LEAKY.search(out) is None
    marker = "/Users/someone/private-name"
    assert marker not in describe(MAKERS[kind](marker + message))


@given(code=st.one_of(st.none(), st.integers(-5, 400)), message=path_text)
def test_remote_error_errno_picks_a_fixed_sentence(code: int | None, message: str) -> None:
    out = describe(RemoteError(message, message, errno=code))
    assert out in {plainwords.FILE_NOT_FOUND, plainwords.FILE_NOT_ALLOWED, plainwords.FILE_PROBLEM}


def test_the_makers_cover_every_sentence_type() -> None:
    covered = {type(m("x")) for m in MAKERS.values()}
    for kind, _ in plainwords._SENTENCES:
        assert kind in covered, kind.__name__


@pytest.mark.parametrize("kind", sorted(MAKERS))
def test_describe_never_raises(kind: str) -> None:
    assert isinstance(describe(MAKERS[kind]("")), str)
