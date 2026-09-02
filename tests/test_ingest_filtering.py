"""Impossible records are dropped at ingest, and the rate is watched.

Dropping is right — a merged set is not a match that happened. But dropping
silently would hide a systemic break, so the count travels into the run record
and the gate reads it back."""
import json
import sqlite3

import pytest

from bsetl.ingest import crawler
from bsetl.ingest.budget import CrawlStats
from bsetl.quality import Severity, Thresholds, run_quality_checks
from bsetl.transform.records import record_is_well_formed
from bsetl.transform.schema import (
    create_matches_table_if_not_exists,
    get_matches_column_defs,
)

N = len(get_matches_column_defs())


def row_with(record):
    r = [None] * N
    r[1], r[4] = "20251102T002849.000Z", record
    return tuple(r)


def build_rows(records, monkeypatch):
    """Run the row builder over one fabricated set per record value."""
    monkeypatch.setattr(crawler, "group_ranked_matches", lambda log, stats=None: log)
    monkeypatch.setattr(
        crawler, "build_clean_row",
        lambda g, *a, **k: row_with(g),
    )
    stats = CrawlStats()
    rows = crawler._build_clean_rows_from_logs(
        {"#A": list(records)}, None, {}, False, None, None, stats
    )
    return rows, stats


def test_impossible_records_are_dropped(monkeypatch):
    rows, stats = build_rows(["T1-T1", "T1-T1-T1", "T2-T1-T1"], monkeypatch)
    assert [r[4] for r in rows] == ["T1-T1", "T2-T1-T1"]
    assert stats.malformed_records == 1


def test_partial_sets_are_kept(monkeypatch):
    """A bare T1 is a set cut off by the battle-log window — about a quarter of
    all rows. Dropping those would discard legitimate data wholesale."""
    rows, stats = build_rows(["T1", "T2", "D", "T1-D"], monkeypatch)
    assert len(rows) == 4
    assert stats.malformed_records == 0


def test_draw_extended_sets_are_kept(monkeypatch):
    rows, stats = build_rows(["D-T1-T1", "T1-D-T1", "D-D-T2-T2"], monkeypatch)
    assert len(rows) == 3
    assert stats.malformed_records == 0


def test_drop_count_reaches_the_run_summary():
    stats = CrawlStats()
    stats.record_malformed(4)
    assert stats.summary()["malformed_records"] == 4


# ------------------------------------------------------- the gate reads it back
def seed_run(db, *, inserted, dropped, status="ok"):
    conn = sqlite3.connect(db)
    create_matches_table_if_not_exists(conn)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS pipeline_runs (
            run_id TEXT PRIMARY KEY, started_utc TEXT, status TEXT,
            rows_inserted INTEGER, stats_json TEXT)"""
    )
    conn.execute(
        "INSERT INTO pipeline_runs VALUES ('r1', '2026-09-01T00:00:00Z', ?, ?, ?)",
        (status, inserted, json.dumps({"malformed_records": dropped})),
    )
    conn.commit()
    conn.close()
    return db


LOOSE = Thresholds(min_rows=0, min_distinct_brawlers=0)


def named(report, name):
    return next(r for r in report.results if r.name == name)


def test_a_trickle_of_drops_is_fine(tmp_path):
    db = seed_run(str(tmp_path / "s.db"), inserted=100_000, dropped=12)
    assert named(run_quality_checks(db, thresholds=LOOSE), "ingest_health").severity is Severity.OK


def test_dropping_a_large_share_fails(tmp_path):
    """The case the user cares about: never quietly discard 5% of a crawl."""
    db = seed_run(str(tmp_path / "s.db"), inserted=95, dropped=5)
    res = named(run_quality_checks(db, thresholds=LOOSE), "ingest_health")
    assert res.severity is Severity.FAIL
    assert "5.00%" in res.message


def test_no_run_history_is_skipped(tmp_path):
    p = str(tmp_path / "s.db")
    conn = sqlite3.connect(p)
    create_matches_table_if_not_exists(conn)
    conn.close()
    assert named(run_quality_checks(p, thresholds=LOOSE), "ingest_health").severity is Severity.SKIP


@pytest.mark.parametrize("record,ok", [
    ("T1-T1", True), ("D-T1-T1", True), ("T1", True),
    ("T1-T1-T1", False), ("T1-T1-T2", False), ("", False), (None, False),
])
def test_rule_is_shared_between_ingest_and_gate(record, ok):
    assert record_is_well_formed(record) is ok
