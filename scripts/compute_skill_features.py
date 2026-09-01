#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Ensure repository root is importable when this file is executed by absolute path
import sys as _sys
from pathlib import Path as _P
_REPO_ROOT = _P(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_REPO_ROOT))

from data_clean.skill_config import (
    SKILL_COLUMN,
    SKILL_COVERAGE_COLUMN,
    SKILL_FEATURE_VERSION,
    default_skill_config,
    derive_season_label,
)
from data_clean.skill_features import (
    base_bin_start,
    collect_avg_elo_by_bin,
    collect_global_avg_elo,
    midrank_percentile,
    parse_battle_time_utc,
    percentile_to_normal_score,
    percentile_to_logit_score,
)


def _ensure_feature_columns(conn: sqlite3.Connection) -> None:
    cur = conn.execute("PRAGMA table_info('matches');")
    existing = {row[1] for row in cur.fetchall()}
    ops: List[str] = []
    if SKILL_COLUMN not in existing:
        ops.append(f"ALTER TABLE matches ADD COLUMN {SKILL_COLUMN} REAL;")
    if SKILL_COVERAGE_COLUMN not in existing:
        ops.append(f"ALTER TABLE matches ADD COLUMN {SKILL_COVERAGE_COLUMN} INTEGER;")
    for sql in ops:
        conn.execute(sql)
    if ops:
        conn.commit()


def _create_metadata_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS skill_bin_metadata (
            feature_version TEXT,
            season          TEXT,
            bin_start_utc   TEXT,
            bin_end_utc     TEXT,
            n_samples       INTEGER,
            coverage_ok     INTEGER,
            epsilon         REAL,
            bin_width_days  INTEGER,
            fallback_used   INTEGER,
            created_utc     TEXT
        );
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_skill_meta_season ON skill_bin_metadata(season);"
    )
    conn.commit()


def _delete_existing_metadata(conn: sqlite3.Connection, season: str) -> None:
    conn.execute(
        "DELETE FROM skill_bin_metadata WHERE season = ? AND feature_version = ?",
        (season, SKILL_FEATURE_VERSION),
    )
    conn.commit()


