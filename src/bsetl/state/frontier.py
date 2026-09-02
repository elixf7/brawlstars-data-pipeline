"""The crawl frontier, persisted.

BFS state used to live only in memory, so every run restarted from its seed
tags. That is wasteful on a schedule: a run that stops on budget has a queue of
known-good unvisited tags, and throwing it away means the next run rediscovers
them at full API cost. Persisting the frontier turns a series of short runs into
one long crawl.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

FRONTIER_TABLE = "crawl_frontier"


def create_frontier_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {FRONTIER_TABLE} (
            tag          TEXT PRIMARY KEY,
            depth        INTEGER NOT NULL,
            enqueued_utc TEXT NOT NULL
        )
        """
    )
    conn.commit()


def load_frontier(db_path: str) -> list[tuple[str, int]]:
    """Return pending (tag, depth) pairs, shallowest first.

    Shallowest-first preserves BFS order across the run boundary: resuming
    should continue the breadth-first sweep, not dive into whatever happened to
    be deepest when the previous run stopped.
    """
    try:
        conn = sqlite3.connect(db_path)
    except sqlite3.OperationalError:
        return []
    try:
        rows = conn.execute(
            f"SELECT tag, depth FROM {FRONTIER_TABLE} ORDER BY depth, enqueued_utc"
        ).fetchall()
        return [(t, int(d)) for t, d in rows]
    except sqlite3.OperationalError:
        return []  # table absent: nothing to resume
    finally:
        conn.close()


def save_frontier(db_path: str, items: list[tuple[str, int]]) -> int:
    """Replace the stored frontier with `items`. Returns the number saved."""
    conn = sqlite3.connect(db_path)
    try:
        create_frontier_table(conn)
        conn.execute(f"DELETE FROM {FRONTIER_TABLE}")
        if items:
            now = datetime.now(tz=UTC).isoformat()
            conn.executemany(
                f"INSERT OR REPLACE INTO {FRONTIER_TABLE} (tag, depth, enqueued_utc) "
                "VALUES (?, ?, ?)",
                [(t, int(d), now) for t, d in items],
            )
        conn.commit()
        return len(items)
    finally:
        conn.close()


def frontier_size(db_path: str) -> int:
    try:
        conn = sqlite3.connect(db_path)
    except sqlite3.OperationalError:
        return 0
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {FRONTIER_TABLE}").fetchone()[0])
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()
