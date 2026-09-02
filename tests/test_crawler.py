import sqlite3

import pytest

from bsetl.ingest.budget import CrawlStats, Outcome, RunBudget
from bsetl.ingest.crawler import (
    BUDGET_SKIP,
    fetch_json_async,
    group_ranked_matches,
    insert_rows_matches_in_chunks,
)
from bsetl.transform.schema import get_matches_column_defs


def a_row(battle_time="20251102T002849.000Z", star_tag="#ABC"):
    """One matches row. id is None, as the crawler writes it — SQLite assigns
    the rowid, and identity comes from the (battle_time, map, star_player_tag)
    unique index instead."""
    n = len(get_matches_column_defs())
    return tuple(
        [None, battle_time, "brawlBall", "Hot Potato", "T1-T1", "RICO", 11, star_tag, 18, 17.5]
        + [None] * (n - 10)
    )


def test_insert_reports_rows_actually_inserted(tmp_path):
    db = str(tmp_path / "s.db")
    assert insert_rows_matches_in_chunks(db, [a_row("2025A", "#A"), a_row("2025B", "#B")]) == 2


def test_reinserting_the_same_rows_reports_zero(tmp_path):
    """Yield is measured on inserts, not attempts. INSERT OR IGNORE makes the
    two diverge sharply once the database is warm, and only the former says
    whether the requests were worth making."""
    db = str(tmp_path / "s.db")
    rows = [a_row("2025A", "#A"), a_row("2025B", "#B")]
    assert insert_rows_matches_in_chunks(db, rows) == 2
    assert insert_rows_matches_in_chunks(db, rows) == 0
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0] == 2
    conn.close()


def test_insert_of_nothing_is_zero(tmp_path):
    assert insert_rows_matches_in_chunks(str(tmp_path / "s.db"), []) == 0


def test_partial_overlap_counts_only_the_new_rows(tmp_path):
    db = str(tmp_path / "s.db")
    insert_rows_matches_in_chunks(db, [a_row("2025A", "#A")])
    assert insert_rows_matches_in_chunks(db, [a_row("2025A", "#A"), a_row("2025C", "#C")]) == 1


@pytest.mark.asyncio
async def test_spent_budget_skips_without_making_a_request():
    """The sentinel must be distinguishable from an empty response: a skipped
    tag goes back on the frontier, an empty one must not."""
    stats = CrawlStats(budget=RunBudget(max_requests=1))
    stats.record_request(Outcome.OK)  # budget now spent

    # session and semaphore are None: reaching them would raise, proving the
    # short-circuit happens before any network work.
    result = await fetch_json_async(
        "https://example.invalid", {}, None, None, stats=stats
    )
    assert result is BUDGET_SKIP
    assert result is not None
    assert stats.requests_made == 1  # unchanged


def test_malformed_battles_are_counted_not_silently_dropped():
    stats = CrawlStats()
    log = {"items": [{"battle": {"type": "soloRanked"}}, {"no_battle_key": True}]}
    group_ranked_matches(log, stats)
    assert stats.parse_failures == 1


def test_parse_failures_are_optional():
    assert group_ranked_matches({"items": [{"broken": True}]}) == []
