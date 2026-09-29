"""Content signals: facts about a file's text that its path cannot tell.

Signals are detected only for files in one of the classifier's languages (by
extension) and only from the file's own bytes, so they can be computed once,
when a history is walked, and stored with the recording. The classifier's
``signal`` rules then match on the stored names.

- ``generated-header``: a comment in the first lines says the file is tool
  output (``Code generated ... DO NOT EDIT``, ``@generated``, "auto-generated").
- ``minified``: JavaScript whose lines average at least 200 characters, the
  shape of a minified bundle that does not carry a ``.min.js`` name.
- ``rust-inline-tests``: Rust source with an in-file ``#[cfg(test)]`` module.
- ``rust-tests-added``: such a file gained ``#[test]`` functions in the commit
  (the walker compares with the parent version), so the commit changes tests
  even when no file under ``tests/`` changed.
"""

from __future__ import annotations

import re
from typing import Final

from commitminer.languages import Language, language_of

GENERATED_HEADER: Final = "generated-header"
MINIFIED: Final = "minified"
RUST_INLINE_TESTS: Final = "rust-inline-tests"
RUST_TESTS_ADDED: Final = "rust-tests-added"

SIGNALS: Final[dict[str, str]] = {
    GENERATED_HEADER: "a comment in the first lines marks the file as generated",
    MINIFIED: "JavaScript with an average line length of at least 200 characters",
    RUST_INLINE_TESTS: "Rust source with an in-file #[cfg(test)] module",
    RUST_TESTS_ADDED: "a Rust source file gained #[test] functions in this commit",
}
"""Every signal name and what it means."""

MAX_CONTENT: Final = 1 << 20
"""Bytes of a file that detection looks at; the rest is ignored."""

HEADER_LINES: Final = 30
"""A generated-code marker must appear in this many first lines."""

MINIFIED_MIN_BYTES: Final = 512
MINIFIED_MEAN_LINE: Final = 200
_MINIFIED_SAMPLE: Final = 64 * 1024

_COMMENT_START = re.compile(r"^\s*(?://|/\*|\*|#|\"\"\"|''')")
_GENERATED_MARK = re.compile(
    r"@generated\b"
    r"|\bgenerated\b.*\bdo not (?:edit|modify)\b"
    r"|\bdo not (?:edit|modify)\b.*\bgenerated\b"
    r"|\b(?:auto-?generated|automatically generated|machine generated)\b",
    re.IGNORECASE,
)
_CFG_TEST = re.compile(r"^\s*#\[cfg\((?:all\()?test\b", re.MULTILINE)
_TEST_ATTRIBUTE = re.compile(r"^\s*#\[(?:\w+::)*test(?:\]|\()", re.MULTILINE)


def _text(content: bytes) -> str:
    return content[:MAX_CONTENT].decode("utf-8", "replace")


def has_generated_header(text: str) -> bool:
    """True when a comment line among the first :data:`HEADER_LINES` marks generated code."""
    for line in text.splitlines()[:HEADER_LINES]:
        if _COMMENT_START.match(line) and _GENERATED_MARK.search(line):
            return True
    return False


def is_minified(text: str) -> bool:
    """True for text of at least 512 characters whose lines average 200 or more."""
    sample = text[:_MINIFIED_SAMPLE]
    if len(sample) < MINIFIED_MIN_BYTES:
        return False
    return len(sample) / (sample.count("\n") + 1) >= MINIFIED_MEAN_LINE


def has_inline_tests(text: str) -> bool:
    """True when Rust source has a ``#[cfg(test)]`` (or ``#[cfg(all(test, ...))]``) item."""
    return _CFG_TEST.search(text) is not None


def count_test_functions(text: str) -> int:
    """Number of ``#[test]``-style attributes (``#[test]``, ``#[tokio::test]``, ...)."""
    return len(_TEST_ATTRIBUTE.findall(text))


def rust_tests_added(content: bytes, previous: bytes) -> bool:
    """True when ``content`` has more test functions than ``previous`` (empty if new)."""
    return count_test_functions(_text(content)) > count_test_functions(_text(previous))


def detect(language: Language | None, content: bytes) -> tuple[str, ...]:
    """Signals of one file version, sorted; empty for files outside the known languages."""
    if language is None:
        return ()
    text = _text(content)
    found: list[str] = []
    if has_generated_header(text):
        found.append(GENERATED_HEADER)
    if language is Language.JAVASCRIPT and is_minified(text):
        found.append(MINIFIED)
    if language is Language.RUST and has_inline_tests(text):
        found.append(RUST_INLINE_TESTS)
    return tuple(sorted(found))


def detect_path(path: str, content: bytes) -> tuple[str, ...]:
    """:func:`detect` with the language taken from the path's extension."""
    return detect(language_of(path), content)
