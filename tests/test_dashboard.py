"""The status page is rendered from the database, so it cannot claim anything
the data does not support."""
import json
import sqlite3
from pathlib import Path

import pytest

from bsetl.publish.dashboard import collect, render_dashboard, write_status_json
from bsetl.transform.schema import (
    create_matches_table_if_not_exists,
    get_matches_column_defs,
    get_matches_insert_statement,
)

FIXTURE = Path(__file__).parent / "fixtures" / "season_sample.db"
N = len(get_matches_column_defs())


def test_page_is_self_contained(tmp_path):
    """Served from Pages with no network: no external requests may appear."""
    page = render_dashboard(str(FIXTURE))
    for forbidden in ("http://", "src=", "<script", "cdn."):
        assert forbidden not in page, f"page references {forbidden}"
    # The one allowed link is the source repository.
    assert page.count("https://") == 1


def test_page_reports_the_real_numbers():
    page = render_dashboard(str(FIXTURE))
    conn = sqlite3.connect(f"file:{FIXTURE}?mode=ro", uri=True)
    n = conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
    conn.close()
    assert f"{n:,}" in page
    assert "season43" in page          # read from data, not the filename
    assert "passing" in page


def test_page_shows_a_failing_gate_as_failing(tmp_path):
    db = tmp_path / "broken.db"
    conn = sqlite3.connect(db)
    create_matches_table_if_not_exists(conn)
    conn.execute("DROP INDEX uniq_matches_key")
    row = [None, "20251102T002849.000Z", "brawlBall", "M", "T1-T1",
           "RICO", 11, "#A", 18, 17.5] + [None] * (N - 10)
    conn.execute(get_matches_insert_statement(), tuple(row))
    conn.commit()
    conn.close()
    page = render_dashboard(str(db))
    assert "failing" in page and "passing" not in page


def test_empty_database_renders_without_crashing(tmp_path):
    db = tmp_path / "empty.db"
    conn = sqlite3.connect(db)
    create_matches_table_if_not_exists(conn)
    conn.close()
    page = render_dashboard(str(db))
    assert "No matches yet" in page
    assert "No runs recorded yet" in page


def test_status_json_is_machine_readable(tmp_path):
    out = write_status_json(str(FIXTURE), str(tmp_path / "status.json"))
    d = json.loads(out.read_text())
    assert d["season_in_database"] == "season43"
    assert d["total_sets"] > 1_000
    assert d["quality_ok"] is True
    assert 0 <= d["skill_ns_coverage"] <= 1


@pytest.mark.parametrize("field", ["season", "total", "daily", "runs", "report"])
def test_collect_gathers_each_section(field):
    assert field in collect(str(FIXTURE))
