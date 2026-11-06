"""Plain-words text for errors: the only error text the UI and job failure lists may show.

`describe(exc)` maps every Sift error type to one fixed sentence. It never includes `str(exc)`,
the exception's attributes, a path or an errno, so a name from the phone or the Mac, or text from
adb or the operating system, can never reach a screen through it (the detail goes to the log).
The one value it reads is `RemoteError.errno`, to pick between fixed sentences.
"""

from sift.backup.staging import ChecksumMismatch, IncompleteCopy, TargetExists
from sift.core.db import (
    DatabaseBusyError,
    DatabaseCorruptError,
    DatabaseTooNewError,
    MigrationError,
)
from sift.core.instance import AlreadyRunningError
from sift.device.adb_manager import (
    AdbDownloadError,
    AdbError,
    AdbPortBusy,
    AdbServerError,
    ConsentRequired,
)
from sift.device.transport import (
    DeviceDisconnected,
    DeviceTimeout,
    FileChanged,
    RemoteError,
    TransportError,
)
from sift.server.errors import ApiError

GENERIC = "Something went wrong. Details are in Logs."
FILE_NOT_FOUND = "A file or folder on the phone was not found."
FILE_NOT_ALLOWED = "The phone would not let Sift open a file or folder."
FILE_PROBLEM = "The phone reported a problem with a file or folder."

# Most specific first: the first type that matches wins.
_SENTENCES: tuple[tuple[type[BaseException], str], ...] = (
    (DeviceDisconnected, "The phone disconnected."),
    (DeviceTimeout, "The phone stopped responding."),
    (FileChanged, "A file changed while it was being copied."),
    (RemoteError, FILE_PROBLEM),  # refined by errno in describe()
    (TransportError, "Sift could not talk to the phone properly."),
    (IncompleteCopy, "A file was not copied completely, so it was not kept."),
    (ChecksumMismatch, "A copied file did not read back correctly, so it was discarded."),
    (TargetExists, "A file with that name already exists, so it was left alone."),
    (ConsentRequired, "Sift needs your permission before it downloads adb."),
    (AdbDownloadError, "Sift could not download or install adb."),
    (AdbServerError, "Sift could not reach the adb server."),
    (AdbError, "There is a problem with adb."),
    (AdbPortBusy, "Another program is using adb's port and is not answering."),
    (AlreadyRunningError, "Sift is already running."),
    (DatabaseCorruptError, "Sift's database is damaged."),
    (DatabaseTooNewError, "Sift's data was written by a newer version of Sift."),
    (DatabaseBusyError, "Sift's database is busy. Try again in a moment."),
    (MigrationError, "Sift could not update its data to this version."),
    (ApiError, "The request could not be completed."),
)

SENTENCES: frozenset[str] = frozenset(
    {GENERIC, FILE_NOT_FOUND, FILE_NOT_ALLOWED, *(text for _, text in _SENTENCES)}
)

# The phone's Linux errno values (what LST2 reports), not this Mac's numbering.
_ENOENT = 2
_EACCES = 13


def describe(exc: BaseException) -> str:
    """One fixed plain sentence for `exc`; a generic one for a type Sift does not know."""
    if isinstance(exc, RemoteError) and not isinstance(exc, FileChanged):
        if exc.errno == _ENOENT:
            return FILE_NOT_FOUND
        if exc.errno == _EACCES:
            return FILE_NOT_ALLOWED
    for kind, sentence in _SENTENCES:
        if isinstance(exc, kind):
            return sentence
    return GENERIC
