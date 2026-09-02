#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys
import time

from bsetl.cli import add_logging_flags, configure_logging


def load_tags(path: str) -> list[str]:
    tags: list[str] = []
    with open(path) as f:
        for line in f:
            t = line.strip()
            if not t:
                continue
            tags.append(t if t.startswith("#") else f"#{t}")
    return tags


def chunked(seq: list[str], n: int) -> list[list[str]]:
    return [seq[i:i + n] for i in range(0, len(seq), n)]


def main() -> None:
    p = argparse.ArgumentParser(description="Queue multiple short Brawl Stars crawls")
    p.add_argument("--tags-file", required=True, help="File with seed tags (one per line)")
    p.add_argument("--per-run-tags", type=int, required=True, help="Tags per run (chunk size)")
    p.add_argument("--max-runs", type=int, default=None, help="Optional cap on number of runs")
    p.add_argument("--sleep-between-runs", type=float, default=3.0, help="Seconds to sleep between runs")

    # Common settings to preserve
    p.add_argument("--clean-db-path", required=True)
    p.add_argument("--latest-runtime", default=None)
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

    # Budgets apply per subprocess run, not to the queue as a whole. For a
    # scheduled crawl prefer a single budgeted `bsetl-ingest`, which resumes
    # from the persistent frontier and bounds the whole run at once.
    p.add_argument("--max-requests", type=int, default=None)
    p.add_argument("--max-seconds", type=float, default=None)
    p.add_argument("--min-rows-per-1k-requests", type=float, default=None)

    add_logging_flags(p)
    args = p.parse_args()
    configure_logging(args)

    tags = load_tags(args.tags_file)
    if not tags:
        print("No tags found in tags file.", file=sys.stderr)
        sys.exit(1)

    batches = chunked(tags, args.per_run_tags)
    if args.max_runs is not None:
        batches = batches[:args.max_runs]

    py = sys.executable
    run_mod = "bsetl.cli.ingest"

    for i, batch in enumerate(batches, 1):
        print(f"[{i}/{len(batches)}] Running batch of {len(batch)} tags...")
        cmd = [
            py, "-m", run_mod,
            "--clean-db-path", args.clean_db_path,
            "--max-depth", str(args.max_depth),
            "--batch-size", str(args.batch_size),
            "--concurrency", str(args.concurrency),
            "--requests-per-second", str(args.requests_per_second),
        ]
        # The API key is inherited from the environment (BS_API_KEY) by the
        # child process; it is never passed as an argument, which would expose
        # it in the process table and shell history.
        if args.latest_runtime:
            cmd += ["--latest-runtime", args.latest_runtime]
        if args.fetch_player_data:
            cmd += ["--fetch-player-data"]
        if args.prefilter_initial_tags:
            cmd += ["--prefilter-initial-tags"]
        if args.elo_queue_min is not None:
            cmd += ["--elo-queue-min", str(args.elo_queue_min)]
        if args.elo_queue_max is not None:
            cmd += ["--elo-queue-max", str(args.elo_queue_max)]
        if args.elo_game_min is not None:
            cmd += ["--elo-game-min", str(args.elo_game_min)]
        if args.elo_game_max is not None:
            cmd += ["--elo-game-max", str(args.elo_game_max)]

        if args.fetched_tags_ttl_hours > 0.0:
            cmd += ["--fetched-tags-ttl-hours", str(args.fetched_tags_ttl_hours)]
        if args.flush_every_n_batches > 0:
            cmd += ["--flush-every-n-batches", str(args.flush_every_n_batches)]
        for flag, val in (
            ("--max-requests", args.max_requests),
            ("--max-seconds", args.max_seconds),
            ("--min-rows-per-1k-requests", args.min_rows_per_1k_requests),
        ):
            if val is not None:
                cmd += [flag, str(val)]

        # Inline provide tags for this run
        cmd += ["--tags"] + batch

        try:
            subprocess.run(cmd, check=True)
        except subprocess.CalledProcessError as e:
            print(f"Batch {i} failed with exit code {e.returncode}. Stopping.", file=sys.stderr)
            sys.exit(e.returncode)

        time.sleep(args.sleep_between_runs)


if __name__ == "__main__":
    main()


