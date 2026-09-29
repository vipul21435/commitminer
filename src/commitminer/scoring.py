"""Candidate filter and a transparent linear scorer.

A commit is a candidate when it changes at least one source file and at least
one test file and stays under a size limit. Candidates are scored with
``score = sum(weight * value)`` over a few features whose values lie in
``[0, 1]``; every feature keeps its raw detail, value, weight and contribution,
so the ranking can always be explained line by line.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from commitminer.classify import Category, Classification, classify
from commitminer.models import Commit, FileChange


class RejectReason(StrEnum):
    """Why a commit was not proposed, checked in this order."""

    EMPTY = "empty"
    NO_SOURCE = "no-source"
    SOURCE_UNCHANGED = "source-unchanged"
    NO_TEST = "no-test"
    TOO_LARGE = "too-large"


@dataclass(frozen=True, slots=True)
class Weights:
    """Feature weights; the defaults add up to 10, the best possible score."""

    small_diff: float = 3.0
    test_lines_added: float = 3.0
    linked_reference: float = 2.0
    fix_keyword: float = 1.0
    focused_source: float = 1.0


@dataclass(frozen=True, slots=True)
class Settings:
    """Filter limits and scorer weights."""

    max_lines: int = 400
    """Largest accepted diff: added plus deleted lines over source and test files."""
    test_lines_cap: int = 40
    """Added test lines at which ``test_lines_added`` reaches its full value."""
    weights: Weights = field(default_factory=Weights)

    def __post_init__(self) -> None:
        if self.max_lines < 1 or self.test_lines_cap < 1:
            raise ValueError("max_lines and test_lines_cap must be at least 1")


@dataclass(frozen=True, slots=True)
class ClassifiedFile:
    """A changed file with the classifier's verdict."""

    change: FileChange
    classification: Classification

    @property
    def category(self) -> Category:
        """Shortcut for ``classification.category``."""
        return self.classification.category


@dataclass(frozen=True, slots=True)
class DiffStats:
    """Per-category view of one commit's changed files."""

    files: tuple[ClassifiedFile, ...]

    def _of(self, category: Category) -> tuple[ClassifiedFile, ...]:
        return tuple(f for f in self.files if f.category is category)

    @property
    def source_files(self) -> tuple[ClassifiedFile, ...]:
        """Changed source files."""
        return self._of(Category.SOURCE)

    @property
    def test_files(self) -> tuple[ClassifiedFile, ...]:
        """Changed test files (test code and test data)."""
        return self._of(Category.TEST)

    def added(self, category: Category) -> int:
        """Lines added in files of ``category``."""
        return sum(f.change.added or 0 for f in self._of(category))

    def deleted(self, category: Category) -> int:
        """Lines deleted in files of ``category``."""
        return sum(f.change.deleted or 0 for f in self._of(category))

    @property
    def changed_lines(self) -> int:
        """Added plus deleted lines over source and test files: the size the filter checks."""
        return sum(
            f.change.changed_lines
            for f in self.files
            if f.category in (Category.SOURCE, Category.TEST)
        )


def diff_stats(commit: Commit) -> DiffStats:
    """Classify every file a commit changed (renames by their new path)."""
    return DiffStats(tuple(ClassifiedFile(f, classify(f.path)) for f in commit.files))


@dataclass(frozen=True, slots=True)
class Feature:
    """One scorer feature: ``contribution = weight * value``."""

    name: str
    value: float
    weight: float
    contribution: float
    detail: str


@dataclass(frozen=True, slots=True)
class Candidate:
    """A commit that passed the filter, with its score breakdown."""

    commit: Commit
    stats: DiffStats
    features: tuple[Feature, ...]
    score: float


@dataclass(frozen=True, slots=True)
class Rejection:
    """A commit that the filter dropped, and why."""

    commit: Commit
    reason: RejectReason


_CLOSING_REF = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b:?\s+"
    r"((?:[\w.-]+/[\w.-]+)?#\d+|https://github\.com/\S+/(?:issues|pull)/\d+)",
    re.IGNORECASE,
)
_BARE_REF = re.compile(
    r"(?<![\w/&])(?:[\w.-]+/[\w.-]+)?#\d+\b"
    r"|\bgh-\d+\b"
    r"|https://github\.com/\S+/(?:issues|pull)/\d+",
    re.IGNORECASE,
)
_FIX_WORD = re.compile(
    r"\b(fix(?:e[sd]|ing)?|bug(?:s|fix)?|regression|crash(?:es|ed)?|incorrect(?:ly)?"
    r"|wrong|broken)\b",
    re.IGNORECASE,
)


