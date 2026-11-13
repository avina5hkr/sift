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


_SPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class Lexicon:
    phrases: tuple[str, ...]
    regexes: tuple[re.Pattern[str], ...]
    words: re.Pattern[str] | None = None  # every phrase as one whole-word alternation

    def hit(self, text: str | None) -> str | None:
        """The first phrase or pattern found in `text`, or None."""
        if not text:
            return None
        flat = _SPACE.sub(" ", text)  # "account\nno" matches "account no"
        if self.words is not None:
            found = self.words.search(flat)
            if found:
                return found.group(1)
        upper = flat.upper()
        for rx in self.regexes:
            found = rx.search(upper)
            if found:
                return found.group(0)
        return None


def _shipped(name: str) -> str:
    return (resources.files("sift") / "resources" / "lexicons" / f"{name}.yaml").read_text("utf-8")


def parse(text: str) -> Lexicon:
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise ValueError("the word list must have phrases and regexes")
    raw_phrases, raw_regexes = data.get("phrases") or [], data.get("regexes") or []
    if not isinstance(raw_phrases, list) or not isinstance(raw_regexes, list):
        raise ValueError("phrases and regexes must be lists")
    phrases = tuple(
        dict.fromkeys(
            _SPACE.sub(" ", str(p).casefold().strip()) for p in raw_phrases if str(p).strip()
        )
    )
    regexes = tuple(re.compile(str(r)) for r in raw_regexes)
    ordered = sorted(phrases, key=len, reverse=True)  # the longest phrase wins
    words = (
        re.compile(r"(?<!\w)(" + "|".join(re.escape(p) for p in ordered) + r")(?!\w)")
        if ordered
        else None
    )
    return Lexicon(phrases, regexes, words)


def source_text(name: str, folder: Path | None) -> str:
    """The word list's text as it would be read now (part of the junk tabs' freshness key)."""
    try:
        if folder is not None and (folder / f"{name}.yaml").is_file():
            return (folder / f"{name}.yaml").read_text("utf-8")
    except OSError:
        pass
    return _shipped(name)


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
    except (OSError, yaml.YAMLError, re.error, ValueError) as e:
        log.warning("The %s word list could not be read (%s); using the built-in one", name, e)
        return parse(_shipped(name))
