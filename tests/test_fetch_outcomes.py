"""How a fetch ends must be distinguishable, because the three endings mean
different things: a player with no ranked games is answered, a request that
never happened is not, and a request whose retries all failed is not either.
Collapsing them to None loses frontier tags silently."""
import asyncio
import json

import pytest

from bsetl.ingest.budget import CrawlStats, Outcome, RunBudget
from bsetl.ingest.crawler import BUDGET_SKIP, FETCH_FAILED, fetch_json_async


class FakeResponse:
    def __init__(self, status, body="", headers=None):
        self.status = status
        self._body = body
        self.headers = headers or {}

    async def json(self):
        return json.loads(self._body)

    async def text(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """Replays a scripted sequence of responses, repeating the last."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def get(self, url, headers=None):
        r = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        return r


@pytest.fixture(autouse=True)
def no_sleeping(monkeypatch):
    """The retry path sleeps a second between attempts; not in a test suite."""
    async def instant(_seconds):
        return None
    monkeypatch.setattr(asyncio, "sleep", instant)


async def fetch(session, stats=None, retries=3):
    return await fetch_json_async(
        "https://api.example/players/x", {}, session, asyncio.Semaphore(4),
        retries=retries, stats=stats,
    )


def test_sentinels_are_distinct_from_each_other_and_from_none():
    assert BUDGET_SKIP is not FETCH_FAILED
    assert BUDGET_SKIP is not None and FETCH_FAILED is not None
    assert repr(BUDGET_SKIP) == "BUDGET_SKIP"
    assert repr(FETCH_FAILED) == "FETCH_FAILED"


@pytest.mark.asyncio
async def test_success_returns_the_body_and_counts_a_fetch():
    stats = CrawlStats()
    session = FakeSession([FakeResponse(200, '{"items": []}')])
    assert await fetch(session, stats) == {"items": []}
    assert stats.outcomes == {"ok": 1}
    assert stats.tags_fetched == 1


@pytest.mark.asyncio
async def test_not_found_is_an_answer_not_a_failure():
    """A tag with no accessible profile is settled: it must not be requeued."""
    stats = CrawlStats()
    session = FakeSession([FakeResponse(404, '{"reason": "notFound"}')])
    assert await fetch(session, stats) is None
    assert stats.outcomes == {"not_found": 1}
    assert session.calls == 1  # answered first time, no retries


@pytest.mark.asyncio
async def test_exhausted_retries_report_failure_not_emptiness():
    stats = CrawlStats()
    session = FakeSession([FakeResponse(500, "upstream is unwell")])
    assert await fetch(session, stats) is FETCH_FAILED
    assert session.calls == 3
    assert stats.outcomes == {"error": 3}


@pytest.mark.asyncio
async def test_rate_limit_then_success():
    stats = CrawlStats()
    session = FakeSession([
        FakeResponse(429, "", {"Retry-After": "0.5"}),
        FakeResponse(200, '{"items": [1]}'),
    ])
    assert await fetch(session, stats) == {"items": [1]}
    assert stats.outcomes == {"rate_limited": 1, "ok": 1}


@pytest.mark.asyncio
async def test_a_spent_budget_makes_no_request_at_all():
    stats = CrawlStats(budget=RunBudget(max_requests=1))
    stats.record_request(Outcome.OK)
    session = FakeSession([FakeResponse(200, "{}")])
    assert await fetch(session, stats) is BUDGET_SKIP
    assert session.calls == 0


@pytest.mark.asyncio
async def test_transport_errors_are_counted_as_errors():
    class Exploding:
        def get(self, url, headers=None):
            raise ConnectionResetError("connection reset")

    stats = CrawlStats()
    assert await fetch(Exploding(), stats) is FETCH_FAILED
    assert stats.outcomes == {"error": 3}


# ------------------------------------------------------------------ end to end
class FakeClientSession:
    """Stands in for aiohttp.ClientSession, failing every request."""

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def get(self, url, headers=None):
        return FakeResponse(503, "upstream is unwell")


@pytest.mark.asyncio
async def test_a_run_that_cannot_reach_the_api_keeps_its_frontier(monkeypatch, tmp_path):
    """The failure that would hurt most: an outage silently consuming the
    frontier, so the next run has nothing left to crawl."""
    import datetime as dt

    from bsetl.ingest import crawler
    from bsetl.state import load_frontier, recent_runs

    monkeypatch.setattr(crawler.aiohttp, "ClientSession", FakeClientSession)
    monkeypatch.setattr(crawler.aiohttp, "TCPConnector", lambda **k: None)

    db = str(tmp_path / "s.db")
    stats = await crawler.process_tags_and_write_async(
        player_tags=["#A", "#B", "#C"],
        api_key="x",
        latest_runtime=dt.datetime(2020, 1, 1, tzinfo=dt.UTC),
        clean_db_path=db,
        requests_per_second=10_000,
    )

    assert stats.rows_inserted == 0
    assert stats.outcomes.get("error", 0) > 0
    # Every seed is unanswered, so every seed survives for the next run.
    assert sorted(t for t, _ in load_frontier(db)) == ["#A", "#B", "#C"]
    assert recent_runs(db)[0]["frontier_after"] == 3
