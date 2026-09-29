"""Build the two small git repositories of the ledger demo (standard library only).

    python examples/ledger/build_repos.py OUT

creates ``OUT/upstream`` and ``OUT/fork``. The code is an original toy duration
parser written for this demo. Authors, committers and dates are fixed, so the
commit shas are the same on every machine.

``upstream``, branch ``main``:

1. Add duration parser
2. Reject numbers without a unit (fixes #7)            <- fix A: 2 source hunks + 1 test hunk
3. Document the accepted units                         <- docs only
4. Accept days and weeks (fixes #9)                    <- fix B: 2 source hunks + 1 test hunk

``upstream``, branch ``release`` (from 1), a maintenance branch:

- Add license headers                                  <- every line of parse.py moves down
- cherry-pick of fix A                                 <- same fix, other line numbers
- Accept upper-case units (fixes #11)                  <- a fix of its own
- cherry-pick of fix B, conflict resolved by hand      <- 2 of its 3 hunks unchanged

``fork``, an independent copy that re-indented the code with two spaces:

1. Import durations with two-space indentation
2. Reject numbers without a unit                       <- fix A, re-indented
3. Move the package to lib/                            <- renames only
4. Accept days and weeks                               <- fix B, re-indented and moved
5. Allow spaces between parts (fixes #3)               <- a fix of its own
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PARSE = '''"""Parse short durations such as 1h30m into seconds."""

UNITS = {"s": 1, "m": 60, "h": 3600}


def parse(text):
    """Return the number of seconds in a duration such as 1h30m."""
    total = 0
    number = ""
    for char in text:
        if char.isdigit():
            number += char
        elif char in UNITS:
            total += int(number) * UNITS[char]
            number = ""
        else:
            raise ValueError(f"bad unit {char!r}")
    return total
'''
TESTS = """import pytest

from durations.parse import parse


def test_hours_and_minutes():
    assert parse("1h30m") == 5400
"""
TESTS_A = """

def test_a_number_needs_a_unit():
    with pytest.raises(ValueError):
        parse("90")
    with pytest.raises(ValueError):
        parse("h")
"""
TESTS_B = """

def test_days_and_weeks():
    assert parse("1w2d") == 9 * 86400
"""
TESTS_UPPER = """

def test_upper_case_units():
    assert parse("1H30M") == 5400
"""
TESTS_SPACES = """

def test_spaces_between_parts():
    assert parse("1h 30m") == 5400
