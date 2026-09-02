"""Season boundaries are arithmetic, so these assertions are exact.

The dates below were checked against Supercell's Ranked 2.0 schedule and
against two real databases whose daily mean elo steps down on exactly the
predicted day."""
from datetime import date

import pytest

from bsetl.transform.seasons import (
    ANCHOR_NUMBER,
    ANCHOR_START,
    current_season,
    days_until_next_season,
    season_bounds,
    season_for_battle_time,
    season_for_database,
    season_label,
    season_number_at,
    season_start,
    season_start_iso,
    seasons_spanned,
    third_thursday,
)


@pytest.mark.parametrize("y,m,expected", [
    (2025, 10, date(2025, 10, 16)),
    (2025, 11, date(2025, 11, 20)),
    (2025, 12, date(2025, 12, 18)),
    (2026, 1, date(2026, 1, 15)),
    (2026, 4, date(2026, 4, 16)),
    (2026, 10, date(2026, 10, 15)),
    # A month beginning on a Thursday: the 1st counts, so the third is the 15th.
    (2026, 1, date(2026, 1, 15)),
])
def test_third_thursday(y, m, expected):
    assert third_thursday(y, m) == expected
    assert expected.weekday() == 3


def test_anchor_is_self_consistent():
    assert season_start(ANCHOR_NUMBER) == ANCHOR_START
    assert ANCHOR_START == third_thursday(ANCHOR_START.year, ANCHOR_START.month)


def test_seasons_are_consecutive_months():
    assert season_start(49) == date(2026, 4, 16)
    assert season_start(48) == date(2026, 3, 19)
    assert season_start(43) == date(2025, 10, 16)
    assert season_start(42) == date(2025, 9, 18)


def test_bounds_are_half_open_and_contiguous():
    start, end = season_bounds(43)
    assert (start, end) == (date(2025, 10, 16), date(2025, 11, 20))
    assert end == season_start(44)


@pytest.mark.parametrize("day,expected", [
    (date(2025, 10, 15), 42),   # day before the reset
    (date(2025, 10, 16), 43),   # reset day belongs to the new season
    (date(2025, 11, 19), 43),
    (date(2025, 11, 20), 44),
    (date(2026, 4, 16), 49),
])
def test_which_season_a_day_falls_in(day, expected):
    assert season_number_at(day) == expected


def test_early_month_still_belongs_to_the_previous_season():
    """Rollover is mid-month, so 1 November is still October's season."""
    assert season_number_at(date(2025, 11, 3)) == 43


def test_battle_time_maps_to_a_season():
    assert season_for_battle_time("20251102T002849.000Z") == "season43"
    assert season_for_battle_time("20251015T235959.000Z") == "season42"
    assert season_for_battle_time("garbage") is None
    assert season_for_battle_time("") is None


def test_season_start_iso_bounds_ingestion():
    """Passed to --latest-runtime so post-rollover crawls do not pull pre-reset
    matches into the new season's database."""
    assert season_start_iso(43) == "2025-10-16T00:00:00Z"


def test_current_season_is_consistent_with_its_own_bounds():
    label = current_season()
    n = int(label.removeprefix("season"))
    start, end = season_bounds(n)
    today = date.today()
    assert start <= today < end
    assert 0 < days_until_next_season() <= 35
    assert season_label(n) == label


# ------------------------------------------------------------------ databases
def test_database_season_is_read_from_data_not_the_filename(tmp_path):
    import sqlite3

    from bsetl.transform.schema import (
        create_matches_table_if_not_exists,
        get_matches_column_defs,
        get_matches_insert_statement,
    )
    n = len(get_matches_column_defs())
    db = str(tmp_path / "misleadingly_named_season99.db")
    conn = sqlite3.connect(db)
    create_matches_table_if_not_exists(conn)
    row = [None, "20251102T002849.000Z", "brawlBall", "M", "T1-T1",
           "RICO", 11, "#A", 18, 17.5] + [None] * (n - 10)
    conn.execute(get_matches_insert_statement(), tuple(row))
    conn.commit()
    conn.close()

    assert season_for_database(db) == "season43"
    assert seasons_spanned(db) == ["season43"]


def test_a_database_spanning_a_reset_is_visible(tmp_path):
    import sqlite3

    from bsetl.transform.schema import (
        create_matches_table_if_not_exists,
        get_matches_column_defs,
        get_matches_insert_statement,
    )
    n = len(get_matches_column_defs())
    db = str(tmp_path / "mixed.db")
    conn = sqlite3.connect(db)
    create_matches_table_if_not_exists(conn)
    for i, ts in enumerate(["20251015T120000.000Z", "20251020T120000.000Z"]):
        row = [None, ts, "brawlBall", "M", "T1-T1", "RICO", 11, f"#A{i}", 18, 17.5]
        row += [None] * (n - 10)
        conn.execute(get_matches_insert_statement(), tuple(row))
    conn.commit()
    conn.close()

    assert seasons_spanned(db) == ["season42", "season43"]


def test_missing_database_is_handled(tmp_path):
    assert season_for_database(str(tmp_path / "nope.db")) is None
