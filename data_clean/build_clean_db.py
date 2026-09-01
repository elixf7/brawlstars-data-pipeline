#!/usr/bin/env python3
"""clean_season37.py

Transform `Data/Season37/SQLite/giant_37.db` into a new, efficiently
structured SQLite database where **each match is stored on a single row** and
all brawlers are flattened into dedicated columns.

Usage (from project root):
    python3 data_clean/clean_season37.py

The script will create (or replace) `data_clean/season37_clean.db` with a
single table `matches` that contains:

• core match metadata (id, time, mode, map, record)  
• star-player fields  
• 3×5 columns for each team (total 30) describing the brawlers used, e.g.
  `t1_b0_name`, `t1_b0_elo`, … `t2_b2_power`.

That yields **39 columns**; every field is NULL-able to accommodate missing
data.  This wide format lets you query an entire match in one scan while
still supporting indexes on common filters.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, List, Sequence, Tuple
from .schema import create_matches_table_if_not_exists, get_matches_insert_statement

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
OLD_DB_PATH = Path("src/data_raw/Season37/SQLite/giant_37.db")
NEW_DIR = Path("src/data_clean")
NEW_DB_PATH = NEW_DIR / "season37_clean.db"
BATCH_SIZE = 10_000  # number of matches to process per transaction

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def ensure_paths() -> None:
    if not OLD_DB_PATH.exists():
        sys.exit(f"Source DB not found: {OLD_DB_PATH}")
    NEW_DIR.mkdir(parents=True, exist_ok=True)
    if NEW_DB_PATH.exists():
        print(f"[Warning] removing existing {NEW_DB_PATH}")
        NEW_DB_PATH.unlink()


def connect_db(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    uri_flag = "?mode=ro" if read_only else ""
    conn = sqlite3.connect(f"file:{path}{uri_flag}", uri=True)
    conn.row_factory = sqlite3.Row
    # Performance pragmas (safe for one-shot scripts)
    if not read_only:
        conn.executescript("""
        PRAGMA journal_mode = WAL;
        PRAGMA synchronous   = OFF;
        PRAGMA temp_store    = MEMORY;
        PRAGMA foreign_keys  = ON;
        """)
    return conn


def create_schema(dst: sqlite3.Connection) -> None:
    create_matches_table_if_not_exists(dst)


# ---------------------------------------------------------------------------
# Main ETL routine
# ---------------------------------------------------------------------------

def parse_star_player(raw: str | None) -> Tuple[Any, ...]:
    if not raw:
        return (None, None, None, None)
    try:
        arr = json.loads(raw)
        if isinstance(arr, list) and len(arr) >= 4:
            return (arr[0], arr[1], arr[2], arr[3])
    except json.JSONDecodeError:
        pass
    return (None, None, None, None)


def parse_brawler_array(raw: str | None) -> List[dict]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass
    return []


def transform():
    ensure_paths()

    src_conn = connect_db(OLD_DB_PATH, read_only=True)
    dst_conn = connect_db(NEW_DB_PATH)

    create_schema(dst_conn)

    src_cur = src_conn.execute(
        """
        SELECT id,
               Battle_Time,
               Mode,
               Map,
               Record,
               Star_Player,
               Team1_Brawlers,
               Team2_Brawlers
          FROM game_table
        ORDER BY id
        """
    )

    total_rows = 0
    INSERT_SQL = get_matches_insert_statement()

    while True:
        rows = src_cur.fetchmany(BATCH_SIZE)
        if not rows:
            break

        match_rows: List[Tuple[Any, ...]] = []

        for r in rows:
            (
                match_id,
                battle_time,
                mode,
                map_name,
                record,
                star_raw,
                t1_raw,
                t2_raw,
            ) = r

            star_brawler, star_power, star_tag, star_elo = parse_star_player(star_raw)

            # Compute average elo of the 6 players (ignoring NULLs)
            def collect_elos(raw: str | None) -> List[int]:
                return [b.get("elo") for b in parse_brawler_array(raw) if b.get("elo") is not None]

            elo_values = collect_elos(t1_raw) + collect_elos(t2_raw)
            avg_elo = (sum(elo_values) / len(elo_values)) if elo_values else None
            # Skip matches with unrealistically high average Elo (bots/corrupted)
            if avg_elo is not None and avg_elo > 23:
                continue

            # Prepare list in the exact column order
            out: List[Any] = [
                match_id,
                battle_time,
                mode,
                map_name,
                record,
                star_brawler,
                star_power,
                star_tag,
                star_elo,
                avg_elo,
            ]

            def pad_brawlers(raw: str | None) -> List[dict]:
                arr = parse_brawler_array(raw)
                arr += [{}] * (3 - len(arr))  # ensure length 3
                return arr[:3]

            for team_raw in (t1_raw, t2_raw):
                for b in pad_brawlers(team_raw):
                    out.extend([
                        b.get("name"),
                        b.get("elo"),
                        b.get("rank"),
                        b.get("highestTrophies"),
                        b.get("power"),
                    ])

            match_rows.append(tuple(out))

        dst_conn.executemany(INSERT_SQL, match_rows)
        dst_conn.commit()

        total_rows += len(rows)
        print(f"Processed {total_rows:,} matches…", end="\r", flush=True)

    print(f"\nDone!  {total_rows:,} matches copied into {NEW_DB_PATH}")
    # Final checkpoint and disable WAL so that *-wal and *-shm sidecar files
    # are removed once all connections close.  This keeps the workspace tidy
    # while still letting us benefit from WAL during the bulk insert stage.
    dst_conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
    dst_conn.execute("PRAGMA journal_mode=DELETE;")

    src_conn.close()
    dst_conn.close()


if __name__ == "__main__":
    transform() 