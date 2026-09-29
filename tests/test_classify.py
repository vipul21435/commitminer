from __future__ import annotations

import pytest

from commitminer.classify import FALLBACK_RULE, RULES, Category, Classification, classify

# (path, category, rule id): at least one positive example per rule, plus paths that
# look like a rule's target but must fall through to a later rule.
CASES: list[tuple[str, Category, str]] = [
    ("tests/test_parser.py", Category.TEST, "py-test-dir"),
    ("tests/data/valid/array.toml", Category.TEST, "py-test-dir"),
    ("src/pkg/test/helpers.py", Category.TEST, "py-test-dir"),
    ("Tests/README.md", Category.TEST, "py-test-dir"),
    ("src/pkg/test_utils.py", Category.TEST, "py-test-file"),
    ("pkg/parser_test.py", Category.TEST, "py-test-file"),
    ("conftest.py", Category.TEST, "py-test-file"),
    ("pkg/tests.py", Category.TEST, "py-test-file"),
    (".github/workflows/tests.yaml", Category.CONFIG, "ci-config"),
    (".travis.yml", Category.CONFIG, "ci-config"),
    ("pyproject.toml", Category.CONFIG, "py-config-file"),
    ("setup.py", Category.CONFIG, "py-config-file"),
    (".bumpversion.cfg", Category.CONFIG, "py-config-file"),
    ("requirements-dev.txt", Category.CONFIG, "py-config-file"),
    ("poetry.lock", Category.CONFIG, "py-config-file"),
    ("Makefile", Category.CONFIG, "py-config-file"),
    ("docs/conf.py", Category.DOCS, "docs-dir"),
    ("doc/usage.rst", Category.DOCS, "docs-dir"),
    ("README.md", Category.DOCS, "docs-file"),
    ("CHANGELOG.md", Category.DOCS, "docs-file"),
    ("LICENSE", Category.DOCS, "docs-file"),
    ("src/pkg/notes.txt", Category.DOCS, "docs-file"),
    ("benchmark/run.py", Category.OTHER, "py-tooling-dir"),
    ("scripts/release.py", Category.OTHER, "py-tooling-dir"),
    ("src/pkg/parser.py", Category.SOURCE, "py-source"),
    ("pkg/__init__.pyi", Category.SOURCE, "py-source"),
    ("pkg/_speedups.pyx", Category.SOURCE, "py-source"),
    ("src\\pkg\\win.py", Category.SOURCE, "py-source"),
    ("src/pkg/py.typed", Category.OTHER, FALLBACK_RULE),
    ("assets/logo.png", Category.OTHER, FALLBACK_RULE),
    # Near misses: names that contain a keyword but do not follow the convention.
    ("src/pkg/testing.py", Category.SOURCE, "py-source"),
    ("src/pkg/contest.py", Category.SOURCE, "py-source"),
    ("src/latest/mod.py", Category.SOURCE, "py-source"),
    ("src/pkg/docstrings.py", Category.SOURCE, "py-source"),
]


@pytest.mark.parametrize(("path", "category", "rule_id"), CASES)
def test_classify_table(path: str, category: Category, rule_id: str) -> None:
    assert classify(path) == Classification(category, rule_id)


def test_every_rule_has_a_positive_example() -> None:
    covered = {rule_id for _, _, rule_id in CASES}
    assert {rule.rule_id for rule in RULES} | {FALLBACK_RULE} == covered


def test_rule_ids_are_unique() -> None:
    ids = [rule.rule_id for rule in RULES]
    assert len(ids) == len(set(ids))


def test_custom_rule_table() -> None:
    assert classify("tests/test_x.py", rules=()) == Classification(Category.OTHER, FALLBACK_RULE)
