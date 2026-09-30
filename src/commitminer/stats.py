"""Per-category measurements of one commit, from numstat, content signals and the patch.

Lines inside Rust ``#[cfg(test)]`` modules are counted as test lines, not
source lines, whenever the walker could find the modules (it needs file
contents). Measurements that need the patch return ``None`` when a changed
source or test file has none: hand-built records and recordings made before
patches were read.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from commitminer.classify import RULES, Category, Classification, Rule, classify
from commitminer.models import Commit, FileChange
from commitminer.signals import RUST_TESTS_ADDED


@dataclass(frozen=True, slots=True)
class ClassifiedFile:
    """A changed file with the classifier's verdict."""

    change: FileChange
    classification: Classification

    @property
    def category(self) -> Category:
        """Shortcut for ``classification.category``."""
        return self.classification.category

    @property
    def inline_added(self) -> int:
        """Lines added inside inline test modules."""
        return self.change.patch.test_added if self.change.patch else 0

    @property
    def inline_deleted(self) -> int:
        """Lines deleted inside inline test modules."""
        return self.change.patch.test_deleted if self.change.patch else 0

    @property
    def outside_tests(self) -> int:
        """Added plus deleted lines outside inline test modules."""
        return self.change.changed_lines - self.inline_added - self.inline_deleted


@dataclass(frozen=True, slots=True)
class DiffStats:
    """Per-category view of one commit's changed files."""

    files: tuple[ClassifiedFile, ...]

    def of(self, *categories: Category) -> tuple[ClassifiedFile, ...]:
        """Changed files in any of ``categories``."""
        return tuple(f for f in self.files if f.category in categories)

    def only(self, *categories: Category) -> bool:
        """True when there are files and every one is in ``categories``."""
        return bool(self.files) and len(self.of(*categories)) == len(self.files)

    @property
    def source_files(self) -> tuple[ClassifiedFile, ...]:
        """Changed source files."""
        return self.of(Category.SOURCE)

    @property
    def test_files(self) -> tuple[ClassifiedFile, ...]:
        """Changed test files (test code and test data)."""
        return self.of(Category.TEST)

    @property
    def inline_test_files(self) -> tuple[ClassifiedFile, ...]:
        """Source files that gained in-file tests: new ``#[test]`` functions or test lines."""
        return tuple(
            f
            for f in self.source_files
            if RUST_TESTS_ADDED in f.change.signals or f.inline_added > 0
        )

    def added(self, category: Category) -> int:
        """Lines added in files of ``category``; inline test lines count as test lines."""
        if category is Category.SOURCE:
            return sum((f.change.added or 0) - f.inline_added for f in self.source_files)
        total = sum(f.change.added or 0 for f in self.of(category))
        if category is Category.TEST:
            total += sum(f.inline_added for f in self.source_files)
        return total

    def deleted(self, category: Category) -> int:
        """Lines deleted in files of ``category``; inline test lines count as test lines."""
        if category is Category.SOURCE:
            return sum((f.change.deleted or 0) - f.inline_deleted for f in self.source_files)
        total = sum(f.change.deleted or 0 for f in self.of(category))
        if category is Category.TEST:
            total += sum(f.inline_deleted for f in self.source_files)
        return total

    @property
    def changed_lines(self) -> int:
        """Added plus deleted lines over source and test files: the size the filter checks."""
        return sum(f.change.changed_lines for f in self.of(Category.SOURCE, Category.TEST))

    @property
    def source_changed(self) -> bool:
        """True when some source file changed lines outside its inline test modules."""
        return any(f.outside_tests > 0 for f in self.source_files)

    def _patched(self, files: Sequence[ClassifiedFile]) -> bool:
        return all(f.change.patch is not None or f.change.binary for f in files)

    @property
    def source_patched(self) -> bool:
        """True when every changed text source file has patch measurements."""
        return self._patched(self.source_files)

    @property
    def code_files(self) -> tuple[ClassifiedFile, ...]:
        """Source files whose code changed (not only comments, whitespace or inline tests).

        A source file without patch measurements counts when it changed any
        line outside its inline tests.
        """
        return tuple(
            f
            for f in self.source_files
            if (f.change.patch.code_hunks if f.change.patch else f.outside_tests) > 0
        )

    @property
    def code_hunks(self) -> int | None:
        """Hunks that change source code, or ``None`` without patch data."""
        if not self.source_patched:
            return None
        return sum(f.change.patch.code_hunks for f in self.source_files if f.change.patch)

    @property
    def code_lines(self) -> int | None:
        """Source code lines added plus deleted, or ``None`` without patch data."""
        if not self.source_patched:
            return None
        return sum(f.change.patch.code_lines for f in self.source_files if f.change.patch)

    @property
    def assertions(self) -> int | None:
        """Assertion lines added in test files and inline test modules (``None``: unknown)."""
        tests = self.test_files + self.inline_test_files
        if not self._patched(tests):
            return None
        return sum(f.change.patch.asserts for f in tests if f.change.patch)

    @property
    def fail_to_pass(self) -> tuple[str, ...]:
        """Likely fail-to-pass test ids: ``path::name`` for each test the patch touched.

        Test functions of the changed test files and of source files with
        inline tests, as :mod:`commitminer.testids` names them; sorted.
        """
        found = {
            f"{f.change.path}::{name}"
            for f in self.test_files + self.inline_test_files
            if f.change.patch is not None
            for name in f.change.patch.tests
        }
        return tuple(sorted(found))

    @property
    def public_api(self) -> tuple[str, ...] | None:
        """Public declarations touched by source code changes (``None``: unknown)."""
        if not self.source_patched:
            return None
        names = {name for f in self.source_files if f.change.patch for name in f.change.patch.api}
        return tuple(sorted(names))


def diff_stats(commit: Commit, rules: Sequence[Rule] = RULES) -> DiffStats:
    """Classify every file a commit changed (renames by their new path)."""
    return DiffStats(
        tuple(ClassifiedFile(f, classify(f.path, rules, f.signals)) for f in commit.files)
    )
