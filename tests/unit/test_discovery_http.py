"""Polite HTTP client: rate limiting, retries, Retry-After, permanent errors, no cookies."""

from __future__ import annotations

import httpx
import pytest
import respx

from job_agent.config import SourcesSection
from job_agent.discovery.http import PoliteHttpClient, build_user_agent, parse_retry_after
from job_agent.errors import HumanActionRequired, PermanentError, RecoverableError

URL = "https://boards-api.greenhouse.io/v1/boards/example"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(round(seconds, 3))
        self.now += seconds


def make_client(clock: FakeClock, **settings) -> PoliteHttpClient:
    cfg = SourcesSection(**{"min_request_interval_seconds": 1.0, "max_retries": 3, **settings})
    return PoliteHttpClient(cfg, sleep=clock.sleep, clock=clock, contact_email=None)


@respx.mock
def test_429_with_retry_after_then_success():
    clock = FakeClock()
    route = respx.get(URL).mock(side_effect=[
        httpx.Response(429, headers={"Retry-After": "2"}),
        httpx.Response(200, json={"name": "Example"}),
    ])
    with make_client(clock) as client:
        assert client.get_json(URL) == {"name": "Example"}
    assert route.call_count == 2
    assert 2.0 in clock.sleeps


@respx.mock
def test_retry_after_longer_than_cap_stops_instead_of_hammering():
    clock = FakeClock()
    route = respx.get(URL).mock(return_value=httpx.Response(429, headers={"Retry-After": "3600"}))
    with make_client(clock) as client, pytest.raises(RecoverableError, match="retry after 3600s"):
        client.get_json(URL)
    assert route.call_count == 1 and clock.sleeps == []


@respx.mock
def test_5xx_retries_with_exponential_backoff_then_recoverable_error():
    clock = FakeClock()
    route = respx.get(URL).mock(return_value=httpx.Response(503))
    with make_client(clock) as client, pytest.raises(RecoverableError, match="HTTP 503 after 4 attempt"):
        client.get_json(URL)
    assert route.call_count == 4
    # backoff 1, 2, 4 (the per-host interval is already satisfied by the backoff waits)
    assert clock.sleeps == [1.0, 2.0, 4.0]


@respx.mock
def test_timeout_is_retried():
    clock = FakeClock()
    route = respx.get(URL).mock(side_effect=[httpx.ReadTimeout("slow"), httpx.Response(200, json=[])])
    with make_client(clock) as client:
        assert client.get_json(URL) == []
    assert route.call_count == 2


@respx.mock
@pytest.mark.parametrize("status", [401, 403])
def test_access_denied_is_permanent_and_not_retried(status):
    clock = FakeClock()
    route = respx.get(URL).mock(return_value=httpx.Response(status, text="denied"))
    with make_client(clock) as client, pytest.raises(PermanentError, match="access denied.*not bypassing"):
        client.get_json(URL)
    assert route.call_count == 1


@respx.mock
def test_404_is_board_not_found():
    clock = FakeClock()
    respx.get(URL).mock(return_value=httpx.Response(404, json={"status": 404}))
    with make_client(clock) as client, pytest.raises(PermanentError, match="board not found") as info:
        client.get_json(URL)
    assert info.value.human_action and "board token" in info.value.human_action


@respx.mock
def test_challenge_page_requires_human_and_is_not_bypassed():
    clock = FakeClock()
    respx.get(URL).mock(return_value=httpx.Response(
        200, headers={"content-type": "text/html"}, text="<html><title>Attention Required</title>captcha</html>"))
    with make_client(clock) as client, pytest.raises(HumanActionRequired, match="not bypassing"):
        client.get_json(URL)


@respx.mock
def test_invalid_json_is_recoverable():
    clock = FakeClock()
    respx.get(URL).mock(return_value=httpx.Response(200, headers={"content-type": "application/json"},
                                                    text="{truncated"))
    with make_client(clock) as client, pytest.raises(RecoverableError, match="not valid JSON"):
        client.get_json(URL)


@respx.mock
def test_min_interval_per_host_user_agent_and_no_cookies():
    clock = FakeClock()
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={}, headers={"Set-Cookie": "session=abc; Path=/"})

    respx.get(URL).mock(side_effect=record)
    other = "https://api.lever.co/v0/postings/acme"
    respx.get(other).mock(side_effect=record)
    cfg = SourcesSection(min_request_interval_seconds=1.5, user_agent="job-agent/0.1 (test)")
    client = PoliteHttpClient(cfg, sleep=clock.sleep, clock=clock, contact_email="me@example.com")
    client.get_json(URL)
    clock.now += 0.5
    client.get_json(URL)  # same host 0.5s later -> waits the remaining 1.0s
    client.get_json(other)  # different host -> no wait
    client.close()
    assert clock.sleeps == [1.0]
    assert all("cookie" not in {k.lower() for k in r.headers} for r in seen)
    assert seen[0].headers["User-Agent"] == "job-agent/0.1 (test) (contact: me@example.com)"
    assert seen[0].headers["Accept"] == "application/json"


def test_refuses_plain_http():
    with pytest.raises(PermanentError, match="non-HTTPS"):
        make_client(FakeClock()).get("http://boards-api.greenhouse.io/v1/boards/x")


def test_helpers():
    assert build_user_agent("ua", None) == "ua"
    assert build_user_agent("ua", " a@b.c ") == "ua (contact: a@b.c)"
    assert parse_retry_after("7") == 7.0
    assert parse_retry_after("garbage") is None
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0  # in the past -> no wait
