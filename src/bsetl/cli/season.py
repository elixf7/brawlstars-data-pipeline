#!/usr/bin/env python3
"""Ranked season arithmetic. Seasons start on the third Thursday of a month."""
from __future__ import annotations

import argparse
from datetime import datetime

from bsetl.transform.seasons import (
    current_season,
    days_until_next_season,
    season_bounds,
    season_for_database,
    season_number_at,
    season_start_iso,
    seasons_spanned,
)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    cur = sub.add_parser("current", help="The season in progress")
    cur.add_argument("--format", choices=["label", "env", "text"], default="text")

    insp = sub.add_parser("inspect", help="Which season(s) a database holds")
    insp.add_argument("--clean-db-path", required=True)

    args = p.parse_args()

    if args.command == "current":
        label = current_season()
        n = season_number_at(datetime.now().date())
        start, end = season_bounds(n)
        if args.format == "label":
            print(label)
        elif args.format == "env":
            # Consumed by the workflow via >> $GITHUB_ENV
            print(f"SEASON={label}")
            print(f"SEASON_START={season_start_iso(n)}")
        else:
            print(f"{label}: {start} to {end} "
                  f"({days_until_next_season()} days until the next reset)")
        return

    spanned = seasons_spanned(args.clean_db_path)
    derived = season_for_database(args.clean_db_path)
    print(f"season: {derived or 'unknown'}")
    if len(spanned) > 1:
        raise SystemExit(
            f"error: this database spans {len(spanned)} seasons "
            f"({', '.join(spanned)}); a reset falls inside it"
        )


if __name__ == "__main__":
    main()
