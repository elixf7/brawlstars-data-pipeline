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

import re
import tempfile
from collections.abc import Iterable
from pathlib import Path

from bsetl.logconfig import get_logger
from bsetl.publish.hub import PublishError, resolve_token
from bsetl.transform.schema import schema_drift

logger = get_logger(__name__)

#: Repo prefix for working state. Deliberately outside `data/`, which is what
#: the dataset card's config globs.
STATE_PREFIX = "state"

#: Matches only the files this module writes, so pruning cannot reach anything
#: else stored under the prefix.
_SEASON_STATE = re.compile(rf"{STATE_PREFIX}/(season\d+)\.db")


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


def download_state(
    repo_id: str, season: str, dest: str, *, token: str | None = None
) -> bool:
    """Copy a season's stored database to `dest`, without judging its schema.

    Returns False when that season has nothing stored.
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
        return False

    # Copy out of the cache: the crawl writes to this file, and mutating a
    # cached blob in place would corrupt the cache for later downloads.
    dest_path.write_bytes(Path(cached).read_bytes())
    return True


def drop_foreign_seasons(db_path: str | Path, season: str) -> int:
    """Delete matches outside `season`'s window. Returns how many went.

    Stored state can hold rows the current rules would not have accepted —
    ingest is bounded by whatever `--latest-runtime` said at the time, and that
    boundary has been wrong before: read as midnight rather than 09:00, it let
    in the nine hours of pre-reset play on reset day.

    The quality gate fails a database that spans a reset, and rightly so. But a
    correction to the season rules must not leave the pipeline stuck behind its
    own gate over rows it now knows are misplaced, so restoring state reconciles
    them rather than waiting for someone to notice a red run.
    """
    import sqlite3

    from bsetl.transform.seasons import season_bounds_stamps

    try:
        number = int(season.removeprefix("season"))
    except ValueError:
        return 0
    lo, hi = season_bounds_stamps(number)
    conn = sqlite3.connect(db_path)
    try:
        n = conn.execute(
            "DELETE FROM matches WHERE battle_time IS NULL "
            "OR battle_time < ? OR battle_time >= ?", (lo, hi),
        ).rowcount
        conn.commit()
    except sqlite3.OperationalError:
        return 0  # no matches table yet
    finally:
        conn.close()
    if n:
        logger.warning(
            "Dropped %d row(s) from restored %s state outside %s..%s; "
            "they belong to an adjacent season", n, season, lo, hi,
        )
    return n


def pull_state(
    repo_id: str, season: str, dest: str, *, token: str | None = None
) -> bool:
    """Fetch the working database for `season` into `dest`.

    Returns False when the season has no stored state yet — the first run of a
    season is expected to start empty, and that is not an error.
    """
    dest_path = Path(dest)
    if not download_state(repo_id, season, dest, token=token):
        logger.info("No stored state for %s yet; starting a new season database", season)
        return False

    if not _schema_is_current(dest_path, season):
        dest_path.unlink()
        return False

    drop_foreign_seasons(dest_path, season)

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


def prune_state(
    repo_id: str, keep: Iterable[str], *, token: str | None = None
) -> list[str]:
    """Delete stored working databases for seasons outside `keep`.

    Squashing history reclaims *old versions* of a file, but a finished season's
    working database is not an old version — it sits at HEAD forever, and a new
    one appears every month. Left alone these come to outweigh the published
    data several times over while being of no use to anyone: the season they
    belong to has already been exported to Parquet and is not crawled again.

    Only `state/season<N>.db` is ever considered, so anything else kept under
    the prefix is left alone. Returns the paths removed.
    """
    try:
        from huggingface_hub import HfApi
    except ImportError as e:
        raise PublishError(
            "huggingface-hub is not installed. Install the extra: uv sync --extra hub"
        ) from e

    keep_set = set(keep)
    api = HfApi(token=resolve_token(token))
    try:
        files = api.list_repo_files(repo_id=repo_id, repo_type="dataset")
    except Exception as e:
        logger.warning("Could not list %s to prune state: %s", repo_id, e)
        return []

    stale = [
        f for f in files
        if (m := _SEASON_STATE.fullmatch(f)) and m.group(1) not in keep_set
    ]
    for path in stale:
        api.delete_file(
            path_in_repo=path,
            repo_id=repo_id,
            repo_type="dataset",
            commit_message=f"Drop working state for {path}; the season is published",
        )
        logger.info("Pruned %s", path)
    if not stale:
        logger.info("No retired season state to prune in %s", repo_id)
    return stale


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


#: How many seasons back to look for a database to seed from. Two, so a season
#: that was somehow never crawled does not break the one after it.
SEED_LOOKBACK = 2


def seeds_for_new_season(
    repo_id: str,
    season: str,
    out: str,
    *,
    min_elo: float,
    limit: int,
    token: str | None = None,
) -> int:
    """Write seed tags for a season that has no stored state yet.

    Ranked resets monthly, and a reset leaves the pipeline with an empty
    database and nowhere to start: there is no endpoint that lists players, so
    without seeds the crawl has nothing to expand from and the first run of
    every season fails. Previously seeds were a committed file that someone had
    to remember to write.

    The previous season's database is the natural source — the same people are
    still playing — and taking the strongest of them means the new season opens
    pointed at the top of the ladder rather than wherever BFS drifts. Elo itself
    resets, so these players start low and climb back; what carries across is
    who they are, not what they were rated.

    A committed `out` file wins, so a season can still be seeded by hand.
    Returns the number of tags written.
    """
    from bsetl.state.seeding import high_elo_tags
    from bsetl.transform.seasons import season_label

    out_path = Path(out)
    if out_path.exists() and out_path.read_text().strip():
        n = len([ln for ln in out_path.read_text().splitlines() if ln.strip()])
        logger.info("Using the committed seed file %s (%d tags)", out_path, n)
        return n

    number = int(season.removeprefix("season"))

    with tempfile.TemporaryDirectory() as tmp:
        for back in range(1, SEED_LOOKBACK + 1):
            previous = season_label(number - back)
            scratch = str(Path(tmp) / f"{previous}.db")
            if not download_state(repo_id, previous, scratch, token=token):
                logger.info("No stored state for %s; looking further back", previous)
                continue
            tags = high_elo_tags(scratch, min_elo, limit)
            if not tags:
                logger.warning("%s held no players at elo >= %s", previous, min_elo)
                continue
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text("\n".join(tags) + "\n")
            logger.info(
                "Seeded %s with %d player(s) at elo >= %s from %s",
                season, len(tags), min_elo, previous,
            )
            return len(tags)

    raise PublishError(
        f"Cannot seed {season}: no stored state in the previous {SEED_LOOKBACK} "
        f"season(s), and no committed seed file at {out_path}. Write one with "
        "bsetl-state seeds --from-db, or commit seeds manually."
    )
