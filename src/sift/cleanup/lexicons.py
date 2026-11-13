"""The greeting and document word lists (spec 9), YAML files the owner can edit.

They ship in `sift/resources/lexicons` and are copied into the app's support folder on first
use; the copy there is the one read. Phrases match whole words in OCR text (NFKC, casefolded);
regexes run on the upper-cased text. A file that cannot be read falls back to the shipped one.
"""

import logging
import re
import shutil
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

import yaml

log = logging.getLogger("sift.analysis")
NAMES = ("greeting", "document")


@dataclass(frozen=True)
class Lexicon:
    phrases: tuple[str, ...]
    regexes: tuple[re.Pattern[str], ...]

    def hit(self, text: str | None) -> str | None:
        """The first phrase or pattern found in `text`, or None."""
        if not text:
            return None
        for phrase in self.phrases:
            if re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text):
                return phrase
        upper = text.upper()
        for rx in self.regexes:
            found = rx.search(upper)
            if found:
                return found.group(0)
        return None


def _shipped(name: str) -> str:
    return (resources.files("sift") / "resources" / "lexicons" / f"{name}.yaml").read_text("utf-8")


def parse(text: str) -> Lexicon:
    data = yaml.safe_load(text) or {}
    phrases = tuple(str(p).casefold().strip() for p in data.get("phrases") or [] if str(p).strip())
    regexes = tuple(re.compile(str(r)) for r in data.get("regexes") or [])
    return Lexicon(phrases, regexes)


def load(name: str, folder: Path | None = None) -> Lexicon:
    """The lexicon `name` from `folder` (copied there on first use), else the shipped one."""
    if folder is None:
        return parse(_shipped(name))
    path = folder / f"{name}.yaml"
    try:
        if not path.exists():
            folder.mkdir(parents=True, exist_ok=True)
            with resources.as_file(
                resources.files("sift") / "resources" / "lexicons" / f"{name}.yaml"
            ) as src:
                shutil.copyfile(src, path)
        return parse(path.read_text("utf-8"))
    except (OSError, yaml.YAMLError, re.error, AttributeError) as e:
        log.warning("The %s word list could not be read (%s); using the built-in one", name, e)
        return parse(_shipped(name))
