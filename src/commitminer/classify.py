"""Multi-language file classifier driven by one ordered rule table.

Each row of :data:`RULES` has a rule id, the languages it applies to, a matcher,
a category and a rationale. The first rule that matches decides the category,
and the rule id travels with the result, so a reviewer can always see why a
file was put where it was.

Matchers (``kind``):

- ``dir``: some directory component of the path matches one of the globs.
- ``name``: the file name matches one of the globs.
- ``path``: the whole repository-relative path matches one of the globs;
  ``**/`` spans any number of directories.
- ``signal``: the file carries one of the named content signals
  (see :mod:`commitminer.signals`).

Globs support ``*``, ``?``, ``[...]`` and ``{a,b}``; ``*`` never crosses a
``/``. Matching is case-insensitive unless a rule sets ``case_sensitive``.
``languages`` restricts a rule to files of those languages (by extension); an
empty tuple means every file. Paths use ``/``; a ``\\`` is read as ``/``.

Order matters: vendored copies first (their tests are not this project's),
then test directories (so golden files and fixtures stay test data even when
they look generated), then generated files, test file names, the Java main
source set, configuration, documentation, tooling, source, and finally prose
file names such as README or LICENSE (after source, so a module named
``license.py`` stays source).
"""

from __future__ import annotations

import functools
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Literal, get_args

from commitminer.languages import Language, language_of
from commitminer.signals import GENERATED_HEADER, MINIFIED, RUST_INLINE_TESTS, SIGNALS


class Category(StrEnum):
    """What a changed file is, for the purpose of building a fail-to-pass task."""

    SOURCE = "source"
    TEST = "test"
    DOCS = "docs"
    CONFIG = "config"
    GENERATED = "generated"
    VENDORED = "vendored"
    OTHER = "other"


RuleKind = Literal["dir", "name", "path", "signal"]
RULE_KINDS: Final[tuple[str, ...]] = get_args(RuleKind)

