"""Batch mining: the batch file, resuming each source from its watermark, collisions, export."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from commitminer import batch as batch_module
from commitminer import github, pulls
from commitminer.batch import (
    BatchConfig,
    BatchResult,
    RepoRun,
    RepoSpec,
    batch_records,
    best_new,
    load_batch,
    outcome,
    outcome_counts,
    parse_batch,
    render_best,
    render_collisions,
    render_run,
    run_batch,
    short,
    walk_clone,
    walk_history,
    walk_pulls,
)
from commitminer.config import ConfigError
from commitminer.export import Resume, schema_text
from commitminer.fixtures import ReplayTransport
from commitminer.github import GitHubClient
from commitminer.history import read_history, write_history
from commitminer.ledger import (
    Entry,
    Ledger,
    Match,
    Status,
    Verdict,
    Watermark,
    open_ledger,
)
from commitminer.scoring import Candidate, mine
from commitminer.settings import Settings
from fixturefiles import FakeClock
from gitrepo import GitRepo
from test_cli_ledger import BUILDER

ROOT = Path(__file__).resolve().parent.parent
TOMLI = ROOT / "examples" / "tomli"
NOW = "2026-01-02T03:04:05+00:00"


def clock() -> str:
    return NOW


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def demo(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, dict[str, str]]]:
    """The ledger demo's upstream (with a release branch) and its re-indented fork."""
    root = tmp_path_factory.mktemp("batch-demo") / "repos"
    return root, BUILDER.build(root)


@pytest.fixture
def ledger(tmp_path: Path) -> Ledger:
    return open_ledger(tmp_path / "ledger.sqlite3", now=clock)


def replay_client(spec: RepoSpec) -> GitHubClient:
    assert spec.replay is not None
    return GitHubClient(transport=ReplayTransport(spec.replay), clock=FakeClock())


# --- the batch file ---------------------------------------------------------------------------


def test_a_minimal_batch_file_gets_the_defaults(tmp_path: Path) -> None:
    path = write(
        tmp_path / "commitminer.toml",
        '[[batch.repos]]\nname = "hukkin/tomli"\nhistory = "tomli.jsonl.gz"\n',
    )
    config = load_batch(path)
    assert config.path == path
    assert config.ledger == tmp_path / ".commitminer" / "ledger.sqlite3"
    assert (config.out, config.report, config.min_overlap, config.owner) == (None, None, 0.5, None)
    (spec,) = config.repos
    assert (spec.name, spec.source, spec.target) == (
        "hukkin/tomli",
        "history",
        str(tmp_path / "tomli.jsonl.gz"),
    )
    assert (spec.unit, spec.label, spec.url, spec.config) == (
        "commits",
        "hukkin/tomli [history]",
        None,
        None,
    )
    assert spec.settings == Settings()


def test_every_key_of_the_batch_file(tmp_path: Path) -> None:
    other = write(tmp_path / "strict.toml", "[filter]\nmax_lines = 50\n")
    path = write(
        tmp_path / "batch.toml",
        f"""
[filter]
max_lines = 100

[batch]
ledger = "/abs/team.sqlite3"
out = "out/b.jsonl"
report = "out/b.html"
min_overlap = 1
owner = "sourcing"

[[batch.repos]]
name = "demo/durations"
clone = "repos/upstream"
rev = "release"
content = false
url = "https://example.invalid/durations"
config = "{other.name}"

[[batch.repos]]
name = "hukkin/tomli"
github = "hukkin/tomli"
limit = 25
max_files = 100
max_wait = 0
api_url = "https://ghe.example.invalid/api/v3"
cache_dir = "cache"
replay = "prs"
""",
    )
    config = load_batch(path)
    assert config.ledger == Path("/abs/team.sqlite3")
    assert (config.out, config.report) == (tmp_path / "out/b.jsonl", tmp_path / "out/b.html")
    assert (config.min_overlap, config.owner) == (1.0, "sourcing")
    clone, prs = config.repos
    assert (clone.source, clone.target, clone.rev, clone.content) == (
        "clone",
        str(tmp_path / "repos/upstream"),
        "release",
        False,
    )
    assert (clone.url, clone.config) == ("https://example.invalid/durations", other)
    # The entry's own config replaces the batch file's tables.
    assert clone.settings.max_lines == 50
    assert prs.settings.max_lines == 100
    assert (prs.source, prs.target, prs.unit, prs.limit, prs.max_files, prs.max_wait) == (
        "pull-requests",
        "hukkin/tomli",
        "pull requests",
        25,
        100,
        0.0,
    )
    assert (prs.api_url, prs.cache_dir, prs.replay) == (
        "https://ghe.example.invalid/api/v3",
        tmp_path / "cache",
        tmp_path / "prs",
    )


