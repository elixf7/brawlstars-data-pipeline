#!/usr/bin/env python3
"""Run the quality gate against a season database."""
from __future__ import annotations

import argparse
import json

from bsetl.cli import add_logging_flags, configure_logging
from bsetl.quality import Thresholds, run_quality_checks


def add_threshold_flags(p: argparse.ArgumentParser) -> None:
    d = Thresholds()
    g = p.add_argument_group("thresholds")
    g.add_argument("--min-rows", type=int, default=d.min_rows,
                   help=f"Fail below this row count (default: {d.min_rows:,})")
    g.add_argument("--max-core-null-rate", type=float, default=d.max_core_null_rate)
    g.add_argument("--min-skill-coverage", type=float, default=d.min_skill_coverage)
    g.add_argument("--min-distinct-brawlers", type=int, default=d.min_distinct_brawlers)


def thresholds_from(args: argparse.Namespace) -> Thresholds:
    return Thresholds(
        min_rows=args.min_rows,
        max_core_null_rate=args.max_core_null_rate,
        min_skill_coverage=args.min_skill_coverage,
        min_distinct_brawlers=args.min_distinct_brawlers,
    )


def main() -> None:
    p = argparse.ArgumentParser(
        description="Check whether a season database is fit to publish. "
                    "Exits non-zero on failure."
    )
    p.add_argument("--clean-db-path", required=True)
    p.add_argument("--season", default=None, help="Default: derived from the path")
    p.add_argument("--json", action="store_true", help="Emit the report as JSON")
    p.add_argument("--strict", action="store_true",
                   help="Treat warnings as failures")
    add_threshold_flags(p)
    add_logging_flags(p)
    args = p.parse_args()
    configure_logging(args)

    report = run_quality_checks(
        args.clean_db_path, season=args.season, thresholds=thresholds_from(args)
    )

    print(json.dumps(report.to_dict(), indent=2) if args.json else report.render())

    failed = not report.ok or (args.strict and report.warnings)
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
