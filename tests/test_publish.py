import sqlite3

import pyarrow.dataset as ds
import pytest

from bsetl.publish.card import render_dataset_card
from bsetl.publish.hub import PublishError, push_season, resolve_token
from bsetl.publish.parquet import (
    _partition_key,
    export_clean_sqlite,
    export_matches_to_parquet,
    matches_arrow_schema,
)
from bsetl.transform.schema import (
    create_matches_table_if_not_exists,
    get_matches_column_defs,
    get_matches_insert_statement,
)

COLS = [n for n, _ in get_matches_column_defs()]


def seed_db(path, rows):
    conn = sqlite3.connect(path)
    create_matches_table_if_not_exists(conn)
    conn.executemany(get_matches_insert_statement(), rows)
    conn.commit()
    conn.close()


def a_row(battle_time, star_tag, avg_elo=17.5):
    n = len(COLS)
    return tuple(
        [None, battle_time, "brawlBall", "Hot Potato", "T1-T1", "RICO", 11, star_tag, 18, avg_elo]
        + [None] * (n - 10)
    )


@pytest.fixture
def db(tmp_path):
    p = str(tmp_path / "season.db")
    seed_db(p, [
        a_row("20251102T002849.000Z", "#A"),
        a_row("20251102T112233.000Z", "#B"),
        a_row("20251103T010101.000Z", "#C"),
    ])
    return p


# -------------------------------------------------------------- partitioning
@pytest.mark.parametrize("ts,expected", [
    ("20251102T002849.000Z", "2025-11-02"),
    ("20251102T002849Z", "2025-11-02"),
    (None, "unknown"),
    ("", "unknown"),
    ("garbage", "unknown"),
])
def test_partition_key(ts, expected):
    assert _partition_key(ts) == expected


def test_a_season_is_one_file(db, tmp_path):
    """Rows are time-ordered and Parquet keeps per-row-group bounds, so a date
    range still skips groups without needing a directory per day."""
    out = tmp_path / "data"
    result = export_matches_to_parquet(db, str(out), season="season43")
    assert result.rows == 3
    assert result.files == 1
    assert (out / "season=season43" / "data.parquet").is_file()
    # The days covered are still reported, for the metadata sidecar.
    assert sorted(result.partitions) == ["2025-11-02", "2025-11-03"]


def test_day_partitioning_is_still_available(db, tmp_path):
    out = tmp_path / "data"
    result = export_matches_to_parquet(db, str(out), season="season43",
                                       partition="day")
    assert result.files == 2
    assert (out / "season=season43" / "battle_date=2025-11-02").is_dir()


def test_an_unknown_partitioning_is_rejected(db, tmp_path):
    with pytest.raises(ValueError, match="season.*day"):
        export_matches_to_parquet(db, str(tmp_path / "d"), partition="hour")


def test_rows_are_written_in_time_order(db, tmp_path):
    """Out-of-order rows would make row-group bounds overlap and stop the
    date filtering from pruning anything."""
    out = tmp_path / "data"
    export_matches_to_parquet(db, str(out), season="season43")
    times = ds.dataset(str(out), partitioning="hive").to_table(
        columns=["battle_time"])["battle_time"].to_pylist()
    assert times == sorted(times)


def test_exported_rows_round_trip(db, tmp_path):
    out = tmp_path / "data"
    export_matches_to_parquet(db, str(out))
    table = ds.dataset(str(out), partitioning="hive").to_table()
    assert table.num_rows == 3
    assert sorted(table["star_player_tag"].to_pylist()) == ["#A", "#B", "#C"]
    assert table["avg_elo"].to_pylist() == [17.5, 17.5, 17.5]


def test_all_null_column_keeps_its_declared_type(db):
    """Inferring types from data would give an all-NULL column the null type,
    producing a file that will not concatenate with other seasons."""
    conn = sqlite3.connect(db)
    schema = matches_arrow_schema(conn)
    conn.close()
    # t1_b0_elo is NULL in every fixture row, but is declared INTEGER.
    assert str(schema.field("t1_b0_elo").type) == "int64"
    assert str(schema.field("t1_b0_name").type) == "string"
    assert str(schema.field("avg_elo").type) == "double"
    assert all(str(f.type) != "null" for f in schema)


def test_export_column_order_matches_the_source(db, tmp_path):
    out = tmp_path / "data"
    export_matches_to_parquet(db, str(out))
    schema = ds.dataset(str(out), partitioning="hive").schema
    assert [n for n in schema.names if n != "battle_date"] == COLS


