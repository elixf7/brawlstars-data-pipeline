import sqlite3

import pytest

from bsetl.state.frontier import frontier_size, load_frontier, save_frontier
from bsetl.state.runs import finish_run, recent_runs, start_run


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "season.db")


# ---------------------------------------------------------------- frontier
def test_frontier_absent_reads_as_empty(db):
    assert load_frontier(db) == []
    assert frontier_size(db) == 0


def test_frontier_round_trips(db):
    save_frontier(db, [("#A", 0), ("#B", 1)])
    assert sorted(load_frontier(db)) == [("#A", 0), ("#B", 1)]
    assert frontier_size(db) == 2


def test_frontier_returns_shallowest_first():
    """Resuming must continue the breadth-first sweep, not dive deepest-first."""
    import os
    import tempfile
    db = os.path.join(tempfile.mkdtemp(), "s.db")
    save_frontier(db, [("#deep", 3), ("#shallow", 0), ("#mid", 1)])
    assert [d for _, d in load_frontier(db)] == [0, 1, 3]


def test_saving_replaces_rather_than_appends(db):
    save_frontier(db, [("#A", 0), ("#B", 0)])
    save_frontier(db, [("#C", 1)])
    assert load_frontier(db) == [("#C", 1)]


def test_empty_save_clears_the_frontier(db):
    save_frontier(db, [("#A", 0)])
    save_frontier(db, [])
    assert load_frontier(db) == []


def test_frontier_coexists_with_matches_table(db):
    from bsetl.transform.schema import create_matches_table_if_not_exists

    conn = sqlite3.connect(db)
    create_matches_table_if_not_exists(conn)
    conn.close()
    save_frontier(db, [("#A", 0)])
    assert load_frontier(db) == [("#A", 0)]


# -------------------------------------------------------------------- runs
def test_run_is_visible_while_still_running(db):
    """A crashed process must leave evidence, not nothing."""
    start_run(db, {"max_depth": 2}, frontier_before=5)
    (row,) = recent_runs(db)
    assert row["status"] == "running"
    assert row["finished_utc"] is None
    assert row["frontier_before"] == 5


def test_finishing_a_run_records_the_outcome(db):
    run_id = start_run(db, {"max_depth": 2})
    finish_run(
        db, run_id,
        status="ok",
        stop_reason="request_budget",
        stats={"requests_made": 120, "rows_inserted": 30,
               "tags_fetched": 100, "parse_failures": 2, "elapsed_seconds": 4.2},
        frontier_after=17,
    )
    (row,) = recent_runs(db)
    assert row["status"] == "ok"
    assert row["stop_reason"] == "request_budget"
    assert row["requests_made"] == 120
    assert row["rows_inserted"] == 30
    assert row["frontier_after"] == 17
    assert row["finished_utc"] is not None


def test_recent_runs_is_newest_first_and_limited(db):
    for _ in range(5):
        finish_run(db, start_run(db, {}), status="ok", stop_reason=None,
                   stats={}, frontier_after=0)
    assert len(recent_runs(db, limit=3)) == 3


def test_recent_runs_on_a_fresh_db_is_empty(db):
    sqlite3.connect(db).close()
    assert recent_runs(db) == []
