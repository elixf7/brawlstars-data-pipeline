"""Uploading a prepared season directory to the Hugging Face Hub."""
from __future__ import annotations

import os
from pathlib import Path

from bsetl.logconfig import get_logger

logger = get_logger(__name__)


class PublishError(RuntimeError):
    pass


def resolve_token(token: str | None = None) -> str:
    token = token or os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_TOKEN")
    if not token:
        raise PublishError(
            "No Hugging Face token. Set HF_TOKEN (see .env.example), or pass --token."
        )
    return token


#: Files under data/ that predate season partitioning. The exporter cannot
#: produce this shape any more, so removing it on publish migrates the dataset
#: without a separate step.
LEGACY_PREFIXES = ("data/battle_date=**",)


def _delete_patterns(season: str | None) -> list[str] | None:
    """Remote paths this publish is allowed to remove.

    Scoped to the season being published plus the retired flat layout, so other
    seasons are never touched.
    """
    if season is None:
        return None
    return [f"data/season={season}/**", *LEGACY_PREFIXES]


def remote_seasons(repo_id: str, *, token: str | None = None) -> list[str]:
    """Seasons already published to `repo_id`, from its file list.

    The card names one config per season, so it has to know what is there —
    and only the Hub knows, since the runner holds a single season's export.

    Answers `[]` rather than raising on any failure: not being able to list a
    repo is not a reason to fail a publish, and the caller falls back to naming
    the season it is publishing. Missing an archived season from the config
    list is cosmetic and self-corrects on the next run; refusing to publish a
    crawl over it would not be.
    """
    try:
        from huggingface_hub import HfApi

        files = HfApi(token=token).list_repo_files(repo_id=repo_id, repo_type="dataset")
    except Exception as e:  # network, auth, no such repo, huggingface-hub absent
        logger.warning("Could not list %s to find other seasons: %s", repo_id, e)
        return []
    found = set()
    for f in files:
        # data/season=season54/data.parquet
        if f.startswith("data/season=") and f.endswith(".parquet"):
            found.add(f.split("data/season=", 1)[1].split("/", 1)[0])
    return sorted(found)


def push_season(
    local_dir: str,
    repo_id: str,
    *,
    token: str | None = None,
    private: bool = False,
    commit_message: str | None = None,
    season: str | None = None,
) -> str:
    """Upload `local_dir` to a dataset repo, creating it if needed.

    When `season` is given, the remote copy of that season is made to match the
    local export exactly — files no longer produced are removed. Uploading alone
    only ever adds, so a change in layout leaves the old files behind and a
    reader globbing the dataset sees two incompatible partition schemes at once.

    Only that season's prefix is touched, so other seasons are untouched.
    """
    try:
        from huggingface_hub import HfApi
    except ImportError as e:
        raise PublishError(
            "huggingface-hub is not installed. Install the extra: uv sync --extra hub"
        ) from e

    path = Path(local_dir)
    if not path.is_dir():
        raise PublishError(f"Not a directory: {path}")
    if not any(path.rglob("*.parquet")):
        raise PublishError(f"No parquet files under {path}; run bsetl-export first")

    api = HfApi(token=resolve_token(token))
    api.create_repo(repo_id=repo_id, repo_type="dataset", private=private, exist_ok=True)
    logger.info("Uploading %s to dataset repo %s", path, repo_id)
    api.upload_folder(
        folder_path=str(path),
        repo_id=repo_id,
        repo_type="dataset",
        commit_message=commit_message or f"Publish {path.name}",
        delete_patterns=_delete_patterns(season),
    )
    url = f"https://huggingface.co/datasets/{repo_id}"
    logger.info("Published: %s", url)
    return url
