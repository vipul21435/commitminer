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
