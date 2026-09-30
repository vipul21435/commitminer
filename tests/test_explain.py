"""``commitminer explain`` on fixed synthetic commits, pinned by golden files.

The repository below is built from scratch with fixed authors and dates, so its
commit shas and every number in the explanations are reproducible. To refresh
the golden files after an intended change, run::

    UPDATE_GOLDEN=1 uv run pytest tests/test_explain.py

and review the diff under ``tests/golden/``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from commitminer.cli import app
from commitminer.explain import ExplainError, explain_json, find_commit, render_commit
from commitminer.models import Commit, FileChange
from commitminer.scoring import evaluate
from commitminer.settings import Settings
from gitrepo import GitRepo

GOLDEN = Path(__file__).resolve().parent / "golden"
runner = CliRunner()

OPS = '''"""Arithmetic helpers."""


def divide(a, b):
    return a / b


def _clamp(value, low, high):
    return max(low, min(value, high))
'''
OPS_FIXED = OPS.replace(
    "def divide(a, b):\n",
    "def divide(a, b, default=None):\n    if b == 0:\n        return default\n",
)
TEST_OPS = "from calc.ops import divide\n\n\ndef test_divide():\n    assert divide(6, 3) == 2\n"
TEST_OPS_FIXED = TEST_OPS + (
    "\n\ndef test_divide_by_zero():\n"
    "    assert divide(1, 0) is None\n"
    "    assert divide(1, 0, default=0) == 0\n"
)

MEAN = """pub fn mean(values: &[i64]) -> i64 {
    values.iter().sum::<i64>() / values.len() as i64
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn mean_of_three() {
        assert_eq!(mean(&[1, 2, 3]), 2);
    }
}
"""
MEAN_FIXED = MEAN.replace(
    "    values.iter().sum::<i64>() / values.len() as i64\n",
    "    let n = values.len() as i64;\n    (values.iter().sum::<i64>() + n / 2) / n\n",
).replace(
    "        assert_eq!(mean(&[1, 2, 3]), 2);\n    }\n",
    "        assert_eq!(mean(&[1, 2, 3]), 2);\n    }\n\n"
    "    #[test]\n    fn mean_rounds_half_up() {\n        assert_eq!(mean(&[1, 2]), 2);\n    }\n",
)

LEX = """package parse

// isDigit reports whether c is an ASCII digit.
func isDigit(c byte) bool {
	return c >= '0' && c <= '9'
}

func scanNumber(s string, i int) int {
	for i < len(s) && isDigit(s[i]) {
		i++
	}
	return i
}
"""
LEX_FIXED = LEX.replace(
    "func scanNumber(s string, i int) int {\n",
    "func scanNumber(s string, i int) int {\n\tif i < len(s) && s[i] == '-' {\n\t\ti++\n\t}\n",
)
PARSE = """package parse

import "strconv"

// Int parses the leading decimal number of s.
func Int(s string) (int, error) {
	end := scanNumber(s, 0)
	return strconv.Atoi(s[:end])
}
"""
PARSE_FIXED = PARSE.replace(
    "func Int(s string) (int, error) {\n",
    "func Int(s string, base int) (int, error) {\n",
).replace(
    "\treturn strconv.Atoi(s[:end])\n",
    "\tn, err := strconv.ParseInt(s[:end], base, 64)\n\treturn int(n), err\n",
)
PARSE_TEST = """package parse

import "testing"

