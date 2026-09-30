"""The GitHub client: ETag cache, pagination, rate limits and retries (offline)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from commitminer import github
from commitminer.fixtures import ReplayTransport
from commitminer.github import (
    CachedResponse,
    GitHubClient,
    GitHubError,
    RateLimitError,
    RequestStats,
    ResponseCache,
    SystemClock,
    default_cache_dir,
    next_link,
)
from fixturefiles import API, FakeClock, reply, write_fixture

PULLS = "GET /repos/o/r/pulls?per_page=2"


def client_for(
    directory: Path,
    clock: FakeClock | None = None,
    cache: ResponseCache | None = None,
    **options: float,
) -> GitHubClient:
    return GitHubClient(
        transport=ReplayTransport(directory),
        clock=clock or FakeClock(),
        cache=cache,
        max_retries=int(options.get("max_retries", 4)),
        max_wait=options.get("max_wait", 300.0),
    )


def mock_client(
    handler: Callable[[httpx.Request], httpx.Response],
    clock: FakeClock | None = None,
    token: str | None = None,
    cache: ResponseCache | None = None,
) -> GitHubClient:
    return GitHubClient(
        transport=httpx.MockTransport(handler), clock=clock or FakeClock(), token=token, cache=cache
    )


# --- conditional requests and pagination ---------------------------------------------


def test_etag_cache_turns_repeat_requests_into_304s(tmp_path: Path) -> None:
    fixtures = tmp_path / "fixtures"
    write_fixture(fixtures, PULLS, reply(200, [{"number": 1}], {"etag": '"v1"'}))
    cache = ResponseCache(tmp_path / "cache")
    with client_for(fixtures, cache=cache) as first:
        assert first.get("/repos/o/r/pulls", {"per_page": 2}).data == [{"number": 1}]
        assert first.stats.not_modified == 0
    with client_for(fixtures, cache=cache) as second:
        assert second.get("/repos/o/r/pulls", {"per_page": 2}).data == [{"number": 1}]
        assert (second.stats.requests, second.stats.not_modified) == (1, 1)


def test_last_modified_is_sent_back_as_if_modified_since(tmp_path: Path) -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("if-modified-since"))
        if request.headers.get("if-modified-since"):
            return httpx.Response(304)
        return httpx.Response(200, json={"a": 1}, headers={"last-modified": "Mon, 1 Jan 2024"})

    cache = ResponseCache(tmp_path / "cache")
    for _ in range(2):
        with mock_client(handler, cache=cache) as client:
            assert client.get("/x").data == {"a": 1}
    assert seen == [None, "Mon, 1 Jan 2024"]


def test_answers_without_validators_are_not_cached(tmp_path: Path) -> None:
    cache = ResponseCache(tmp_path / "cache")
    with mock_client(lambda _: httpx.Response(200, json=[]), cache=cache) as client:
        client.get("/x")
    assert not (tmp_path / "cache").exists()


def test_pages_follow_link_headers(tmp_path: Path) -> None:
    fixtures = tmp_path / "fixtures"
    link = f'<{API}/repositories/7/pulls?per_page=2&page=2>; rel="next", <{API}/x>; rel="last"'
    write_fixture(fixtures, PULLS, reply(200, [1, 2], {"link": link}))
    write_fixture(fixtures, "GET /repositories/7/pulls?page=2&per_page=2", reply(200, [3]))
    with client_for(fixtures) as client:
        assert list(client.items("/repos/o/r/pulls", {"per_page": 2})) == [1, 2, 3]
        assert client.stats.requests == 2


def test_items_needs_a_list(tmp_path: Path) -> None:
    write_fixture(tmp_path, "GET /x", reply(200, {"not": "a list"}))
    with client_for(tmp_path) as client, pytest.raises(GitHubError, match="expected a list"):
        list(client.items("/x"))


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, None),
        ("", None),
        ('<https://a/2>; rel="next"', "https://a/2"),
        ("<https://a/2>; rel=next", "https://a/2"),
        ('<https://a/9>; rel="last", <https://a/3>; rel="next"', "https://a/3"),
        ('garbage, <https://a/9>; rel="last"', None),
    ],
)
def test_next_link(header: str | None, expected: str | None) -> None:
    assert next_link(header) == expected


def test_requests_carry_the_api_headers_and_the_token() -> None:
    seen: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers)
        return httpx.Response(200, json={})

    with mock_client(handler, token="t0ken") as client:
        client.get("/x")
    with mock_client(handler) as client:
        client.get("/x")
    assert seen[0]["authorization"] == "Bearer t0ken"
    assert seen[0]["accept"] == "application/vnd.github+json"
    assert seen[0]["user-agent"].startswith("commitminer/")
    assert "authorization" not in seen[1]


# --- errors -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "body", "message"),
    [
        (404, {"message": "Not Found"}, "not found: .*check OWNER/REPO"),
        (401, {"message": "Bad credentials"}, "check GITHUB_TOKEN.*Bad credentials"),
        (422, {"message": "Validation Failed"}, "answered 422 .*Validation Failed"),
        (403, {"message": "Resource not accessible"}, "answered 403 .*not accessible"),
        (410, ["no message"], "answered 410 .*no message"),
    ],
)
def test_errors_are_reported_with_githubs_message(
    tmp_path: Path, status: int, body: object, message: str
) -> None:
    write_fixture(tmp_path, "GET /x", reply(status, body))
    with client_for(tmp_path) as client, pytest.raises(GitHubError, match=message):
        client.get("/x")
    assert client.stats.retries == 0


def test_plain_text_errors_and_non_json_answers() -> None:
    with (
        mock_client(lambda _: httpx.Response(418, text="teapot\n")) as client,
        pytest.raises(GitHubError, match=r"418 .*teapot"),
    ):
        client.get("/x")
    with (
        mock_client(lambda _: httpx.Response(418)) as client,
        pytest.raises(GitHubError, match=r"\(no message\)"),
    ):
        client.get("/x")
    with (
        mock_client(lambda _: httpx.Response(200, text="<html>")) as client,
        pytest.raises(GitHubError, match="not JSON"),
    ):
        client.get("/x")


def test_an_unrequested_304_is_an_error() -> None:
    with (
        mock_client(lambda _: httpx.Response(304)) as client,
        pytest.raises(GitHubError, match="answered 304"),
    ):
        client.get("/x")


# --- rate limits and retries -------------------------------------------------------------


def limits(remaining: int, reset: float, limit: int = 60) -> dict[str, str]:
    return {
        "x-ratelimit-limit": str(limit),
        "x-ratelimit-remaining": str(remaining),
        "x-ratelimit-reset": str(int(reset)),
    }


def test_an_exhausted_budget_waits_for_the_reset_before_the_next_request(tmp_path: Path) -> None:
    clock = FakeClock()
    write_fixture(tmp_path, "GET /a", reply(200, [], limits(0, clock.now + 30)))
    write_fixture(tmp_path, "GET /b", reply(200, [], limits(59, clock.now + 3600)))
    with client_for(tmp_path, clock) as client:
        client.get("/a")
        assert clock.sleeps == []
        client.get("/b")
    assert clock.sleeps == [31.0]
    assert client.stats.remaining == 59
    assert client.stats.waited == 31.0


def test_a_reset_in_the_past_does_not_wait(tmp_path: Path) -> None:
    clock = FakeClock()
    write_fixture(tmp_path, "GET /a", reply(200, [], limits(0, clock.now - 5)))
    with client_for(tmp_path, clock) as client:
        client.get("/a")
        client.get("/a")
    assert clock.sleeps == []


def test_retry_after_is_honoured_on_403_and_429(tmp_path: Path) -> None:
    clock = FakeClock()
    write_fixture(
        tmp_path,
        "GET /x",
        reply(403, {"message": "slow down"}, {"retry-after": "7"}),
        reply(429, {"message": "slow down"}, {"retry-after": "2"}),
        reply(200, {"ok": True}),
    )
    with client_for(tmp_path, clock) as client:
        assert client.get("/x").data == {"ok": True}
    assert clock.sleeps == [7.0, 2.0]
    assert (client.stats.requests, client.stats.retries) == (3, 2)


def test_a_primary_limit_answer_waits_until_the_reset(tmp_path: Path) -> None:
    clock = FakeClock()
    write_fixture(
        tmp_path,
        "GET /x",
        reply(403, {"message": "API rate limit exceeded"}, limits(0, clock.now + 100)),
        reply(200, [], limits(4999, clock.now + 3700, 5000)),
    )
    with client_for(tmp_path, clock) as client:
        client.get("/x")
    assert clock.sleeps == [101.0]
    assert "4999 of 5000 left" in client.stats.describe()


def test_secondary_limits_back_off_from_a_minute(tmp_path: Path) -> None:
    clock = FakeClock()
    secondary = {"message": "You have exceeded a secondary rate limit"}
    write_fixture(
        tmp_path,
        "GET /x",
        reply(403, secondary),
        reply(429, {"message": "Too many"}),
        reply(200, []),
    )
    with client_for(tmp_path, clock, max_wait=1000) as client:
        client.get("/x")
    assert clock.sleeps == [60.0, 120.0]


def test_an_unparsable_retry_after_falls_back_to_the_other_rules(tmp_path: Path) -> None:
    clock = FakeClock()
    write_fixture(
        tmp_path,
        "GET /x",
        reply(429, {}, {"retry-after": "Wed, 21 Oct 2015 07:28:00 GMT"}),
        reply(200, []),
    )
    with client_for(tmp_path, clock) as client:
        client.get("/x")
    assert clock.sleeps == [60.0]


def test_server_errors_back_off_exponentially(tmp_path: Path) -> None:
    clock = FakeClock()
    write_fixture(tmp_path, "GET /x", reply(502, {}), reply(503, {}), reply(200, [1]))
    with client_for(tmp_path, clock) as client:
        assert client.get("/x").data == [1]
    assert clock.sleeps == [1.0, 2.0]


def test_retries_give_up(tmp_path: Path) -> None:
    clock = FakeClock()
    write_fixture(tmp_path, "GET /x", reply(500, {"message": "boom"}))
    with (
        client_for(tmp_path, clock, max_retries=2) as client,
        pytest.raises(GitHubError, match="gave up after 3 attempts: 500 boom"),
    ):
        client.get("/x")
    assert clock.sleeps == [1.0, 2.0]


def test_long_waits_raise_instead_of_hanging(tmp_path: Path) -> None:
    clock = FakeClock()
    write_fixture(tmp_path, "GET /a", reply(200, [], limits(0, clock.now + 3000)))
    write_fixture(tmp_path, "GET /b", reply(403, {}, {"retry-after": "900"}))
    with client_for(tmp_path, clock, max_wait=60) as client:
        client.get("/a")
        with pytest.raises(RateLimitError, match=r"rate limit used up until .*set GITHUB_TOKEN"):
            client.get("/a")
    with (
        client_for(tmp_path, clock, max_wait=60) as client,
        pytest.raises(RateLimitError, match="Retry-After: waiting 900 s"),
    ):
        client.get("/b")
    with (
        GitHubClient(
            transport=ReplayTransport(tmp_path), clock=clock, token="t", max_wait=60
        ) as client,
        pytest.raises(RateLimitError) as raised,
    ):
        client.get("/b")
    assert "GITHUB_TOKEN" not in str(raised.value)
    assert clock.sleeps == []


def test_network_errors_are_retried(tmp_path: Path) -> None:
    calls: list[int] = []

    def flaky(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200, json=[])

    clock = FakeClock()
    with mock_client(flaky, clock) as client:
        client.get("/x")
    assert clock.sleeps == [1.0, 2.0]
    assert client.stats.requests == 3

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with mock_client(down, FakeClock()) as client, pytest.raises(GitHubError, match="refused"):
        client.get("/x")
    assert client.stats.requests == 5


def test_request_stats_describe() -> None:
    assert RequestStats().describe() == (
        "0 requests (0 answered 304 from the cache), 0 retries, waited 0 s"
    )
    stats = RequestStats(3, 1, 1, 2.4, 60, 57, 1_700_000_000)
    assert stats.describe().endswith(
        "waited 2 s; rate limit 57 of 60 left, resets 2023-11-14 22:13:20 UTC"
    )
    assert RequestStats(limit=60, remaining=1).describe().endswith("1 of 60 left")


def test_malformed_rate_limit_headers_are_ignored(tmp_path: Path) -> None:
    write_fixture(tmp_path, "GET /x", reply(200, [], {"x-ratelimit-remaining": "many"}))
    with client_for(tmp_path) as client:
        client.get("/x")
    assert client.stats.remaining is None


def test_system_clock() -> None:
    clock = SystemClock()
    assert clock.time() > 1_600_000_000
    clock.sleep(0)


# --- the on-disk cache ------------------------------------------------------------------


def test_cache_round_trip_and_bad_entries(tmp_path: Path) -> None:
    cache = ResponseCache(tmp_path)
    entry = CachedResponse("https://h/x", '"e"', None, None, "[]")
    assert cache.get(entry.url) is None
    cache.put(entry)
    assert cache.get(entry.url) == entry
    path = next(tmp_path.glob("*.json"))
    good = json.loads(path.read_text())
    for broken in (
        "not json",
        json.dumps([1]),
        json.dumps({**good, "format": "other"}),
        json.dumps({**good, "version": 99}),
        json.dumps({**good, "url": "https://h/y"}),
        json.dumps({**good, "body": 3}),
        json.dumps({**good, "etag": 3}),
    ):
        path.write_text(broken)
        assert cache.get(entry.url) is None
    assert [p.name for p in tmp_path.iterdir()] == [path.name]


def test_a_failed_cache_write_leaves_no_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*_: object, **__: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(github.json, "dump", fail)
    with pytest.raises(OSError, match="disk full"):
        ResponseCache(tmp_path).put(CachedResponse("u", None, None, None, "[]"))
    assert list(tmp_path.iterdir()) == []


def test_default_cache_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert default_cache_dir() == tmp_path / "commitminer" / "github"
    monkeypatch.delenv("XDG_CACHE_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert default_cache_dir() == tmp_path / "home" / ".cache" / "commitminer" / "github"
