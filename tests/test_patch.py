"""Patch parsing and per-language line analysis (commitminer.patch)."""

from __future__ import annotations

import pytest

from commitminer.languages import Language
from commitminer.models import PatchStats
from commitminer.patch import (
    SYNTAX,
    FilePatch,
    Hunk,
    PatchError,
    analyze,
    code_text,
    declarations,
    parse_patch,
    rust_test_regions,
    strip_comment,
)

PY = SYNTAX[Language.PYTHON]
RS = SYNTAX[Language.RUST]
GO = SYNTAX[Language.GO]
JS = SYNTAX[Language.JAVASCRIPT]
JAVA = SYNTAX[Language.JAVA]


def block(*lines: str) -> bytes:
    return ("\n".join(lines) + "\n").encode()


# --- parse_patch --------------------------------------------------------------------


def test_parse_hunks_by_count() -> None:
    text = block(
        "diff --git a/a.py b/a.py",
        "index 1..2 100644",
        "--- a/a.py",
        "+++ b/a.py",
        "@@ -3 +3,2 @@ def f():",
        "--- old dashes",
        "++++ new pluses",
        "+second",
        "@@ -9,0 +11 @@",
        "+tail\r",
        "\\ No newline at end of file",
        "diff --git a/gone.txt b/gone.txt",
        "deleted file mode 100644",
        "@@ -1,2 +0,0 @@",
        "-x",
        "-",
    )
    first, second = parse_patch(text)
    assert first == FilePatch(
        (
            Hunk(3, 3, ("-- old dashes",), ("+++ new pluses", "second")),
            Hunk(9, 11, (), ("tail",)),
        )
    )
    assert (first.added, first.deleted) == (3, 1)
    assert second.hunks == (Hunk(1, 0, ("x", ""), ()),)


def test_parse_binary_rename_and_mode_blocks() -> None:
    text = block(
        "diff --git a/logo.png b/logo.png",
        "new file mode 100644",
        "Binary files /dev/null and b/logo.png differ",
        "diff --git a/old.py b/new.py",
        "similarity index 100%",
        "rename from old.py",
        "rename to new.py",
        "diff --git a/run.sh b/run.sh",
        "old mode 100644",
        "new mode 100755",
    )
    binary, rename, mode = parse_patch(text)
    assert binary == FilePatch((), binary=True)
    assert rename == FilePatch(())
    assert mode == FilePatch(())
    assert parse_patch(b"") == []


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (b"garbage\n", "expected a diff header"),
        (block("diff --git a/a b/a", "@@ bad @@"), "bad hunk header"),
        (block("diff --git a/a b/a", "@@ -1,2 +1 @@", "-only one"), "expected 2 lines"),
        (b"diff --git a/a b/a\n@@ -0,0 +1 @@", "end of patch"),
    ],
)
def test_parse_rejects_malformed_patches(text: bytes, message: str) -> None:
    with pytest.raises(PatchError, match=message):
        parse_patch(text)


# --- comments and code lines --------------------------------------------------------


@pytest.mark.parametrize(
    ("syntax", "line", "expected"),
    [
        (PY, "x = 1  # pragma: no cover", "x = 1  "),
        (PY, "s = '# not a comment'  # a comment", "s = '# not a comment'  "),
        (PY, 's = "a \\" # still text" # c', 's = "a \\" # still text" '),
        (PY, 'doc = """opens # here', 'doc = """opens # here'),
        (RS, 'let url = "http://x"; // note', 'let url = "http://x"; '),
        (RS, "a(/* inline */ b) // tail", "a(  b) "),
        (RS, "a(); /* opens", "a(); "),
        (GO, "x := `raw // text`", "x := `raw // text`"),
        (JAVA, "char c = '\"'; // quote", "char c = '\"'; "),
    ],
)
def test_strip_comment(syntax: object, line: str, expected: str) -> None:
    assert strip_comment(line, syntax) == expected  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("syntax", "line", "expected"),
    [
        (PY, "    x = 1  # why", "    x = 1"),
        (PY, "    # only a comment", None),
        (PY, "   ", None),
        (GO, "\t\tx := 1 // why", "x := 1"),
        (GO, " * continued block comment", None),
        (GO, " */", None),
        (GO, "*p = 1", "*p = 1"),
        (RS, "/* whole */", None),
    ],
)
def test_code_text(syntax: object, line: str, expected: str | None) -> None:
    assert code_text(line, syntax) == expected  # type: ignore[arg-type]


