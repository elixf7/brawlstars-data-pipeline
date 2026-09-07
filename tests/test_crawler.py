import sqlite3

import pytest

from bsetl.ingest.budget import CrawlStats, Outcome, RunBudget
from bsetl.ingest.crawler import (
    BUDGET_SKIP,
    FETCH_FAILED,
    fetch_json_async,
    get_brawler_data,
    group_ranked_matches,
    insert_rows_matches_in_chunks,
    process_team,
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


# ------------------------------------------------- slots when a profile is missing
@pytest.mark.parametrize("cached", [None, BUDGET_SKIP, FETCH_FAILED])
def test_a_slot_is_still_recorded_when_the_profile_never_arrived(cached):
    """The sentinels are truthy objects, not dicts. Everything the row needs
    comes from the battle log anyway, so a missing profile costs nothing but
    the power-level refinement."""
    b = get_brawler_data(cached, "RICO", 14, 11, "#A1")
    assert b == {"name": "RICO", "elo": 14, "power": 11, "tag": "#A1"}


def test_the_profile_refines_power_when_it_is_there():
    profile = {"brawlers": [{"name": "RICO", "power": 9}]}
    assert get_brawler_data(profile, "RICO", 14, 11, "#A1")["power"] == 9


def test_every_slot_carries_the_tag_of_whoever_brought_it():
    team = [("RICO", 11, "#A1", 14), ("COLT", 11, "#A2", 15), ("BULL", 10, "#A3", 13)]
    brawlers, _ = process_team(team, {"#A2": FETCH_FAILED})
    assert [b["tag"] for b in brawlers] == ["#A1", "#A2", "#A3"]
    assert [b["name"] for b in brawlers] == ["RICO", "COLT", "BULL"]
