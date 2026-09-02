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


def push_season(
    local_dir: str,
    repo_id: str,
    *,
    token: str | None = None,
    private: bool = False,
    commit_message: str | None = None,
) -> str:
    """Upload `local_dir` to a dataset repo, creating it if needed.

    Returns the dataset URL.
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
    )
    url = f"https://huggingface.co/datasets/{repo_id}"
    logger.info("Published: %s", url)
    return url
