from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path


def _query_scalar(conn: sqlite3.Connection, sql: str) -> str | None:
    cur = conn.execute(sql)
    row = cur.fetchone()
    return row[0] if row and row[0] is not None else None


def _query_list(conn: sqlite3.Connection, sql: str) -> list[str]:
    cur = conn.execute(sql)
    return [r[0] for r in cur.fetchall() if r and r[0] is not None]


def _file_size_bytes(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def _count_rows(conn: sqlite3.Connection) -> int:
    cur = conn.execute("SELECT COUNT(*) FROM matches")
    return int(cur.fetchone()[0])


def _unique_brawler_names(conn: sqlite3.Connection) -> list[str]:
    """Return sorted list of all distinct brawler names across both teams and all slots."""
    name_cols = [
        f"t{team}_b{slot}_name" for team in (1, 2) for slot in range(3)
    ]
    # UNION ALL + GROUP BY is faster than UNION (avoids temp-table dedup of 6M rows)
    unions = " UNION ALL ".join([f"SELECT {c} AS name FROM matches WHERE {c} IS NOT NULL" for c in name_cols])
    sql = f"SELECT name FROM ({unions}) GROUP BY name"
    cur = conn.execute(sql)
    return sorted(row[0] for row in cur.fetchall())


def _brawler_usage_top(conn: sqlite3.Connection, top_n: int = 20) -> list[list]:
    name_cols = [
        f"t{team}_b{slot}_name" for team in (1, 2) for slot in range(3)
    ]
    unions = " UNION ALL ".join([f"SELECT {c} AS name FROM matches WHERE {c} IS NOT NULL" for c in name_cols])
    sql = f"SELECT name, COUNT(*) AS cnt FROM ({unions}) GROUP BY name ORDER BY cnt DESC LIMIT ?"
    cur = conn.execute(sql, (top_n,))
    return [[name, count] for name, count in cur.fetchall()]


def compute_season_metadata(clean_db_path: str, season_label: str | None = None, top_n: int = 20) -> dict:
    """Compute basic season metadata from a clean DB.

    Returns a dictionary with: season_label, start_time, end_time, num_matches,
    num_unique_brawlers, unique_brawlers, modes, maps, brawler_usage_top,
    data_bytes, created_utc.
    """
    path = Path(clean_db_path)
    conn = sqlite3.connect(path)
    try:
        start_time = _query_scalar(conn, "SELECT MIN(battle_time) FROM matches")
        end_time = _query_scalar(conn, "SELECT MAX(battle_time) FROM matches")
        num_matches = _count_rows(conn)
        unique_brawlers = _unique_brawler_names(conn)
        num_unique_brawlers = len(unique_brawlers)
        modes = sorted(set(_query_list(conn, "SELECT DISTINCT mode FROM matches WHERE mode IS NOT NULL")))
        maps = sorted(set(_query_list(conn, "SELECT DISTINCT map FROM matches WHERE map IS NOT NULL")))
        usage = _brawler_usage_top(conn, top_n=top_n)
    finally:
        conn.close()

    created_utc = datetime.now(tz=UTC).isoformat()
    if season_label is None:
        # derive from file name if possible
        season_label = path.stem.replace("_clean", "")

    return {
        "season_label": season_label,
        "start_time": start_time,
        "end_time": end_time,
        "num_matches": num_matches,
        "num_unique_brawlers": num_unique_brawlers,
        "unique_brawlers": unique_brawlers,
        "modes": modes,
        "maps": maps,
        "brawler_usage_top": usage,
        "data_bytes": _file_size_bytes(path),
        "created_utc": created_utc,
    }


def write_season_metadata(clean_db_path: str, season_label: str, top_n: int = 20):
    """Compute and persist metadata to JSON sidecar under data_clean/.

    Returns (path, data) so callers can print the result without recomputing.
    """
    data = compute_season_metadata(clean_db_path, season_label=season_label, top_n=top_n)
    out = Path(clean_db_path).with_name(f"{season_label}_metadata.json")
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    return out, data


