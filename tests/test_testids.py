"""Test functions by line range, and which of them a patch touches."""

from __future__ import annotations

from commitminer.languages import Language
from commitminer.models import Commit, FileChange, PatchStats, TestFunction
from commitminer.patch import SYNTAX, FilePatch, Hunk, analyze, defined_tests, touched_tests
from commitminer.stats import diff_stats
from commitminer.testids import test_functions as find_tests

PYTHON = '''"""Tests for the parser."""

import pytest

from pkg import parse


@pytest.mark.parametrize(
    "text",
    ["1h", "2m"],
)
def test_units(text):
    assert parse(text) > 0
    # trailing comment


def helper():
    return 1


class TestParse:
    """Grouped tests."""

    def test_hours(self):
        assert parse("1h") == 3600

    @pytest.mark.slow
    @pytest.mark.xfail(strict=True)
    def test_days(self):
        assert parse("1d") == 86400

    class Nested:
        def test_inner(self):
            pass

    def not_a_test(self):
        pass


async def test_async():
    await parse("1h")
if True:
    def test_conditional():
        pass


class TestTail:
    def helper(self):
        pass
'''

RUST = """pub fn half(n: i64) -> i64 {
    n / 2
}

fn helper() {}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn halves() {
        assert_eq!(half(4), 2);
    }

    #[test]
    #[should_panic(expected = "boom")]
    /// A doc comment between the attributes and the function.
    fn panics() {
        panic!("boom");
    }

    mod nested {
        #[tokio::test]
        async fn deep() {}
    }

    fn not_a_test() {}
}

#[test]
fn top_level() {}
"""

GO = """package parse

import "testing"

func helper(t *testing.T) {}

func TestInt(t *testing.T) {
	if n, _ := Int("42"); n != 42 {
		t.Errorf("got %d", n)
	}
}

func TestNegative(t *testing.T) { Int("-1") }

func BenchmarkInt(b *testing.B) {}
"""

JAVA = """package com.acme;

import org.junit.jupiter.api.Test;

public class ReportTest {
    private int helper() { return 1; }

    @Test
    void builds() {
        assertEquals(1, helper());
    }

    @ParameterizedTest
    @ValueSource(ints = {1, 2})
    public void accepts(int n) {
        assertTrue(n > 0);
    }

    static class Inner {
        @Test void inner() { }
    }

    @Override
    public String toString() { return "x"; }
}
"""

JS = """import { greet } from './index.js';

describe('greet', () => {
  test('greets by name', () => {
    expect(greet('x')).toBe('hello x');
  });

  it.skip("is skipped", () => expect(1).toBe(1));
  test.each([1, 2])('handles %i', (n) => expect(n).toBeTruthy())
  it('closes over the next line', () => {
    expect(greet('')).toBe('hello ');
  });
});

function helper() {}
"""


def names(content: str, language: Language) -> list[tuple[str, int, int]]:
    return [(f.name, f.start, f.end) for f in find_tests(content.encode(), language)]


def test_python_functions_methods_decorators_and_nesting() -> None:
    assert names(PYTHON, Language.PYTHON) == [
        ("test_units", 8, 13),  # from the first decorator to the last code line, no comment
        ("TestParse::test_hours", 24, 25),
        ("TestParse::test_days", 27, 30),
        ("TestParse::Nested::test_inner", 33, 34),
        ("test_async", 40, 41),
        ("test_conditional", 43, 44),
    ]


def test_rust_test_functions_carry_their_module_path() -> None:
    assert names(RUST, Language.RUST) == [
        ("tests::halves", 11, 14),
        ("tests::panics", 16, 21),
        ("tests::nested::deep", 24, 25),
        ("top_level", 31, 32),
    ]


def test_go_top_level_test_functions() -> None:
    assert names(GO, Language.GO) == [("TestInt", 7, 11), ("TestNegative", 13, 13)]


def test_java_annotated_methods_with_their_class() -> None:
    assert names(JAVA, Language.JAVA) == [
        ("ReportTest#builds", 8, 11),
        ("ReportTest#accepts", 13, 17),
        ("Inner#inner", 20, 20),
    ]