def _insert_metadata(
    conn: sqlite3.Connection,
    season: str,
    bin_stats: Dict[datetime, Dict[str, object]],
    *,
    bin_width_days: int,
    epsilon: float,
    fallback_strategy: str,
) -> List[Dict[str, object]]:
    created_utc = datetime.now(tz=timezone.utc).isoformat()
    rows: List[Tuple] = []
    out_json: List[Dict[str, object]] = []
    for start, info in sorted(bin_stats.items()):
        n = int(info["n"])  # type: ignore[index]
        ok = 1 if bool(info["coverage_ok"]) else 0  # type: ignore[index]
        fallback_used = 1 if (fallback_strategy == "global_season_ecdf" and not ok) else 0
        end = start + timedelta(days=bin_width_days)
        rows.append(
            (
                SKILL_FEATURE_VERSION,
                season,
                start.isoformat(),
                end.isoformat(),
                n,
                ok,
                float(epsilon),
                int(bin_width_days),
                fallback_used,
                created_utc,
            )
        )
        out_json.append(
            {
                "feature_version": SKILL_FEATURE_VERSION,
                "season": season,
                "bin_start_utc": start.isoformat(),
                "bin_end_utc": end.isoformat(),
                "n_samples": n,
                "coverage_ok": bool(ok),
                "epsilon": float(epsilon),
                "bin_width_days": int(bin_width_days),
                "fallback_used": bool(fallback_used),
                "created_utc": created_utc,
            }
        )
    conn.executemany(
        """
        INSERT INTO skill_bin_metadata (
            feature_version, season, bin_start_utc, bin_end_utc,
            n_samples, coverage_ok, epsilon, bin_width_days, fallback_used, created_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    return out_json


def _write_json_sidecar(clean_db_path: str, season: str, payload: Dict[str, object]) -> Path:
    db_path = Path(clean_db_path)
    out_path = db_path.with_name(f"{season}_skill_ns_metadata.json")
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    return out_path


def _update_rows(
    conn: sqlite3.Connection,
    clean_db_path: str,
    *,
    bin_width_days: int,
    epsilon: float,
    bin_stats: Dict[datetime, Dict[str, object]],
    fallback_strategy: str,
    mapping: str,
    update_batch_size: int = 20_000,
) -> None:
    # Optional global ECDF fallback
    global_vals: Optional[List[float]] = None
    if fallback_strategy == "global_season_ecdf":
        global_vals = collect_global_avg_elo(clean_db_path)

    cur = conn.execute("SELECT id, battle_time, avg_elo FROM matches")
    updates: List[Tuple[Optional[float], int, int]] = []
    rows = cur.fetchmany(update_batch_size)
    while rows:
        for row in rows:
            row_id = int(row[0])
            ts = row[1]
            avg = row[2]
            # Compute coverage flag from bin membership
            try:
                dt = parse_battle_time_utc(ts) if ts is not None else None
            except ValueError:
                dt = None
            if dt is None:
                coverage_ok = 0
                skill_value = None
                updates.append((skill_value, coverage_ok, row_id))
                continue
            bstart = base_bin_start(dt, base_width_days=bin_width_days)
            info = bin_stats.get(bstart)
            coverage_ok = 1 if (info and bool(info["coverage_ok"])) else 0
            skill_value: Optional[float] = None

            if avg is not None:
                avg_f = float(avg)
                if coverage_ok == 1 and info is not None:
                    s_vals = info["values"]  # type: ignore[index]
                    p = midrank_percentile(avg_f, s_vals)  # type: ignore[arg-type]
                    skill_value = (
                        percentile_to_normal_score(p, epsilon)
                        if mapping == "normal"
                        else percentile_to_logit_score(p, epsilon)
                    )
                elif fallback_strategy == "global_season_ecdf" and global_vals:
                    p = midrank_percentile(avg_f, global_vals)
                    skill_value = (
                        percentile_to_normal_score(p, epsilon)
                        if mapping == "normal"
                        else percentile_to_logit_score(p, epsilon)
                    )

            updates.append((skill_value, coverage_ok, row_id))

        conn.executemany(
            f"UPDATE matches SET {SKILL_COLUMN} = ?, {SKILL_COVERAGE_COLUMN} = ? WHERE id = ?",
            updates,
        )
        conn.commit()
        updates.clear()
        rows = cur.fetchmany(update_batch_size)


def main() -> None:
    cfg = default_skill_config()
    p = argparse.ArgumentParser(description="Compute and write time-local ECDF skill feature")
    p.add_argument("--clean-db-path", required=True)
    p.add_argument("--bin-width-days", type=int, default=int(cfg["bin_width_days"]))
    p.add_argument("--min-bin-count", type=int, default=int(cfg["min_bin_count"]))
    p.add_argument("--epsilon", type=float, default=float(cfg["epsilon"]))
    p.add_argument("--mapping", choices=["normal", "logit"], default=str(cfg.get("mapping", "normal")))
    p.add_argument("--fallback-strategy", choices=["none", "global_season_ecdf"], default=str(cfg.get("fallback_strategy", "none")))
    p.add_argument("--season", default=None)
    p.add_argument("--read-batch-size", type=int, default=100_000)
    p.add_argument("--update-batch-size", type=int, default=20_000)

    args = p.parse_args()
    clean_db_path = args.clean_db_path
    season = args.season or derive_season_label(clean_db_path)
    bin_width_days: int = max(1, args.bin_width_days)
    min_bin_count: int = max(1, args.min_bin_count)
    epsilon: float = max(1e-9, float(args.epsilon))
    mapping: str = args.mapping
    fallback_strategy: str = args.fallback_strategy
    read_batch_size: int = max(1_000, args.read_batch_size)
    update_batch_size: int = max(1_000, args.update_batch_size)

    db_path = Path(clean_db_path)
    if not db_path.exists():
        raise SystemExit(f"Clean DB not found: {db_path}")

    # Collect values by fixed bins
    avg_elo_by_bin = collect_avg_elo_by_bin(
        clean_db_path=clean_db_path,
        base_width_days=bin_width_days,
        batch_size=read_batch_size,
    )
    # Build bin stats (sorted arrays + coverage flag)
    from data_clean.skill_features import build_bin_stats  # local import to avoid cycle order
    bin_stats = build_bin_stats(avg_elo_by_bin, min_bin_count=min_bin_count)

    # Open DB and ensure columns/metadata table
    conn = sqlite3.connect(str(db_path))
    try:
        _ensure_feature_columns(conn)
        _create_metadata_table(conn)
        _delete_existing_metadata(conn, season)
        _update_rows(
            conn,
            clean_db_path=clean_db_path,
            bin_width_days=bin_width_days,
            epsilon=epsilon,
            bin_stats=bin_stats,
            fallback_strategy=fallback_strategy,
            mapping=mapping,
            update_batch_size=update_batch_size,
        )
        meta_rows = _insert_metadata(
            conn,
            season=season,
            bin_stats=bin_stats,
            bin_width_days=bin_width_days,
            epsilon=epsilon,
            fallback_strategy=fallback_strategy,
        )
    finally:
        conn.close()

    payload = {
        "feature_version": SKILL_FEATURE_VERSION,
        "season": season,
        "bin_width_days": bin_width_days,
        "min_bin_count": min_bin_count,
        "epsilon": epsilon,
        "mapping": mapping,
        "fallback_strategy": fallback_strategy,
        "bins": meta_rows,
        "created_utc": datetime.now(tz=timezone.utc).isoformat(),
    }
    sidecar = _write_json_sidecar(clean_db_path, season, payload)
    print(f"Wrote skill features into {db_path}")
    print(f"Wrote metadata JSON: {sidecar}")


if __name__ == "__main__":
    main()


