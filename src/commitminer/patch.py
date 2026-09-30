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
    """Split the patch text of one commit into file blocks and hunks, in output order.

    A path whose type changed (a regular file became a symlink or a submodule,
    or back) is one ``--numstat`` entry, but git prints it as two blocks under
    the same ``diff --git`` line: the old file deleted, then the new one created.
    Such a pair is merged into one :class:`FilePatch`, so the blocks still line
    up with the numstat entries.
    """
    lines = text.split(b"\n")
    patches: list[FilePatch] = []
    previous: tuple[bytes, bool] | None = None  # header line, "deleted file mode" seen
    i = 0
    while i < len(lines):
        if not lines[i]:
            i += 1
            continue
        if not lines[i].startswith(DIFF_HEADER):
            raise PatchError(f"expected a diff header, got {lines[i][:60]!r}")
        header = lines[i]
        i += 1
        binary = deleted_file = new_file = False
        while i < len(lines) and lines[i] and not lines[i].startswith((b"@@", DIFF_HEADER)):
            if lines[i].startswith((b"Binary files ", b"GIT binary patch")):
                binary = True
            deleted_file |= lines[i].startswith(b"deleted file mode ")
            new_file |= lines[i].startswith(b"new file mode ")
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
        if new_file and previous == (header, True):
            # The second half of a type change: one file, one patch.
            first = patches.pop()
            patches.append(FilePatch(first.hunks + tuple(hunks), first.binary or binary))
            previous = None
            continue
        patches.append(FilePatch(tuple(hunks), binary))
        previous = (header, deleted_file)
    return patches


_TEXT_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class _Runs:
    """Collects the runs of deleted and added lines of a unified diff as hunks."""

    def __init__(self) -> None:
        self.hunks: list[Hunk] = []
        self.deleted: list[str] = []
        self.added: list[str] = []
        self.start = (0, 0)

    def begin(self, old: int, new: int) -> None:
        if not self.deleted and not self.added:
            self.start = (old, new)

    def flush(self) -> None:
        if self.deleted or self.added:
            old, new = self.start
            # git numbers an empty side by the line before it, as in "@@ -2,0 +3 @@".
            self.hunks.append(
                Hunk(
                    old if self.deleted else old - 1,
                    new if self.added else new - 1,
                    tuple(self.deleted),
                    tuple(self.added),
                )
            )
            self.deleted, self.added = [], []


def hunks_from_unified(text: str) -> tuple[Hunk, ...]:
    """Split a unified diff with context lines into ``--unified=0`` hunks.

    GitHub lists a pull request's files with a patch of three context lines.
    Every run of deleted and added lines between context lines becomes one
    hunk, numbered the way ``git diff --unified=0`` numbers it, so the result
    can be measured like the walker's patches. Each hunk header's line counts
    are checked.
    """
    runs = _Runs()
    old_no = new_no = old_left = new_left = 0
    for line in text.split("\n"):
        if line.startswith("\\"):
            continue  # "\ No newline at end of file"
        if old_left == 0 and new_left == 0:
            if not line:
                continue
            match = _TEXT_HUNK.match(line)
            if match is None:
                raise PatchError(f"expected a hunk header, got {line[:60]!r}")
            runs.flush()
            old_left = 1 if match.group(2) is None else int(match.group(2))
            new_left = 1 if match.group(4) is None else int(match.group(4))
            # An empty range starts at the line before it.
            old_no = int(match.group(1)) + (0 if old_left else 1)
            new_no = int(match.group(3)) + (0 if new_left else 1)
            continue
        tag, body = line[:1], line[1:]
        if tag == "-":
            if runs.added:
                runs.flush()
            runs.begin(old_no, new_no)
            runs.deleted.append(body.removesuffix("\r"))
            old_no, old_left = old_no + 1, old_left - 1
        elif tag == "+":
            runs.begin(old_no, new_no)
            runs.added.append(body.removesuffix("\r"))
            new_no, new_left = new_no + 1, new_left - 1
        elif tag in (" ", ""):
            runs.flush()
            old_no, new_no = old_no + 1, new_no + 1
            old_left, new_left = old_left - 1, new_left - 1
        else:
            raise PatchError(f"unexpected line in a hunk: {line[:60]!r}")
        if old_left < 0 or new_left < 0:
            raise PatchError("a hunk has more lines than its header says")
    if old_left or new_left:
        raise PatchError("the patch ends inside a hunk")
    runs.flush()
    return tuple(runs.hunks)


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
    regex_literals: bool = False
    """``/.../`` regular expression literals (JavaScript), whose ``//`` is not a comment."""


