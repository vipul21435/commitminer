"""Glob syntax, pattern checks and matching time bounds."""

from __future__ import annotations

import time

import pytest

from commitminer.globs import (
    MAX_ALTERNATIVES,
    GlobError,
    check_pattern,
    component_regex,
    expand_braces,
    path_glob,
)


@pytest.mark.parametrize(
    ("pattern", "text", "expected"),
    [
        ("*.py", "a.py", True),
        ("*.py", "a.pyc", False),
        ("a?c", "abc", True),
        ("a?c", "ac", False),
        ("*.{js,ts}", "a.ts", True),
        ("*.{js,ts}", "a.tsx", False),
        ("deno.json{,c}", "deno.json", True),
        ("deno.json{,c}", "deno.jsonc", True),
        ("Test[A-Z]*", "TestX", True),
        ("Test[!A-Z]*", "TestX", False),
        ("Test[!A-Z]*", "Test_x", True),
        ("[^a]", "^", True),
        ("[^a]", "b", False),
        ("[]a]", "]", True),
        ("[{]x}", "{x}", True),
        ("a[", "a[", True),
        ("x[!]y", "x[!]y", True),
        ("a{b", "a{b", True),
        ("a.b", "axb", False),
        ("**.py", "a.py", True),
        ("(a)|b", "(a)|b", True),
    ],
)
def test_component_regex(pattern: str, text: str, expected: bool) -> None:
    assert (component_regex(pattern, case_sensitive=True).match(text) is not None) is expected


def test_component_case_sensitivity() -> None:
    assert component_regex("*Test.java").match("latest.java") is not None
    assert component_regex("*Test.java", case_sensitive=True).match("latest.java") is None


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("**/src/test/**", "src/test/java/A.java", True),
        ("**/src/test/**", "mod/src/test/r.json", True),
        ("**/src/test/**", "src/tests/A.java", False),
        ("**/src/test/**", "src/test", False),
        (".github/**", ".github/workflows/ci.yml", True),
        (".github/**", ".github", False),
        (".gitlab-ci*", ".gitlab-ci.yml", True),
        (".gitlab-ci*", "sub/.gitlab-ci.yml", False),
        ("*.py", "pkg/a.py", False),
        ("a/*/c", "a/b/c", True),
        ("a/*/c", "a/b/b/c", False),
        ("a/**/c", "a/c", True),
        ("a/**/c", "a/x/y/c", True),
        ("**/a/**/b/**", "a/b/x", True),
        ("**/a/**/b/**", "a/x/a/y/b/z", True),
        ("**/a/**/b/**", "b/a/x", False),
        ("**/**/x", "x", True),
        ("**", "any/path.txt", True),
        ("{src,lib}/**/*.rs", "lib/a/b.rs", True),
        ("{src,lib}/**/*.rs", "bin/a.rs", False),
    ],
)
def test_path_glob(pattern: str, path: str, expected: bool) -> None:
    assert path_glob(pattern).match_path(path) is expected


def test_expand_braces() -> None:
    assert expand_braces("a{b,c}d{e,f}") == ("abde", "abdf", "acde", "acdf")
    assert expand_braces("[{]{x,y}") == ("[{]x", "[{]y")
    assert expand_braces("plain") == ("plain",)
    with pytest.raises(GlobError, match=f"more than {MAX_ALTERNATIVES} alternatives"):
        expand_braces("{a,b,c,d}" * 5)


@pytest.mark.parametrize(
    ("pattern", "kind", "message"),
    [
        ("", "component", "empty pattern"),
        ("[z-a]*.json", "component", "empty character range z-a"),
        ("x[!b-a]", "component", "empty character range b-a"),
        ("src/fixtures", "component", "cannot contain '/'"),
        ("a\\b", "path", "backslash"),
        ("/src/**", "path", "without a leading or trailing '/'"),
        ("src/", "path", "without a leading or trailing '/'"),
        ("src//x", "path", "empty path segment"),
        ("{a,}/x", "path", "empty path segment"),
        ("{,}", "component", "an alternative is empty"),
    ],
)
def test_check_pattern_rejects(pattern: str, kind: str, message: str) -> None:
    with pytest.raises(GlobError, match=message):
        check_pattern(pattern, kind)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "pattern", ["[a-z]*", "x[!]y", "[-a]", "[a-]", "{a,b}/**/c", "**/src/main/**", "a-b"]
)
def test_check_pattern_accepts(pattern: str) -> None:
    check_pattern(pattern, "path")


def test_path_glob_checks_its_pattern() -> None:
    with pytest.raises(GlobError, match="empty path segment"):
        path_glob("a//b")


def _elapsed(action: object) -> float:
    start = time.perf_counter()
    assert callable(action)
    action()
    return time.perf_counter() - start


def test_many_globstars_match_in_linear_time() -> None:
    # A regex translation took 0.2 s with 8 "**" segments here and did not finish with 12.
    deep = "/".join(["a"] * 400) + "/y.py"
    glob = path_glob("/".join(["**"] * 12) + "/x")
    assert _elapsed(lambda: glob.match_path(deep)) < 0.5
    assert not glob.match_path(deep)
    spaced = path_glob("/".join(["**/a"] * 12) + "/**/b")
    assert _elapsed(lambda: spaced.match_path(deep)) < 0.5
    assert not spaced.match_path(deep)
    assert spaced.match_path("/".join(["a"] * 12) + "/b")


def test_many_stars_in_one_component_match_in_linear_time() -> None:
    regex = component_regex("*a" * 30 + "b", case_sensitive=True)
    assert _elapsed(lambda: regex.match("a" * 5000)) < 0.5
    assert regex.match("a" * 5000) is None
    assert regex.match("a" * 30 + "b") is not None
