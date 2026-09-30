"""Test functions of a file version, with the lines they span.

A fail-to-pass task runs the tests the fix added or changed. Given the new
version of a file, :func:`test_functions` lists its test functions and the
lines each one spans, so :func:`commitminer.patch.touched_tests` can name the
ones a patch adds to, changes or deletes from. Names follow each framework's
own ids, and the export prefixes them with the file path
(``tests/test_x.py::test_name``):

- Python: ``test_name``, or ``TestCase::test_name`` for a method, as pytest
  node ids spell it; the first decorator above the ``def`` starts the range.
- Rust: ``tests::name`` for a ``#[test]`` function, with its module path, as
  ``cargo test`` filters it; the attribute line starts the range.
- Go: ``TestName`` for ``func TestName(...)`` at the top level.
- Java: ``Class#method`` for a method under ``@Test`` (or JUnit's other test
  annotations), with the innermost enclosing class.
- JavaScript/TypeScript: the name string of ``test("...")`` and ``it("...")``
  (also ``.only``, ``.skip``, ``.each``); ``describe`` names are not included.

Ranges are found line by line: Python blocks by indentation (a decorator run,
the ``def``, then every deeper-indented line; trailing blank and comment lines
are left out so an insertion after a function is not a change to it), the
other languages by brace matching with the lexer of :mod:`commitminer.patch`.
Like the other measurements this is a heuristic; the README's Known issues
list the gaps.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Final

from commitminer.languages import Language
from commitminer.models import TestFunction
from commitminer.patch import SYNTAX, Syntax, item_end

_PY_CLASS: Final = re.compile(r"^(?P<indent>[ \t]*)class\s+(?P<name>\w+)\b")
_PY_DEF: Final = re.compile(r"^[ \t]*(?:async\s+)?def\b")
_PY_DECORATOR: Final = re.compile(r"^[ \t]*@")
_RS_MOD: Final = re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?mod\s+(?P<name>\w+)\s*\{")
_RS_ATTRIBUTE: Final = re.compile(r"^\s*#!?\[")
_JAVA_CLASS: Final = re.compile(
    r"^\s*(?:(?:public|protected|private|static|final|abstract|sealed|non-sealed|strictfp)\s+)*"
    r"(?:class|interface|enum|record)\s+(?P<name>\w+)\b"
)
_JAVA_ANNOTATION: Final = re.compile(r"^\s*@\w")


def _lines(content: bytes) -> list[str]:
    return content.decode("utf-8", "replace").split("\n")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def _python(lines: Sequence[str], syntax: Syntax) -> list[TestFunction]:
    """Blocks by indentation: classes qualify the test methods inside them."""
    found: list[TestFunction] = []
    # Open blocks: (indent, qualified name or None for a class, start line).
    stack: list[tuple[int, str | None, str, int]] = []
    decorators: int | None = None
    last_code = 0
    for number, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = _indent(line)
        while stack and indent <= stack[-1][0]:
            _, test, _, start = stack.pop()
            if test is not None:
                found.append(TestFunction(test, start, last_code))
        if _PY_DECORATOR.match(line):
            if decorators is None:
                decorators = number
            last_code = number
            continue
        definition = syntax.test_def.match(line)
        if definition is not None:
            classes = [name for _, test, name, _ in stack if test is None]
            qualified = "::".join([*classes, definition.group("name")])
            stack.append((indent, qualified, "", decorators or number))
        elif (klass := _PY_CLASS.match(line)) is not None:
            stack.append((indent, None, klass.group("name"), number))
        elif not _PY_DEF.match(line):
            # A decorator's continuation lines; the run ends at the next def or class.
            last_code = number
            continue
        decorators = None
        last_code = number
    for _, test, _, start in stack:
        if test is not None:
            found.append(TestFunction(test, start, last_code))
    return found


def _braced(lines: Sequence[str], language: Language, syntax: Syntax) -> list[TestFunction]:
    """Rust, Go, Java and JavaScript: a definition line, then its braces until they balance.

    Rust modules and Java classes qualify the names inside them; an attribute
    or annotation run above the definition starts the range.
    """
    found: list[TestFunction] = []
    scopes: list[tuple[str, int]] = []  # (name, last line) of enclosing modules or classes
    armed: int | None = None  # the line of the attribute run that makes the next fn a test
    scope_pattern = {Language.RUST: _RS_MOD, Language.JAVA: _JAVA_CLASS}.get(language)
    attribute_line = {Language.RUST: _RS_ATTRIBUTE, Language.JAVA: _JAVA_ANNOTATION}.get(language)
    joiner = "::" if language is Language.RUST else "#"
    for index, line in enumerate(lines):
        number = index + 1
        while scopes and scopes[-1][1] < number:
            scopes.pop()
        if scope_pattern is not None and (scope := scope_pattern.match(line)) is not None:
            scopes.append((scope.group("name"), item_end(lines, index, language) + 1))
            armed = None
            continue
        if syntax.test_attribute is not None:
            attribute = syntax.test_attribute.match(line)
            if attribute is not None:
                armed = armed or number
                line = line[attribute.end() :]  # "@Test void x() {" on one line
                if not line.strip():
                    continue
            elif attribute_line is not None and attribute_line.match(line):
                continue  # another attribute in the same run
        definition = syntax.test_def.match(line)
        if definition is None:
            if line.strip() and not line.lstrip().startswith("//"):
                armed = None
            continue
        if syntax.test_attribute is not None and armed is None:
            continue  # a plain function
        start = armed or number
        armed = None
        end = item_end(lines, index, language) + 1
        name = definition.group("name")
        if language is Language.RUST:
            name = "::".join([*(scope for scope, _ in scopes), name])
        elif language is Language.JAVA and scopes:
            name = f"{scopes[-1][0]}{joiner}{name}"
        found.append(TestFunction(name, start, end))
    return found


def _non_overlapping(found: list[TestFunction]) -> tuple[TestFunction, ...]:
    """Cut a range at the start of the next one (a JavaScript one-liner without ``;``)."""
    ordered = sorted(found, key=lambda f: (f.start, f.end))
    result: list[TestFunction] = []
    for index, item in enumerate(ordered):
        end = item.end
        if index + 1 < len(ordered) and ordered[index + 1].start <= end:
            end = max(item.start, ordered[index + 1].start - 1)
        result.append(TestFunction(item.name, item.start, end))
    return tuple(result)


def test_functions(content: bytes, language: Language) -> tuple[TestFunction, ...]:
    """The test functions of one file version, in line order, with non-overlapping ranges."""
    syntax = SYNTAX[language]
    lines = _lines(content)
    if language is Language.PYTHON:
        found = _python(lines, syntax)
    else:
        found = _braced(lines, language, syntax)
    return _non_overlapping(found)