# --- public API ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("language", "line", "labels"),
    [
        (Language.PYTHON, "def load(fp, /, *, parse_float=float):", {"def load"}),
        (Language.PYTHON, "async def fetch[T](x: T) -> T:", {"def fetch"}),
        (Language.PYTHON, "class TOMLDecodeError(ValueError):", {"class TOMLDecodeError"}),
        (Language.PYTHON, "def _private(x):", set()),
        (Language.PYTHON, "    def method(self):", set()),
        (Language.RUST, "pub fn parse(s: &str) -> Result<Version, Error> {", {"pub fn parse"}),
        (Language.RUST, "pub const unsafe fn raw() {}", {"pub fn raw"}),
        (Language.RUST, "pub const MAX: u32 = 3;", {"pub const MAX"}),
        (Language.RUST, "pub static mut COUNT: u32 = 0;", {"pub static COUNT"}),
        (Language.RUST, 'pub extern "C" fn cb() {}', {"pub fn cb"}),
        (Language.RUST, "pub(crate) fn internal() {}", set()),
        (Language.RUST, "fn private() {}", set()),
        (Language.GO, "func (f *FlagSet) Parse(args []string) error {", {"func Parse"}),
        (Language.GO, "func New[T any]() *T {", {"func New"}),
        (Language.GO, "func helper() {}", set()),
        (Language.GO, "type Flag struct {", {"type Flag"}),
        (Language.GO, 'var ErrHelp = errors.New("help")', {"var ErrHelp"}),
        (Language.GO, "const maxDepth = 3", set()),
        (Language.JAVASCRIPT, "export async function load(url) {", {"export function load"}),
        (Language.JAVASCRIPT, "export function* ids() {", {"export function ids"}),
        (Language.JAVASCRIPT, "export default class Parser {", {"export class Parser"}),
        (Language.JAVASCRIPT, "export default (a) => a", {"export default"}),
        (Language.JAVASCRIPT, "export { parse, format as fmt }", {"export parse", "export fmt"}),
        (Language.JAVASCRIPT, "export { parse, }", {"export parse"}),
        (Language.JAVASCRIPT, "module.exports.parse = parse", {"exports.parse"}),
        (Language.JAVASCRIPT, "module.exports = { parse }", {"module.exports"}),
        (Language.TYPESCRIPT, "export interface Options {", {"export interface Options"}),
        (Language.TYPESCRIPT, "export type { Options }", {"export Options"}),
        (Language.TYPESCRIPT, "const local = 1", set()),
        (Language.JAVA, "public final class Parser {", {"public class Parser"}),
        (Language.JAVA, "public static <T> List<T> of(T... xs) {", {"public of()"}),
        (
            Language.JAVA,
            "public Map<String, Integer> counts(String s) {",
            {"public counts()"},
        ),
        (Language.JAVA, "public Parser(String text) {", {"public Parser()"}),
        (Language.JAVA, "private int size() {", set()),
    ],
)
def test_declarations(language: Language, line: str, labels: set[str]) -> None:
    syntax = SYNTAX[language]
    code = code_text(line, syntax)
    assert code is not None
    assert declarations([code], syntax) == labels


# --- analyze ------------------------------------------------------------------------


def patch(*hunks: Hunk) -> FilePatch:
    return FilePatch(tuple(hunks))


def test_comment_only_hunks_are_not_code() -> None:
    pragma = patch(
        Hunk(10, 10, ("    import re",), ("    import re  # pragma: no cover",)),
        Hunk(20, 20, (), ("    # explain the next line", "")),
    )
    assert analyze(pragma, Language.PYTHON) == PatchStats(2, 0, 0, 0)


def test_python_indentation_is_code_but_go_indentation_is_not() -> None:
    moved = patch(Hunk(5, 5, ("        return x",), ("    return x",)))
    assert analyze(moved, Language.PYTHON).code_hunks == 1
    reindented = patch(Hunk(5, 5, ("\treturn x",), ("\t\treturn x",)))
    assert analyze(reindented, Language.GO) == PatchStats(1, 0, 0, 0)


def test_code_lines_skip_blanks_and_comments_and_collect_api() -> None:
    fix = patch(
        Hunk(3, 3, ("def load(fp):",), ("def load(fp, *, strict=False):", "    # new flag", "")),
        Hunk(40, 42, (), ("def _helper():", "    return 1")),
    )
    stats = analyze(fix, Language.PYTHON)
    assert stats == PatchStats(2, 2, 3, 1, api=("def load",))
    assert (stats.code_lines, stats.test_lines) == (4, 0)


def test_a_statement_moved_between_hunks_is_a_code_change() -> None:
    swap = patch(Hunk(3, 2, ("a()",), ()), Hunk(6, 5, (), ("a()",)))
    assert analyze(swap, Language.GO).code_hunks == 2


