import io
from email.message import Message
from unittest.mock import Mock
from urllib.error import HTTPError

import pytest

from dc_importer.http import BudgetReached, PoliteHTTP, SourceUnavailable
from dc_importer.mtgtop8 import INDEX


@pytest.fixture
def clock(monkeypatch):
    class Clock:
        elapsed = 1000.0

        def now(self):
            return self.elapsed

        def sleep(self, seconds):
            self.elapsed += seconds

    value = Clock()
    monkeypatch.setattr("dc_importer.http.time.time", value.now)
    monkeypatch.setattr("dc_importer.http.time.monotonic", value.now)
    monkeypatch.setattr("dc_importer.http.time.sleep", value.sleep)
    return value


def response(body, charset=None):
    result = io.BytesIO(body)
    result.headers = Message()
    if charset:
        result.headers["Content-Type"] = f"text/html; charset={charset}"
    result.url = INDEX
    return result


def test_host_spacing_survives_new_worker_and_limits_requests(clock):
    state = {}
    first = PoliteHTTP(state)
    first.gate(INDEX)
    second = PoliteHTTP(state, max_requests=1)
    second.gate("https://www.mtgtop8.com/event?e=1&f=EDH")
    assert clock.elapsed == 1005
    with pytest.raises(BudgetReached):
        second.gate(INDEX)
    assert second.requests == 1


def test_runtime_budget_does_not_wait_past_deadline(clock):
    http = PoliteHTTP({}, max_seconds=4)
    http.gate(INDEX)
    with pytest.raises(BudgetReached):
        http.gate(INDEX)
    assert clock.elapsed == 1000


def test_robots_delay_and_denial(clock):
    http = PoliteHTTP({})
    http.opener = Mock()
    http.opener.open.return_value = response(b"User-agent: *\nDisallow: /event\nCrawl-delay: 8\n")
    http.load_robots()
    http.gate(INDEX)
    assert clock.elapsed == 1008
    with pytest.raises(SourceUnavailable, match="robots"):
        http.gate("https://mtgtop8.com/event?e=1&f=EDH")


def test_429_persists_retry_after_and_does_not_retry(clock):
    state = {}
    http = PoliteHTTP(state)
    http.opener = Mock()
    http.opener.open.side_effect = HTTPError(INDEX, 429, "limited", {"Retry-After": "120"}, None)
    with pytest.raises(SourceUnavailable, match="429"):
        http.page(INDEX)
    assert http.opener.open.call_count == 1
    assert state["cooldown"]["mtgtop8.com"] == 1120
    with pytest.raises(SourceUnavailable, match="cooling down"):
        PoliteHTTP(state).page(INDEX)


def test_latin1_fallback_and_access_challenge(clock):
    http = PoliteHTTP({})
    http.opener = Mock()
    http.opener.open.side_effect = [
        response("Lórien Revealed".encode("iso-8859-1")),
        response(b"<title>Verify you are human</title>"),
    ]
    assert http.page(INDEX).text == "Lórien Revealed"
    with pytest.raises(SourceUnavailable, match="challenge"):
        http.page(INDEX)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/event",
        "http://mtgtop8.com/event",
        "https://mtgtop8.com:1234/event",
        "https://mtgtop8.com/compare",
    ],
)
def test_unexpected_destinations_are_rejected(url, clock):
    with pytest.raises(SourceUnavailable):
        PoliteHTTP({}).gate(url)
