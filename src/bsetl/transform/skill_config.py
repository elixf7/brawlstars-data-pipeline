from __future__ import annotations

import json
import re
from pathlib import Path

"""
Skill feature scope and configuration (minimal, versioned).
"""

# -----------------------------------------------------------------------------
# Versioning and stable identifiers
# -----------------------------------------------------------------------------

# Stable output column name for the feature on `matches`
SKILL_COLUMN: str = "skill_ns"
# Stable coverage/quality flag column on `matches` (1 = coverage OK, 0 = low coverage)
SKILL_COVERAGE_COLUMN: str = "skill_ns_ok"

# Version of the computation pipeline (change only when method/config changes)
SKILL_FEATURE_VERSION: str = "skill_ns_v1"

# -----------------------------------------------------------------------------
# Defaults (centralized)
# -----------------------------------------------------------------------------
BIN_WIDTH_DAYS: int = 3
# No temporal smoothing/merging for computation; keep for compatibility
SMOOTHING_WINDOW_BINS: int = 0
MIN_BIN_COUNT: int = 5_000
EPSILON: float = 1e-3
SHRINKAGE_MIN_N: int = 2_000
RECOMPUTE_CADENCE_DAYS: int = 3


def default_skill_config() -> dict[str, object]:
    """Return a copy of the default configuration as a plain dict."""
    return {
        "skill_column": SKILL_COLUMN,
        "skill_coverage_column": SKILL_COVERAGE_COLUMN,
        "feature_version": SKILL_FEATURE_VERSION,
        "bin_width_days": BIN_WIDTH_DAYS,
        "smoothing_window_bins": SMOOTHING_WINDOW_BINS,
        "min_bin_count": MIN_BIN_COUNT,
        "epsilon": EPSILON,
        "shrinkage_min_n": SHRINKAGE_MIN_N,
        "recompute_cadence_days": RECOMPUTE_CADENCE_DAYS,
        "fallback_strategy": "none",
        "mapping": "normal",
    }


# -----------------------------------------------------------------------------
# Season helpers
# -----------------------------------------------------------------------------

_SEASON_RE = re.compile(r"(season)(\d+)", flags=re.IGNORECASE)


def _extract_season_token(text: str) -> str | None:
    """Extract normalized 'seasonNN' token from arbitrary text; return None if absent."""
    m = _SEASON_RE.search(text)
    if not m:
        return None
    prefix, digits = m.group(1).lower(), m.group(2)
    return f"{prefix}{digits}"


def derive_season_label(clean_db_path: str) -> str:
    """Derive a normalized season label (e.g., 'season42') from a clean DB path."""
    p = Path(clean_db_path).resolve()

    token = _extract_season_token(p.parent.name)
    if token:
        return token

    for part in p.parts:
        token = _extract_season_token(part)
        if token:
            return token

    token = _extract_season_token(p.stem)
    if token:
        return token

    for meta_path in p.parent.glob("*_metadata.json"):
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
            label = str(data.get("season_label", "")).strip()
            token = _extract_season_token(label)
            if token:
                return token
        except Exception:
            continue

    if p.parent.name:
        return p.parent.name
    return "unknown_season"


