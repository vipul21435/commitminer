"""Test functions by line range, and which of them a patch touches."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from commitminer import patch as patch_module
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


WRAPPED = """import unittest


def test_add_with_fixtures(
    tmp_path, monkeypatch
) -> None:
    result = add(1, 2)
    assert result == 3


class TestParser(
    unittest.TestCase,
):
    def test_empty(self):
        self.assertEqual(parse(""), {})

    def test_long_signature_in_a_class(
        self, value: int = (
            1
        )
    ) -> None:
        assert value == 1


def test_after():
    pass
"""


def test_python_wrapped_signatures_and_base_lists_keep_their_blocks() -> None:
    # black and ruff format close a wrapped signature at the def's own indentation; that
    # line used to end the range before the body, and a wrapped base list lost the class.
    assert names(WRAPPED, Language.PYTHON) == [
        ("test_add_with_fixtures", 4, 8),
        ("TestParser::test_empty", 14, 15),
        ("TestParser::test_long_signature_in_a_class", 17, 22),
        ("test_after", 25, 26),
    ]


CONTINUED = '''def test_string():
    expected = """
[table]

key = "value (with a bracket"
"""
    assert parse(expected) == {}


def test_backslash():
    total = 1 + \\
2
    assert total == 3  # a bracket in a comment: (


def test_quotes():
    assert text("it's", 'say "hi" (', "a\\"b", \'\'\'x\'\'\') == "["
    assert join("a\\
b") == 2


def test_last():
    pass
'''


def test_python_continuation_lines_never_close_a_block() -> None:
    # Lines inside a multi-line string, after a backslash, or inside brackets are part of
    # the statement above, whatever their indentation; brackets in strings and comments
    # do not count.
    assert names(CONTINUED, Language.PYTHON) == [
        ("test_string", 1, 7),
        ("test_backslash", 10, 13),
        ("test_quotes", 16, 19),
        ("test_last", 22, 23),
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


def test_javascript_one_liners_are_scanned_once(monkeypatch: pytest.MonkeyPatch) -> None:
    # 'semi: false' one-liners have no brace and no ';', so each test's scan used to run to
    # the end of the describe block: 4000 of them took 9.3 s. It now stops at the next test.
    lexed = 0
    braces = patch_module._Lexer.braces

    def counting(self: Any, line: str) -> Iterator[str]:
        nonlocal lexed
        lexed += 1
        return braces(self, line)

    monkeypatch.setattr(patch_module._Lexer, "braces", counting)
    count = 400
    body = "".join(f"  it('case {i}', () => expect(f({i})).toBe({i}))\n" for i in range(count))
    source = f"describe('f', () => {{\n{body}}})\n".encode()
    found = find_tests(source, Language.JAVASCRIPT)
    assert [(f.start, f.end) for f in found[:2]] == [(2, 2), (3, 3)]
    assert (found[-1].start, found[-1].end) == (count + 1, count + 2)
    assert lexed < 3 * count  # every line about once, not count * count / 2


def test_a_test_whose_braces_never_close_ends_before_the_next_one() -> None:
    # A file version saved mid-edit: the first test's braces never balance.
    source = "#[test]\nfn first() {\n    assert!(true);\n\n#[test]\nfn second() {}\n"
    assert names(source, Language.RUST) == [("first", 1, 4), ("second", 5, 6)]


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
    # A Java method or an indented Python def needs its class among the same lines: a bare
    # method name is not an id pytest or JUnit accepts.
    java, python = SYNTAX[Language.JAVA], SYNTAX[Language.PYTHON]
    assert defined_tests(("@Test", "void builds() {"), java) == set()
    assert defined_tests(("@Test void one() { }",), java) == set()
    new_file = ("public class XTest {", "    @Test", "    void builds() {", "    }", "}")
    assert defined_tests(new_file, java) == {"XTest#builds"}
    nested = ("class Outer {", "  static class Inner {", "    @Test void one() {}", "  }", "}")
    assert defined_tests(nested, java) == {"Inner#one"}
    assert defined_tests(("    def test_lazy_import(self):",), python) == set()
    classes = (
        "class TestA:",
        "    def test_m(self):",
        "        pass",
        "",
        "    class Inner:",
        "        def test_n(self):",
        "            pass",
        "def test_top():",
        "    def test_nested_helper():",
    )
    assert defined_tests(classes, python) == {"TestA::test_m", "TestA::Inner::test_n", "test_top"}
    # A class added inside one the lines do not show (or inside a function): the outer
    # scope is unknown, so no id, rather than one that leaves it out.
    nested_in_unseen = ("    class TestPositive:", "        def test_small(self):")
    assert defined_tests(nested_in_unseen, python) == set()
    in_a_function = ("def test_f():", "    class Helper:", "        def test_x(self):")
    assert defined_tests(in_a_function, python) == {"test_f"}
    then_top = (*nested_in_unseen, "            pass", "class TestTop:", "    def test_y(self):")
    assert defined_tests(then_top, python) == {"TestTop::test_y"}
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
