"""Run history.

Every crawl writes a row here: what it was asked to do, what it did, and why it
stopped. A run is recorded as `running` before any request is made and updated
on the way out, so a process that dies leaves visible evidence rather than
nothing at all.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any

RUNS_TABLE = "pipeline_runs"


def create_runs_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {RUNS_TABLE} (
            run_id          TEXT PRIMARY KEY,
            started_utc     TEXT NOT NULL,
            finished_utc    TEXT,
            status          TEXT NOT NULL,
            stop_reason     TEXT,
            requests_made   INTEGER,
            rows_inserted   INTEGER,
            tags_fetched    INTEGER,
            parse_failures  INTEGER,
            elapsed_seconds REAL,
            frontier_before INTEGER,
            frontier_after  INTEGER,
            stats_json      TEXT,
            config_json     TEXT
        )
        """
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_runs_started ON {RUNS_TABLE}(started_utc)"
    )
    conn.commit()


def start_run(db_path: str, config: dict[str, Any], frontier_before: int = 0) -> str:
    run_id = uuid.uuid4().hex[:16]
    conn = sqlite3.connect(db_path)
    try:
        create_runs_table(conn)
        conn.execute(
            f"INSERT INTO {RUNS_TABLE} "
            "(run_id, started_utc, status, frontier_before, config_json) "
            "VALUES (?, ?, 'running', ?, ?)",
            (
                run_id,
                datetime.now(tz=UTC).isoformat(),
                frontier_before,
                json.dumps(config, default=str, sort_keys=True),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return run_id


def finish_run(
    db_path: str,
    run_id: str,
    *,
    status: str,
    stop_reason: str | None,
    stats: dict[str, Any],
    frontier_after: int,
) -> None:
    conn = sqlite3.connect(db_path)
    try:
        create_runs_table(conn)
        conn.execute(
            f"""
            UPDATE {RUNS_TABLE} SET
                finished_utc = ?, status = ?, stop_reason = ?,
                requests_made = ?, rows_inserted = ?, tags_fetched = ?,
                parse_failures = ?, elapsed_seconds = ?,
                frontier_after = ?, stats_json = ?
            WHERE run_id = ?
            """,
            (
                datetime.now(tz=UTC).isoformat(),
                status,
                stop_reason,
                stats.get("requests_made"),
                stats.get("rows_inserted"),
                stats.get("tags_fetched"),
                stats.get("parse_failures"),
                stats.get("elapsed_seconds"),
                frontier_after,
                json.dumps(stats, default=str, sort_keys=True),
                run_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def recent_runs(db_path: str, limit: int = 20) -> list[dict[str, Any]]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            f"SELECT * FROM {RUNS_TABLE} ORDER BY started_utc DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
