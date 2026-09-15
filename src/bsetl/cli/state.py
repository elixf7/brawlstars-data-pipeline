#!/usr/bin/env python3
"""Move the working season database between a runner and the Hub."""
from __future__ import annotations

import argparse

from bsetl.cli import add_logging_flags, configure_logging
from bsetl.publish.hub import PublishError
from bsetl.publish.state import (
    prune_state,
    pull_state,
    push_state,
    seeds_for_new_season,
    squash_history,
)
from bsetl.transform.seasons import season_label


def main() -> None:
    p = argparse.ArgumentParser(
        description="Pull or push the working season database. Reads HF_TOKEN "
                    "from the environment."
    )
    sub = p.add_subparsers(dest="command", required=True)

    pull = sub.add_parser("pull", help="Restore the working database for a season")
    pull.add_argument("--repo-id", required=True)
    pull.add_argument("--season", required=True)
    pull.add_argument("--dest", required=True)
    pull.add_argument("--allow-missing", action="store_true",
                      help="Exit 0 when the season has no stored state yet")

    push = sub.add_parser("push", help="Store the working database for a season")
    push.add_argument("--repo-id", required=True)
    push.add_argument("--season", required=True)
    push.add_argument("--db-path", required=True)

    seeds = sub.add_parser(
        "seeds",
        help="Write seed tags for a season with no stored state, sampled from "
             "the strongest players of the season before it",
    )
    seeds.add_argument("--repo-id", required=True)
    seeds.add_argument("--season", required=True)
    seeds.add_argument("--out", required=True)
    seeds.add_argument("--min-elo", type=float, default=18.0)
    seeds.add_argument("--limit", type=int, default=5000)

    prune = sub.add_parser(
        "prune",
        help="Delete stored working databases for seasons that are finished "
             "and published. Keeps the current season and the one before it, "
             "which is what the next season is seeded from.",
    )
    prune.add_argument("--repo-id", required=True)
    prune.add_argument("--season", required=True, help="The season still in progress")
    prune.add_argument("--keep-previous", type=int, default=1,
                       help="How many finished seasons to keep alongside it")

    sq = sub.add_parser(
        "squash",
        help="Collapse the dataset repo's history into one commit. Removes old "
             "versions of the working database; keeps every current file.",
    )
    sq.add_argument("--repo-id", required=True)
    sq.add_argument("--yes", action="store_true", help="Required; the history is not recoverable")

    add_logging_flags(p)
    args = p.parse_args()
    configure_logging(args)

    try:
        if args.command == "squash":
            if not args.yes:
                print("Would squash history for", args.repo_id)
                print("Current files are untouched; old versions are discarded.")
                print("Re-run with --yes.")
                return
            squash_history(args.repo_id)
            print("squashed")
        elif args.command == "pull":
            found = pull_state(args.repo_id, args.season, args.dest)
            if not found and not args.allow_missing:
                raise SystemExit(f"error: no stored state for {args.season}")
            print("restored" if found else "no stored state; starting fresh")
        elif args.command == "seeds":
            n = seeds_for_new_season(
                args.repo_id, args.season, args.out,
                min_elo=args.min_elo, limit=args.limit,
            )
            print(f"{n} seed tag(s) at {args.out}")
        elif args.command == "prune":
            number = int(args.season.removeprefix("season"))
            keep = [season_label(number - b) for b in range(args.keep_previous + 1)]
            removed = prune_state(args.repo_id, keep)
            print(f"kept {', '.join(keep)}; removed {len(removed)}")
        else:
            print(push_state(args.repo_id, args.season, args.db_path))
    except PublishError as e:
        raise SystemExit(f"error: {e}") from None


if __name__ == "__main__":
    main()
