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
from bsetl.transform.schema import schema_drift

logger = get_logger(__name__)

#: Repo prefix for working state. Deliberately outside `data/`, which is what
#: the dataset card's config globs.
STATE_PREFIX = "state"


def state_path(season: str) -> str:
    return f"{STATE_PREFIX}/{season}.db"


def _schema_is_current(db_path: Path, season: str) -> bool:
    """Whether stored state can still be crawled into.

    A schema change makes prior state unusable: the crawl builds rows to the
    current column list, and the gate rejects a database that does not match it.
    Resuming anyway would either fail on insert or publish the wrong columns, so
    the run starts the season over instead. That loses the frontier and the
    rows collected so far, which is the intended cost of changing the schema —
    and it is a decision the pipeline should make on its own rather than
    requiring someone to delete a file on the Hub.
    """
    import sqlite3

    try:
        conn = sqlite3.connect(db_path)
        try:
            missing, stale = schema_drift(conn)
        finally:
            conn.close()
    except sqlite3.DatabaseError as e:
        logger.warning(
            "Stored state for %s is not a readable database (%s); "
            "discarding it and starting the season over", season, e,
        )
        return False
    if not missing and not stale:
        return True
    logger.warning(
        "Stored state for %s predates the current schema (missing %s, stale %s); "
        "discarding it and starting the season over",
        season, missing or "nothing", stale or "nothing",
    )
    return False


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

    if not _schema_is_current(dest_path, season):
        dest_path.unlink()
        return False

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

    # Vacuum before uploading. This file is pushed on every run and the Hub
    # keeps every version, so pages freed by deletes and rebuilt indexes would
    # otherwise be re-uploaded week after week.
    before = src.stat().st_size
    try:
        import sqlite3
        conn = sqlite3.connect(src)
        try:
            conn.execute("VACUUM")
        finally:
            conn.close()
        after = src.stat().st_size
        if after < before:
            logger.info("Vacuumed %s: %.1f MB -> %.1f MB",
                        src.name, before / 1e6, after / 1e6)
    except Exception as e:
        logger.warning("Could not vacuum %s before upload: %s", src.name, e)

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


def squash_history(repo_id: str, *, token: str | None = None) -> None:
    """Collapse the dataset repo's commit history into a single commit.

    The working database is pushed on every run and grows all season, and the
    Hub keeps every version — so the stored revisions of `state/` come to dwarf
    the dataset itself. Squashing discards those old *versions*.

    It does not discard data. Every file present at HEAD stays exactly as it is,
    and each season lives at its own path, so published seasons are unaffected.
    What is lost is the ability to check out an earlier commit.
    """
    try:
        from huggingface_hub import HfApi
    except ImportError as e:
        raise PublishError(
            "huggingface-hub is not installed. Install the extra: uv sync --extra hub"
        ) from e

    api = HfApi(token=resolve_token(token))
    api.super_squash_history(repo_id=repo_id, repo_type="dataset")
    logger.info("Squashed commit history for %s; current files are unchanged", repo_id)
