"""``commitminer report``: Markdown and self-contained HTML from an export, pinned by golden files.

The golden reports come from the ledger demo repositories (fixed dates, so
fixed shas) mined against a ledger claimed under a fixed clock. Refresh them
after an intended change with::

    UPDATE_GOLDEN=1 uv run pytest tests/test_report.py

and review the diff under ``tests/golden/``.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from commitminer import ledger as ledger_module
from commitminer.cli import app
from commitminer.export import Run, write_jsonl
from commitminer.ledger import Entry, Match, Status, Verdict
from commitminer.models import Commit, FileChange, PatchStats
from commitminer.report import (
    BatchExport,
    Export,
    ReportError,
    format_for,
    from_records,
    outcome_of,
    read_export,
    render_html,
    render_markdown,
    repo_web_url,
)
from commitminer.scoring import mine
from commitminer.settings import Settings
from test_cli_ledger import BUILDER

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "tests" / "golden"
HISTORY = ROOT / "examples" / "tomli" / "history.jsonl.gz"
runner = CliRunner()


def _golden(name: str, text: str) -> None:
    path = GOLDEN / name
    if os.environ.get("UPDATE_GOLDEN"):
        path.write_text(text, encoding="utf-8")
    assert text == path.read_text(encoding="utf-8"), f"{name} differs; see the module docstring"


@pytest.fixture(scope="module")
def fork_export(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch_module: pytest.MonkeyPatch
) -> Path:
    """The fork of the ledger demo, mined against a ledger that holds upstream's fixes."""
    monkeypatch_module.setattr(ledger_module, "utc_now", lambda: "2026-01-02T03:04:05+00:00")
    root = tmp_path_factory.mktemp("report") / "repos"
    BUILDER.build(root)
    work = root.parent
    upstream, fork, ledger = work / "up.jsonl", work / "fork.jsonl", work / "ledger.sqlite3"
    steps = [
        ["mine", str(root / "upstream"), "--repo-name", "demo/durations", "--out", str(upstream)],
        ["ledger", "add", str(ledger), str(upstream), "--owner", "alice"],
        [
            *("mine", str(root / "fork"), "--repo-name", "demo/durations-fork"),
            *("--ledger", "ledger.sqlite3", "--out", str(fork)),
            *("--url", "https://example.invalid/demo/fork.git"),
        ],
    ]
    monkeypatch_module.chdir(work)  # so the ledger path in the export is stable
    for args in steps:
        result = runner.invoke(app, [*args, "--explain", "0"] if args[0] == "mine" else args)
        assert result.exit_code == 0, result.output
    return fork


@pytest.fixture(scope="module")
def monkeypatch_module() -> pytest.MonkeyPatch:  # type: ignore[misc]
    patcher = pytest.MonkeyPatch()
    yield patcher
    patcher.undo()