def test_assertions_are_counted_net_of_deletions() -> None:
    tests = patch(
        Hunk(
            1,
            1,
            ("    assert parse('a') == 1", "    assert old()"),
            (
                "    assert parse('a') == 1",
                "        assert parse('b') == 2  # moved into a block",
                "    self.assertEqual(x, 3)",
                "    with pytest.raises(ValueError):",
                "    # assert in a comment does not count",
                "    assert_frame_equal(a, b)",
            ),
        )
    )
    assert analyze(tests, Language.PYTHON).asserts == 4


@pytest.mark.parametrize(
    ("language", "line"),
    [
        (Language.RUST, "assert_eq!(v.major, 1);"),
        (Language.RUST, 'assert_match(&r, &["1.0.0"]);'),
        (Language.RUST, '#[should_panic(expected = "empty")]'),
        (Language.GO, 't.Errorf("got %v", got)'),
        (Language.GO, "require.NoError(t, err)"),
        (Language.JAVASCRIPT, "expect(parse('a')).toBe(1)"),
        (Language.TYPESCRIPT, "assert.deepStrictEqual(a, b)"),
        (Language.JAVASCRIPT, "t.deepEqual(a, b)"),
        (Language.JAVA, 'assertEquals(1, parse("a"));'),
        (Language.JAVA, "assertThrows(IllegalStateException.class, () -> run());"),
    ],
)
def test_assertion_patterns(language: Language, line: str) -> None:
    assert analyze(patch(Hunk(0, 1, (), (line,))), language).asserts == 1


def test_unknown_languages_count_every_line_as_code() -> None:
    data = patch(Hunk(0, 1, (), ("# heading", "")), Hunk(5, 7, ("x",), ()))
    assert analyze(data, None) == PatchStats(2, 2, 2, 1)


# --- Rust inline test modules -------------------------------------------------------

LIB = """\
pub fn add(a: i32, b: i32) -> i32 {
    a + b
}

#[cfg(test)]
mod tests {
    // a brace in a comment: {
    /* nested /* block { */ comment */
    const OPEN: char = '{';
    const S: &str = "}\\" {";
    const R: &str = r#"raw } "quoted" "#;
    fn longest<'a>(x: &'a str) -> &'a str { x }

    #[test]
    fn adds() {
        assert_eq!(super::add(1, 2), 3);
    }
}

pub fn sub(a: i32, b: i32) -> i32 {
    a - b
}
"""


def test_rust_test_regions_skip_braces_in_comments_strings_and_chars() -> None:
    assert rust_test_regions(LIB.encode()) == ((5, 18),)


def test_rust_test_regions_other_shapes() -> None:
    declared = b"#[cfg(test)]\nmod tests;\n\npub fn f() {}\n"
    assert rust_test_regions(declared) == ((1, 2),)
    helper = b'#[cfg(all(test, feature = "x"))]\nuse super::*;\n'
    assert rust_test_regions(helper) == ((1, 2),)
    unbalanced = b"fn a() {}\n#[cfg(test)]\nmod tests {\n    fn x() {\n"
    assert rust_test_regions(unbalanced) == ((2, 5),)
    two = b"#[cfg(test)]\nfn a() {}\n\n#[cfg(test)]\nmod b {\n}\n"
    assert rust_test_regions(two) == ((1, 2), (4, 6))
    string_across_lines = b'#[cfg(test)]\nmod t {\n    const S: &str = "a\n}";\n}\nfn f() {}\n'
    assert rust_test_regions(string_across_lines) == ((1, 5),)
    assert rust_test_regions(b"pub fn f() {}\n") == ()
    across = (
        b"#[cfg(test)]\nmod t {\n    /* open\n       } still a comment */\n"
        b'    const R: &str = r#"one\n    } two"#;\n}\nfn f() {}\n'
    )
    assert rust_test_regions(across) == ((1, 7),)


def test_inline_test_lines_are_split_from_code() -> None:
    lines = LIB.splitlines()
    fix = patch(
        Hunk(2, 2, ("    a.wrapping_add(b)",), (lines[1],)),
        Hunk(13, 14, (), lines[13:17]),
        Hunk(21, 21, ("    // old comment",), (lines[20],)),
    )
    regions = rust_test_regions(LIB.encode())
    stats = analyze(fix, Language.RUST, regions, ((12, 17),))
    assert stats == PatchStats(3, 2, 2, 1, test_added=4, asserts=1)


def test_assertions_outside_inline_test_modules_do_not_count_when_there_are_some() -> None:
    code = patch(Hunk(1, 2, (), ("    debug_assert!(a > 0);",)))
    assert analyze(code, Language.RUST, ((10, 20),)).asserts == 0
    assert analyze(code, Language.RUST).asserts == 1
