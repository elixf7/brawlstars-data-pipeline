#!/usr/bin/env python3
"""Move the working season database between a runner and the Hub."""
from __future__ import annotations

import argparse

from bsetl.cli import add_logging_flags, configure_logging
from bsetl.publish.hub import PublishError
from bsetl.publish.state import pull_state, push_state


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

    add_logging_flags(p)
    args = p.parse_args()
    configure_logging(args)

    try:
        if args.command == "pull":
            found = pull_state(args.repo_id, args.season, args.dest)
            if not found and not args.allow_missing:
                raise SystemExit(f"error: no stored state for {args.season}")
            print("restored" if found else "no stored state; starting fresh")
        else:
            print(push_state(args.repo_id, args.season, args.db_path))
    except PublishError as e:
        raise SystemExit(f"error: {e}") from None


if __name__ == "__main__":
    main()
