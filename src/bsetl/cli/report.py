#!/usr/bin/env python3
"""Render the pipeline status page from a season database."""
from __future__ import annotations

import argparse
from pathlib import Path

from bsetl.cli import add_logging_flags, configure_logging
from bsetl.publish.dashboard import write_dashboard, write_status_json


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--clean-db-path", required=True)
    p.add_argument("--out-dir", default="site", help="Directory to write into")
    add_logging_flags(p)
    args = p.parse_args()
    configure_logging(args)

    out = Path(args.out_dir)
    page = write_dashboard(args.clean_db_path, str(out / "index.html"))
    status = write_status_json(args.clean_db_path, str(out / "status.json"))
    print(f"{page}\n{status}")


if __name__ == "__main__":
    main()
