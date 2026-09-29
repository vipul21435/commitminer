"""Parse ``--unified=0`` patches and measure them line by line, per language.

``git log -p --unified=0`` prints one block per changed file, in the same order
as ``--numstat``. With zero context lines every hunk is a run of deleted lines
followed by a run of added lines, and its header gives both counts, so hunks
are read by count and a content line such as ``+++ x`` can never be mistaken
for a header.

:func:`analyze` turns one file's hunks into :class:`~commitminer.models.PatchStats`:

- **Code lines** leave out blank lines and comment-only lines. A hunk whose
  deleted and added code lines are the same sequence after dropping trailing
  comments and (outside Python) surrounding whitespace changes nothing a test
  could see: re-indenting C-like code, or adding ``# pragma: no cover``.
  Python keeps its indentation, because there it is syntax.
- **Inline test lines**: for Rust files with ``#[cfg(test)]`` modules, lines
  inside those modules (found with a small lexer in :func:`rust_test_regions`)
  count as test lines, not code.
- **Assertions**: added lines that assert (``assert``, ``assert_eq!``,
  ``expect(``, ``t.Errorf``, ``assertEquals`` ...), minus identical ones the
  same hunk deleted, so moving an assertion does not count.
- **Public API**: declarations in changed code lines that are visible outside
  the module: top-level ``def``/``class`` without a leading underscore, ``pub``
  items, exported Go identifiers, ``export``, ``public``.
- **Hunk hashes**: one whitespace- and position-insensitive hash per hunk that
  changes more than whitespace, for dedupe (see :mod:`commitminer.fingerprint`).

Everything is line based; nothing is parsed into a syntax tree, so the rules
are heuristics with known gaps (see the README's Known issues).
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import Final

from commitminer.fingerprint import hunk_hash
from commitminer.languages import Language
from commitminer.models import PatchStats

DIFF_HEADER: Final = b"diff --git "
"""First bytes of every file block in ``git log -p`` output."""

MAX_API_NAMES: Final = 32
"""At most this many public names are kept per file (sorted)."""

_HUNK = re.compile(rb"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_NO_NEWLINE = b"\\"


class PatchError(ValueError):
    """A patch does not have the shape ``git log -p --unified=0`` produces."""


@dataclass(frozen=True, slots=True)
class Hunk:
    """One ``--unified=0`` hunk: deleted lines, then added lines, without the +/-."""

    old_start: int
    new_start: int
    deleted: tuple[str, ...]
    added: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FilePatch:
    """The hunks of one file block; ``binary`` when git printed no text diff."""

    hunks: tuple[Hunk, ...]
    binary: bool = False

    @property
    def added(self) -> int:
        """Added lines over all hunks."""
        return sum(len(h.added) for h in self.hunks)

    @property
    def deleted(self) -> int:
        """Deleted lines over all hunks."""
        return sum(len(h.deleted) for h in self.hunks)


def _decode(line: bytes) -> str:
    return line.decode("utf-8", "replace").removesuffix("\r")


def _take(lines: list[bytes], start: int, count: int, sign: bytes) -> tuple[list[str], int]:
    """Read ``count`` lines that start with ``sign``, skipping no-newline markers."""
    taken: list[str] = []
    i = start
    while len(taken) < count:
        if i >= len(lines) or not lines[i].startswith(sign):
            got = lines[i][:60] if i < len(lines) else b"end of patch"
            raise PatchError(f"expected {count} lines starting with {sign!r}, got {got!r}")
        taken.append(_decode(lines[i][1:]))
        i += 1
        while i < len(lines) and lines[i].startswith(_NO_NEWLINE):
            i += 1
    return taken, i


def parse_patch(text: bytes) -> list[FilePatch]:
    """Split the patch text of one commit into file blocks and hunks, in output order."""
    lines = text.split(b"\n")
    patches: list[FilePatch] = []
    i = 0
    while i < len(lines):
        if not lines[i]:
            i += 1
            continue
        if not lines[i].startswith(DIFF_HEADER):
            raise PatchError(f"expected a diff header, got {lines[i][:60]!r}")
        i += 1
        binary = False
        while i < len(lines) and lines[i] and not lines[i].startswith((b"@@", DIFF_HEADER)):
            if lines[i].startswith((b"Binary files ", b"GIT binary patch")):
                binary = True
            i += 1
        hunks: list[Hunk] = []
        while i < len(lines) and lines[i].startswith(b"@@"):
            match = _HUNK.match(lines[i])
            if match is None:
                raise PatchError(f"bad hunk header {lines[i][:60]!r}")
            old_count = 1 if match.group(2) is None else int(match.group(2))
            new_count = 1 if match.group(4) is None else int(match.group(4))
            deleted, i = _take(lines, i + 1, old_count, b"-")
            added, i = _take(lines, i, new_count, b"+")
            old_start, new_start = int(match.group(1)), int(match.group(3))
            hunks.append(Hunk(old_start, new_start, tuple(deleted), tuple(added)))
        patches.append(FilePatch(tuple(hunks), binary))
    return patches


@dataclass(frozen=True, slots=True)
class Syntax:
    """What :func:`analyze` needs to know about one language."""

    line_comment: str
    quotes: str
    """Characters that open a one-line string literal (a comment marker inside is text)."""
    block_comments: bool
    """``/* ... */`` comments, whose continuation lines start with ``*``."""
    indent_matters: bool
    assertion: re.Pattern[str]
    api: tuple[tuple[re.Pattern[str], str], ...]
    """Declaration patterns and label templates; ``{kind}`` and ``{name}`` come from the match."""


_IDENT_JS = r"[A-Za-z_$][\w$]*"
_C_LIKE_STAR = re.compile(r"^\s*\*(?:\s|/|$)")

SYNTAX: Final[dict[Language, Syntax]] = {
    Language.PYTHON: Syntax(
        line_comment="#",
        quotes="'\"",
        block_comments=False,
        indent_matters=True,
        assertion=re.compile(
            r"^\s*assert\b|\.assert[A-Z_]\w*\s*\(|\bassert_\w+\s*\("
            r"|\bpytest\.(?:raises|warns|fail)\s*\("
        ),
        api=(
            (re.compile(r"^(?:async\s+)?def\s+(?P<name>[A-Za-z]\w*)\s*[(\[]"), "def {name}"),
            (re.compile(r"^class\s+(?P<name>[A-Za-z]\w*)\b"), "class {name}"),
        ),
    ),
    Language.RUST: Syntax(
        line_comment="//",
        quotes='"',
        block_comments=True,
        indent_matters=False,
        assertion=re.compile(
            r"\b(?:debug_)?assert(?:_eq|_ne|_matches)?!\s*[(\[{]|\bassert_\w+\s*\("
            r"|#\[should_panic\b"
        ),
        api=(
            (
                re.compile(
                    r"^pub\s+(?:(?:const|async|unsafe|default|extern(?:\s+\"[^\"]*\")?)\s+)*"
                    r"(?P<kind>fn|struct|enum|trait|type|const|static|mod|union)\s+"
                    r"(?:mut\s+)?(?P<name>[A-Za-z_]\w*)"
                ),
                "pub {kind} {name}",
            ),
        ),
    ),
    Language.GO: Syntax(
        line_comment="//",
        quotes='"`',
        block_comments=True,
        indent_matters=False,
        assertion=re.compile(
            r"\bt\.(?:Error|Errorf|Fatal|Fatalf|Fail|FailNow)\s*\("
            r"|\b(?:assert|require)\.[A-Z]\w*\s*\("
        ),
        api=(
            (re.compile(r"^func\s+(?:\([^)]*\)\s*)?(?P<name>[A-Z]\w*)\s*[\[(]"), "func {name}"),
            (re.compile(r"^(?P<kind>type|var|const)\s+(?P<name>[A-Z]\w*)\b"), "{kind} {name}"),
        ),
    ),
    Language.JAVA: Syntax(
        line_comment="//",
        quotes="\"'",
        block_comments=True,
        indent_matters=False,
        assertion=re.compile(r"\bassert[A-Z]\w*\s*\(|^\s*assert\s|\bfail\s*\(|\bverify\s*\("),
        api=(
            (
                re.compile(
                    r"^public\s+(?:(?:static|final|abstract|sealed|non-sealed|strictfp)\s+)*"
                    r"(?P<kind>class|interface|enum|record|@interface)\s+(?P<name>\w+)"
                ),
                "public {kind} {name}",
            ),
            (
                re.compile(
                    r"^public\s+(?:(?:static|final|abstract|synchronized|native|default|strictfp)"
                    r"\s+)*(?:<[^>]*>\s*)?(?:[\w.$?]+(?:<.*>)?(?:\[\])*\s+)?(?P<name>\w+)\s*\("
                ),
                "public {name}()",
            ),
        ),
    ),
}

_JS_SYNTAX = Syntax(
    line_comment="//",
    quotes="\"'`",
    block_comments=True,
    indent_matters=False,
    assertion=re.compile(
        r"\bexpect\s*\(|\bassert(?:\.\w+)*\s*\(|\.should\b"
        r"|\bt\.(?:is|not|deepEqual|notDeepEqual|true|false|truthy|falsy|throws|throwsAsync"
        r"|notThrows|equal|strictEqual|same|match|ok|regex)\s*\("
    ),
    api=(
        (
            re.compile(
                r"^export\s+(?:default\s+)?(?:declare\s+)?(?:abstract\s+)?(?:async\s+)?"
                rf"(?P<kind>function|class|const|let|var|interface|type|enum|namespace)\b\*?\s*"
                rf"(?P<name>{_IDENT_JS})"
            ),
            "export {kind} {name}",
        ),
        (re.compile(r"^export\s+default\b"), "export default"),
        (re.compile(r"^export\s*(?:type\s*)?\{(?P<names>[^}]*)\}"), "export {name}"),
        (re.compile(rf"^(?:module\.)?exports\.(?P<name>{_IDENT_JS})\s*="), "exports.{name}"),
        (re.compile(r"^module\.exports\s*="), "module.exports"),
    ),
)
SYNTAX[Language.JAVASCRIPT] = _JS_SYNTAX
SYNTAX[Language.TYPESCRIPT] = _JS_SYNTAX


def strip_comment(line: str, syntax: Syntax) -> str:
    """``line`` without its trailing comment; a comment marker inside a string is kept.

    A string still open at the end of the line (a multi-line literal) keeps the
    rest of the line, so nothing is ever removed on a guess.
    """
    quote = ""
    i = 0
    while i < len(line):
        char = line[i]
        if quote:
            if char == "\\":
                i += 2
                continue
            if char == quote:
                quote = ""
        elif char in syntax.quotes:
            quote = char
        elif line.startswith(syntax.line_comment, i):
            return line[:i]
        elif syntax.block_comments and line.startswith("/*", i):
            end = line.find("*/", i + 2)
            if end == -1:
                return line[:i]
            line = line[:i] + " " + line[end + 2 :]
        i += 1
    return line


def code_text(line: str, syntax: Syntax) -> str | None:
    """The normalised code on a line, or ``None`` for blank and comment-only lines."""
    if syntax.block_comments and _C_LIKE_STAR.match(line):
        return None
    text = strip_comment(line, syntax)
    text = text.rstrip() if syntax.indent_matters else text.strip()
    return text if text.strip() else None


def declarations(code: Iterable[str], syntax: Syntax) -> set[str]:
    """Labels of the public declarations on normalised code lines."""
    found: set[str] = set()
    for line in code:
        for pattern, label in syntax.api:
            match = pattern.match(line)
            if match is None:
                continue
            groups = match.groupdict()
            if "names" in groups:
                for item in (groups["names"] or "").split(","):
                    name = item.split(" as ")[-1].strip()
                    if re.fullmatch(_IDENT_JS, name):
                        found.add(label.format(name=name))
            else:
                found.add(label.format(**groups))
            break
    return found


Region = tuple[int, int]
"""A 1-based, inclusive line range."""


def _in(regions: Sequence[Region], line: int) -> bool:
    return any(start <= line <= end for start, end in regions)


def _assertions(lines: Iterable[str], syntax: Syntax) -> Counter[str]:
    codes = (code_text(line, syntax) for line in lines)
    return Counter(c.strip() for c in codes if c is not None and syntax.assertion.search(c))


def analyze(
    patch: FilePatch,
    language: Language | None,
    new_regions: Sequence[Region] = (),
    old_regions: Sequence[Region] = (),
) -> PatchStats:
    """Measure one file's patch; ``*_regions`` are inline test modules in each version."""
    hashes = tuple(h for hunk in patch.hunks if (h := hunk_hash(hunk.deleted, hunk.added)))
    syntax = SYNTAX.get(language) if language is not None else None
    if syntax is None:
        count = len(patch.hunks)
        return PatchStats(count, count, patch.added, patch.deleted, hunk_hashes=hashes)
    code_hunks = code_added = code_deleted = test_added = test_deleted = asserts = 0
    api: set[str] = set()
    tests_only = bool(new_regions or old_regions)
    for hunk in patch.hunks:
        old = list(enumerate(hunk.deleted, hunk.old_start))
        new = list(enumerate(hunk.added, hunk.new_start))
        old_test = [line for number, line in old if _in(old_regions, number)]
        new_test = [line for number, line in new if _in(new_regions, number)]
        test_deleted += len(old_test)
        test_added += len(new_test)
        old_code = [
            c for n, line in old if not _in(old_regions, n) and (c := code_text(line, syntax))
        ]
        new_code = [
            c for n, line in new if not _in(new_regions, n) and (c := code_text(line, syntax))
        ]
        if old_code != new_code:
            code_hunks += 1
            code_added += len(new_code)
            code_deleted += len(old_code)
            api |= declarations(old_code + new_code, syntax)
        added_scope = new_test if tests_only else hunk.added
        deleted_scope = old_test if tests_only else hunk.deleted
        gained = _assertions(added_scope, syntax) - _assertions(deleted_scope, syntax)
        asserts += sum(gained.values())
    return PatchStats(
        hunks=len(patch.hunks),
        code_hunks=code_hunks,
        code_added=code_added,
        code_deleted=code_deleted,
        test_added=test_added,
        test_deleted=test_deleted,
        asserts=asserts,
        api=tuple(sorted(api)[:MAX_API_NAMES]),
        hunk_hashes=hashes,
    )


