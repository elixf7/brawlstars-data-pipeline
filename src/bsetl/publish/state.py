"""Carrying the working database between runs.

CI runners are ephemeral: nothing on disk survives a job. But the whole design
of a bounded crawl assumes continuity — the frontier is what the next run picks
up, `fetched_tags` is what stops it re-fetching, and the unique index is what
makes re-crawling a no-op. Start each run from an empty database and every one
of those guarantees is lost.

So the working database is stored alongside the published dataset, under a
`state/` prefix that the dataset's data config does not match. It is pipeline
state, versioned by the same mechanism as the data it produced, and never
presented as part of it.
"""
from __future__ import annotations

from pathlib import Path

from bsetl.logconfig import get_logger
from bsetl.publish.hub import PublishError, resolve_token

logger = get_logger(__name__)

#: Repo prefix for working state. Deliberately outside `data/`, which is what
#: the dataset card's config globs.
STATE_PREFIX = "state"


def state_path(season: str) -> str:
    return f"{STATE_PREFIX}/{season}.db"


def pull_state(
    repo_id: str, season: str, dest: str, *, token: str | None = None
) -> bool:
    """Fetch the working database for `season` into `dest`.

    Returns False when the season has no stored state yet — the first run of a
    season is expected to start empty, and that is not an error.
    """
    try:
        from huggingface_hub import hf_hub_download
        from huggingface_hub.errors import EntryNotFoundError, RepositoryNotFoundError
    except ImportError as e:
        raise PublishError(
            "huggingface-hub is not installed. Install the extra: uv sync --extra hub"
        ) from e

    dest_path = Path(dest)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        cached = hf_hub_download(
            repo_id=repo_id,
            repo_type="dataset",
            filename=state_path(season),
            token=resolve_token(token),
        )
    except (EntryNotFoundError, RepositoryNotFoundError):
        logger.info("No stored state for %s yet; starting a new season database", season)
        return False

    # Copy out of the cache: the crawl writes to this file, and mutating a
    # cached blob in place would corrupt the cache for later downloads.
    dest_path.write_bytes(Path(cached).read_bytes())
    logger.info("Restored %s (%.1f MB) from %s",
                season, dest_path.stat().st_size / 1e6, repo_id)
    return True


def push_state(
    repo_id: str, season: str, db_path: str, *, token: str | None = None
) -> str:
    """Store the working database so the next run can resume from it."""
    try:
        from huggingface_hub import HfApi
    except ImportError as e:
        raise PublishError(
            "huggingface-hub is not installed. Install the extra: uv sync --extra hub"
        ) from e

    src = Path(db_path)
    if not src.exists():
        raise PublishError(f"No database to store: {src}")

    api = HfApi(token=resolve_token(token))
    api.create_repo(repo_id=repo_id, repo_type="dataset", exist_ok=True)
    api.upload_file(
        path_or_fileobj=str(src),
        path_in_repo=state_path(season),
        repo_id=repo_id,
        repo_type="dataset",
        commit_message=f"Update {season} working state",
    )
    logger.info("Stored %s (%.1f MB) to %s", season, src.stat().st_size / 1e6, repo_id)
    return state_path(season)