ENTRY = '\n[[batch.repos]]\nname = "r"\nhistory = "h.jsonl"\n'


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[filter]\nmax_lines = 1\n", "no \\[batch\\] table"),
        ("batch = 1\n", "\\[batch\\] must be a table"),
        ("[batch]\nnope = 1\n" + ENTRY, "\\[batch\\]: unknown key 'nope'"),
        ("[batch]\nmin_overlap = 0\n" + ENTRY, "min_overlap: expected a number in \\(0, 1\\]"),
        ("[batch]\nmin_overlap = 1.5\n" + ENTRY, "min_overlap"),
        ("[batch]\nmin_overlap = true\n" + ENTRY, "min_overlap"),
        ("[batch]\nowner = 3\n" + ENTRY, "batch.owner: expected a non-empty string"),
        ("[batch]\nledger = ''\n" + ENTRY, "batch.ledger: expected a non-empty string"),
        ("[batch]\n", "batch.repos: list at least one repository"),
        ("[batch]\nrepos = []\n", "batch.repos: list at least one repository"),
        ("[batch]\nrepos = [1]\n", "batch.repos\\[0\\]: expected a table"),
        ("[[batch.repos]]\nname = 'r'\n", "give exactly one of clone, history, github"),
        (
            "[[batch.repos]]\nname = 'r'\nclone = 'a'\nhistory = 'b'\n",
            "give exactly one of clone, history, github",
        ),
        (
            "[[batch.repos]]\nname = 'r'\nhistory = 'h'\nrev = 'main'\n",
            "unknown key 'rev' \\(it applies to clone entries\\)",
        ),
        (
            "[[batch.repos]]\nname = 'r'\nclone = 'c'\nreplay = 'p'\n",
            "unknown key 'replay' \\(it applies to github entries\\)",
        ),
        ("[[batch.repos]]\nname = 'r'\nclone = 'c'\nbogus = 1\n", "unknown key 'bogus'$"),
        ("[[batch.repos]]\nhistory = 'h'\n", "batch.repos\\[0\\].name: expected a non-empty"),
        ("[[batch.repos]]\nname = ' '\nhistory = 'h'\n", ".name: expected a non-empty"),
        ("[[batch.repos]]\nname = 'r'\nhistory = 5\n", ".history: expected a non-empty"),
        ("[[batch.repos]]\nname = 'r'\ngithub = 'nope'\n", "'nope' is not OWNER/REPO"),
        ("[[batch.repos]]\nname = 'r'\nclone = 'c'\ncontent = 1\n", "content: expected true"),
        ("[[batch.repos]]\nname = 'r'\ngithub = 'o/r'\nlimit = 0\n", "limit: expected a pos"),
        ("[[batch.repos]]\nname = 'r'\ngithub = 'o/r'\nmax_files = true\n", "max_files"),
        ("[[batch.repos]]\nname = 'r'\ngithub = 'o/r'\nmax_wait = -1\n", "max_wait: expected"),
        ("[[batch.repos]]\nname = 'r'\ngithub = 'o/r'\nmax_wait = 'x'\n", "max_wait"),
        ("[[batch.repos]]\nname = 'r'\ngithub = 'o/r'\nmax_wait = true\n", "max_wait"),
        ("[[batch.repos]]\nname = 'r'\ngithub = 'o/r'\napi_url = 1\n", "api_url: expected"),
        ("[[batch.repos]]\nname = 'r'\nhistory = 'h'\nurl = ''\n", "url: expected"),
        ("[[batch.repos]]\nname = 'r'\nclone = 'c'\nrev = 1\n", "rev: expected"),
        (ENTRY + ENTRY, "r \\[history\\] is listed twice"),
        ("[filter]\nbogus = 1\n" + ENTRY, "\\[filter\\]: unknown key 'bogus'"),
        (
            "[[batch.repos]]\nname = 'r'\nhistory = 'h'\nconfig = 'missing.toml'\n",
            "missing.toml: cannot read",
        ),
    ],
)
def test_batch_file_errors(tmp_path: Path, text: str, message: str) -> None:
    path = write(tmp_path / "batch.toml", text)
    with pytest.raises(ConfigError, match=message):
        load_batch(path)


