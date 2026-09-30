"""The bundled tomli recording: provenance, integrity and the pinned demo ranking."""

from __future__ import annotations

from pathlib import Path

from commitminer.history import read_history
from commitminer.scoring import mine

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "tomli"
HEAD = "5a77b12a7a9f052ce5a20c335d2825658f6aea52"


def test_recording_header_and_provenance() -> None:
    header, commits = read_history(EXAMPLE / "history.jsonl.gz")
    assert header.repo == "hukkin/tomli"
    assert header.url == "https://github.com/hukkin/tomli"
    assert header.head == HEAD
    assert len(commits) == header.commits == 312
    assert commits[0].sha == HEAD
    assert (EXAMPLE / "LICENSE").read_text().startswith("MIT License")
    assert HEAD in (EXAMPLE / "README.md").read_text()


def test_recording_is_a_consistent_history() -> None:
    _, commits = read_history(EXAMPLE / "history.jsonl.gz")
    shas = [c.sha for c in commits]
    assert len(set(shas)) == len(shas)
    assert all(len(c.parents) <= 1 for c in commits)  # merges are excluded
    assert sum(len(c.files) for c in commits) == 5292


def test_demo_ranking_is_pinned() -> None:
    _, commits = read_history(EXAMPLE / "history.jsonl.gz")
    result = mine(commits)
    assert result.walked == 312
    assert len(result.candidates) == 44
    assert result.rejected_by_reason() == {
        "docs-only": 42,
        "no-source": 100,
        "source-unchanged": 1,
        "source-cosmetic": 6,
        "no-test": 115,
        "oversize": 4,
    }
    assert result.bands() == {"easy": 15, "medium": 17, "hard": 12}
    top = [
        (c.commit.sha[:10], c.score, c.difficulty.value, c.difficulty.band)
        for c in result.candidates[:3]
    ]
    assert top == [
        ("5ab9ec926d", 6.885, 4.74, "hard"),
        ("948211d852", 6.6375, 3.42, "medium"),
        ("8b962e1349", 6.565, 0.74, "easy"),
    ]
    # Ranked 5th before patches were read: it only adds "# pragma: no cover" comments.
    cosmetic = {r.commit.sha[:10] for r in result.rejections if r.reason == "source-cosmetic"}
    assert "27be26fa4d" in cosmetic


def test_every_demo_candidate_has_a_fingerprint() -> None:
    _, commits = read_history(EXAMPLE / "history.jsonl.gz")
    result = mine(commits)
    prints = [c.fingerprint for c in result.candidates]
    assert all(value is not None for value in prints)
    # No two tomli candidates are the same fix.
    assert len({value.patch for value in prints if value is not None}) == len(prints)


# --- the Go fork pair and the demo batch ----------------------------------------------------

MAPSTRUCTURE = EXAMPLE.parent / "mapstructure"
UPSTREAM_HEAD = "8508981c8b6c964e6986dd8aa85490e70ce3c2e2"
FORK_HEAD = "52aa5c6dc1d27226460807054ca2107b2d54fb2d"


def test_mapstructure_recordings_and_provenance() -> None:
    upstream_header, upstream = read_history(MAPSTRUCTURE / "mitchellh.jsonl.gz")
    fork_header, fork = read_history(MAPSTRUCTURE / "go-viper.jsonl.gz")
    assert (upstream_header.repo, upstream_header.head, len(upstream)) == (
        "mitchellh/mapstructure",
        UPSTREAM_HEAD,
        236,
    )
    assert (fork_header.repo, fork_header.head, len(fork)) == (
        "go-viper/mapstructure",
        FORK_HEAD,
        374,
    )
    assert (sum(len(c.files) for c in upstream), sum(len(c.files) for c in fork)) == (364, 589)
    # The fork continued the original: all 236 upstream commits are in its history.
    assert {c.sha for c in upstream} <= {c.sha for c in fork}
    assert (MAPSTRUCTURE / "LICENSE").read_text().startswith("The MIT License (MIT)")
    readme = (MAPSTRUCTURE / "README.md").read_text()
    assert UPSTREAM_HEAD in readme
    assert FORK_HEAD in readme


def test_go_candidates_are_classified_by_the_go_rules() -> None:
    _, commits = read_history(MAPSTRUCTURE / "mitchellh.jsonl.gz")
    result = mine(commits)
    assert (len(result.candidates), result.bands()) == (105, {"easy": 70, "medium": 28, "hard": 7})
    best = result.candidates[0]
    assert (best.commit.sha[:10], best.score) == ("1d69ed7aa0", 9.4375)
    assert [(f.change.path, f.classification.rule_id) for f in best.stats.files] == [
        ("mapstructure.go", "go-source"),
        ("mapstructure_test.go", "go-test-file"),
    ]
    assert best.stats.fail_to_pass == (
        "mapstructure_test.go::TestDecodeFrom_EmbeddedSquashConfig",
        "mapstructure_test.go::TestDecode_EmbeddedSquashConfig",
    )


def test_the_demo_batch_collides_the_fork_with_its_upstream(tmp_path: Path) -> None:
    from commitminer.batch import load_batch, run_batch
    from commitminer.cli import _batch_client
    from commitminer.ledger import open_ledger

    config = load_batch(EXAMPLE.parent / "batch" / "commitminer.toml")
    assert [(r.name, r.source) for r in config.repos] == [
        ("hukkin/tomli", "history"),
        ("hukkin/tomli", "pull-requests"),
        ("mitchellh/mapstructure", "history"),
        ("go-viper/mapstructure", "history"),
    ]
    assert config.repos[1].replay == EXAMPLE / "prs"
    with open_ledger(tmp_path / "ledger.sqlite3") as ledger:
        result = run_batch(config, ledger, client_factory=_batch_client)
        assert len(ledger.entries()) == 44 + 105 + 32
    walked = [run.result.walked for run in result.runs if run.result]
    assert walked == [312, 24, 236, 374]
    assert len(result.collisions) == 106
    assert sum(c.same_commit for c in result.collisions) == 105
    (other,) = [c for c in result.collisions if not c.same_commit]
    assert (other.candidate.commit.sha[:10], other.match.entry.sha[:10]) == (
        "2e2be32560",
        "b37a0d6b00",
    )
    assert (other.match.shared, other.match.smaller) == (1, 2)
