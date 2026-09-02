"""End-to-end coverage against realistically shaped data.

Hand-built rows agree with whatever assumptions wrote them. These run the real
stages over a sample of a real season — every brawler, every mode, genuine elo
and timestamp distributions — which is where disagreements between the code and
the data actually show up.
"""
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pyarrow.dataset as ds
import pytest

from bsetl.publish.card import render_dataset_card
from bsetl.publish.parquet import export_clean_sqlite, export_matches_to_parquet
from bsetl.quality import Severity, run_quality_checks
from bsetl.transform.metadata import compute_season_metadata

FIXTURE = Path(__file__).parent / "fixtures" / "season_sample.db"


@pytest.fixture
def season(tmp_path):
    """A writable copy; several stages modify the database in place."""
    dst = tmp_path / "season.db"
    shutil.copy2(FIXTURE, dst)
    return str(dst)


def test_fixture_is_present_and_populated():
    assert FIXTURE.exists(), "run the fixture builder; see tests/fixtures/README.md"
    conn = sqlite3.connect(f"file:{FIXTURE}?mode=ro", uri=True)
    assert conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0] > 1_000
    conn.close()


# ------------------------------------------------------------------- gate
def test_real_shaped_season_passes_the_quality_gate(season):
    """The gate must not reject data that is merely real."""
    report = run_quality_checks(season, season="season42")
    failures = [f"{r.name}: {r.message}" for r in report.failures]
    assert report.ok, f"gate rejected real data: {failures}"


def test_gate_catches_a_dropped_index_on_real_data(season):
    conn = sqlite3.connect(season)
    conn.execute("DROP INDEX uniq_matches_key")
    conn.commit()
    conn.close()
    report = run_quality_checks(season)
    assert any(r.name == "dedup_index" and r.severity is Severity.FAIL
               for r in report.results)


def test_gate_catches_corrupted_elo_on_real_data(season):
    conn = sqlite3.connect(season)
    conn.execute("UPDATE matches SET avg_elo = 99 WHERE rowid = 1")
    conn.commit()
    conn.close()
    report = run_quality_checks(season)
    assert any(r.name == "elo_range" and r.severity is Severity.FAIL
               for r in report.results)


# ----------------------------------------------------------------- export
def test_export_is_faithful_to_the_source(season, tmp_path):
    out = tmp_path / "parquet"
    result = export_matches_to_parquet(season, str(out))

    conn = sqlite3.connect(f"file:{season}?mode=ro", uri=True)
    n, elo_sum = conn.execute(
        "SELECT COUNT(*), ROUND(SUM(avg_elo), 4) FROM matches"
    ).fetchone()
    src_cols = [r[1] for r in conn.execute("PRAGMA table_info(matches)")]
    conn.close()

    table = ds.dataset(str(out), partitioning="hive").to_table()
    assert result.rows == n == table.num_rows
    assert [c for c in table.schema.names if c != "battle_date"] == src_cols
    assert round(sum(v for v in table["avg_elo"].to_pylist() if v is not None), 4) == elo_sum
    assert all(str(f.type) != "null" for f in table.schema)


def test_export_partitions_cover_every_day(season, tmp_path):
    result = export_matches_to_parquet(season, str(tmp_path / "p"))
    conn = sqlite3.connect(f"file:{season}?mode=ro", uri=True)
    days = {r[0] for r in conn.execute(
        "SELECT DISTINCT substr(battle_time,1,8) FROM matches")}
    conn.close()
    assert len(result.partitions) == len(days)


def test_clean_sqlite_keeps_the_data_and_the_index(season, tmp_path):
    from bsetl.state.frontier import save_frontier

    save_frontier(season, [("#PENDING", 1)])
    out = str(tmp_path / "clean.db")
    export_clean_sqlite(season, out)

    conn = sqlite3.connect(out)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    indexes = {r[1] for r in conn.execute("PRAGMA index_list('matches')")}
    rows = conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
    conn.close()

    assert "crawl_frontier" not in tables
    assert "matches" in tables and rows > 1_000
    assert "uniq_matches_key" in indexes, "the export must stay safe to crawl into"


# --------------------------------------------------------------- metadata
def test_metadata_reflects_the_real_season(season):
    meta = compute_season_metadata(season, season_label="season42")
    assert meta["num_matches"] > 1_000
    assert meta["num_unique_brawlers"] > 80
    assert set(meta["modes"]) <= {
        "brawlBall", "gemGrab", "heist", "bounty", "hotZone", "knockout"}
    assert meta["start_time"] < meta["end_time"]
    assert meta["brawler_usage_top"][0][1] > 0


def test_card_renders_from_real_metadata(season):
    card = render_dataset_card(
        compute_season_metadata(season, season_label="season42"),
        repo_id="me/bs",
    )
    assert card.startswith("---\n")
    assert "unknown" not in card.split("## Limitations")[0]


# ---------------------------------------------------- skill feature rebuild
def test_skill_feature_recomputes_over_real_timestamps(season):
    """Exercises the ECDF path end to end: real timestamps, real elo spread,
    several 3-day bins."""
    conn = sqlite3.connect(season)
    conn.execute("UPDATE matches SET skill_ns = NULL, skill_ns_ok = NULL")
    conn.commit()
    conn.close()

    proc = subprocess.run(
        [sys.executable, "-m", "bsetl.cli.skill_features",
         "--clean-db-path", season, "--bin-width-days", "3",
         "--min-bin-count", "50", "--season", "season42"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]

    conn = sqlite3.connect(season)
    total, scored = conn.execute(
        "SELECT COUNT(*), COUNT(skill_ns) FROM matches"
    ).fetchone()
    mean = conn.execute(
        "SELECT AVG(skill_ns) FROM matches WHERE skill_ns_ok = 1"
    ).fetchone()[0]
    conn.close()

    assert scored > total * 0.5, "most rows should fall in a covered bin"
    # A percentile mapped symmetrically centres near zero.
    assert abs(mean) < 0.2, f"skill_ns mean {mean} is off-centre"
