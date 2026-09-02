"""Logging setup.

Named logconfig rather than logging so that `import logging` inside this
package unambiguously means the standard library.

Library code logs and never prints. A scheduled run's only account of itself is
its log, so the messages have to carry levels a reader can filter on: routine
progress at DEBUG, things a person should look at at WARNING, and nothing on
stdout that a caller might be parsing.
"""
from __future__ import annotations

import logging
import sys

LOGGER_NAME = "bsetl"
_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_DATEFMT = "%H:%M:%S"


def setup_logging(verbosity: int = 0, quiet: bool = False) -> None:
    """Configure the package logger. -v gives DEBUG, -q limits to warnings."""
    if quiet:
        level = logging.WARNING
    elif verbosity >= 1:
        level = logging.DEBUG
    else:
        level = logging.INFO

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    logger.handlers.clear()
    # stderr, so stdout stays clean for the JSON run summary.
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))
    logger.addHandler(handler)
    logger.propagate = False


def get_logger(name: str) -> logging.Logger:
    """Return a child of the package logger, e.g. get_logger(__name__)."""
    suffix = name.removeprefix("bsetl.").removeprefix("bsetl")
    return logging.getLogger(f"{LOGGER_NAME}.{suffix}" if suffix else LOGGER_NAME)


def progress_enabled() -> bool:
    """Whether to draw progress bars.

    tqdm redraws with carriage returns, which a CI log renders as thousands of
    unreadable lines. Only draw when a person is actually watching.
    """
    return sys.stderr.isatty()
