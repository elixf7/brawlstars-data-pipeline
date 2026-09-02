#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
from datetime import UTC, datetime
from pathlib import Path

from bsetl.cli import add_logging_flags, configure_logging
from bsetl.config import get_api_key
from bsetl.ingest.budget import RunBudget
from bsetl.ingest.crawler import process_tags_and_write_async
from bsetl.logconfig import get_logger

# Explicit: __name__ is "__main__" under `python -m`.
logger = get_logger("bsetl.cli.ingest")


def parse_tags(tags: list[str], tags_file: str | None) -> list[str]:
    out = list(tags or [])
    if tags_file:
        path = Path(tags_file)
        if path.exists():
            for line in path.read_text().splitlines():
                t = line.strip()
                if t and not t.startswith("#!"):
                    out.append(t)
        else:
            # Only the first run of a season needs seeds; later runs resume the
            # stored frontier, so a missing file is not fatal.
            logger.info(
                "Seed file %s not found; relying on the stored frontier", tags_file
            )
    # ensure leading '#'
    out = [t if t.startswith("#") else f"#{t}" for t in out]
    # de-dup while preserving order
    seen = set()
    deduped: list[str] = []
    for t in out:
        if t not in seen:
            seen.add(t)
            deduped.append(t)
    return deduped


def parse_datetime_utc(s: str | None) -> datetime:
    if not s:
        return datetime(1970, 1, 1, tzinfo=UTC)
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def main() -> None:
    p = argparse.ArgumentParser(description="Run one Brawl Stars ranked crawl")
    p.add_argument("--tags", nargs="*", default=[], help="Seed tags (with or without leading '#')")
    p.add_argument("--tags-file", default=None, help="Path to file of seed tags (one per line)")
    p.add_argument("--clean-db-path", required=True, help="Path to clean SQLite DB")
    p.add_argument("--latest-runtime", default=None, help="ISO time (e.g., 2025-10-10T00:00:00Z)")

    p.add_argument("--max-depth", type=int, default=2)
    p.add_argument("--batch-size", type=int, default=1500)
    p.add_argument("--concurrency", type=int, default=40)
    p.add_argument("--requests-per-second", type=float, default=5.0)
    p.add_argument("--fetch-player-data", action="store_true")
    p.add_argument("--prefilter-initial-tags", action="store_true")
    p.add_argument("--elo-queue-min", type=float, default=None)
    p.add_argument("--elo-queue-max", type=float, default=None)
    p.add_argument("--elo-game-min", type=float, default=None)
    p.add_argument("--elo-game-max", type=float, default=None)
    p.add_argument("--fetched-tags-ttl-hours", type=float, default=0.0,
                   help="Skip tags fetched within this many hours (0 = disabled)")
    p.add_argument("--flush-every-n-batches", type=int, default=0,
                   help="Flush rows to DB every N BFS batches to cap memory use (0 = disabled)")

    b = p.add_argument_group(
        "budget",
        "Bounds on the run. An unattended crawl must stop on its own; without "
        "any of these it runs until the frontier empties.",
    )
    b.add_argument("--max-requests", type=int, default=None,
                   help="Stop after this many API requests")
    b.add_argument("--max-seconds", type=float, default=None,
                   help="Stop after this many seconds of wall clock")
    b.add_argument("--min-rows-per-1k-requests", type=float, default=None,
                   help="Stop when the trailing window yields fewer newly inserted "
                        "rows than this per 1000 requests")
    b.add_argument("--yield-grace-requests", type=int, default=2000,
                   help="Requests to spend before yield is judged (default: 2000)")
    b.add_argument("--yield-window-requests", type=int, default=2000,
                   help="Width of the trailing window used to measure yield (default: 2000)")
    b.add_argument("--max-database-rows", type=int, default=None,
                   help="Stop collecting once the database holds this many sets. "
                        "A ceiling on the finished dataset, checked before each run.")
    b.add_argument("--no-resume", action="store_true",
                   help="Ignore the stored frontier and start from seeds only")

    p.add_argument("--provision-key", action="store_true",
                   help="Mint a short-lived API key scoped to this host's IP from "
                        "the developer portal, and revoke it when the run ends. "
                        "Reads BS_DEV_EMAIL and BS_DEV_PASSWORD instead of BS_API_KEY. "
                        "This is how scheduled runs authenticate.")

    add_logging_flags(p)
    args = p.parse_args()
    configure_logging(args)
    tags = parse_tags(args.tags, args.tags_file)
    resuming = not args.no_resume and Path(args.clean_db_path).exists()
    if not tags and not resuming:
        raise SystemExit("No seed tags provided (use --tags or --tags-file)")
    latest_runtime = parse_datetime_utc(args.latest_runtime)

    # A season should not grow without bound. Checked before the run rather
    # than mid-crawl: one run adds a small fraction of the ceiling, so the
    # overshoot is negligible and the check costs one query.
    if args.max_database_rows and Path(args.clean_db_path).exists():
        import sqlite3
        try:
            conn = sqlite3.connect(f"file:{args.clean_db_path}?mode=ro", uri=True)
            have = conn.execute("SELECT COUNT(*) FROM sqlite_master "
                                "WHERE type='table' AND name='matches'").fetchone()[0]
            rows = conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0] if have else 0
            conn.close()
        except Exception:
            rows = 0
        if rows >= args.max_database_rows:
            logger.info("Database holds %d sets, at or above the %d ceiling; "
                        "collecting nothing further this season.",
                        rows, args.max_database_rows)
            print(json.dumps({"requests_made": 0, "rows_inserted": 0,
                              "stop_reason": "row_ceiling_reached",
                              "database_rows": rows}, indent=2))
            return

    budget = RunBudget(
        max_requests=args.max_requests,
        max_seconds=args.max_seconds,
        min_rows_per_1k_requests=args.min_rows_per_1k_requests,
        yield_grace_requests=args.yield_grace_requests,
        yield_window_requests=args.yield_window_requests,
    )

    async def _run(api_key: str):
        return await process_tags_and_write_async(
            player_tags=tags,
            api_key=api_key,
            latest_runtime=latest_runtime,
            max_depth=args.max_depth,
            batch_size=args.batch_size,
            concurrency=args.concurrency,
            fetch_player_data=args.fetch_player_data,
            clean_db_path=args.clean_db_path,
            elo_queue_min=args.elo_queue_min,
            elo_queue_max=args.elo_queue_max,
            elo_game_min=args.elo_game_min,
            elo_game_max=args.elo_game_max,
            prefilter_initial_tags=args.prefilter_initial_tags,
            requests_per_second=args.requests_per_second,
            fetched_tags_ttl_hours=args.fetched_tags_ttl_hours,
            flush_every_n_batches=args.flush_every_n_batches,
            budget=budget,
            resume=not args.no_resume,
        )

    # Either mint a key for this host and revoke it on the way out, or use the
    # one already in the environment. The key is held only for the run.
    if args.provision_key:
        from bsetl.ingest.keyprovision import PortalError, ephemeral_key

        try:
            key_source = ephemeral_key()
        except PortalError as e:
            raise SystemExit(f"error: {e}") from None
    else:
        try:
            key_source = contextlib.nullcontext(get_api_key())
        except RuntimeError as e:
            raise SystemExit(f"error: {e}") from None

    with key_source as provisioned:
        api_key = provisioned.key if args.provision_key else provisioned
        if args.provision_key:
            logger.info("Provisioned key %s for %s", provisioned.key_id, provisioned.ip)
        stats = asyncio.run(_run(api_key))

    print(json.dumps(stats.summary(), indent=2))


if __name__ == "__main__":
    main()






