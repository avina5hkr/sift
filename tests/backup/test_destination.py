"""The phone's folder name under the backup folder: never one of Sift's own folders."""

import pytest

from sift.backup.destination import sanitize_label
from sift.backup.puller import STAGING_DIR
from sift.backup.run import device_label
from sift.core.config import Config
from sift.core.db import Database


@pytest.mark.parametrize("label", [".staging", ".STAGING", ".Staging", " .staging ", ".hidden"])
def test_a_label_never_starts_with_a_dot(label: str) -> None:
    safe = sanitize_label(label)
    assert not safe.startswith(".")
    assert safe.casefold() != STAGING_DIR.casefold()


@pytest.mark.parametrize("label", ["Phone", "My_ phone_", "Galaxy S9", "_staging"])
def test_a_safe_label_is_kept(label: str) -> None:
    assert sanitize_label(label) == label


def test_a_stored_or_configured_staging_label_is_never_used_as_is(db: Database) -> None:
    with db.tx() as conn:
        conn.execute("INSERT INTO device (uuid, serial, label) VALUES ('d1', 'S1', '.Staging')")
    config = Config()
    config.backup.device_label = ".staging"
    assert device_label(db, config, "d1") == "_Staging"
    assert device_label(db, config, None) == "_staging"