_IDENT_JS = r"[A-Za-z_$][\w$]*"
_C_LIKE_STAR = re.compile(r"^\s*\*(?:\s|/|$)")
"""A line that starts like a block comment continuation: ``*`` then a space, ``/`` or nothing."""
_COMMENT_TAIL = re.compile(r"^\s*\*(?:/|\s*$)")
"""A bare ``*`` or a line that starts by closing a block comment: never code."""

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
    regex_literals=True,
)
SYNTAX[Language.JAVASCRIPT] = _JS_SYNTAX
SYNTAX[Language.TYPESCRIPT] = _JS_SYNTAX


_REGEX_LITERAL = re.compile(r"/(?![*/])(?:\\.|\[(?:\\.|[^\]\\])*\]|[^/\\\[])+/")
_REGEX_BEFORE_CHARS = frozenset("(,=:[!&|?{};+-*%<>~^")
_REGEX_BEFORE_WORD = re.compile(
    r"\b(?:return|typeof|instanceof|in|of|new|delete|void|throw|case|do|else|yield|await)$"
)


def regex_end(line: str, start: int) -> int | None:
    """Where the JavaScript regex literal at ``line[start]`` (a ``/``) ends, or ``None``.

    A ``/`` opens a regex only where an expression can start: at the start of
    the line, after an operator or opening bracket, or after a keyword such as
    ``return``. Elsewhere it divides. The literal must close on the same line.
    """
    before = line[:start].rstrip()
    if before and before[-1] not in _REGEX_BEFORE_CHARS and not _REGEX_BEFORE_WORD.search(before):
        return None
    found = _REGEX_LITERAL.match(line, start)
    return found.end() if found is not None else None