def test_unreadable_or_invalid_batch_files(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read"):
        load_batch(tmp_path / "missing.toml")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_batch(write(tmp_path / "bad.toml", "[batch\n"))


def test_dot_dot_after_a_symlinked_directory_follows_the_symlink(tmp_path: Path) -> None:
    # real/configs/b.toml reached as work/cfg/b.toml, with cfg -> ../real/configs: the OS
    # resolves cfg/../shared to real/shared, and so must the batch file (a lexical
    # normalization used to make it work/shared and create a new, empty ledger there).
    configs, shared, work = tmp_path / "real/configs", tmp_path / "real/shared", tmp_path / "work"
    for directory in (configs, shared, work):
        directory.mkdir(parents=True)
    (shared / "h.jsonl.gz").write_bytes((TOMLI / "history.jsonl.gz").read_bytes())
    write(
        configs / "b.toml",
        '[batch]\nledger = "../shared/ledger.sqlite3"\n'
        '[[batch.repos]]\nname = "hukkin/tomli"\nhistory = "../shared/h.jsonl.gz"\n',
    )
    (work / "cfg").symlink_to(Path("../real/configs"))
    config = load_batch(work / "cfg" / "b.toml")
    assert config.ledger == work / "cfg" / ".." / "shared" / "ledger.sqlite3"
    assert config.ledger.resolve() == (shared / "ledger.sqlite3").resolve()
    history = Path(config.repos[0].target)
    assert history.resolve() == (shared / "h.jsonl.gz").resolve()
    assert read_history(history)[0].repo == "hukkin/tomli"
    # Without a symlink the short form is kept (it names the same file).
    plain = load_batch(configs / "b.toml")
    assert plain.ledger == shared / "ledger.sqlite3"
    assert plain.repos[0].target == str(shared / "h.jsonl.gz")


def test_the_copied_constants_match_their_modules() -> None:
    assert batch_module.API_URL == github.API_URL
    assert batch_module.REPO_NAME.pattern == pulls.REPO_NAME.pattern
    assert batch_module.DEFAULT_MAX_FILES == pulls.DEFAULT_MAX_FILES


def test_a_batch_table_does_not_disturb_mine(tmp_path: Path) -> None:
    """mine and explain read the same file for its classifier and scoring tables."""
    from commitminer.config import load_config

    path = write(tmp_path / "commitminer.toml", "[filter]\nmax_lines = 9\n" + ENTRY)
    assert load_config(path).settings().max_lines == 9


# --- walking one source -----------------------------------------------------------------------


def spec(source: str, target: str | Path, **options: Any) -> RepoSpec:
    return RepoSpec("demo/repo", source, str(target), Settings(), **options)


def mark(position: str, source: str = "clone") -> Watermark:
    return Watermark("demo/repo", source, position, 1, 1, NOW)


def fix(repo: GitRepo, number: int) -> str:
    """A commit that changes source and tests together."""
    return repo.commit(
        f"Fix case {number} (fixes #{number})",
        {
            "src/pkg/mod.py": "".join(f"def f{i}():\n    return {i}\n\n" for i in range(number)),
            "tests/test_mod.py": "".join(
                f"def test_f{i}():\n    assert f{i}() == {i}\n\n" for i in range(number)
            ),
        },
    )


def test_a_clone_resumes_from_its_watermark(git_repo: GitRepo) -> None:
    first = fix(git_repo, 1)
    second = fix(git_repo, 2)
    root = git_repo.root
    walked = walk_clone(spec("clone", root), None, frozenset(), full=False)
    assert [c.sha for c in walked.commits] == [second, first]
    assert walked.resume == Resume("first", None, second, 0, None)
    assert walked.url is None
    third = fix(git_repo, 3)
    resumed = walk_clone(spec("clone", root), mark(second), {first, second}, full=False)
    assert [c.sha for c in resumed.commits] == [third]
    assert resumed.resume == Resume("resumed", second, third, 0, None)
    same = walk_clone(spec("clone", root), mark(third), {first, second, third}, full=False)
    assert same.commits == ()
    assert same.resume.mode == "up-to-date"
    # --full walks everything again (nothing is passed over).
    full = walk_clone(spec("clone", root, url="u"), mark(third), frozenset(), full=True)
    assert (len(full.commits), full.resume.mode, full.resume.previous, full.url) == (
        3,
        "full",
        third,
        "u",
    )
    # A rev other than HEAD.
    older = walk_clone(spec("clone", root, rev=first), None, frozenset(), full=False)
    assert [c.sha for c in older.commits] == [first]


def test_a_clone_whose_watermark_cannot_be_used_is_walked_in_full(git_repo: GitRepo) -> None:
    first = fix(git_repo, 1)
    second = fix(git_repo, 2)
    root = git_repo.root
    # History rewritten: the watermark is not an ancestor of the new head.
    git_repo.git("reset", "-q", "--hard", first)
    rewritten = fix(git_repo, 4)
    walked = walk_clone(spec("clone", root), mark(second), {first, second}, full=False)
    assert [c.sha for c in walked.commits] == [rewritten]
    assert walked.resume == Resume(
        "full",
        second,
        rewritten,
        1,
        f"watermark {second[:10]} is not an ancestor of {rewritten[:10]}; walked everything",
    )
    gone = walk_clone(spec("clone", root), mark("0" * 40), frozenset(), full=False)
    assert gone.resume.note == (
        f"watermark {'0' * 10} is not in the clone at {rewritten[:10]}; walked everything"
    )
    assert len(gone.commits) == 2


def test_a_clone_that_git_cannot_read_is_a_source_error(tmp_path: Path) -> None:
    with pytest.raises(batch_module.SourceError, match="git exited with"):
        walk_clone(spec("clone", tmp_path), None, frozenset(), full=False)


def test_a_recording_passes_over_the_commits_evaluated_before(
    git_repo: GitRepo, tmp_path: Path
) -> None:
    fix(git_repo, 1)
    fix(git_repo, 2)
    from commitminer.gitlog import head_sha, walk

    old = tmp_path / "old.jsonl"
    commits = walk(git_repo.root)
    write_history(old, commits, "demo/repo", url="https://example.invalid/r", head=commits[0].sha)
    first = walk_history(spec("history", old), None, frozenset(), full=False)
    assert first.resume == Resume("first", None, commits[0].sha, 0, None)
    assert first.url == "https://example.invalid/r"
    seen = {c.sha for c in first.commits}
    again = walk_history(spec("history", old), mark(commits[0].sha, "history"), seen, False)
    assert (again.commits, again.resume.mode, again.resume.skipped) == ((), "up-to-date", 2)
    # A newer recording of the same repository: only its new commit is replayed.
    third = fix(git_repo, 3)
    new = tmp_path / "new.jsonl"
    write_history(new, walk(git_repo.root), "demo/repo", head=head_sha(git_repo.root))
    resumed = walk_history(spec("history", new), mark(commits[0].sha, "history"), seen, False)
    assert [c.sha for c in resumed.commits] == [third]
    assert resumed.resume == Resume("resumed", commits[0].sha, third, 2, None)
    # A recording without a head: the newest commit stands in; an empty one has none.
    headless = tmp_path / "headless.jsonl"
    write_history(headless, walk(git_repo.root), "demo/repo")
    assert walk_history(spec("history", headless), None, set(), False).resume.watermark == third
    empty = tmp_path / "empty.jsonl"
    write_history(empty, [], "demo/repo")
    assert walk_history(spec("history", empty), None, set(), False).resume.watermark is None


def test_a_missing_or_broken_recording_is_a_source_error(tmp_path: Path) -> None:
    with pytest.raises(batch_module.SourceError, match=r"missing\.jsonl"):
        walk_history(spec("history", tmp_path / "missing.jsonl"), None, set(), False)
    bad = write(tmp_path / "bad.jsonl", "{}\n")
    with pytest.raises(batch_module.SourceError, match="not a commitminer-history file"):
        walk_history(spec("history", bad), None, set(), False)
    # A truncated gzip stream and bytes that are not UTF-8 are source errors too, so the
    # batch reports the repository as failed instead of stopping (they used to escape as
    # EOFError and UnicodeDecodeError).
    truncated = tmp_path / "truncated.jsonl.gz"
    truncated.write_bytes((TOMLI / "history.jsonl.gz").read_bytes()[:60000])
    with pytest.raises(batch_module.SourceError, match="truncated or corrupt gzip stream"):
        walk_history(spec("history", truncated), None, set(), False)
    latin = tmp_path / "latin.jsonl"
    latin.write_bytes(b"\xff\xfe{}\n")
    with pytest.raises(batch_module.SourceError, match="not UTF-8 text"):
        walk_history(spec("history", latin), None, set(), False)


def pr_spec(**options: Any) -> RepoSpec:
    settings: dict[str, Any] = {"replay": TOMLI / "prs", "limit": 25, **options}
    return RepoSpec("hukkin/tomli", "pull-requests", "hukkin/tomli", Settings(), **settings)


def test_pull_requests_resume_from_the_newest_update_listed() -> None:
    first = walk_pulls(pr_spec(), None, frozenset(), False, replay_client)
    assert len(first.commits) == 24
    assert first.resume == Resume("first", None, "2026-04-14T16:51:36Z", 0, None)
    assert first.url == "https://github.com/hukkin/tomli"
    assert first.notes == (
        "github: 52 requests (0 answered 304 from the cache), 0 retries, waited 0 s; "
        "rate limit 4759 of 5000 left, resets 2026-09-30 01:25:39 UTC "
        f"(replayed from {TOMLI / 'prs'})",
        "passed over: 6 closed without merging; #278 with more than 300 changed files",
    )
    watermark = mark("2026-04-14T16:51:36Z", "pull-requests")
    again = walk_pulls(pr_spec(), watermark, frozenset(), False, replay_client)
    assert (again.commits, again.resume.mode) == ((), "up-to-date")
    assert again.notes[0].startswith("github: 1 request (")
    # --full lists and reads everything again (a batch passes no walked shas then).
    full = walk_pulls(
        pr_spec(url="https://example.invalid/t"), watermark, frozenset(), True, replay_client
    )
    assert (len(full.commits), full.resume.mode, full.resume.skipped) == (24, "full", 0)
    assert full.resume.watermark == "2026-04-14T16:51:36Z"
    assert full.url == "https://example.invalid/t"


def test_pull_request_errors_are_source_errors(tmp_path: Path) -> None:
    missing = pr_spec(replay=tmp_path / "none")
    with pytest.raises(batch_module.SourceError, match="no such fixture directory"):
        walk_pulls(missing, None, frozenset(), False, batch_module_client)


def test_a_cache_that_cannot_be_written_is_a_source_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A cache_dir occupied by a file (or a read-only cache, or a full disk) raises OSError
    # from the response cache; it used to escape and abort the whole batch.
    occupied = write(tmp_path / "cachefile", "x")

    def cached_client(spec: RepoSpec) -> GitHubClient:
        assert spec.replay is not None
        return GitHubClient(
            transport=ReplayTransport(spec.replay),
            cache=github.ResponseCache(occupied),
            clock=FakeClock(),
        )

    with pytest.raises(batch_module.SourceError, match=r"hukkin/tomli: .*File exists"):
        walk_pulls(pr_spec(), None, frozenset(), False, cached_client)

    def unreadable(root: Path, rev: str) -> str:
        raise PermissionError(13, "Permission denied", str(root))

    monkeypatch.setattr(batch_module, "resolve_commit", unreadable)
    with pytest.raises(batch_module.SourceError, match="Permission denied"):
        walk_clone(spec("clone", tmp_path), None, frozenset(), full=False)


def batch_module_client(spec: RepoSpec) -> GitHubClient:
    assert spec.replay is not None
    return GitHubClient(transport=ReplayTransport(spec.replay))


def test_web_urls_and_api_roots(monkeypatch: pytest.MonkeyPatch) -> None:
    assert batch_module.web_url("https://api.github.com/", "o/r") == "https://github.com/o/r"
    assert batch_module.web_url("https://ghe.example.invalid/api/v3", "o/r") == (
        "https://ghe.example.invalid/o/r"
    )
    monkeypatch.delenv("GITHUB_API_URL", raising=False)
    assert batch_module.api_root(pr_spec()) == "https://api.github.com"
    monkeypatch.setenv("GITHUB_API_URL", "https://ghe.example.invalid/api/v3")
    assert batch_module.api_root(pr_spec(replay=None)) == "https://ghe.example.invalid/api/v3"
    assert batch_module.api_root(pr_spec(api_url="https://x.invalid")) == "https://x.invalid"
    # Fixtures are keyed by the path of the API they came from, not the host's.
    assert batch_module.api_root(pr_spec()) == "https://api.github.com"


# --- running a batch --------------------------------------------------------------------------


def demo_batch(tmp_path: Path, root: Path, *entries: str) -> BatchConfig:
    text = "".join(f"\n[[batch.repos]]\n{entry}\n" for entry in entries)
    return load_batch(write(tmp_path / "batch.toml", text))


UPSTREAM = 'name = "demo/durations"\nclone = "{root}/upstream"'
FORK = 'name = "demo/durations-fork"\nclone = "{root}/fork"'


def test_a_fork_collides_with_its_upstream(
    demo: tuple[Path, dict[str, dict[str, str]]], tmp_path: Path, ledger: Ledger
) -> None:
    root, shas = demo
    config = demo_batch(tmp_path, root, UPSTREAM.format(root=root), FORK.format(root=root))
    seen: list[str] = []
    result = run_batch(config, ledger, progress=lambda run: seen.append(run.spec.name))
    assert seen == ["demo/durations", "demo/durations-fork"]
    upstream, fork = result.runs
    assert outcome_counts(upstream) == {
        "new": 3, "recorded": 0, "internal": 0, "collision": 0, "unknown": 0,
    }  # fmt: skip
    assert outcome_counts(fork)["collision"] == 3
    assert outcome_counts(fork)["new"] == 1
    assert [(c.repo, c.status, c.same_commit) for c in result.collisions] == [
        ("demo/durations-fork", Status.DUPLICATE, False)
    ] * 3
    assert {c.match.entry.sha for c in result.collisions} == {
        shas["upstream"][key] for key in ("base", "fix_a", "fix_b")
    }
    assert {c.match.in_run for c in result.collisions} == {True}
    text = render_collisions(result)
    assert text.splitlines()[0] == "collisions with other repositories: 3 (3 same fix)"
    fork_fix_b = shas["fork"]["fix_b"][:10]
    assert (
        f"  demo/durations-fork {fork_fix_b}  duplicate: same fix as demo/durations "
        f"{shas['upstream']['fix_b'][:10]} (earlier in this run)"
    ) in text.splitlines()
    assert [e.status for e in ledger.entries()] == ["proposed"] * 4
    assert render_run(upstream) == (
        "demo/durations [clone]: first run, walked 4 commits: 3 candidates, 1 rejected; "
        f"3 new; watermark {shas['upstream']['fix_b'][:10]}"
    )
    # A second run walks nothing and finds nothing.
    again = run_batch(config, ledger)
    assert [run.walked.resume.mode for run in again.runs if run.walked] == ["up-to-date"] * 2
    assert again.collisions == ()
    assert render_run(again.runs[0]) == (
        f"demo/durations [clone]: up to date at {shas['upstream']['fix_b'][:10]}, walked 0 commits"
    )
    assert render_collisions(again) == "collisions with other repositories: none"
    assert render_best(again, 5) == "no new candidates"


def test_cherry_picks_onto_a_release_branch_match_the_same_repository(
    demo: tuple[Path, dict[str, dict[str, str]]], tmp_path: Path, ledger: Ledger
) -> None:
    root, shas = demo
    main = demo_batch(tmp_path, root, UPSTREAM.format(root=root))
    run_batch(main, ledger)
    release = demo_batch(tmp_path, root, UPSTREAM.format(root=root) + '\nrev = "release"')
    (run,) = run_batch(release, ledger).runs
    assert run.walked is not None
    resume = run.walked.resume
    assert (resume.mode, resume.previous, resume.skipped) == ("full", shas["upstream"]["fix_b"], 1)
    assert resume.note is not None
    assert "is not an ancestor of" in resume.note
    assert outcome_counts(run) == {
        "new": 1, "recorded": 0, "internal": 2, "collision": 0, "unknown": 0,
    }  # fmt: skip
    line, note = render_run(run).splitlines()
    assert "1 new, 2 matching other commits of this repository" in line
    assert note == f"  {resume.note}"


def test_full_dry_run_and_only(
    demo: tuple[Path, dict[str, dict[str, str]]], tmp_path: Path, ledger: Ledger
) -> None:
    root, _ = demo
    config = demo_batch(tmp_path, root, UPSTREAM.format(root=root), FORK.format(root=root))
    # A dry run gives the verdicts of a real run and leaves the ledger as it was.
    dry = run_batch(config, ledger, dry_run=True)
    assert dry.dry_run
    assert len(dry.collisions) == 3
    assert ledger.entries() == []
    assert ledger.watermarks() == []
    only = run_batch(config, ledger, only=["demo/durations"])
    assert [run.spec.name for run in only.runs] == ["demo/durations"]
    with pytest.raises(ConfigError, match="no repository named 'nope'"):
        run_batch(config, ledger, only=["nope"])
    # --full walks everything again: upstream's fixes are already recorded.
    full = run_batch(config, ledger, full=True)
    assert full.full
    upstream, fork = full.runs
    assert outcome_counts(upstream)["recorded"] == 3
    assert upstream.walked is not None
    assert upstream.walked.resume.mode == "full"
    assert fork.walked is not None
    assert fork.walked.resume.mode == "first"
    assert outcome_counts(fork)["collision"] == 3
    marks = ledger.watermarks()
    assert [(m.repo, m.runs, m.walked) for m in marks] == [
        ("demo/durations", 2, 8),
        ("demo/durations-fork", 1, 5),
    ]


def test_a_repository_that_fails_does_not_stop_the_others(
    demo: tuple[Path, dict[str, dict[str, str]]], tmp_path: Path, ledger: Ledger
) -> None:
    root, _ = demo
    config = demo_batch(
        tmp_path,
        root,
        'name = "gone"\nhistory = "missing.jsonl.gz"',
        'name = "hukkin/tomli"\ngithub = "hukkin/tomli"',
        'name = "truncated"\nhistory = "truncated.jsonl.gz"',
        UPSTREAM.format(root=root),
    )
    (config.path.parent / "truncated.jsonl.gz").write_bytes(
        (TOMLI / "history.jsonl.gz").read_bytes()[:60000]
    )
    result = run_batch(config, ledger)
    gone, prs, truncated, upstream = result.runs
    assert truncated.error is not None
    assert "truncated or corrupt gzip stream" in truncated.error
    assert gone.error is not None
    assert "missing.jsonl.gz" in gone.error
    assert render_run(gone).startswith("gone [history]: failed: ")
    assert prs.error == "no GitHub client for pull-request entries"
    assert upstream.result is not None
    assert [run.spec.name for run in result.failed] == ["gone", "hukkin/tomli", "truncated"]
    assert set(outcome_counts(gone).values()) == {0}
    assert {run.spec.name for run, _, _ in best_new(result)} == {"demo/durations"}
    records = batch_records(result)
    assert records[0]["failed"] == [
        {"repo": "gone", "source": "history", "error": gone.error},
        {"repo": "hukkin/tomli", "source": "pull-requests", "error": prs.error},
        {"repo": "truncated", "source": "history", "error": truncated.error},
    ]
    assert records[0]["runs"] == 1


def test_an_interrupted_or_unfinished_batch_records_nothing(
    demo: tuple[Path, dict[str, dict[str, str]]], tmp_path: Path, ledger: Ledger
) -> None:
    # Regression: each repository was committed as soon as it was mined, before the export
    # was written, so a batch interrupted in between lost those candidates from every later
    # export (the next run found them up to date).
    root, _ = demo
    config = demo_batch(tmp_path, root, UPSTREAM.format(root=root), FORK.format(root=root))

    def interrupt(run: RepoRun) -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_batch(config, ledger, progress=interrupt)
    assert (ledger.entries(), ledger.watermarks()) == ([], [])

    def export_fails(result: BatchResult) -> None:
        # The export is written with the recorded verdicts, before anything is committed.
        assert len(result.runs) == 2
        with open_ledger(tmp_path / "ledger.sqlite3", readonly=True) as other:
            assert other.entries() == []
        raise OSError(28, "No space left on device")

    with pytest.raises(OSError, match="No space left"):
        run_batch(config, ledger, finish=export_fails)
    assert (ledger.entries(), ledger.watermarks()) == ([], [])
    finished: list[BatchResult] = []
    result = run_batch(config, ledger, finish=finished.append)
    assert finished == [result]
    assert [outcome_counts(run)["new"] for run in result.runs] == [3, 1]
    assert len(ledger.entries()) == 4
    assert [m.repo for m in ledger.watermarks()] == ["demo/durations", "demo/durations-fork"]


def test_the_same_commits_under_two_names_collide_as_the_same_commit(
    tmp_path: Path, ledger: Ledger
) -> None:
    history = TOMLI / "history.jsonl.gz"
    config = demo_batch(
        tmp_path,
        tmp_path,
        f'name = "hukkin/tomli"\nhistory = "{history}"',
        f'name = "mirror/tomli"\nhistory = "{history}"',
    )
    result = run_batch(config, ledger)
    assert len(result.collisions) == 44
    assert all(c.same_commit and c.status is Status.DUPLICATE for c in result.collisions)
    assert render_collisions(result).splitlines() == [
        "collisions with other repositories: 44 (44 same commit)",
        "  44 candidates of mirror/tomli are commits of hukkin/tomli too "
        "(a fork or a mirror shares its history)",
    ]
    lines = render_best(result, 3).splitlines()
    assert lines[0] == "best new candidates across the batch (3 of 44):"
    assert lines[1].split() == ["score", "diff", "repo", "rank", "commit", "date", "subject"]
    assert lines[2].split()[:5] == ["6.88", "4.74", "hard", "hukkin/tomli", "1"]
    assert all(len(line) <= 120 for line in lines)
    assert [item[1] for item in best_new(result)][:3] == [1, 2, 3]


def test_the_batch_export_validates_against_the_schema(
    demo: tuple[Path, dict[str, dict[str, str]]], tmp_path: Path, ledger: Ledger
) -> None:
    root, _ = demo
    config = demo_batch(
        tmp_path,
        root,
        UPSTREAM.format(root=root),
        FORK.format(root=root),
        f'name = "hukkin/tomli"\ngithub = "hukkin/tomli"\nlimit = 25\nreplay = "{TOMLI / "prs"}"',
        'name = "gone"\nclone = "nowhere"',
    )
    result = run_batch(config, ledger, client_factory=replay_client)
    records = batch_records(result)
    validator = Draft202012Validator(json.loads(schema_text()))
    for record in records:
        errors = [error.message for error in validator.iter_errors(record)]
        assert errors == [], record.get("sha")
    kinds = [record["kind"] for record in records]
    assert kinds[0] == "batch"
    assert kinds.count("run") == 3
    assert kinds.count("candidate") == 3 + 4 + 6
    batch = records[0]
    assert (batch["runs"], len(batch["collisions"]), batch["min_overlap"]) == (3, 3, 0.5)
    assert batch["collisions"][0]["match"]["source"] == "run"
    runs = [record for record in records if record["kind"] == "run"]
    assert [run["resume"]["mode"] for run in runs] == ["first"] * 3
    assert [run["ledger"]["counts"]["new"] for run in runs] == [3, 1, 6]


# --- rendering --------------------------------------------------------------------------------


def test_short_watermarks() -> None:
    assert short("a" * 40) == "a" * 10
    assert short("2026-04-14T16:51:36Z") == "2026-04-14T16:51:36Z"
    assert short("") == "-"
    assert short(None) == "-"


def _entry(repo: str, sha: str) -> Entry:
    return Entry(1, "f", repo, sha, "s", "proposed", None, NOW, 1)


def test_outcomes() -> None:
    new = Verdict(Status.NEW)
    assert outcome("r", "a", new) == "new"
    assert outcome("r", "a", Verdict(Status.UNKNOWN)) == "unknown"
    own = Verdict(Status.DUPLICATE, (Match(_entry("r", "a"), 1, 1, True),))
    assert outcome("r", "a", own) == "recorded"
    other = Verdict(Status.OVERLAP, (Match(_entry("r", "b"), 1, 2, False),))
    assert outcome("r", "a", other) == "internal"
    elsewhere = Verdict(
        Status.DUPLICATE,
        (Match(_entry("r", "b"), 1, 1, True), Match(_entry("x", "c"), 1, 2, False)),
    )
    assert outcome("r", "a", elsewhere) == "collision"


def _ranked(run_spec: RepoSpec, walked: batch_module.Walked) -> RepoRun:
    """``walked`` ranked, every candidate new."""
    result = mine(walked.commits)
    verdicts = tuple(Verdict(Status.NEW) for _ in result.candidates)
    return RepoRun(run_spec, walked, result, verdicts)


def test_render_run_of_pull_requests_and_counts() -> None:
    walked = walk_pulls(pr_spec(), None, frozenset(), False, replay_client)
    lines = render_run(_ranked(pr_spec(), walked)).splitlines()
    assert lines[0] == (
        "hukkin/tomli [pull-requests]: first run, walked 24 pull requests: 6 candidates, "
        "18 rejected; 6 new; watermark 2026-04-14T16:51:36Z"
    )
    assert lines[1].startswith("  github: 52 requests")
    assert lines[2].startswith("  passed over: 6 closed without merging")
    best = mine(walked.commits).candidates[0].commit
    one = replace(walked, commits=(best,), notes=())
    assert render_run(_ranked(pr_spec(), one)) == (
        "hukkin/tomli [pull-requests]: first run, walked 1 pull request: 1 candidate, "
        "0 rejected; 1 new; watermark 2026-04-14T16:51:36Z"
    )


def test_render_best_clips_long_subjects(tmp_path: Path, ledger: Ledger) -> None:
    repo = GitRepo(tmp_path / "long")
    repo.commit(
        "Fix " + "a very long subject " * 5,
        {"src/pkg/mod.py": "x = 1\n", "tests/test_mod.py": "def test_x():\n    assert x\n"},
    )
    config = demo_batch(tmp_path, tmp_path, f'name = "r"\nclone = "{repo.root}"')
    result = run_batch(config, ledger)
    row = render_best(result, 1).splitlines()[2]
    assert row.endswith("...")
    assert len(row.split("  ")[-1]) == 50


def test_batch_result_failed_is_empty_without_errors(ledger: Ledger, tmp_path: Path) -> None:
    config = BatchConfig(tmp_path / "b.toml", tmp_path / "l", None, None, 0.5, None, ())
    result = run_batch(config, ledger)
    assert isinstance(result, BatchResult)
    assert (result.runs, result.failed, result.collisions) == ((), (), ())
    assert result.ledger == tmp_path / "ledger.sqlite3"


def test_parse_batch_accepts_a_parsed_document(tmp_path: Path) -> None:
    config = parse_batch({"batch": {"repos": [{"name": "r", "history": "h"}]}}, tmp_path / "x")
    assert config.repos[0].target == str(tmp_path / "h")


def test_history_of_the_demo_recording_is_read_once(tmp_path: Path) -> None:
    header, commits = read_history(TOMLI / "history.jsonl.gz")
    assert header.head == commits[0].sha


def test_collision_kinds_add_up(tmp_path: Path) -> None:
    """A same-commit match found by overlap (other settings) is counted once, as same commit."""
    from commitminer.batch import Collision
    from commitminer.models import Commit

    commit = Commit("a" * 40, (), NOW, "Fix\n", ())
    candidate = Candidate(commit, None, (), 1.0, None)  # type: ignore[arg-type]
    config = BatchConfig(tmp_path / "b.toml", tmp_path / "l", None, None, 0.5, None, ())
    same_sha = Match(_entry("up", "a" * 40), 1, 2, False)
    other = Match(_entry("up", "b" * 40), 1, 2, False)
    result = BatchResult(
        config,
        tmp_path / "l",
        (),
        (Collision("fork", 1, candidate, same_sha), Collision("fork", 2, candidate, other)),
    )
    assert render_collisions(result).splitlines()[0] == (
        "collisions with other repositories: 2 (1 same commit, 1 overlap)"
    )
