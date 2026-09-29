"""Path-convention file classifier for Python repositories.

Rules are one ordered table; the first rule that matches decides the category,
and the rule id travels with the result so a reviewer can see why a file was
put where it was. Matching is case-insensitive on POSIX-style paths.

Rule kinds:

- ``dir``: any directory component of the path equals one of the patterns.
- ``name``: the file name matches one of the ``fnmatch`` patterns.
- ``prefix``: the path starts with one of the patterns.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from fnmatch import fnmatchcase
from typing import Literal


class Category(StrEnum):
    """What a changed file is, for the purpose of building a fail-to-pass task."""

    SOURCE = "source"
    TEST = "test"
    DOCS = "docs"
    CONFIG = "config"
    OTHER = "other"


RuleKind = Literal["dir", "name", "prefix"]


@dataclass(frozen=True, slots=True)
class Rule:
    """One row of the classifier table."""

    rule_id: str
    category: Category
    kind: RuleKind
    patterns: tuple[str, ...]
    rationale: str

    def matches(self, directories: tuple[str, ...], name: str, path: str) -> bool:
        """True when this rule applies to the lower-cased path parts."""
        if self.kind == "dir":
            return any(part in self.patterns for part in directories)
        if self.kind == "name":
            return any(fnmatchcase(name, pattern) for pattern in self.patterns)
        return any(path.startswith(pattern) for pattern in self.patterns)


RULES: tuple[Rule, ...] = (
    Rule(
        "py-test-dir",
        Category.TEST,
        "dir",
        ("tests", "test"),
        "pytest and unittest layouts keep tests and their data under tests/ or test/",
    ),
    Rule(
        "py-test-file",
        Category.TEST,
        "name",
        ("test_*.py", "*_test.py", "tests.py", "conftest.py"),
        "pytest discovery names and shared fixtures, wherever they live",
    ),
    Rule(
        "ci-config",
        Category.CONFIG,
        "prefix",
        (".github/", ".circleci/", ".gitlab-ci", ".travis", "azure-pipelines"),
        "continuous integration settings",
    ),
    Rule(
        "py-config-file",
        Category.CONFIG,
        "name",
        (
            "pyproject.toml",
            "setup.py",
            "noxfile.py",
            ".flake8",
            ".coveragerc",
            ".pre-commit-config.yaml",
            "requirements*.txt",
            "*.cfg",
            "*.ini",
            "*.lock",
            "manifest.in",
            "makefile",
            ".gitignore",
            ".gitattributes",
            ".editorconfig",
            ".readthedocs.y*ml",
        ),
        "packaging, tooling and repository settings",
    ),
    Rule(
        "docs-dir",
        Category.DOCS,
        "dir",
        ("docs", "doc", "documentation"),
        "documentation trees, including their conf.py",
    ),
    Rule(
        "docs-file",
        Category.DOCS,
        "name",
        (
            "*.md",
            "*.rst",
            "*.txt",
            "readme*",
            "changelog*",
            "changes*",
            "history*",
            "license*",
            "copying*",
            "notice*",
            "authors*",
            "contributing*",
        ),
        "prose and legal files",
    ),
    Rule(
        "py-tooling-dir",
        Category.OTHER,
        "dir",
        ("benchmark", "benchmarks", "scripts", "tools", "examples", "fuzzer", "profiler"),
        "helper code that ships outside the package and is not under test",
    ),
    Rule(
        "py-source",
        Category.SOURCE,
        "name",
        ("*.py", "*.pyi", "*.pyx", "*.pxd"),
        "Python modules, stubs and Cython sources",
    ),
)
"""The classifier table, in match order."""

FALLBACK_RULE = "fallback"
"""Rule id reported when no rule matches; the category is then OTHER."""


@dataclass(frozen=True, slots=True)
class Classification:
    """The category of one path and the id of the rule that decided it."""

    category: Category
    rule_id: str


def classify(path: str, rules: tuple[Rule, ...] = RULES) -> Classification:
    """Classify a repository-relative path with the first matching rule."""
    parts = path.replace("\\", "/").lower().split("/")
    directories, name = tuple(parts[:-1]), parts[-1]
    lowered = "/".join(parts)
    for rule in rules:
        if rule.matches(directories, name, lowered):
            return Classification(rule.category, rule.rule_id)
    return Classification(Category.OTHER, FALLBACK_RULE)