def linked_reference(message: str) -> tuple[float, str]:
    """1.0 for a closing keyword with a reference, 0.5 for a bare reference, else 0.0."""
    closing = _CLOSING_REF.search(message)
    if closing is not None:
        return 1.0, closing.group(0)
    bare = _BARE_REF.search(message)
    if bare is not None:
        return 0.5, bare.group(0)
    return 0.0, "no issue or pull-request reference"


def fix_keyword(subject: str) -> tuple[float, str]:
    """1.0 when the subject line reads like a bug fix."""
    match = _FIX_WORD.search(subject)
    return (1.0, match.group(0)) if match else (0.0, "no fix keyword in the subject")


def check(stats: DiffStats, settings: Settings) -> RejectReason | None:
    """Return the first reason to drop the commit, or ``None`` if it is a candidate."""
    if not stats.files:
        return RejectReason.EMPTY
    if not stats.source_files:
        return RejectReason.NO_SOURCE
    if not stats.added(Category.SOURCE) and not stats.deleted(Category.SOURCE):
        # Pure renames, mode changes or binary files: nothing for a test to catch.
        return RejectReason.SOURCE_UNCHANGED
    if not stats.test_files:
        return RejectReason.NO_TEST
    if stats.changed_lines > settings.max_lines:
        return RejectReason.TOO_LARGE
    return None


def _feature(name: str, value: float, weight: float, detail: str) -> Feature:
    value = round(value, 4)
    return Feature(name, value, weight, round(weight * value, 4), detail)


def features(commit: Commit, stats: DiffStats, settings: Settings) -> tuple[Feature, ...]:
    """Compute every feature for a commit that passed :func:`check`."""
    weights = settings.weights
    size = stats.changed_lines
    test_added = stats.added(Category.TEST)
    sources = len(stats.source_files)
    ref_value, ref_detail = linked_reference(commit.message)
    fix_value, fix_detail = fix_keyword(commit.subject)
    return (
        _feature(
            "small_diff",
            1 - min(size, settings.max_lines) / settings.max_lines,
            weights.small_diff,
            f"{size} of at most {settings.max_lines} source+test lines changed",
        ),
        _feature(
            "test_lines_added",
            min(test_added, settings.test_lines_cap) / settings.test_lines_cap,
            weights.test_lines_added,
            f"{test_added} test lines added (full value at {settings.test_lines_cap})",
        ),
        _feature("linked_reference", ref_value, weights.linked_reference, ref_detail),
        _feature("fix_keyword", fix_value, weights.fix_keyword, fix_detail),
        _feature(
            "focused_source",
            1 / sources,
            weights.focused_source,
            f"{sources} source file{'s' if sources != 1 else ''} changed",
        ),
    )


def evaluate(commit: Commit, settings: Settings) -> Candidate | Rejection:
    """Filter one commit and, if it passes, score it."""
    stats = diff_stats(commit)
    reason = check(stats, settings)
    if reason is not None:
        return Rejection(commit, reason)
    feats = features(commit, stats, settings)
    return Candidate(commit, stats, feats, round(sum(f.contribution for f in feats), 4))


def _rank_key(candidate: Candidate) -> tuple[float, float, str]:
    timestamp = datetime.fromisoformat(candidate.commit.date).timestamp()
    return (-candidate.score, -timestamp, candidate.commit.sha)


@dataclass(frozen=True, slots=True)
class MineResult:
    """Ranked candidates (best first) and the rejected commits."""

    walked: int
    candidates: tuple[Candidate, ...]
    rejections: tuple[Rejection, ...]

    def rejected_by_reason(self) -> dict[str, int]:
        """Rejection counts per reason, in the order the filter checks them."""
        counts = Counter(r.reason for r in self.rejections)
        return {reason.value: counts[reason] for reason in RejectReason if counts[reason]}


def mine(commits: Iterable[Commit], settings: Settings | None = None) -> MineResult:
    """Filter and score commits; rank by score, then newest first, then sha."""
    settings = settings or Settings()
    candidates: list[Candidate] = []
    rejections: list[Rejection] = []
    walked = 0
    for commit in commits:
        walked += 1
        outcome = evaluate(commit, settings)
        if isinstance(outcome, Candidate):
            candidates.append(outcome)
        else:
            rejections.append(outcome)
    candidates.sort(key=_rank_key)
    return MineResult(walked, tuple(candidates), tuple(rejections))
