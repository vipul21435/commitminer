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
    assert len(result.candidates) == 47
    assert result.rejected_by_reason() == {
        "no-source": 142,
        "source-unchanged": 1,
        "no-test": 118,
        "too-large": 4,
    }
    top = [(c.commit.sha[:10], c.score) for c in result.candidates[:3]]
    assert top == [("2a2aa62f1b", 7.55), ("948211d852", 7.4375), ("5ab9ec926d", 7.16)]
