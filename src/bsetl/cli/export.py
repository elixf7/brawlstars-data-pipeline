#!/usr/bin/env python3
"""Turn a working season database into a publishable dataset directory."""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from bsetl.cli import add_logging_flags, configure_logging
from bsetl.publish.card import render_dataset_card
from bsetl.publish.parquet import (
    export_clean_sqlite,
    export_matches_to_parquet,
)
from bsetl.quality import run_quality_checks
from bsetl.transform.metadata import compute_season_metadata
from bsetl.transform.seasons import season_for_database


def main() -> None:
    p = argparse.ArgumentParser(
        description="Export a season database to Parquet plus a dataset card."
    )
    p.add_argument("--clean-db-path", required=True, help="Working season database")
    p.add_argument("--out-dir", required=True, help="Directory to build the dataset in")
    p.add_argument("--season", default=None, help="Season label (default: derived from path)")
    p.add_argument("--repo-id", default=None, help="Hub repo id, for the card's load example")
    p.add_argument("--compression", default="zstd", choices=["zstd", "snappy", "gzip", "none"])
    p.add_argument("--skip-checks", action="store_true",
                   help="Export even if the quality gate fails. The gate exists so "
                        "a bad crawl is not published; skip it deliberately.")
    p.add_argument("--with-sqlite", action="store_true",
                   help="Also emit a SQLite copy with pipeline state stripped, for "
                        "consumers that expect it")
    add_logging_flags(p)
    args = p.parse_args()
    configure_logging(args)

    season = args.season or season_for_database(args.clean_db_path) or "unknown"

    # Gate first: exporting a bad season wastes a minute, publishing one is
    # worse, and the whole point is that nobody is watching.
    report = run_quality_checks(args.clean_db_path, season=season)
    if not report.ok:
        print(report.render())
        if not args.skip_checks:
            raise SystemExit(
                "\nRefusing to export: the quality gate failed. "
                "Investigate, or pass --skip-checks to override."
            )
        print("\n--skip-checks given; exporting anyway.\n")
    elif report.warnings:
        for w in report.warnings:
            print(f"warning: {w.name}: {w.message}")

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    result = export_matches_to_parquet(
        args.clean_db_path,
        str(out / "data"),
        season=season,
        compression=args.compression if args.compression != "none" else None,
    )

    meta = compute_season_metadata(args.clean_db_path, season_label=season)
    export_info = {**asdict(result), "compression_ratio": round(result.compression_ratio, 2),
                   "compression": args.compression,
                   "exported_utc": datetime.now(tz=UTC).isoformat()}
    # The partition list can run to hundreds of entries; the count is the useful part.
    export_info["partitions"] = len(result.partitions)

    (out / "metadata.json").write_text(
        json.dumps(
            {"season": meta, "export": export_info, "quality": report.to_dict()},
            indent=2, ensure_ascii=False,
        )
    )
    (out / "README.md").write_text(
        render_dataset_card(meta, export=export_info, repo_id=args.repo_id)
    )

    if args.with_sqlite:
        export_clean_sqlite(args.clean_db_path, str(out / f"{season}.db"))

    print(json.dumps({
        "season": season,
        "rows": result.rows,
        "files": result.files,
        "parquet_mb": round(result.parquet_bytes / 1e6, 1),
        "source_mb": round(result.source_bytes / 1e6, 1),
        "compression_ratio": round(result.compression_ratio, 2),
        "out_dir": str(out),
    }, indent=2))


if __name__ == "__main__":
    main()