_CFG_TEST_LINE = re.compile(r"^\s*#\[cfg\((?:all\()?test\b")
_CODE_TOKEN = re.compile(r"//|/\*|(?<![\w])b?r(#*)\"|\"|'|[{};]")
_COMMENT_TOKEN = re.compile(r"/\*|\*/")
_STRING_TOKEN = re.compile(r"\\.|\"", re.DOTALL)
_CHAR_LITERAL = re.compile(r"'(?:\\(?:u\{[0-9A-Fa-f_]*\}|x[0-9A-Fa-f]{2}|.)|[^\\'])'")


class _RustLexer:
    """Just enough of Rust's lexical grammar to match braces: comments, strings, chars.

    State (an open block comment or string) carries over from one line to the
    next. Each step jumps with a regex to the next token that matters.
    """

    def __init__(self) -> None:
        self.comment_depth = 0
        self.raw_end: str | None = None
        self.in_string = False

    def braces(self, line: str) -> Iterator[str]:
        """Yield the ``{``, ``}`` and ``;`` characters of ``line`` that are code."""
        i = 0
        while i < len(line):
            if self.comment_depth:
                found = _COMMENT_TOKEN.search(line, i)
                if found is None:
                    return
                self.comment_depth += 1 if found.group() == "/*" else -1
                i = found.end()
            elif self.raw_end is not None:
                end = line.find(self.raw_end, i)
                if end == -1:
                    return
                i = end + len(self.raw_end)
                self.raw_end = None
            elif self.in_string:
                found = _STRING_TOKEN.search(line, i)
                if found is None:
                    return
                i = found.end()
                self.in_string = found.group() != '"'
            else:
                found = _CODE_TOKEN.search(line, i)
                if found is None:
                    return
                token, i = found.group(), found.end()
                if token == "//":
                    return
                if token == "/*":
                    self.comment_depth = 1
                elif found.group(1) is not None:
                    self.raw_end = '"' + found.group(1)
                elif token == '"':
                    self.in_string = True
                elif token == "'":
                    char = _CHAR_LITERAL.match(line, found.start())
                    i = char.end() if char is not None else i  # else a lifetime such as 'a
                else:
                    yield token


def rust_test_regions(content: bytes) -> tuple[Region, ...]:
    """Line ranges of ``#[cfg(test)]`` items: from the attribute to the item's closing brace.

    An item that ends in ``;`` before any brace (``mod tests;``) ends there; one
    whose braces never balance runs to the end of the text.
    """
    lines = content.decode("utf-8", "replace").split("\n")
    regions: list[Region] = []
    index = 0
    while index < len(lines):
        if not _CFG_TEST_LINE.match(lines[index]):
            index += 1
            continue
        end = _item_end(lines, index)
        regions.append((index + 1, end + 1))
        index = end + 1
    return tuple(regions)


def _item_end(lines: Sequence[str], start: int) -> int:
    lexer = _RustLexer()
    depth = 0
    for number in range(start, len(lines)):
        for char in lexer.braces(lines[number]):
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth <= 0:
                    return number
            elif depth == 0:
                return number  # "mod tests;" or "use super::*;"
    return len(lines) - 1
