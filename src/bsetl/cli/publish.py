#!/usr/bin/env python3
"""Publish an exported season directory to the Hugging Face Hub."""
from __future__ import annotations

import argparse
from pathlib import Path

from bsetl.cli import add_logging_flags, configure_logging
from bsetl.publish.hub import PublishError, push_season


def main() -> None:
    p = argparse.ArgumentParser(
        description="Upload an exported dataset directory to the Hugging Face Hub. "
                    "Reads HF_TOKEN from the environment."
    )
    p.add_argument("--local-dir", required=True, help="Directory produced by bsetl-export")
    p.add_argument("--repo-id", required=True, help="e.g. your-name/brawlstars-ranked")
    p.add_argument("--token", default=None, help="Overrides HF_TOKEN")
    p.add_argument("--private", action="store_true", help="Create the repo private")
    p.add_argument("--message", default=None, help="Commit message")
    p.add_argument("--yes", action="store_true",
                   help="Actually upload. Without it, only reports what would happen.")
    add_logging_flags(p)
    args = p.parse_args()
    configure_logging(args)

    path = Path(args.local_dir)
    files = sorted(path.rglob("*.parquet")) if path.is_dir() else []
    size_mb = sum(f.stat().st_size for f in files) / 1e6

    if not args.yes:
        # Uploading is public and hard to walk back, so it is never the default.
        print(f"Would upload {len(files)} parquet file(s), {size_mb:.1f} MB")
        print(f"  from: {path}")
        print(f"  to:   https://huggingface.co/datasets/{args.repo_id}"
              f"{' (private)' if args.private else ' (public)'}")
        print("\nRe-run with --yes to publish.")
        return

    try:
        url = push_season(
            str(path), args.repo_id,
            token=args.token, private=args.private, commit_message=args.message,
        )
    except PublishError as e:
        raise SystemExit(f"error: {e}") from None
    print(url)


if __name__ == "__main__":
    main()