func TestInt(t *testing.T) {
	if n, _ := Int("42"); n != 42 {
		t.Errorf("got %d", n)
	}
}
"""
PARSE_TEST_FIXED = PARSE_TEST.replace('Int("42")', 'Int("42", 10)').replace("}\n}\n", "}\n") + (
    '\tif n, _ := Int("-7", 10); n != -7 {\n\t\tt.Errorf("got %d", n)\n\t}\n'
    '\tif n, _ := Int("ff", 16); n != 255 {\n\t\tt.Fatalf("got %d", n)\n\t}\n}\n'
)

INDEX = """// Greeting helpers.
export function greet(name) {
  return `hello ${name}`; // plain
}
"""
INDEX_COMMENTS = """// Greeting helpers for the command line.
export function greet(name) {
    return `hello ${name}`; // plain, no punctuation
}
"""
INDEX_TEST = "import { greet } from './index.js';\n\ntest('greets', () => {});\n"

STATS = "package com.acme;\n\npublic final class Stats {\n}\n"


@pytest.fixture(scope="module")
def repo(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    """A small multi-language repository; returns the sha of each named commit."""
    git = GitRepo(tmp_path_factory.mktemp("explain") / "repo")
    shas = {
        "setup": git.commit(
            "Initial layout",
            {
                "src/calc/ops.py": OPS,
                "tests/test_ops.py": TEST_OPS,
                "src/lib.rs": MEAN,
                "parse/lex.go": LEX,
                "parse/parse.go": PARSE,
                "parse/parse_test.go": PARSE_TEST,
                "lib/index.js": INDEX,
                "lib/index.test.js": INDEX_TEST,
                "src/main/java/com/acme/Stats.java": STATS,
                "README.md": "# calc\n",
            },
        )
    }
    shas["python-fix"] = git.commit(
        "Fix divide by zero handling (fixes #12)",
        {"src/calc/ops.py": OPS_FIXED, "tests/test_ops.py": TEST_OPS_FIXED},
    )
    shas["rust-inline"] = git.commit("Round the mean half up", {"src/lib.rs": MEAN_FIXED})
    shas["go-cross-file"] = git.commit(
        "Parse negative numbers in any base (#31)",
        {
            "parse/lex.go": LEX_FIXED,
            "parse/parse.go": PARSE_FIXED,
            "parse/parse_test.go": PARSE_TEST_FIXED,
        },
    )
    shas["js-cosmetic"] = git.commit(
        "Reword the greeting comments",
        {"lib/index.js": INDEX_COMMENTS, "lib/index.test.js": INDEX_TEST + "// more later\n"},
    )
    shas["docs-only"] = git.commit(
        "Document usage", {"README.md": "# calc\n\nUsage: see docs.\n", "docs/usage.md": "Hi\n"}
    )
    report = "".join(f"    int field{n} = {n};\n" for n in range(420))
    shas["java-oversize"] = git.commit(
        "Add the report generator",
        {
            "src/main/java/com/acme/Report.java": "package com.acme;\n\n"
            "public class Report {\n" + report + "}\n",
            "src/test/java/com/acme/ReportTest.java": "package com.acme;\n\n"
            "class ReportTest {\n    @Test void builds() { assertEquals(1, 1); }\n}\n",
        },
    )
    shas["root"] = str(git.root)
    return shas


def _golden(name: str, text: str) -> None:
    path = GOLDEN / name
    if os.environ.get("UPDATE_GOLDEN"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="ascii")
    assert text == path.read_text(encoding="ascii"), f"{name} differs; see the module docstring"


@pytest.mark.parametrize(
    "name",
    ["python-fix", "rust-inline", "go-cross-file", "js-cosmetic", "docs-only", "java-oversize"],
)
def test_explain_golden(repo: dict[str, str], name: str) -> None:
    result = runner.invoke(app, ["explain", repo[name], "--repo", repo["root"]])
    assert result.exit_code == 0, result.output
    _golden(f"explain-{name}.txt", result.stdout)


def test_explain_json_golden(repo: dict[str, str]) -> None:
    for name in ("python-fix", "java-oversize"):
        result = runner.invoke(
            app, ["explain", repo[name], "--repo", repo["root"], "--json", "--repo-name", "calc"]
        )
        assert result.exit_code == 0, result.output
        record = json.loads(result.stdout)
        _golden(f"explain-{name}.json", json.dumps(record, indent=2, sort_keys=True) + "\n")


def test_explain_a_recording_matches_the_clone(repo: dict[str, str], tmp_path: Path) -> None:
    recording = tmp_path / "h.jsonl"
    recorded = runner.invoke(app, ["record", repo["root"], "--out", str(recording)])
    assert recorded.exit_code == 0, recorded.output
    live = runner.invoke(app, ["explain", repo["go-cross-file"], "--repo", repo["root"]])
    replay = runner.invoke(
        app, ["explain", repo["go-cross-file"][:8].upper(), "--history", str(recording)]
    )
    assert replay.exit_code == 0, replay.output
    assert replay.stdout == live.stdout


def test_explain_uses_config_weights_and_bands(repo: dict[str, str], tmp_path: Path) -> None:
    config = tmp_path / "commitminer.toml"
    config.write_text(
        "[difficulty]\nmedium_at = 0.5\nhard_at = 1.0\n\n[score.weights]\nlinked_reference = 0\n",
        encoding="utf-8",
    )
    result = runner.invoke(
        app, ["explain", repo["python-fix"], "--repo", repo["root"], "--config", str(config)]
    )
    assert result.exit_code == 0, result.output
    first, *rest = result.stdout.splitlines()
    assert first.endswith("0 built-in rules disabled, 3 scoring settings")
    assert "verdict  candidate: score 5.58 of 10, difficulty 1.62 of 10 (hard)" in rest
    assert "  difficulty (easy < 0.5 <= medium < 1 <= hard)" in rest


def test_explain_errors(repo: dict[str, str], tmp_path: Path) -> None:
    both = runner.invoke(app, ["explain", "abcd", "--repo", ".", "--history", "h.jsonl"])
    assert both.exit_code == 2
    assert "give --repo or --history, not both" in both.output
    unknown = runner.invoke(app, ["explain", "no-such-rev", "--repo", repo["root"]])
    assert unknown.exit_code == 2
    assert "error: git exited with" in unknown.output
    missing = runner.invoke(app, ["explain", "abcd", "--history", str(tmp_path / "none.jsonl")])
    assert missing.exit_code == 2
    assert "No such file" in missing.output


def test_explain_refuses_merge_commits(git_repo: GitRepo) -> None:
    git_repo.commit("base", {"a.py": "a\n"})
    git_repo.git("checkout", "-q", "-b", "side")
    git_repo.commit("side", {"b.py": "b\n"})
    git_repo.git("checkout", "-q", "main")
    git_repo.commit("main", {"c.py": "c\n"})
    git_repo.git("merge", "-q", "--no-ff", "-m", "merge side", "side")
    result = runner.invoke(app, ["explain", "HEAD", "--repo", str(git_repo.root)])
    assert result.exit_code == 2
    assert "HEAD is a merge commit" in result.output


def test_explain_defaults_to_the_current_directory(
    repo: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo["root"])
    result = runner.invoke(app, ["explain", "HEAD~5"])
    assert result.exit_code == 0, result.output
    assert f"commit   {repo['python-fix']}" in result.stdout


def _commit(sha: str) -> Commit:
    return Commit(sha, (), "2024-01-01T00:00:00+00:00", "x", ())


@pytest.mark.parametrize(
    ("prefix", "message"),
    [
        ("abc", "at least 4 hex digits"),
        ("xyz1", "at least 4 hex digits"),
        ("ffff", "no commit ffff in the history"),
        ("abcd", "abcd is ambiguous: 2 commits"),
    ],
)
def test_find_commit_errors(prefix: str, message: str) -> None:
    commits = [_commit("abcd1" + "0" * 35), _commit("abcd2" + "0" * 35)]
    with pytest.raises(ExplainError, match=message):
        find_commit(commits, prefix)
    assert find_commit(commits, "ABCD1").sha.startswith("abcd1")


def test_render_commit_without_patch_data_with_binaries_and_renames() -> None:
    commit = Commit(
        "a" * 40,
        (),
        "2024-01-01T00:00:00+00:00",
        "Fix image loading\n",
        (
            FileChange("src/pkg/img.py", 3, 1, old_path="src/pkg/image.py"),
            FileChange("tests/data/x.png", None, None),
            FileChange("tests/test_img.py", 4, 0),
        ),
    )
    settings = Settings()
    rows = render_commit(evaluate(commit, settings), settings).splitlines()
    assert rows[1] == "base     (root commit)"
    assert rows[5] == (
        "patch    fingerprint unknown (no hunk hashes recorded, or only indentation "
        "and blank lines changed)"
    )
    assert rows[6] == "tests    no test function recognised in the changed tests"
    assert rows[10].split() == [
        "source",
        "py-source",
        "3",
        "1",
        "?",
        "?",
        "?",
        "?",
        "src/pkg/image.py",
        "->",
        "src/pkg/img.py",
    ]
    assert rows[11].split()[2:8] == ["-", "-", "-", "-", "-", "-"]
    docs = Commit("b" * 40, (), commit.date, "Docs\n", (FileChange("README.md", 1, 0),))
    record = explain_json(evaluate(docs, settings), settings, "r")
    assert record["reason"] == "docs-only"
    assert "reason_detail" not in record
    assert record["schema_version"] == 5
    assert record["pull_request"] is None
