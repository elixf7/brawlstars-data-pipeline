import sqlite3

import pytest

from bsetl.quality import Severity, Thresholds, run_quality_checks
from bsetl.quality.checks import record_is_well_formed
from bsetl.transform.schema import (
    create_matches_table_if_not_exists,
    get_matches_column_defs,
    get_matches_insert_statement,
)

COLS = [n for n, _ in get_matches_column_defs()]
N = len(COLS)


def a_row(battle_time="20251102T002849.000Z", tag="#A", record="T1-T1",
          avg_elo=17.5, mode="brawlBall", brawler="RICO"):
    row = [None, battle_time, mode, "Hot Potato", record, brawler, 11, tag, 18, avg_elo]
    row += [None] * (N - 10)
    for slot, i in enumerate(range(10, 10 + 30, 5)):
        row[i] = f"{brawler}{slot}"      # distinct names per slot
    return tuple(row)


def build(path, rows, *, with_index=True):
    conn = sqlite3.connect(path)
    create_matches_table_if_not_exists(conn)
    if not with_index:
        conn.execute("DROP INDEX IF EXISTS uniq_matches_key")
    conn.executemany(get_matches_insert_statement(), rows)
    conn.commit()
    conn.close()
    return path


def many(n, **kw):
    return [a_row(battle_time=f"202511{1 + i % 9:02d}T00{i % 10}0000.000Z",
                  tag=f"#T{i}", **kw) for i in range(n)]


def result(report, name):
    return next(r for r in report.results if r.name == name)


LOOSE = Thresholds(min_rows=1, min_distinct_brawlers=1)


# ----------------------------------------------------- the set-shape rule
@pytest.mark.parametrize("record", [
    "T1-T1", "T2-T2", "T2-T1-T1", "T1-T2-T1",
    "D-T1-T1", "T1-D-T1", "D-D-T2-T2",   # draws do not count toward the two
    "T1", "T2", "D", "T1-D", "D-D-D",    # partial: cut off by the 25-battle window
])
def test_records_a_real_set_could_produce(record):
    assert record_is_well_formed(record)


@pytest.mark.parametrize("record", [
    "T1-T1-T1",        # three wins; the set ended at two
    "T1-T1-T2",        # a game after the clinching win
    "T2-T1-T2-T2",     # merged sets
    "T1-T2-T1-T1",
    "T1-X",            # not a token
    "",
])
def test_records_no_real_set_could_produce(record):
    assert not record_is_well_formed(record)


# ------------------------------------------------------------- structural
def test_missing_index_fails_because_recrawling_would_duplicate(tmp_path):
    db = build(str(tmp_path / "s.db"), many(5), with_index=False)
    r = run_quality_checks(db, thresholds=LOOSE)
    assert result(r, "dedup_index").severity is Severity.FAIL
    assert not r.ok


def test_index_present_passes(tmp_path):
    db = build(str(tmp_path / "s.db"), many(5))
    assert result(run_quality_checks(db, thresholds=LOOSE), "dedup_index").severity is Severity.OK


def test_absent_database_fails_without_raising(tmp_path):
    r = run_quality_checks(str(tmp_path / "nope.db"))
    assert not r.ok
    assert "No such database" in r.results[0].message


def test_table_without_matches_short_circuits(tmp_path):
    p = str(tmp_path / "empty.db")
    sqlite3.connect(p).close()
    r = run_quality_checks(p)
    assert not r.ok
    assert len(r.results) == 1          # stops rather than cascading errors


# ------------------------------------------------------------------ volume
def test_thin_crawl_fails_the_row_floor(tmp_path):
    db = build(str(tmp_path / "s.db"), many(3))
    r = run_quality_checks(db, thresholds=Thresholds(min_rows=1_000, min_distinct_brawlers=1))
    assert result(r, "row_count").severity is Severity.FAIL


# ----------------------------------------------------------------- content
def test_impossible_elo_fails(tmp_path):
    db = build(str(tmp_path / "s.db"), many(4) + [a_row(tag="#BOT", avg_elo=40.0)])
    r = run_quality_checks(db, thresholds=LOOSE)
    assert result(r, "elo_range").severity is Severity.FAIL


