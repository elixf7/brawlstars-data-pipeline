from __future__ import annotations

import sqlite3

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


def get_brawler_column_names() -> list[str]:
    names: list[str] = []
    for team in (1, 2):
        for slot in range(3):
            prefix = f"t{team}_b{slot}_"
            names.extend([
                prefix + "name",
                prefix + "elo",
                prefix + "rank",
                prefix + "highest_trophies",
                prefix + "power",
            ])
    return names


def get_matches_column_defs() -> list[tuple[str, str]]:
    cols = MATCHES_COLUMNS.copy()
    for name in get_brawler_column_names():
        # team brawler fields: mostly INTEGER except name
        col_type = "TEXT" if name.endswith("name") else "INTEGER"
        if name.endswith("highest_trophies"):
            col_type = "INTEGER"
        cols.append((name, col_type))
    return cols


def create_matches_table_if_not_exists(conn: sqlite3.Connection) -> None:
    """Create matches schema exactly as specified and add indexes.

    The column order matches the documented contract and totals 40 columns.
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


