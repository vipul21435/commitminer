"""Recording GitHub responses as fixtures and replaying them offline."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from commitminer.fixtures import (
    FixtureError,
    RecordingTransport,
    ReplayTransport,
    fixture_key,
    fixture_name,
    trim,
)
from commitminer.github import GitHubClient
from fixturefiles import API, FakeClock, reply, write_fixture

PULL = {
    "number": 5,
    "title": "Fix it",
    "user": {"login": "someone", "id": 1},
    "base": {"ref": "main", "sha": "b" * 40, "repo": {"full_name": "o/r"}},
    "labels": [{"name": "bug", "color": "red"}],
}


def test_fixture_key_sorts_the_query_and_drops_the_host() -> None:
    url = httpx.URL(f"{API}/repos/o/r/pulls?state=closed&per_page=100&page=2")
    key = fixture_key("GET", url)
    assert key == "GET /repos/o/r/pulls?page=2&per_page=100&state=closed"
    assert fixture_key("GET", httpx.URL("https://ghe.example/api/v3/x")) == "GET /api/v3/x"
    name = fixture_name(key)
    assert name.startswith("get-repos-o-r-pulls-page-2-per-page-100-state-closed-")
    assert name.endswith(".json")
    assert fixture_name("GET /" + "x" * 200) != fixture_name("GET /" + "x" * 201)


def test_trim_keeps_only_the_fields_commitminer_reads() -> None:
    assert trim([PULL]) == [
        {
            "number": 5,
            "title": "Fix it",
            "base": {"ref": "main", "sha": "b" * 40},
            "labels": [{"name": "bug"}],
        }
    ]


def test_recording_stores_trimmed_answers_without_request_headers(tmp_path: Path) -> None:
    answers = iter(
        [
            httpx.Response(429, json={"message": "wait"}, headers={"retry-after": "1"}),
            httpx.Response(
                200,
                json=[PULL],
                headers={"etag": '"e1"', "x-ratelimit-remaining": "58", "set-cookie": "no"},
            ),
            httpx.Response(200, text="plain"),
            httpx.Response(304),
        ]
    )
    recorder = RecordingTransport(httpx.MockTransport(lambda _: next(answers)), tmp_path)
    clock = FakeClock()
    with GitHubClient(transport=recorder, clock=clock, token="secret-token") as client:
        assert client.get("/repos/o/r/pulls").data == trim([PULL])
        with pytest.raises(Exception, match="not JSON"):
            client.get("/text")
        with pytest.raises(Exception, match="answered 304"):
            client.get("/empty")
    assert recorder.recorded == 4
    assert clock.sleeps == [1.0]
    files = sorted(tmp_path.iterdir())
    assert len(files) == 3
    text = "".join(path.read_text() for path in files)
    assert "secret-token" not in text
    assert "set-cookie" not in text
    assert "login" not in text
    pulls = json.loads((tmp_path / fixture_name("GET /repos/o/r/pulls")).read_text())
    assert pulls["key"] == "GET /repos/o/r/pulls"
    assert [r["status"] for r in pulls["responses"]] == [429, 200]
    assert pulls["responses"][1]["headers"] == {
        "content-type": "application/json",
        "etag": '"e1"',
        "x-ratelimit-remaining": "58",
    }
    plain = json.loads((tmp_path / fixture_name("GET /text")).read_text())
    assert plain["responses"][0]["text"] == "plain"
    empty = json.loads((tmp_path / fixture_name("GET /empty")).read_text())
    assert "json" not in empty["responses"][0]


def test_a_recording_replays_to_the_same_answers(tmp_path: Path) -> None:
    link = f'<{API}/repos/o/r/pulls?page=2>; rel="next"'
    pages = {
        "/repos/o/r/pulls": httpx.Response(200, json=[PULL], headers={"link": link}),
        "/repos/o/r/pulls?page=2": httpx.Response(200, json=[{**PULL, "number": 6}]),
    }

    def server(request: httpx.Request) -> httpx.Response:
        return pages[request.url.raw_path.decode()]

    live = RecordingTransport(httpx.MockTransport(server), tmp_path)
    with GitHubClient(transport=live) as client:
        recorded = list(client.items("/repos/o/r/pulls"))
    with GitHubClient(transport=ReplayTransport(tmp_path)) as client:
        assert list(client.items("/repos/o/r/pulls")) == recorded
    assert [item["number"] for item in recorded] == [5, 6]
    live.close()


def test_replay_serves_in_order_repeats_the_last_answer_and_emulates_304(tmp_path: Path) -> None:
    write_fixture(
        tmp_path,
        "GET /x",
        reply(500, {"message": "boom"}),
        reply(200, [1], {"etag": '"v1"'}),
    )
    write_fixture(tmp_path, "GET /t", {"status": 200, "headers": {}, "text": "hi"})
    transport = ReplayTransport(tmp_path)
    assert transport.keys == 2

    def send(path: str, **headers: str) -> httpx.Response:
        request = httpx.Request("GET", f"{API}{path}", headers=headers)
        return transport.handle_request(request)

    assert send("/x").status_code == 500
    assert send("/x").json() == [1]
    assert send("/x").json() == [1]
    assert send("/x", **{"If-None-Match": '"v1"'}).status_code == 304
    assert send("/x", **{"If-None-Match": '"v0"'}).status_code == 200
    assert send("/t").text == "hi"
    with pytest.raises(FixtureError, match="no recorded response for GET /missing"):
        send("/missing")


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("not json", "cannot read fixture"),
        ("[]", "not a commitminer-http-fixture file"),
        (
            json.dumps({"format": "commitminer-http-fixture", "version": 9}),
            "unsupported fixture version 9",
        ),
        (
            json.dumps({"format": "commitminer-http-fixture", "version": 1, "key": "GET /x"}),
            "needs a key and at least one response",
        ),
        (
            json.dumps(
                {
                    "format": "commitminer-http-fixture",
                    "version": 1,
                    "key": "GET /x",
                    "responses": [{"status": "200"}],
                }
            ),
            "needs an integer status",
        ),
        (
            json.dumps(
                {
                    "format": "commitminer-http-fixture",
                    "version": 1,
                    "key": "GET /x",
                    "responses": [{"status": 200, "headers": {"etag": 1}}],
                }
            ),
            "headers must map names to strings",
        ),
    ],
)
def test_replay_rejects_malformed_fixtures(tmp_path: Path, content: str, message: str) -> None:
    (tmp_path / "bad.json").write_text(content)
    with pytest.raises(FixtureError, match=message):
        ReplayTransport(tmp_path)


def test_replay_rejects_a_missing_directory_and_duplicate_keys(tmp_path: Path) -> None:
    with pytest.raises(FixtureError, match="no such fixture directory"):
        ReplayTransport(tmp_path / "none")
    first = write_fixture(tmp_path, "GET /x", reply(200, []))
    (tmp_path / "copy.json").write_text(first.read_text())
    with pytest.raises(FixtureError, match="a second fixture for GET /x"):
        ReplayTransport(tmp_path)


def test_a_recording_directory_that_cannot_be_made_is_a_fixture_error(tmp_path: Path) -> None:
    occupied = tmp_path / "fixtures"
    occupied.write_text("x")
    inner = httpx.MockTransport(lambda _: httpx.Response(200, json=[]))
    with pytest.raises(FixtureError, match=r"cannot create the fixture directory: .*File exists"):
        RecordingTransport(inner, occupied)


def test_a_fixture_that_cannot_be_written_is_a_fixture_error(tmp_path: Path) -> None:
    recorder = RecordingTransport(httpx.MockTransport(lambda _: httpx.Response(200)), tmp_path)
    (tmp_path / fixture_name("GET /x")).mkdir()
    client = GitHubClient(transport=recorder, clock=FakeClock())
    with pytest.raises(FixtureError, match="cannot write the fixture"):
        client.get(f"{API}/x")