def test_a_few_merged_sets_warn_but_do_not_block(tmp_path):
    """0.01% of season42 has this; it should be visible, not fatal."""
    rows = many(999) + [a_row(tag="#BAD", record="T1-T1-T1")]
    db = build(str(tmp_path / "s.db"), rows)
    r = run_quality_checks(db, thresholds=Thresholds(min_rows=1, min_distinct_brawlers=1))
    assert result(r, "record_format").severity is Severity.WARN
    assert r.ok                                  # warnings do not block


def test_widespread_merged_sets_fail(tmp_path):
    rows = many(50) + [a_row(tag=f"#B{i}", record="T1-T1-T1") for i in range(50)]
    db = build(str(tmp_path / "s.db"), rows)
    r = run_quality_checks(db, thresholds=LOOSE)
    assert result(r, "record_format").severity is Severity.FAIL


def test_an_unrecognised_mode_only_warns(tmp_path):
    """A rotation change is news, not a defect."""
    db = build(str(tmp_path / "s.db"), many(5) + [a_row(tag="#N", mode="newMode2027")])
    r = run_quality_checks(db, thresholds=LOOSE)
    assert result(r, "modes").severity is Severity.WARN
    assert r.ok


def test_truncated_brawler_pool_fails(tmp_path):
    db = build(str(tmp_path / "s.db"), many(5))
    r = run_quality_checks(db, thresholds=Thresholds(min_rows=1, min_distinct_brawlers=60))
    assert result(r, "brawler_coverage").severity is Severity.FAIL


def test_unparseable_timestamps_fail(tmp_path):
    rows = many(10) + [a_row(battle_time="not-a-timestamp", tag=f"#Z{i}") for i in range(10)]
    db = build(str(tmp_path / "s.db"), rows)
    r = run_quality_checks(db, thresholds=LOOSE)
    assert result(r, "battle_time_format").severity is Severity.FAIL


# ------------------------------------------------------------- provenance
def test_skill_metadata_labelled_with_the_wrong_season_fails(tmp_path):
    """The bug this repository actually shipped: a sidecar stamped season42
    whose bins cover season43's dates."""
    db = build(str(tmp_path / "s.db"), many(5))
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE skill_bin_metadata (season TEXT, bin_start_utc TEXT, bin_end_utc TEXT)")
    conn.execute("INSERT INTO skill_bin_metadata VALUES ('season42','2025-11-11T00:00:00+00:00','2025-11-14T00:00:00+00:00')")
    conn.commit()
    conn.close()
    r = run_quality_checks(db, season="season43", thresholds=LOOSE)
    res = result(r, "skill_provenance")
    assert res.severity is Severity.FAIL
    assert "season42" in res.message and "season43" in res.message


def test_skill_bins_covering_the_wrong_dates_fail(tmp_path):
    db = build(str(tmp_path / "s.db"), many(5))   # data in Nov 2025
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE skill_bin_metadata (season TEXT, bin_start_utc TEXT, bin_end_utc TEXT)")
    conn.execute("INSERT INTO skill_bin_metadata VALUES ('season99','2024-01-01T00:00:00+00:00','2024-01-04T00:00:00+00:00')")
    conn.commit()
    conn.close()
    r = run_quality_checks(db, season="season99", thresholds=LOOSE)
    assert result(r, "skill_provenance").severity is Severity.FAIL
    assert "computed elsewhere" in result(r, "skill_provenance").message


def test_no_skill_feature_is_skipped_not_failed(tmp_path):
    db = build(str(tmp_path / "s.db"), many(5))
    r = run_quality_checks(db, thresholds=LOOSE)
    assert result(r, "skill_coverage").severity is Severity.SKIP
    assert result(r, "skill_provenance").severity is Severity.SKIP
    assert r.ok


# ---------------------------------------------------------------- reporting
def test_report_renders_and_serialises(tmp_path):
    db = build(str(tmp_path / "s.db"), many(5))
    r = run_quality_checks(db, season="season50", thresholds=LOOSE)
    text = r.render()
    assert "season50" in text
    assert ("PASS" in text) == r.ok
    d = r.to_dict()
    assert d["ok"] is r.ok
    assert sum(d["counts"].values()) == len(r.results)
    assert all(isinstance(x["severity"], str) for x in d["results"])
