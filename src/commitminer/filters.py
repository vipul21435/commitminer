"""Hard filters: the reasons a commit cannot become a fail-to-pass task.

Filters run before any scoring, in the order of :class:`RejectReason`, and the
first one that applies is reported. A rejected commit gets no score: a low
score would hide why it was dropped, a reason code keeps the funnel auditable.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from commitminer.classify import Category
from commitminer.settings import Settings
from commitminer.stats import DiffStats


class RejectReason(StrEnum):
    """Why a commit was not proposed, checked in this order."""

    EMPTY = "empty"
    DOCS_ONLY = "docs-only"
    GENERATED_ONLY = "generated-only"
    NO_SOURCE = "no-source"
    SOURCE_UNCHANGED = "source-unchanged"
    SOURCE_COSMETIC = "source-cosmetic"
    NO_TEST = "no-test"
    OVERSIZE = "oversize"

    @property
    def description(self) -> str:
        """What the reason means, in one line."""
        return DESCRIPTIONS[self]


DESCRIPTIONS: Final[dict[RejectReason, str]] = {
    RejectReason.EMPTY: "the commit changes no files",
    RejectReason.DOCS_ONLY: "every changed file is documentation",
    RejectReason.GENERATED_ONLY: "every changed file is generated or vendored",
    RejectReason.NO_SOURCE: "no source file changed (only tests, config or other files)",
    RejectReason.SOURCE_UNCHANGED: (
        "no source line changed outside inline tests (renames, mode changes, binary files)"
    ),
    RejectReason.SOURCE_COSMETIC: (
        "source changes touch only comments, blank lines or (outside Python) indentation"
    ),
    RejectReason.NO_TEST: "no test file changed and no inline tests were added",
    RejectReason.OVERSIZE: "more source+test lines or source files than the limits allow",
}


def check(stats: DiffStats, settings: Settings) -> RejectReason | None:
    """Return the first reason to drop the commit, or ``None`` if it is a candidate."""
    if not stats.files:
        return RejectReason.EMPTY
    if stats.only(Category.DOCS):
        return RejectReason.DOCS_ONLY
    if stats.only(Category.GENERATED, Category.VENDORED):
        return RejectReason.GENERATED_ONLY
    if not stats.source_files:
        return RejectReason.NO_SOURCE
    if not stats.source_changed:
        # Pure renames, mode changes, binary files or test-module edits: nothing to fix.
        return RejectReason.SOURCE_UNCHANGED
    if stats.source_patched and not stats.code_files:
        return RejectReason.SOURCE_COSMETIC
    if not stats.test_files and not stats.inline_test_files:
        return RejectReason.NO_TEST
    if (
        stats.changed_lines > settings.max_lines
        or len(stats.source_files) > settings.max_source_files
    ):
        return RejectReason.OVERSIZE
    return None


def oversize_detail(stats: DiffStats, settings: Settings) -> str:
    """Which limit an oversize commit broke, with the numbers."""
    parts = []
    if stats.changed_lines > settings.max_lines:
        parts.append(f"{stats.changed_lines} source+test lines (max {settings.max_lines})")
    if len(stats.source_files) > settings.max_source_files:
        parts.append(f"{len(stats.source_files)} source files (max {settings.max_source_files})")
    return ", ".join(parts)