def test_javascript_test_and_it_calls_do_not_overlap() -> None:
    assert names(JS, Language.JAVASCRIPT) == [
        ("greets by name", 4, 6),
        ("is skipped", 8, 8),
        ("handles %i", 9, 9),  # no semicolon: cut at the next test
        ("closes over the next line", 10, 12),
    ]
    assert names(JS, Language.TYPESCRIPT) == names(JS, Language.JAVASCRIPT)


def test_unknown_or_empty_content() -> None:
    assert find_tests(b"", Language.PYTHON) == ()
    assert find_tests(b"x = 1\n", Language.GO) == ()
    assert find_tests(b"\xff\xfe def test_x():\n  pass\n", Language.PYTHON) == ()


# --- which tests a patch touches --------------------------------------------------------


def hunk(new_start: int, added: int, deleted: int = 0, old_start: int = 1) -> Hunk:
    return Hunk(
        old_start,
        new_start,
        tuple(f"old {i}" for i in range(deleted)),
        tuple(f"new {i}" for i in range(added)),
    )


def test_touched_tests_by_line_overlap_and_deletions() -> None:
    functions = find_tests(PYTHON.encode(), Language.PYTHON)
    syntax = SYNTAX[Language.PYTHON]
    patch = FilePatch(
        (
            hunk(12, 1, 1),  # inside test_units
            hunk(25, 0, 1),  # a deletion: git numbers it by the line before, in test_hours
            hunk(37, 2),  # not_a_test
            hunk(50, 3),  # appended after the last class: nothing
        )
    )
    assert touched_tests(patch, syntax, functions) == (
        "TestParse::test_hours",
        "test_units",
    )
    assert touched_tests(FilePatch(()), syntax, functions) == ()


def test_without_contents_only_added_definitions_are_named() -> None:
    syntax = SYNTAX[Language.RUST]
    added = ("    #[test]", "    fn halves() {", "    }", "    fn helper() {}")
    assert defined_tests(added, syntax) == {"halves"}
    # The attribute must come first; a plain fn is not a test.
    assert defined_tests(("fn plain() {}", "#[test]"), syntax) == set()
    assert defined_tests(("def test_a():", "def helper():"), SYNTAX[Language.PYTHON]) == {"test_a"}
    assert defined_tests(("@Test", "void builds() {"), SYNTAX[Language.JAVA]) == {"builds"}
    assert defined_tests(("@Test void one() { }",), SYNTAX[Language.JAVA]) == {"one"}
    assert defined_tests(("test('x', () => {",), SYNTAX[Language.JAVASCRIPT]) == {"x"}
    patch = FilePatch((Hunk(1, 1, (), added),))
    assert touched_tests(patch, syntax, None) == ("halves",)
    assert analyze(patch, Language.RUST).tests == ("halves",)
    assert analyze(patch, None).tests == ()  # no language: nothing is recognised


def test_fail_to_pass_ids_prefix_the_path() -> None:
    def changed(path: str, tests: tuple[str, ...], signals: tuple[str, ...] = ()) -> FileChange:
        return FileChange(path, 3, 0, signals=signals, patch=PatchStats(1, 1, 3, 0, tests=tests))

    commit = Commit(
        "a" * 40,
        (),
        "2024-01-01T00:00:00+00:00",
        "Fix",
        (
            changed("tests/test_a.py", ("test_z", "TestA::test_b")),
            changed("src/lib.rs", ("tests::inline",), ("rust-inline-tests", "rust-tests-added")),
            changed("src/pkg/a.py", ("test_not_in_a_test_file",)),
            FileChange("tests/data/case.toml", 2, 0),
        ),
    )
    assert diff_stats(commit).fail_to_pass == (
        "src/lib.rs::tests::inline",
        "tests/test_a.py::TestA::test_b",
        "tests/test_a.py::test_z",
    )
    assert TestFunction("x", 1, 2).end == 2
