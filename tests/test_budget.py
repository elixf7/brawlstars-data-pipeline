import time

import pytest

from bsetl.ingest.budget import CrawlStats, Outcome, RunBudget, StopReason


def spend(stats, requests, rows=0):
    for _ in range(requests):
        stats.record_request(Outcome.OK)
    if rows:
        stats.record_rows(rows)


def test_unbounded_budget_never_stops():
    stats = CrawlStats()
    spend(stats, 10_000)
    assert stats.should_stop() is None


def test_request_budget_stops_at_the_limit():
    stats = CrawlStats(budget=RunBudget(max_requests=50))
    spend(stats, 49)
    assert stats.should_stop() is None
    stats.record_request(Outcome.OK)
    assert stats.should_stop() is StopReason.REQUEST_BUDGET


def test_time_budget_stops():
    stats = CrawlStats(budget=RunBudget(max_seconds=0.05))
    assert stats.should_stop() is None
    time.sleep(0.06)
    assert stats.should_stop() is StopReason.TIME_BUDGET


def test_yield_is_unknown_until_a_full_window_is_observed():
    stats = CrawlStats(budget=RunBudget(yield_window_requests=1_000))
    spend(stats, 500, rows=10)
    assert stats.recent_yield_per_1k() is None


def test_yield_collapse_stops_the_run():
    budget = RunBudget(
        min_rows_per_1k_requests=50,
        yield_grace_requests=1_000,
        yield_window_requests=1_000,
    )
    stats = CrawlStats(budget=budget)
    # Productive opening: 500 rows per 1000 requests.
    spend(stats, 1_000, rows=500)
    assert stats.should_stop() is None
    # Then the neighbourhood saturates: 2 rows over the next 1000 requests.
    spend(stats, 1_000, rows=2)
    assert stats.should_stop() is StopReason.YIELD_COLLAPSED


def test_healthy_yield_does_not_stop():
    budget = RunBudget(
        min_rows_per_1k_requests=50,
        yield_grace_requests=1_000,
        yield_window_requests=1_000,
    )
    stats = CrawlStats(budget=budget)
    for _ in range(4):
        spend(stats, 1_000, rows=300)
    assert stats.should_stop() is None


def test_grace_period_protects_a_slow_start():
    """A crawl writes nothing until its first flush; that must not read as collapse."""
    budget = RunBudget(
        min_rows_per_1k_requests=100,
        yield_grace_requests=5_000,
        yield_window_requests=1_000,
    )
    stats = CrawlStats(budget=budget)
    spend(stats, 2_000, rows=0)
    assert stats.should_stop() is None
    spend(stats, 3_100, rows=0)
    assert stats.should_stop() is StopReason.YIELD_COLLAPSED


def test_outcomes_and_summary_are_recorded():
    stats = CrawlStats()
    stats.record_request(Outcome.OK)
    stats.record_request(Outcome.NOT_FOUND)
    stats.record_request(Outcome.RATE_LIMITED)
    stats.record_request(Outcome.ERROR)
    stats.record_parse_failure(3)
    stats.record_rows(7)

    s = stats.summary()
    assert s["requests_made"] == 4
    assert s["tags_fetched"] == 1  # only OK counts as a fetched tag
    assert s["rows_inserted"] == 7
    assert s["parse_failures"] == 3
    assert s["http_ok"] == 1 and s["http_not_found"] == 1
    assert s["http_rate_limited"] == 1 and s["http_error"] == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_requests": 0},
        {"max_seconds": -1},
        {"yield_window_requests": 0},
        {"min_rows_per_1k_requests": -5},
    ],
)
def test_nonsense_budgets_are_rejected(kwargs):
    with pytest.raises(ValueError):
        RunBudget(**kwargs)