"""
HEADER = "# SPDX-License-Identifier: MIT\n# Copyright the durations authors.\n\n"
UNITS = 'UNITS = {"s": 1, "m": 60, "h": 3600}'
UNITS_B = 'UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}'
UNITS_UPPER = 'UNITS = {"s": 1, "m": 60, "h": 3600, "S": 1, "M": 60, "H": 3600}'
UNITS_MERGED = (
    'UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "S": 1, "M": 60, "H": 3600}'
)


def fix_a(text: str) -> str:
    """Refuse a unit without a number and a number without a unit."""
    return text.replace(
        "        elif char in UNITS:\n",
        "        elif char in UNITS:\n"
        "            if not number:\n"
        '                raise ValueError(f"missing number before {char!r}")\n',
    ).replace(
        "    return total\n",
        "    if number:\n"
        '        raise ValueError(f"missing unit after {number}")\n'
        "    return total\n",
    )


def fix_b(text: str, units: str = UNITS_B) -> str:
    """Accept days and weeks; ``units`` is the resolved UNITS line."""
    return text.replace("such as 1h30m into", "such as 2d1h30m into").replace(
        next(line for line in text.splitlines() if line.startswith("UNITS = ")), units
    )


def allow_spaces(text: str) -> str:
    return text.replace(
        "        elif char in UNITS:\n",
        "        elif char.isspace():\n            continue\n        elif char in UNITS:\n",
    )


def two_spaces(text: str) -> str:
    """Re-indent four-space Python with two spaces: the same program."""
    lines = []
    for line in text.splitlines(keepends=True):
        body = line.lstrip(" ")
        lines.append(" " * ((len(line) - len(body)) // 2) + body)
    return "".join(lines)


class Repo:
    """A git repository with a fixed author and one fixed date per commit."""

    def __init__(self, root: Path, first_day: int) -> None:
        self.root = root
        self.day = first_day
        root.mkdir(parents=True)
        self.git("init", "-q", "-b", "main")

    def git(self, *args: str, check: bool = True) -> str:
        date = f"2025-03-{self.day:02d}T12:00:00+00:00"
        env = {
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "Demo Author",
            "GIT_AUTHOR_EMAIL": "demo@example.invalid",
            "GIT_COMMITTER_NAME": "Demo Author",
            "GIT_COMMITTER_EMAIL": "demo@example.invalid",
            "GIT_AUTHOR_DATE": date,
            "GIT_COMMITTER_DATE": date,
        }
        proc = subprocess.run(
            ["git", "-C", str(self.root), "-c", "commit.gpgsign=false", *args],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        if check and proc.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
        return proc.stdout

    def write(self, files: dict[str, str]) -> None:
        for name, text in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    def commit(self, message: str, files: dict[str, str] | None = None) -> str:
        self.write(files or {})
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.tick()

    def cherry_pick(self, sha: str, resolved: dict[str, str] | None = None) -> str:
        """Cherry-pick ``sha`` with -x; ``resolved`` are the files of a conflict resolution."""
        self.git("cherry-pick", "-x", sha, check=resolved is None)
        if resolved is not None:
            self.write(resolved)
            self.git("add", "-A")
            self.git("-c", "core.editor=true", "cherry-pick", "--continue")
        return self.tick()

    def tick(self) -> str:
        self.day += 1
        return self.git("rev-parse", "HEAD").strip()


SRC, TEST = "src/durations/parse.py", "tests/test_parse.py"


def build_upstream(root: Path) -> dict[str, str]:
    """The upstream repository; returns the shas by name."""
    repo = Repo(root, first_day=1)
    shas = {"base": repo.commit("Add duration parser", {SRC: PARSE, TEST: TESTS, "README.md": ""})}
    shas["fix_a"] = repo.commit(
        "Reject numbers without a unit (fixes #7)",
        {SRC: fix_a(PARSE), TEST: TESTS + TESTS_A, "CHANGELOG.md": "- Reject 90 and h.\n"},
    )
    shas["docs"] = repo.commit("Document the accepted units", {"README.md": "Units: s, m and h.\n"})
    shas["fix_b"] = repo.commit(
        "Accept days and weeks (fixes #9)",
        {SRC: fix_b(fix_a(PARSE)), TEST: TESTS + TESTS_A + TESTS_B},
    )
    repo.git("checkout", "-q", "-b", "release", shas["base"])
    shas["headers"] = repo.commit("Add license headers", {SRC: HEADER + PARSE})
    shas["pick_a"] = repo.cherry_pick(shas["fix_a"])
    upper = HEADER + fix_a(PARSE).replace(UNITS, UNITS_UPPER)
    shas["upper"] = repo.commit(
        "Accept upper-case units (fixes #11)",
        {SRC: upper, TEST: TESTS + TESTS_A + TESTS_UPPER},
    )
    shas["pick_b"] = repo.cherry_pick(
        shas["fix_b"],
        resolved={
            SRC: fix_b(upper, UNITS_MERGED),
            TEST: TESTS + TESTS_A + TESTS_UPPER + TESTS_B,
        },
    )
    repo.git("checkout", "-q", "main")
    return shas


def build_fork(root: Path) -> dict[str, str]:
    """The fork: re-indented with two spaces, later moved to lib/."""
    repo = Repo(root, first_day=10)
    shas = {
        "base": repo.commit(
            "Import durations with two-space indentation",
            {SRC: two_spaces(PARSE), TEST: two_spaces(TESTS)},
        )
    }
    shas["fix_a"] = repo.commit(
        "Reject numbers without a unit",
        {SRC: two_spaces(fix_a(PARSE)), TEST: two_spaces(TESTS + TESTS_A)},
    )
    repo.git("mv", "src/durations", "lib")
    repo.git("mv", TEST, "tests/test_durations.py")
    shas["move"] = repo.commit("Move the package to lib/")
    shas["fix_b"] = repo.commit(
        "Accept days and weeks",
        {
            "lib/parse.py": two_spaces(fix_b(fix_a(PARSE))),
            "tests/test_durations.py": two_spaces(TESTS + TESTS_A + TESTS_B),
        },
    )
    shas["spaces"] = repo.commit(
        "Allow spaces between parts (fixes #3)",
        {
            "lib/parse.py": two_spaces(allow_spaces(fix_b(fix_a(PARSE)))),
            "tests/test_durations.py": two_spaces(TESTS + TESTS_A + TESTS_B + TESTS_SPACES),
        },
    )
    return shas


def build(out: Path) -> dict[str, dict[str, str]]:
    """Build both repositories under ``out`` (which must not exist yet)."""
    if out.exists():
        raise SystemExit(f"{out} already exists; remove it first")
    return {"upstream": build_upstream(out / "upstream"), "fork": build_fork(out / "fork")}


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: build_repos.py OUT")
    for name, shas in build(Path(sys.argv[1])).items():
        print(f"{name}: " + ", ".join(f"{key} {sha[:10]}" for key, sha in shas.items()))
