"""The crawl frontier, persisted.

BFS state used to live only in memory, so every run restarted from its seed
tags. That is wasteful on a schedule: a run that stops on budget has a queue of
known-good unvisited tags, and throwing it away means the next run rediscovers
them at full API cost. Persisting the frontier turns a series of short runs into
one long crawl.

Each pending tag carries the elo it was discovered at, because the order the
frontier is drained in decides what the dataset ends up holding. See
`load_frontier` for why that order is by elo rather than by depth.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

FRONTIER_TABLE = "crawl_frontier"

#: (tag, depth, elo). `elo` is None for seed tags, which were never observed in
#: a battle log.
FrontierItem = tuple[str, int, int | None]


def create_frontier_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {FRONTIER_TABLE} (
            tag          TEXT PRIMARY KEY,
            depth        INTEGER NOT NULL,
            enqueued_utc TEXT NOT NULL,
            elo          INTEGER
        )
        """
    )
    # A frontier stored before elo was recorded is still perfectly good work;
    # it just sorts last until those tags are re-discovered.
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({FRONTIER_TABLE})")}
    if "elo" not in cols:
        conn.execute(f"ALTER TABLE {FRONTIER_TABLE} ADD COLUMN elo INTEGER")
    conn.commit()


def load_frontier(db_path: str) -> list[FrontierItem]:
    """Return pending (tag, depth, elo) triples, highest elo first.

    Order is the whole point. Draining the frontier breadth-first spends the
    request budget in proportion to how common a player is, and the ladder is
    bottom-heavy — so a FIFO sweep buys mostly mid-elo matches and the top of
    the ladder stays as rare in the dataset as it is in the game.

    Popping the highest observed elo first inverts that: matchmaking pairs like
    with like, so a high-elo player's log is dense with high-elo matches. Tags
    below the cut are not discarded, they simply wait, which keeps them
    available as breadth if the crawl ever needs it.
    """
    try:
        conn = sqlite3.connect(db_path)
    except sqlite3.OperationalError:
        return []
    try:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({FRONTIER_TABLE})")}
        if not cols:
            return []  # table absent: nothing to resume
        # A frontier stored before elo was recorded is still hours of paid-for
        # requests. Reading it must not depend on a column it predates —
        # selecting one blindly raises, and the frontier would be silently
        # dropped, which is the single most expensive way this can fail.
        elo = "elo" if "elo" in cols else "NULL AS elo"
        order = "elo IS NULL, elo DESC, " if "elo" in cols else ""
        rows = conn.execute(
            f"SELECT tag, depth, {elo} FROM {FRONTIER_TABLE} "
            f"ORDER BY {order}depth, enqueued_utc"
        ).fetchall()
        return [(t, int(d), None if e is None else int(e)) for t, d, e in rows]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def save_frontier(db_path: str, items: list[FrontierItem]) -> int:
    """Replace the stored frontier with `items`. Returns the number saved."""
    conn = sqlite3.connect(db_path)
    try:
        create_frontier_table(conn)
        conn.execute(f"DELETE FROM {FRONTIER_TABLE}")
        if items:
            now = datetime.now(tz=UTC).isoformat()
            conn.executemany(
                f"INSERT OR REPLACE INTO {FRONTIER_TABLE} "
                "(tag, depth, enqueued_utc, elo) VALUES (?, ?, ?, ?)",
                [
                    (t, int(d), now, None if e is None else int(e))
                    for t, d, e in items
                ],
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
