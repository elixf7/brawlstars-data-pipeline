"""Command-line entry points. These are the only supported way to run
the pipeline; the notebook is for exploration only."""
from __future__ import annotations

import argparse

from bsetl.logconfig import setup_logging


def add_logging_flags(parser: argparse.ArgumentParser) -> None:
    g = parser.add_mutually_exclusive_group()
    g.add_argument("-v", "--verbose", action="count", default=0,
                   help="Show debug detail")
    g.add_argument("-q", "--quiet", action="store_true",
                   help="Only warnings and errors")


def configure_logging(args: argparse.Namespace) -> None:
    setup_logging(verbosity=getattr(args, "verbose", 0),
                  quiet=getattr(args, "quiet", False))


__all__ = ["add_logging_flags", "configure_logging"]
