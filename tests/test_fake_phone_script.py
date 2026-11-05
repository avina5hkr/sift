"""`scripts/fake_phone.py` prints a summary of the fake phone and can dump its tree as JSON."""

import json
import re
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "fake_phone.py"

EDGE_CASE_LABELS = (
    "non-UTF-8 names",
    "names with newline",
    "trailing-dot names",
    "leading-space folders",
    "zero-byte files",
    "symlinks",
    "files over 4 GiB",
    "mtime 0",
    "mtime negative",
    "mtime after 2038",
    "hidden WhatsApp folders",
    "legacy WhatsApp folder files",
    "loose files in /sdcard",
)


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )


def _counts(stdout: str) -> dict[str, int]:
    found: dict[str, int] = {}
    for line in stdout.splitlines():
        match = re.fullmatch(r"\s*(.+?):\s+(\d[\d,]*)(?:\s.*)?", line)
        if match:
            found[match.group(1)] = int(match.group(2).replace(",", ""))
    return found


def test_summary_reports_counts_and_every_edge_case(tmp_path: Path) -> None:
    out = tmp_path / "tree.json"
    result = _run("--files", "50", "--json", str(out))
    assert result.returncode == 0, result.stderr
    counts = _counts(result.stdout)

    assert counts["files"] >= 50
    assert counts["total bytes"] > 0
    assert re.search(r"total bytes:\s+[\d,]+ \(\d+(\.\d+)? [KMGT]i?B\)", result.stdout)
    for label in EDGE_CASE_LABELS:
        assert counts.get(label, 0) > 0, f"{label} not reported present:\n{result.stdout}"


def test_json_dump_lists_the_tree_without_contents(tmp_path: Path) -> None:
    out = tmp_path / "tree.json"
    result = _run("--files", "50", "--json", str(out))
    assert result.returncode == 0, result.stderr

    tree = json.loads(out.read_text(encoding="utf-8"))
    entries = tree["files"]
    assert len(entries) == _counts(result.stdout)["files"]
    for entry in entries:
        assert set(entry) == {"path", "size", "mtime", "is_symlink"}
    assert any(e["is_symlink"] for e in entries)
    assert any(e["size"] > 4 * 2**30 for e in entries)


def test_same_seed_gives_same_summary_and_files_scales() -> None:
    first = _run("--files", "50", "--seed", "3")
    again = _run("--files", "50", "--seed", "3")
    bigger = _run("--files", "120", "--seed", "3")
    assert first.stdout == again.stdout
    assert _counts(bigger.stdout)["files"] > _counts(first.stdout)["files"]