_RULE_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _translate(pattern: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif char == "*":
            out.append("[^/]*")
            i += 1
        elif char == "?":
            out.append("[^/]")
            i += 1
        elif char == "[" and (end := pattern.find("]", i + 2)) != -1:
            body = pattern[i + 1 : end]
            negate = body.startswith("!")
            body = (body[1:] if negate else body).replace("\\", "\\\\")
            if body.startswith("^"):
                body = "\\" + body  # a literal caret, as in fnmatch
            out.append(("[^" if negate else "[") + body + "]")
            i = end + 1
        elif char == "{" and (end := pattern.find("}", i + 1)) != -1:
            options = pattern[i + 1 : end].split(",")
            out.append("(?:" + "|".join(_translate(option) for option in options) + ")")
            i = end + 1
        else:
            out.append(re.escape(char))
            i += 1
    return "".join(out)


@functools.cache
def glob_regex(pattern: str, case_sensitive: bool = False) -> re.Pattern[str]:
    """Compile a glob (``*``, ``**``, ``?``, ``[...]``, ``{a,b}``) to a full-match regex."""
    return re.compile(_translate(pattern), 0 if case_sensitive else re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Target:
    """A path prepared for matching: its parts, language and content signals."""

    path: str
    directories: tuple[str, ...]
    name: str
    language: Language | None
    signals: frozenset[str]

    @classmethod
    def of(cls, path: str, signals: Iterable[str] = ()) -> Target:
        """Normalise a repository-relative path (``\\`` to ``/``, no leading ``./``)."""
        normal = path.replace("\\", "/")
        while normal.startswith("./"):
            normal = normal[2:]
        parts = normal.split("/")
        return cls(normal, tuple(parts[:-1]), parts[-1], language_of(normal), frozenset(signals))


@dataclass(frozen=True, slots=True)
class Rule:
    """One row of the classifier table."""

    rule_id: str
    category: Category
    kind: RuleKind
    patterns: tuple[str, ...]
    rationale: str
    languages: tuple[Language, ...] = ()
    case_sensitive: bool = False

    def __post_init__(self) -> None:
        if not _RULE_ID.match(self.rule_id):
            raise ValueError(f"rule id {self.rule_id!r}: use lower-case letters, digits and -")
        if self.kind not in RULE_KINDS:
            raise ValueError(f"rule {self.rule_id}: unknown kind {self.kind!r}")
        if not self.patterns or not all(self.patterns):
            raise ValueError(f"rule {self.rule_id}: needs at least one non-empty pattern")
        if self.kind == "signal":
            unknown = sorted(set(self.patterns) - SIGNALS.keys())
            if unknown:
                raise ValueError(f"rule {self.rule_id}: unknown signal {unknown[0]!r}")
        if not self.rationale.strip():
            raise ValueError(f"rule {self.rule_id}: needs a rationale")

    def _glob(self, text: str) -> bool:
        return any(
            glob_regex(pattern, self.case_sensitive).fullmatch(text) for pattern in self.patterns
        )

    def matches(self, target: Target) -> bool:
        """True when this rule applies to ``target``."""
        if self.languages and target.language not in self.languages:
            return False
        if self.kind == "dir":
            return any(self._glob(part) for part in target.directories)
        if self.kind == "name":
            return self._glob(target.name)
        if self.kind == "path":
            return self._glob(target.path)
        return any(signal in target.signals for signal in self.patterns)

    @property
    def languages_label(self) -> str:
        """``any`` or the comma-separated language names."""
        return ", ".join(self.languages) if self.languages else "any"


_JS_TS = (Language.JAVASCRIPT, Language.TYPESCRIPT)
_CODE = tuple(Language)
_JS_EXT = "{js,jsx,mjs,cjs,ts,tsx,mts,cts}"

RULES: tuple[Rule, ...] = (
    Rule(
        "vendored-dir",
        Category.VENDORED,
        "dir",
        ("vendor", "third_party", "third-party", "thirdparty", "node_modules", "bower_components"),
        "copies of other projects' code (Go and Rust vendor/, npm node_modules/); "
        "a change there, tests included, is not this project's fix",
    ),
    Rule(
        "java-test-dir",
        Category.TEST,
        "path",
        ("**/src/test/**", "**/src/testFixtures/**", "**/src/integrationTest/**"),
        "Maven and Gradle test source sets, with their resources",
    ),
    Rule(
        "test-dir",
        Category.TEST,
        "dir",
        ("tests", "test"),
        "test trees and their data: pytest and unittest, Cargo integration tests (tests/), "
        "mocha and tap (test/)",
    ),
    Rule(
        "js-test-dir",
        Category.TEST,
        "dir",
        ("__tests__", "__snapshots__", "__mocks__"),
        "Jest test, snapshot and manual-mock directories",
    ),
    Rule(
        "go-testdata-dir",
        Category.TEST,
        "dir",
        ("testdata",),
        "the go tool ignores testdata/; it holds fixtures and golden files for tests",
    ),
    Rule(
        "lockfile",
        Category.GENERATED,
        "name",
        (
            "*.lock",
            "package-lock.json",
            "npm-shrinkwrap.json",
            "pnpm-lock.yaml",
            "bun.lockb",
            "go.sum",
            "gradle.lockfile",
        ),
        "dependency lockfiles (Cargo.lock, yarn.lock, go.sum, uv.lock, ...) are written by "
        "the package manager",
    ),
    Rule(
        "generated-name",
        Category.GENERATED,
        "name",
        (
            "*.pb.go",
            "*.pb.gw.go",
            "*_pb2.py",
            "*_pb2.pyi",
            "*_pb2_grpc.py",
            "*_pb.js",
            "*_pb.d.ts",
            "*_grpc_pb.js",
            "zz_generated*.go",
            "*_generated.go",
            "*.generated.*",
        ),
        "file names used by protobuf, gRPC and other code generators",
    ),
    Rule(
        "minified-name",
        Category.GENERATED,
        "name",
        ("*.min.js", "*.min.mjs", "*.min.cjs", "*.min.css", "*.js.map", "*.mjs.map", "*.css.map"),
        "minified bundles and source maps are build output",
    ),
    Rule(
        "generated-header",
        Category.GENERATED,
        "signal",
        (GENERATED_HEADER,),
        "a 'Code generated ... DO NOT EDIT', '@generated' or 'auto-generated' comment in "
        "the first 30 lines marks tool output",
        languages=_CODE,
    ),
    Rule(
        "minified-content",
        Category.GENERATED,
        "signal",
        (MINIFIED,),
        "lines averaging 200+ characters: a minified bundle without a .min.js name",
        languages=(Language.JAVASCRIPT,),
    ),
    Rule(
        "js-dist-dir",
        Category.GENERATED,
        "dir",
        ("dist",),
        "compiled JavaScript and type declarations committed under dist/",
        languages=_JS_TS,
    ),
    Rule(
        "py-test-file",
        Category.TEST,
        "name",
        ("test_*.py", "*_test.py", "tests.py", "conftest.py"),
        "pytest discovery names and shared fixtures, wherever they live",
        languages=(Language.PYTHON,),
    ),
    Rule(
        "js-test-file",
        Category.TEST,
        "name",
        (f"*.test.{_JS_EXT}", f"*.spec.{_JS_EXT}"),
        "Jest, Vitest, Mocha and Jasmine test file names",
        languages=_JS_TS,
    ),
    Rule(
        "go-test-file",
        Category.TEST,
        "name",
        ("*_test.go",),
        "go test compiles only files ending in _test.go as tests",
        languages=(Language.GO,),
    ),
    Rule(
        "java-test-file",
        Category.TEST,
        "name",
        ("*Test.java", "*Tests.java", "*TestCase.java", "*IT.java", "Test[A-Z0-9_]*.java"),
        "Maven Surefire and Failsafe test class names (case-sensitive: Latest.java is source)",
        languages=(Language.JAVA,),
        case_sensitive=True,
    ),
    Rule(
        "rust-test-file",
        Category.TEST,
        "name",
        ("tests.rs", "*_tests.rs"),
        "Rust test modules kept in their own file (#[cfg(test)] mod tests; in the parent)",
        languages=(Language.RUST,),
    ),
    Rule(
        "java-main-dir",
        Category.SOURCE,
        "path",
        ("**/src/main/java/**",),
        "Maven and Gradle main source set, decided before the directory rules so package "
        "directories such as com/example/ or tools/ are not read as tooling or docs",
        languages=(Language.JAVA,),
    ),
    Rule(
        "ci-config",
        Category.CONFIG,
        "path",
        (".github/**", ".circleci/**", ".buildkite/**", ".gitlab-ci*", ".travis*", "azure-*.y*ml"),
        "continuous integration settings",
    ),
    Rule(
        "build-config",
        Category.CONFIG,
        "name",
        (
            "makefile",
            "*.mk",
            "dockerfile",
            "*.dockerfile",
            ".dockerignore",
            "cmakelists.txt",
            ".gitignore",
            ".gitattributes",
            ".gitmodules",
            ".editorconfig",
            ".pre-commit-config.yaml",
            "*.cfg",
            "*.ini",
        ),
        "build, container and repository settings shared by every language",
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
            "requirements*.txt",
            "manifest.in",
            ".python-version",
            ".readthedocs.y*ml",
        ),
        "Python packaging and tooling settings",
    ),
    Rule(
        "rust-config-file",
        Category.CONFIG,
        "name",
        (
            "cargo.toml",
            "rust-toolchain",
            "rust-toolchain.toml",
            "rustfmt.toml",
            ".rustfmt.toml",
            "clippy.toml",
            ".clippy.toml",
            "deny.toml",
        ),
        "Cargo manifests, toolchain pins and lint settings",
    ),
    Rule(
        "js-config-file",
        Category.CONFIG,
        "name",
        (
            "package.json",
            "tsconfig*.json",
            "jsconfig*.json",
            "deno.json{,c}",
            "biome.json{,c}",
            ".eslintrc*",
            "eslint.config.*",
            ".prettierrc*",
            "prettier.config.*",
            ".babelrc*",
            "babel.config.*",
            "{jest,vitest,vite,webpack,rollup}.config.*",
            ".npmrc",
            ".nvmrc",
            ".npmignore",
            ".yarnrc*",
        ),
        "npm manifests and JS/TS compiler, linter, bundler and test-runner settings",
    ),
    Rule(
        "go-config-file",
        Category.CONFIG,
        "name",
        ("go.mod", "go.work", ".golangci.y*ml", ".goreleaser.y*ml"),
        "Go module files and linter or release settings",
    ),
    Rule(
        "java-config-file",
        Category.CONFIG,
        "name",
        (
            "pom.xml",
            "build.gradle{,.kts}",
            "settings.gradle{,.kts}",
            "gradle.properties",
            "gradle-wrapper.properties",
            "gradlew{,.bat}",
            "mvnw{,.cmd}",
        ),
        "Maven and Gradle builds and their wrappers",
    ),
    Rule(
        "docs-dir",
        Category.DOCS,
        "dir",
        ("docs", "doc", "documentation"),
        "documentation trees, including their build scripts such as Sphinx's conf.py",
    ),
    Rule(
        "docs-file",
        Category.DOCS,
        "name",
        ("*.md", "*.mdx", "*.rst", "*.adoc", "*.txt"),
        "prose by extension (Markdown, reStructuredText, AsciiDoc, plain text)",
    ),
    Rule(
        "tooling-dir",
        Category.OTHER,
        "dir",
        (
            "benchmark",
            "benchmarks",
            "benches",
            "scripts",
            "tools",
            "examples",
            "example",
            "fuzz",
            "fuzzer",
            "profiler",
        ),
        "helper code outside the package: benchmarks (including Rust benches/), scripts, "
        "examples and fuzzers are not the tests a task runs",
    ),
    Rule(
        "rust-inline-tests",
        Category.SOURCE,
        "signal",
        (RUST_INLINE_TESTS,),
        "Rust source with an in-file #[cfg(test)] module: still source, but a commit can "
        "change its tests without touching tests/",
        languages=(Language.RUST,),
    ),
    Rule(
        "py-source",
        Category.SOURCE,
        "name",
        ("*.py", "*.pyi", "*.pyx", "*.pxd"),
        "Python modules, stubs and Cython sources",
        languages=(Language.PYTHON,),
    ),
    Rule(
        "rust-source",
        Category.SOURCE,
        "name",
        ("*.rs",),
        "Rust source",
        languages=(Language.RUST,),
    ),
    Rule(
        "js-source",
        Category.SOURCE,
        "name",
        (f"*.{_JS_EXT}",),
        "JavaScript and TypeScript modules",
        languages=_JS_TS,
    ),
    Rule(
        "go-source",
        Category.SOURCE,
        "name",
        ("*.go",),
        "Go source",
        languages=(Language.GO,),
    ),
    Rule(
        "java-source",
        Category.SOURCE,
        "name",
        ("*.java",),
        "Java source",
        languages=(Language.JAVA,),
    ),
    Rule(
        "docs-name",
        Category.DOCS,
        "name",
        (
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
        "prose and legal file names (README, LICENSE-MIT, CHANGELOG, ...), checked after "
        "source so a module named license.py or history.rs stays source",
    ),
)
"""The built-in classifier table, in match order."""

FALLBACK_RULE: Final = "fallback"
"""Rule id reported when no rule matches; the category is then OTHER."""


@dataclass(frozen=True, slots=True)
class Classification:
    """The category of one path and the id of the rule that decided it."""

    category: Category
    rule_id: str


def match(path: str, rules: Sequence[Rule] = RULES, signals: Iterable[str] = ()) -> Rule | None:
    """The first rule that matches ``path`` (with its content ``signals``), or ``None``."""
    target = Target.of(path, signals)
    for rule in rules:
        if rule.matches(target):
            return rule
    return None


def classify(
    path: str, rules: Sequence[Rule] = RULES, signals: Iterable[str] = ()
) -> Classification:
    """Classify a repository-relative path with the first matching rule."""
    rule = match(path, rules, signals)
    if rule is None:
        return Classification(Category.OTHER, FALLBACK_RULE)
    return Classification(rule.category, rule.rule_id)