def strip_comment(line: str, syntax: Syntax) -> str:
    """``line`` without its trailing comment; a comment marker inside a string is kept.

    A string still open at the end of the line (a multi-line literal) keeps the
    rest of the line, so nothing is ever removed on a guess. In JavaScript and
    TypeScript a regex literal such as ``/^https?:\\/\\//`` is skipped like a string.
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
        elif syntax.regex_literals and char == "/" and (stop := regex_end(line, i)) is not None:
            i = stop
            continue
        i += 1
    return line


def code_text(line: str, syntax: Syntax, in_comment: bool | None = None) -> str | None:
    """The normalised code on a line, or ``None`` for blank and comment-only lines.

    ``in_comment`` says whether the line starts inside a block comment, when
    that is known from the file's contents. It matters only for a line that
    starts with ``*``, which is either a comment continuation (`` * Returns``)
    or code (an operator-first continuation such as ``* height``, the default
    style of rustfmt and google-java-format). When it is not known, only a
    bare ``*`` or a line that starts with ``*/`` is taken as comment; every
    other ``*`` line is code, so a real change is never dropped on a guess.
    """
    if syntax.block_comments and _C_LIKE_STAR.match(line):
        if in_comment is None:
            in_comment = _COMMENT_TAIL.match(line) is not None
        if in_comment:
            end = line.find("*/")
            if end == -1:
                return None
            line = line[end + 2 :]
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


def _assertions(codes: Iterable[str | None], syntax: Syntax) -> Counter[str]:
    return Counter(c.strip() for c in codes if c is not None and syntax.assertion.search(c))


def _has_star_lines(lines: Iterable[str]) -> bool:
    return any(_C_LIKE_STAR.match(line) for line in lines)


def star_sides(patch: FilePatch, language: Language | None) -> tuple[bool, bool]:
    """Whether the deleted and the added lines of ``patch`` have lines that start with ``*``.

    Only for languages with block comments: those lines need to know whether
    they sit inside a comment (see :func:`code_text` and :func:`comment_regions`).
    """
    syntax = SYNTAX.get(language) if language is not None else None
    if syntax is None or not syntax.block_comments:
        return False, False
    old = any(_has_star_lines(hunk.deleted) for hunk in patch.hunks)
    new = any(_has_star_lines(hunk.added) for hunk in patch.hunks)
    return old, new


def _codes(
    lines: Sequence[str],
    start: int,
    syntax: Syntax,
    lexicon: _Lexicon | None,
    comments: Sequence[Region] | None,
) -> list[str | None]:
    """The code text of each line on one side of a hunk (``start`` is its first line number).

    With ``comments`` (the lines of the file that start inside a block
    comment) a ``*`` line is judged by where it sits. Without them, only a
    comment opened earlier in the same hunk makes a ``*`` line a comment.
    """
    if lexicon is None or not _has_star_lines(lines):
        return [code_text(line, syntax) for line in lines]
    inside: list[bool | None]
    if comments is not None:
        inside = [_in(comments, start + offset) for offset in range(len(lines))]
    else:
        lexer = _Lexer(lexicon)
        inside = []
        for line in lines:
            inside.append(True if lexer.in_comment else None)
            lexer.skip(line)
    return [code_text(line, syntax, flag) for line, flag in zip(lines, inside, strict=True)]


def analyze(
    patch: FilePatch,
    language: Language | None,
    new_regions: Sequence[Region] = (),
    old_regions: Sequence[Region] = (),
    new_comments: Sequence[Region] | None = None,
    old_comments: Sequence[Region] | None = None,
) -> PatchStats:
    """Measure one file's patch.

    ``*_regions`` are inline test modules in each version of the file.
    ``*_comments`` are the lines of each version that start inside a block
    comment (see :func:`comment_regions`), or ``None`` when the contents were
    not read.
    """
    hashes = tuple(h for hunk in patch.hunks if (h := hunk_hash(hunk.deleted, hunk.added)))
    syntax = SYNTAX.get(language) if language is not None else None
    if syntax is None or language is None:
        count = len(patch.hunks)
        return PatchStats(count, count, patch.added, patch.deleted, hunk_hashes=hashes)
    lexicon = LEXICONS.get(language)
    code_hunks = code_added = code_deleted = test_added = test_deleted = asserts = 0
    api: set[str] = set()
    tests_only = bool(new_regions or old_regions)
    for hunk in patch.hunks:
        old_codes = _codes(hunk.deleted, hunk.old_start, syntax, lexicon, old_comments)
        new_codes = _codes(hunk.added, hunk.new_start, syntax, lexicon, new_comments)
        old = list(enumerate(old_codes, hunk.old_start))
        new = list(enumerate(new_codes, hunk.new_start))
        old_test = [code for number, code in old if _in(old_regions, number)]
        new_test = [code for number, code in new if _in(new_regions, number)]
        test_deleted += len(old_test)
        test_added += len(new_test)
        old_code = [c for n, c in old if c is not None and not _in(old_regions, n)]
        new_code = [c for n, c in new if c is not None and not _in(new_regions, n)]
        if old_code != new_code:
            code_hunks += 1
            code_added += len(new_code)
            code_deleted += len(old_code)
            api |= declarations(old_code + new_code, syntax)
        added_scope = new_test if tests_only else new_codes
        deleted_scope = old_test if tests_only else old_codes
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


# --- a small lexer for C-like languages -----------------------------------------------


@dataclass(frozen=True, slots=True)
class _Quote:
    """How a string literal ends: its closing text, and whether it may span lines."""

    end: str
    multiline: bool
    escape: re.Pattern[str] | None = None
    """``\\\\.|<end>`` when backslash escapes apply."""


@dataclass(frozen=True, slots=True)
class _Lexicon:
    """The tokens of one C-like language that matter for comments and braces."""

    code: re.Pattern[str]
    """Comment starts, string and char openers, and ``{``, ``}``, ``;``."""
    quotes: dict[str, _Quote]
    nested_comments: bool = False
    char_literals: bool = False
    """``'x'`` is a char literal (Rust, Go, Java), not a string (JavaScript)."""
    regex_literals: bool = False


def _quote(end: str, multiline: bool, escapes: bool = True) -> _Quote:
    return _Quote(end, multiline, re.compile(r"\\.|" + re.escape(end)) if escapes else None)


_DOUBLE = _quote('"', multiline=False)
_JS_LEXICON = _Lexicon(
    code=re.compile(r"//|/\*|\"|'|`|/|[{};]"),
    quotes={'"': _DOUBLE, "'": _quote("'", multiline=False), "`": _quote("`", multiline=True)},
    regex_literals=True,
)
LEXICONS: Final[dict[Language, _Lexicon]] = {
    Language.RUST: _Lexicon(
        code=re.compile(r"//|/\*|(?<![\w])b?r(#*)\"|\"|'|[{};]"),
        quotes={'"': _quote('"', multiline=True)},
        nested_comments=True,
        char_literals=True,
    ),
    Language.GO: _Lexicon(
        code=re.compile(r"//|/\*|\"|`|'|[{};]"),
        quotes={'"': _DOUBLE, "`": _quote("`", multiline=True, escapes=False)},
        char_literals=True,
    ),
    Language.JAVA: _Lexicon(
        code=re.compile(r"//|/\*|\"\"\"|\"|'|[{};]"),
        quotes={'"': _DOUBLE, '"""': _quote('"""', multiline=True)},
        char_literals=True,
    ),
    Language.JAVASCRIPT: _JS_LEXICON,
    Language.TYPESCRIPT: _JS_LEXICON,
}
"""Lexical rules of the languages with block comments."""

_NESTED_COMMENT = re.compile(r"/\*|\*/")
_CHAR_LITERAL = re.compile(
    r"'(?:\\(?:u\{[0-9A-Fa-f_]*\}|u[0-9A-Fa-f]{4}|U[0-9A-Fa-f]{8}|x[0-9A-Fa-f]{2}|[0-7]{1,3}|.)"
    r"|[^\\'])'"
)


class _Lexer:
    """Just enough of a C-like lexical grammar to find comments and match braces.

    State (an open block comment or a multi-line string) carries over from one
    line to the next; a one-line string left open at the end of a line is
    dropped there. Each step jumps with a regex to the next token that matters.
    """

    def __init__(self, lexicon: _Lexicon) -> None:
        self.lexicon = lexicon
        self.comment_depth = 0
        self.string: _Quote | None = None

    @property
    def in_comment(self) -> bool:
        """True while inside a block comment."""
        return self.comment_depth > 0

    def skip(self, line: str) -> None:
        """Read ``line`` for its effect on the state."""
        for _ in self.braces(line):
            pass

    def braces(self, line: str) -> Iterator[str]:
        """Yield the ``{``, ``}`` and ``;`` characters of ``line`` that are code."""
        i = 0
        while i < len(line):
            if self.comment_depth:
                i = self._comment(line, i)
            elif self.string is not None:
                i = self._string(line, i, self.string)
            else:
                found = self.lexicon.code.search(line, i)
                if found is None:
                    return
                token, i = found.group(), found.end()
                if token == "//":
                    return
                if token in "{};":
                    yield token
                else:
                    i = self._open(line, found, token, i)
        if self.string is not None and not self.string.multiline:
            self.string = None

    def _open(self, line: str, found: re.Match[str], token: str, i: int) -> int:
        """Enter the comment, string or literal that ``token`` opens; return the next index."""
        lexicon = self.lexicon
        if token == "/*":
            self.comment_depth = 1
        elif found.lastindex:  # a Rust raw string: r"...", r#"..."#, br"..."
            self.string = _Quote('"' + (found.group(1) or ""), multiline=True)
        elif token == "'" and lexicon.char_literals:
            char = _CHAR_LITERAL.match(line, found.start())
            return char.end() if char is not None else i  # else a lifetime such as 'a
        elif token == "/":
            end = regex_end(line, found.start())
            return end if end is not None else i
        else:
            self.string = lexicon.quotes[token]
        return i

    def _comment(self, line: str, i: int) -> int:
        if not self.lexicon.nested_comments:
            end = line.find("*/", i)
            if end == -1:
                return len(line)
            self.comment_depth = 0
            return end + 2
        found = _NESTED_COMMENT.search(line, i)
        if found is None:
            return len(line)
        self.comment_depth += 1 if found.group() == "/*" else -1
        return found.end()

    def _string(self, line: str, i: int, quote: _Quote) -> int:
        if quote.escape is None:
            end = line.find(quote.end, i)
            if end == -1:
                return len(line)
            self.string = None
            return end + len(quote.end)
        found = quote.escape.search(line, i)
        if found is None:
            return len(line)
        if found.group() == quote.end:
            self.string = None
        return found.end()


def _text_lines(content: bytes) -> list[str]:
    return content.decode("utf-8", "replace").split("\n")


def comment_regions(content: bytes, language: Language) -> tuple[Region, ...]:
    """Line ranges whose lines start inside a block comment (``/* ... */``).

    In ``/**``, `` * doc``, `` */`` the second and third lines start inside the
    comment, the first does not. Languages without block comments have none.
    """
    lexicon = LEXICONS.get(language)
    if lexicon is None:
        return ()
    lexer = _Lexer(lexicon)
    regions: list[Region] = []
    for number, line in enumerate(_text_lines(content), 1):
        if lexer.in_comment:
            if regions and regions[-1][1] == number - 1:
                regions[-1] = (regions[-1][0], number)
            else:
                regions.append((number, number))
        lexer.skip(line)
    return tuple(regions)


_CFG_TEST_LINE = re.compile(r"^\s*#\[cfg\((?:all\()?test\b")


def rust_test_regions(content: bytes) -> tuple[Region, ...]:
    """Line ranges of ``#[cfg(test)]`` items: from the attribute to the item's closing brace.

    An item that ends in ``;`` before any brace (``mod tests;``) ends there; one
    whose braces never balance runs to the end of the text.
    """
    lines = _text_lines(content)
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
    lexer = _Lexer(LEXICONS[Language.RUST])
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
