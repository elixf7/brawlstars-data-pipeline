"""Brawl Stars ranked data extractor package."""

from .pull_data_4 import process_tags_and_write_async  # re-export core API

__all__ = [
    "process_tags_and_write_async",
]






