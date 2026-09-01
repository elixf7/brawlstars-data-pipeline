#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from typing import List, Optional

from DB_Data_Pull.pull_data_4 import process_tags_and_write_async
from utils.env_utils import get_api_key


def parse_tags(tags: List[str], tags_file: Optional[str]) -> List[str]:
    out = list(tags or [])
    if tags_file:
        with open(tags_file, "r") as f:
            for line in f:
                t = line.strip()
                if t:
                    out.append(t)
    # ensure leading '#'
    out = [t if t.startswith("#") else f"#{t}" for t in out]
    # de-dup while preserving order
    seen = set()
    deduped: List[str] = []
    for t in out:
        if t not in seen:
            seen.add(t)
            deduped.append(t)
    return deduped


def parse_datetime_utc(s: Optional[str]) -> datetime:
    if not s:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
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

    args = p.parse_args()
    api_key = get_api_key()  # BS_API_KEY (or BRAWLSTARS_API_KEY) from the environment
    tags = parse_tags(args.tags, args.tags_file)
    if not tags:
        raise SystemExit("No seed tags provided (use --tags or --tags-file)")
    latest_runtime = parse_datetime_utc(args.latest_runtime)

    async def _run() -> None:
        await process_tags_and_write_async(
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
        )

    asyncio.run(_run())


if __name__ == "__main__":
    main()






