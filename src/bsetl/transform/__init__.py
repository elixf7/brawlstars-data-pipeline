"""Schema, derived features, and dataset metadata."""

from bsetl.transform.metadata import (
    compute_season_metadata,
    write_season_metadata,
)
from bsetl.transform.schema import (
    create_matches_table_if_not_exists,
    get_matches_insert_statement,
)
from bsetl.transform.seasons import current_season, season_for_database

__all__ = [
    "create_matches_table_if_not_exists",
    "get_matches_insert_statement",
    "compute_season_metadata",
    "current_season",
    "season_for_database",
    "write_season_metadata",
]
