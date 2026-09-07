from __future__ import annotations

import sqlite3

from bsetl.transform.skill_config import SKILL_COLUMN, SKILL_COVERAGE_COLUMN

MATCHES_COLUMNS: list[tuple[str, str]] = [
    ("id", "INTEGER PRIMARY KEY"),
    ("battle_time", "TEXT"),
    ("mode", "TEXT"),
    ("map", "TEXT"),
    ("record", "TEXT"),
    ("star_brawler", "TEXT"),
    ("star_power", "INTEGER"),
    ("star_player_tag", "TEXT"),
    ("star_elo", "INTEGER"),
    ("avg_elo", "REAL"),
]


#: Per-slot fields. `tag` identifies the player who brought the brawler, which
#: makes a match joinable to the players in it — every participant, not only the
#: star player. `rank` and `highest_trophies` were dropped: they come from the
#: player-profile endpoint, which the crawl does not call, so they were always
#: null.
BRAWLER_FIELDS: tuple[str, ...] = ("name", "elo", "power", "tag")


def get_brawler_column_names() -> list[str]:
    return [
        f"t{team}_b{slot}_{field}"
        for team in (1, 2)
        for slot in range(3)
        for field in BRAWLER_FIELDS
    ]


def get_matches_column_defs() -> list[tuple[str, str]]:
    cols = MATCHES_COLUMNS.copy()
    for name in get_brawler_column_names():
        col_type = "TEXT" if name.endswith(("name", "tag")) else "INTEGER"
        cols.append((name, col_type))
    return cols


#: Columns the transform adds after ingestion. A `matches` table is well formed
#: with or without them, so shape comparisons must not read them as drift.
OPTIONAL_COLUMNS: frozenset[str] = frozenset({SKILL_COLUMN, SKILL_COVERAGE_COLUMN})


def schema_drift(conn: sqlite3.Connection) -> tuple[list[str], list[str]]:
    """How a `matches` table differs from the schema this codebase writes.

    Returns the columns that are missing and the columns that are no longer part
    of the schema. Both the quality gate and the state restore ask this, and
    they must agree: a database the gate would reject is one the crawl must not
    resume from.
    """
    actual = {r[1] for r in conn.execute("PRAGMA table_info(matches)")}
    if not actual:
        return ([name for name, _ in get_matches_column_defs()], [])
    expected = {name for name, _ in get_matches_column_defs()}
    return (sorted(expected - actual), sorted(actual - expected - OPTIONAL_COLUMNS))


def create_matches_table_if_not_exists(conn: sqlite3.Connection) -> None:
    """Create matches schema exactly as specified and add indexes.

    The column order matches the documented contract and totals 34 columns.
    """
    col_defs = ",\n            ".join([f"{n} {t}" for n, t in get_matches_column_defs()])
    sql = f"""
    CREATE TABLE IF NOT EXISTS matches (
            {col_defs}
    );
    """
    conn.execute(sql)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_matches_mode  ON matches(mode);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_matches_time  ON matches(battle_time);")
    # Unique key to prevent duplicates on repeated pulls
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uniq_matches_key ON matches(battle_time, map, star_player_tag);"
    )
    conn.commit()


def get_matches_insert_statement() -> str:
    """Return a parameterized INSERT OR IGNORE with 40 placeholders in order.

    OR IGNORE ensures duplicate rows (by unique index) are skipped efficiently.
    """
    columns = [name for name, _ in get_matches_column_defs()]
    placeholders = ", ".join(["?"] * len(columns))
    col_list = ", ".join(columns)
    return f"INSERT OR IGNORE INTO matches ({col_list}) VALUES ({placeholders})"


def create_fetched_tags_table_if_not_exists(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fetched_tags (
            tag         TEXT PRIMARY KEY,
            fetched_utc TEXT NOT NULL
        )
        """
    )
    conn.commit()


def upsert_fetched_tags(conn: sqlite3.Connection, tags: list[str], fetched_utc: str) -> None:
    """Bulk-upsert tags with a fetch timestamp. Overwrites existing rows."""
    if not tags:
        return
    conn.executemany(
        "INSERT OR REPLACE INTO fetched_tags (tag, fetched_utc) VALUES (?, ?)",
        [(t, fetched_utc) for t in tags],
    )
    conn.commit()


def load_fetched_tags_from_db(db_path: str) -> set:
    """Return the full set of tags in fetched_tags. Empty set if table or file absent."""
    import os
    if not os.path.exists(db_path):
        return set()
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT tag FROM fetched_tags").fetchall()
        return {r[0] for r in rows}
    except sqlite3.OperationalError:
        return set()
    finally:
        conn.close()