def test_markdown_report_golden(fork_export: Path, tmp_path: Path) -> None:
    out = tmp_path / "report.md"
    result = runner.invoke(app, ["report", str(fork_export), "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert result.stdout == f"wrote the markdown report to {out}\n"
    _golden("report-fork.md", out.read_text(encoding="utf-8"))


def test_html_report_golden(fork_export: Path, tmp_path: Path) -> None:
    out = tmp_path / "report.html"
    result = runner.invoke(app, ["report", str(fork_export), "--out", str(out)])
    assert result.exit_code == 0, result.output
    text = out.read_text(encoding="utf-8")
    _golden("report-fork.html", text)
    # Self-contained: inline CSS only, no scripts, no external assets.
    assert "<style>" in text
    assert "<script" not in text
    assert "<link" not in text
    assert " src=" not in text
    assert re.findall(r'href="([^"]*)"', text)
    assert all(
        href.startswith("https://example.invalid/") for href in re.findall(r'href="([^"]*)"', text)
    )


def test_report_prints_markdown_by_default_and_honours_format_and_top(fork_export: Path) -> None:
    printed = runner.invoke(app, ["report", str(fork_export)])
    assert printed.exit_code == 0, printed.output
    assert printed.stdout.startswith("# CommitMiner report: demo/durations-fork\n")
    assert printed.stdout == (GOLDEN / "report-fork.md").read_text(encoding="utf-8")
    html = runner.invoke(app, ["report", str(fork_export), "--format", "html", "--top", "2"])
    assert html.exit_code == 0, html.output
    assert html.stdout.startswith("<!DOCTYPE html>\n")
    assert "The best 2 of 4 exported candidates." in html.stdout
    assert html.stdout.count("<details><summary>#") == 2
    assert format_for(Path("x.HTML"), None) == "html"
    assert format_for(Path("x.markdown"), None) == "markdown"
    assert format_for(Path("x.txt"), None) == "markdown"
    assert format_for(None, "html") == "html"
    with pytest.raises(ReportError, match="--format must be markdown or html"):
        format_for(None, "pdf")


def test_the_recorded_history_report(tmp_path: Path) -> None:
    export = tmp_path / "tomli.jsonl"
    mined = runner.invoke(
        app,
        ["mine", "--history", str(HISTORY), "--top", "0", "--explain", "0", "--out", str(export)],
    )
    assert mined.exit_code == 0, mined.output
    loaded = read_export(export)
    assert len(loaded.candidates) == 44
    markdown = render_markdown(loaded)
    assert "| walked commits | 312 | 312 |" in markdown
    assert (
        "| rejected: no-test (no test file changed and no inline tests were added) | 115 | 48 |"
        in markdown
    )
    assert "| candidates | 44 | 44 |" in markdown
    assert markdown.count("\n### #") == 44
    assert markdown.count("\n### ") == 44 + 6  # 44 candidates, 6 rejection reasons
    assert "### docs-only (42): every changed file is documentation" in markdown
    assert "likely fail-to-pass tests: tests/test\\_misc.py::test\\_deepcopy" in markdown
    html = render_html(loaded)
    assert html.count("<details>") == 44 + 6
    assert (
        'href="https://github.com/hukkin/tomli/commit/5ab9ec926d9dc1ef79e66215edd51285371fe8a0"'
        in html
    )


def _synthetic_export(path: Path, subject: str, url: str | None) -> Export:
    files = (
        FileChange("src/pkg/a.py", 3, 1, patch=PatchStats(1, 1, 3, 1)),
        FileChange("tests/test_a.py", 4, 0, patch=PatchStats(1, 1, 4, 0, tests=("test_a",))),
    )
    fix = Commit("a" * 40, ("b" * 40,), "2024-02-03T04:05:06+00:00", f"{subject}\n", files)
    docs = Commit(
        "c" * 40,
        ("a" * 40,),
        "2024-02-04T04:05:06+00:00",
        f"{subject} docs\n",
        (FileChange("README.md", 1, 0),),
    )
    result = mine([fix, docs])
    run = Run("demo/x", url, "clone", "commits", Settings())
    write_jsonl(path, result, "demo/x", run=run)
    return read_export(path)


def test_everything_from_the_export_is_escaped(tmp_path: Path) -> None:
    subject = 'Fix <script>alert("x")</script> & `code` *bold* _u_ [link](http://x) | pipe'
    export = _synthetic_export(tmp_path / "e.jsonl", subject, "javascript:alert(1)")
    html = render_html(export)
    assert "<script>" not in html
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; &amp;" in html
    assert 'href="' not in html  # not an http(s) URL: printed as text, never linked
    assert "javascript:alert(1)</a>" not in html
    markdown = render_markdown(export)
    assert "\\<script\\>" in markdown
    assert "\\`code\\` \\*bold\\* \\_u\\_ \\[link\\](http://x) \\| pipe" in markdown
    assert "](javascript:" not in markdown
    # Every cell of the table row stays on one row.
    row = next(line for line in markdown.splitlines() if line.startswith("| 1 |"))
    assert row.count("|") == 18


def test_links_come_from_web_urls_and_git_remotes(tmp_path: Path) -> None:
    assert repo_web_url("https://github.com/o/r.git") == "https://github.com/o/r"
    assert repo_web_url("https://github.com/o/r/") == "https://github.com/o/r"
    assert repo_web_url("git@github.com:o/r.git") == "https://github.com/o/r"
    assert repo_web_url("ssh://git@ghe.example.invalid/o/r") == "https://ghe.example.invalid/o/r"
    assert repo_web_url("/srv/git/r.git") is None
    assert repo_web_url("host:r.git") is None  # the scp form needs a user
    assert repo_web_url("javascript:alert(1)") is None
    assert repo_web_url("../r") is None
    assert repo_web_url("file:///srv/r") is None
    assert repo_web_url(None) is None
    assert repo_web_url("") is None
    export = _synthetic_export(tmp_path / "e.jsonl", "Fix it", "git@github.com:demo/x.git")
    html = render_html(export)
    assert f'href="https://github.com/demo/x/commit/{"a" * 40}"' in html
    assert 'href="https://github.com/demo/x/commit/cccccccccc' in html  # a rejected commit
    markdown = render_markdown(_synthetic_export(tmp_path / "n.jsonl", "Fix it", None))
    assert "](" not in markdown  # no URL: no links at all


def test_report_errors_exit_2(tmp_path: Path) -> None:
    missing = runner.invoke(app, ["report", str(tmp_path / "none.jsonl")])
    assert missing.exit_code == 2
    assert "none.jsonl: cannot read" in missing.stderr
    bad = tmp_path / "bad.jsonl"
    for text, message in [
        ("", "empty file"),
        ("{\n", "line 1: invalid JSON"),
        ("[]\n", "line 1: expected an object"),
        (
            '{"schema_version": 4}\n',
            "schema_version 4, expected 5 or 6 (mine the candidates again)",
        ),
        ('{"schema_version": 5, "kind": "candidate"}\n', "the first record must be the run record"),
        ('{"schema_version": 5, "kind": "run"}\n', "line 1: missing field 'repo'"),
    ]:
        bad.write_text(text, encoding="utf-8")
        result = runner.invoke(app, ["report", str(bad)])
        assert result.exit_code == 2, result.output
        assert result.stderr.startswith("error: ")
        assert message in result.stderr
    export = tmp_path / "e.jsonl"
    _synthetic_export(export, "Fix", None)
    run, candidate = export.read_text().splitlines()
    for lines, message in [
        ([run], "says 1 candidates, the file has 0"),
        ([run, run], "line 2: expected a candidate record"),
        (
            [run, candidate.replace('"rank": 1', '"rank": "1"')],
            "line 2.rank: expected int, got str",
        ),
        ([run.replace('"walked": 2', '"walked": true')], "line 1.walked: expected int, got bool"),
    ]:
        bad.write_text("\n".join(lines) + "\n", encoding="utf-8")
        result = runner.invoke(app, ["report", str(bad)])
        assert result.exit_code == 2, result.output
        assert message in result.stderr
    unwritable = runner.invoke(app, ["report", str(export), "--out", str(tmp_path)])
    assert unwritable.exit_code == 2
    assert "cannot write" in unwritable.stderr
    fmt = runner.invoke(app, ["report", str(export), "--format", "pdf"])
    assert fmt.exit_code == 2
    assert "--format must be markdown or html" in fmt.stderr


PRS = ROOT / "examples" / "tomli" / "prs"


def test_pull_requests_link_to_github(tmp_path: Path) -> None:
    export = tmp_path / "prs.jsonl"
    args = ["prs", "hukkin/tomli", "--limit", "25", "--replay", str(PRS), "--top", "0"]
    result = runner.invoke(app, [*args, "--explain", "0", "--out", str(export)])
    assert result.exit_code == 0, result.output
    loaded = read_export(export)
    markdown = render_markdown(loaded)
    assert "Source: the merged pull requests of [https://github.com/hukkin/tomli]" in markdown
    assert ", 24 pull requests walked." in markdown
    assert "| 1 | [#200](https://github.com/hukkin/tomli/pull/200) |" in markdown
    assert "- pull request #200: [https://github.com/hukkin/tomli/pull/200](" in markdown
    # Rejected pull requests link to the pull request too (built from the repository URL).
    assert "| [#287](https://github.com/hukkin/tomli/pull/287) | 2026-03-25 |" in markdown
    html = render_html(loaded)
    assert '<a href="https://github.com/hukkin/tomli/pull/200">#200</a>' in html


def test_ledger_verdict_texts_and_missing_features(tmp_path: Path) -> None:
    files = (
        FileChange("src/pkg/a.py", 3, 1, patch=PatchStats(1, 1, 3, 1, hunk_hashes=("h1",))),
        FileChange("tests/test_a.py", 4, 0, patch=PatchStats(1, 1, 4, 0, hunk_hashes=("h2",))),
    )
    date = "2024-02-03T04:05:06+00:00"
    result = mine([Commit(f"{d}" * 40, (), date, f"Fix {d}\n", files) for d in "abc"])
    entry = Entry(1, "f", "demo/up", "e" * 40, "Fix e", "claimed", None, "2026-01-02", 3)
    verdicts = [
        Verdict(Status.OVERLAP, (Match(entry, 2, 3, False),)),
        Verdict(Status.DUPLICATE, (Match(entry, 2, 2, True, in_run=True),)),
        Verdict(Status.UNKNOWN),
    ]
    run = Run("demo/x", None, "clone", "commits", Settings(), "l.sqlite3", 0.5, False)
    path = tmp_path / "e.jsonl"
    write_jsonl(path, result, "demo/x", verdicts, run=run, all_verdicts=verdicts)
    markdown = render_markdown(read_export(path))
    assert (
        "- ledger: overlap: 2 of 3 hunks shared with demo/up eeeeeeeeee (claimed on 2026-01-02)"
        in (markdown)
    )
    assert "- ledger: duplicate: same fix as demo/up eeeeeeeeee (earlier in this run)" in markdown
    assert "- ledger: unknown" in markdown
    assert "| ledger: duplicate | 1 | 2 |" in markdown
    assert "| ledger: overlap | 1 | 1 |" in markdown
    assert "| ledger: unknown | 1 | 0 |" in markdown
    assert "| ledger: new | 0 | 0 |" in markdown
    assert "No commit was rejected." in markdown
    # A feature another candidate lacks, and one without a numeric contribution.
    run_line, first, *rest = path.read_text().splitlines()
    record = json.loads(first)
    record["features"] = [{"name": "custom", "contribution": "n/a"}, *record["features"][1:]]
    path.write_text("\n".join([run_line, json.dumps(record), *rest]) + "\n", encoding="utf-8")
    markdown = render_markdown(read_export(path))
    header = next(line for line in markdown.splitlines() if line.startswith("| rank |"))
    assert "| custom |" in header
    assert "| small\\_diff |" in header  # the other candidates still have it
    row = next(line for line in markdown.splitlines() if line.startswith("| 1 |"))
    assert "| ? |" in row  # custom: not a number
    assert "|  |" in row  # small_diff: missing on this candidate


def test_read_export_accepts_blank_lines_and_missing_optional_fields(tmp_path: Path) -> None:
    export = tmp_path / "e.jsonl"
    _synthetic_export(export, "Fix", None)
    run, candidate = (json.loads(line) for line in export.read_text().splitlines())
    del candidate["ledger"], candidate["fingerprint"], candidate["pull_request"]
    candidate["features"].append("not an object")
    run["rejections"].append("not an object")
    run["ledger"] = {"path": "l", "min_overlap": 0.5, "new_only": False, "counts": {"new": 1}}
    export.write_text(
        "\n" + json.dumps(run) + "\n\n" + json.dumps(candidate) + "\n", encoding="utf-8"
    )
    loaded = read_export(export)
    markdown = render_markdown(loaded)
    assert "- fingerprint: none" in markdown
    assert "- ledger: not checked" in markdown
    assert "| ledger: new | 1 | 1 |" in markdown


# --- batch reports --------------------------------------------------------------------------

BATCH_FILE = """
[batch]
ledger = "ledger.sqlite3"

[[batch.repos]]
name = "demo/durations"
clone = "repos/upstream"
url = "https://example.invalid/demo/durations.git"

[[batch.repos]]
name = "demo/durations-fork"
clone = "repos/fork"
url = "https://example.invalid/demo/fork.git"

[[batch.repos]]
name = "demo/gone"
history = "missing.jsonl.gz"
"""


@pytest.fixture(scope="module")
def batch_export(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch_module: pytest.MonkeyPatch
) -> Path:
    """The ledger demo's upstream and fork, and a missing recording, mined as one batch."""
    monkeypatch_module.setattr(ledger_module, "utc_now", lambda: "2026-01-02T03:04:05+00:00")
    work = tmp_path_factory.mktemp("batch-report")
    BUILDER.build(work / "repos")
    (work / "batch.toml").write_text(BATCH_FILE, encoding="utf-8")
    monkeypatch_module.chdir(work)  # relative paths in the export
    result = runner.invoke(app, ["batch", "batch.toml", "--out", "batch.jsonl", "--top", "0"])
    assert result.exit_code == 2, result.output  # the missing recording
    return work / "batch.jsonl"


def test_batch_report_golden(batch_export: Path, tmp_path: Path) -> None:
    loaded = read_export(batch_export)
    assert isinstance(loaded, BatchExport)
    assert [run.run["repo"] for run in loaded.runs] == ["demo/durations", "demo/durations-fork"]
    markdown = render_markdown(loaded)
    _golden("report-batch.md", markdown)
    html = render_html(loaded)
    _golden("report-batch.html", html)
    assert html.startswith("<!DOCTYPE html>\n")
    assert "<title>CommitMiner batch report: batch.toml</title>" in html
    assert "<script" not in html
    hrefs = re.findall(r'href="([^"]*)"', html)
    assert hrefs
    assert all(href.startswith("https://example.invalid/demo/") for href in hrefs)
    assert (
        "| demo/gone | history | failed: missing.jsonl.gz: \\[Errno 2\\] No such file or "
        "directory: 'missing.jsonl.gz' |" in markdown
    )
    assert markdown.count("\n## ") == 3 + 2  # the batch sections, then one per repository
    assert "\n#### #1 " in markdown  # candidates one level down


def test_batch_report_from_the_command_and_after_a_resume(
    batch_export: Path, tmp_path: Path
) -> None:
    printed = runner.invoke(app, ["report", str(batch_export), "--top", "1"])
    assert printed.exit_code == 0, printed.output
    assert printed.stdout.startswith("# CommitMiner batch report: batch.toml\n")
    assert "The best 1 of 4 new candidates, by score" in printed.stdout
    # The next run finds nothing new; --report writes the report with the export.
    again = runner.invoke(
        app, ["batch", "batch.toml", "--only", "demo/durations", "--report", "again.html"]
    )
    assert again.exit_code == 0, again.output
    assert again.stdout.splitlines()[-1] == "wrote the html report to again.html"
    html = Path("again.html").read_text(encoding="utf-8")
    assert "No candidate collides with a fix recorded under another repository." in html
    assert "No new candidates." in html
    assert "<td>up to date</td>" in html


def _batch_lines(batch_export: Path) -> list[str]:
    return batch_export.read_text(encoding="utf-8").splitlines()


def test_batch_exports_are_checked(batch_export: Path, tmp_path: Path) -> None:
    batch, run, *rest = _batch_lines(batch_export)
    bad = tmp_path / "bad.jsonl"
    for lines, message in [
        ([batch], "the batch record says 2 runs, the file has 0"),
        ([batch, rest[0]], "line 2: expected a run record after the batch"),
        ([batch.replace('"dry_run": false', '"dry_run": 0')], "line 1.dry_run: expected bool"),
        ([batch.replace('"runs": 2', '"runs": true')], "line 1.runs: expected int, got bool"),
        ([batch, run], "the run record of demo/durations says 3 candidates, the file has 0"),
    ]:
        bad.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with pytest.raises(ReportError, match=re.escape(message)):
            read_export(bad)
    with pytest.raises(ReportError, match="the batch export: empty file"):
        from_records([], "the batch export")


def test_batch_report_tolerates_odd_records(batch_export: Path) -> None:
    records = [json.loads(line) for line in _batch_lines(batch_export)]
    batch, first_run = records[0], records[1]
    batch["collisions"][0]["match"] = "not an object"
    batch["failed"].append("not an object")
    batch["dry_run"] = batch["full"] = True
    first_run["resume"] = {"mode": "someday", "previous": None, "watermark": 7, "note": "odd"}
    records[3]["date"] = "not a date"  # a new candidate: ranked as if oldest
    records[2]["ledger"] = {
        "status": "duplicate",
        "matches": [
            {
                "source": "ledger",
                "repo": records[2]["repo"],
                "sha": records[2]["sha"],
                "exact": True,
                "status": "proposed",
                "owner": None,
                "first_seen": "2026-01-02T03:04:05+00:00",
            }
        ],
    }
    loaded = from_records(records, "x")
    assert isinstance(loaded, BatchExport)
    markdown = render_markdown(loaded)
    assert "| someday (odd) |" in markdown
    assert "dry run: nothing was recorded; --full: the watermarks were ignored" in markdown
    assert "- ledger: duplicate: already in the ledger (proposed on 2026-01-02)" in markdown
    first_run["resume"] = None
    assert "| clone | - | 4 | 0 | 3 |" in render_markdown(from_records(records, "x"))


def test_outcome_of_exported_candidates() -> None:
    def candidate(status: str | None, *matches: dict[str, str]) -> dict[str, object]:
        verdict = None if status is None else {"status": status, "matches": list(matches)}
        return {"repo": "r", "sha": "a", "ledger": verdict}

    assert outcome_of(candidate(None)) == "unknown"
    assert outcome_of(candidate("new")) == "new"
    assert outcome_of(candidate("unknown")) == "unknown"
    assert outcome_of(candidate("duplicate")) == "duplicate"  # no matches to tell
    assert outcome_of(candidate("duplicate", {"repo": "r", "sha": "a"})) == "recorded"
    assert outcome_of(candidate("overlap", {"repo": "r", "sha": "b"})) == "internal"
    assert outcome_of(candidate("overlap", {"repo": "x", "sha": "b"})) == "collision"
    odd = {"repo": "r", "sha": "a", "ledger": {"status": "overlap", "matches": [1]}}
    assert outcome_of(odd) == "internal"
