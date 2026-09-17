#!/usr/bin/env python3
"""Ranked season arithmetic. Seasons start on the third Thursday of a month."""
from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime

from bsetl.transform.seasons import (
    OPENING_DAYS,
    current_season,
    days_until_next_season,
    in_season_opening,
    opening_yield_floor,
    season_age_hours,
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
    cur.add_argument("--steady-yield-floor", type=float, default=50.0,
                     help="Yield floor once the season is fully under way; the "
                          "value emitted is scaled down while it is not")

    op = sub.add_parser(
        "opening",
        help="Whether the season is new enough to want a daily crawl",
    )
    op.add_argument("--format", choices=["bool", "gha"], default="bool")
    op.add_argument("--days", type=int, default=OPENING_DAYS)

    insp = sub.add_parser("inspect", help="Which season(s) a database holds")
    insp.add_argument("--clean-db-path", required=True)

    args = p.parse_args()

    if args.command == "current":
        label = current_season()
        n = season_number_at(datetime.now(UTC))
        start, end = season_bounds(n)
        if args.format == "label":
            print(label)
        elif args.format == "env":
            # Consumed by the workflow via >> $GITHUB_ENV
            print(f"SEASON={label}")
            print(f"SEASON_START={season_start_iso(n)}")
            print(f"SEASON_AGE_HOURS={season_age_hours():.1f}")
            print(f"MIN_ROWS_PER_1K={opening_yield_floor(args.steady_yield_floor)}")
        else:
            print(f"{label}: {start} to {end} "
                  f"({days_until_next_season()} days until the next reset)")
            print(f"{season_age_hours():.1f} hours in; yield floor "
                  f"{opening_yield_floor(args.steady_yield_floor)} "
                  f"of {int(args.steady_yield_floor)} rows per 1k requests")
        return

    if args.command == "opening":
        opening = in_season_opening(days=args.days)
        if args.format == "gha":
            # Consumed by the workflow via >> $GITHUB_OUTPUT
            print(f"opening={str(opening).lower()}")
        else:
            print(str(opening).lower())
        print(
            f"{current_season()} is {season_age_hours() / 24:.1f} days old; "
            f"{'still opening' if opening else 'past its opening'} "
            f"(window {args.days} days)",
            file=sys.stderr,
        )
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