def test_missing_source_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        export_matches_to_parquet(str(tmp_path / "nope.db"), str(tmp_path / "out"))


# ------------------------------------------------------------ sqlite export
def test_clean_sqlite_drops_pipeline_state_only(db, tmp_path):
    """Consumers get the data, not our crawl frontier and run history."""
    from bsetl.state.frontier import save_frontier
    from bsetl.state.runs import start_run

    save_frontier(db, [("#X", 0, None)])
    start_run(db, {"note": "test"})
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE IF NOT EXISTS fetched_tags (tag TEXT PRIMARY KEY, fetched_utc TEXT)")
    conn.commit()
    before = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert {"crawl_frontier", "pipeline_runs", "fetched_tags"} <= before

    out = str(tmp_path / "clean.db")
    export_clean_sqlite(db, out)

    conn = sqlite3.connect(out)
    after = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0] == 3
    conn.close()
    assert "matches" in after
    assert not ({"crawl_frontier", "pipeline_runs", "fetched_tags"} & after)


# -------------------------------------------------------------------- card
def test_card_reports_real_numbers():
    meta = {
        "season_label": "season42", "num_matches": 2_720_934,
        "start_time": "20251010T175232.000Z", "end_time": "20251106T185617.000Z",
        "num_unique_brawlers": 95, "modes": ["brawlBall", "gemGrab"],
        "maps": ["Hot Potato"], "brawler_usage_top": [["RICO", 91_234]],
    }
    card = render_dataset_card(meta, repo_id="me/bs")
    assert card.startswith("---\n")           # HF frontmatter must come first
    assert "1M<n<10M" in card                 # size bucket from the row count
    assert "2,720,934" in card
    assert "2025-10-10 to 2025-11-06" in card
    assert "me/bs" in card
    assert "91,234" in card
    assert "{" not in card.split("```")[0]    # no unfilled placeholders in prose


def test_card_survives_empty_metadata():
    card = render_dataset_card({})
    assert card.startswith("---\n")
    assert "unknown" in card


def test_card_states_the_sampling_caveat():
    """The crawl over-samples popular players; a card that hid that would be
    misleading to anyone training on it."""
    card = render_dataset_card({"num_matches": 10})
    assert "Not a uniform sample" in card
    assert "No draft order" in card


# ------------------------------------------------------------------ publish
def test_token_is_required(monkeypatch):
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.delenv("HUGGINGFACE_TOKEN", raising=False)
    with pytest.raises(PublishError, match="HF_TOKEN"):
        resolve_token(None)


def test_publishing_an_empty_directory_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "hf_fake")
    (tmp_path / "empty").mkdir()
    with pytest.raises(PublishError, match="No parquet"):
        push_season(str(tmp_path / "empty"), "me/bs")


# ------------------------------------------------- seasons must accumulate
def test_season_is_a_real_column(db, tmp_path):
    """Publishing a new season adds to the dataset rather than replacing the
    last one, and readers get season as a column they can filter on."""
    out = tmp_path / "data"
    export_matches_to_parquet(db, str(out), season="season43")
    table = ds.dataset(str(out), partitioning="hive").to_table()
    assert set(table["season"].to_pylist()) == {"season43"}


def test_two_seasons_coexist(db, tmp_path):
    out = tmp_path / "data"
    export_matches_to_parquet(db, str(out), season="season43")
    # A later season lands beside it rather than clobbering it.
    export_matches_to_parquet(db, str(out / "_other"), season="season44")
    import shutil
    shutil.move(str(out / "_other" / "season=season44"), str(out / "season=season44"))
    shutil.rmtree(out / "_other")

    table = ds.dataset(str(out), partitioning="hive").to_table()
    assert set(table["season"].to_pylist()) == {"season43", "season44"}


# ------------------------------------------------- publishing mirrors a season
def test_publish_removes_only_this_season_and_the_retired_layout():
    """Uploading alone only adds, so a layout change leaves the old files behind
    and a reader sees two partition schemes at once. Other seasons must survive."""
    from bsetl.publish.hub import _delete_patterns

    pats = _delete_patterns("season53")
    assert "data/season=season53/**" in pats
    assert "data/battle_date=**" in pats
    assert not any("season54" in p for p in pats)


def test_publish_without_a_season_deletes_nothing():
    from bsetl.publish.hub import _delete_patterns

    assert _delete_patterns(None) is None
