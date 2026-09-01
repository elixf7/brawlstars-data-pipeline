import os
from typing import Optional


def get_api_key(env_var: str = "BS_API_KEY", fallback_env_vars: Optional[list] = None) -> str:
    """Return the Brawl Stars API key from environment variables.

    Looks for `env_var` first (default: BS_API_KEY). Optionally checks
    a list of fallback environment variable names (e.g., ["BRAWLSTARS_API_KEY"]).

    Raises
    ------
    RuntimeError
        If no API key is found in the provided env var names.
    """
    if fallback_env_vars is None:
        fallback_env_vars = ["BRAWLSTARS_API_KEY"]

    value = os.environ.get(env_var)
    if value:
        return value

    for name in fallback_env_vars:
        value = os.environ.get(name)
        if value:
            return value

    raise RuntimeError(
        "Missing API key. Please set BS_API_KEY in your environment (or BRAWLSTARS_API_KEY)."
    )


def ensure_directories(paths: list) -> None:
    """Create directories if they do not exist.

    Parameters
    ----------
    paths : list
        Absolute or relative directory paths to create.
    """
    for path in paths:
        if path and not os.path.exists(path):
            os.makedirs(path, exist_ok=True)


