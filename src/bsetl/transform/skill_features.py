from __future__ import annotations

import bisect
import math
import sqlite3
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from bsetl.logconfig import get_logger

from .skill_config import (
    BIN_WIDTH_DAYS,
    MIN_BIN_COUNT,
)

# -----------------------------------------------------------------------------
# Parsing and base bin assignment
# -----------------------------------------------------------------------------

_KNOWN_TS_FORMATS: tuple[str, ...] = (
    "%Y%m%dT%H%M%S.%fZ",  # e.g., 20250310T025416.000Z
    "%Y%m%dT%H%M%SZ",     # e.g., 20250310T025416Z
)


def parse_battle_time_utc(ts: str) -> datetime:
    """Parse battle_time (TEXT) into a timezone-aware UTC datetime.

    Supports the repository's canonical formats. Raises ValueError if all
    strategies fail.
    """
    if ts is None:
        raise ValueError("battle_time is None")
    s = ts.strip()
    for fmt in _KNOWN_TS_FORMATS:
        try:
            dt = datetime.strptime(s, fmt)
            return dt.replace(tzinfo=UTC)
        except ValueError:
            pass
    # Last resort: try ISO-8601 after replacing Z
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s).astimezone(UTC)
    except Exception as e:
        raise ValueError(f"Unable to parse battle_time: {ts}") from e


def floor_to_utc_midnight(dt: datetime) -> datetime:
    """Return dt truncated to 00:00:00 UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return datetime(dt.year, dt.month, dt.day, tzinfo=UTC)


def base_bin_start(dt: datetime, base_width_days: int = BIN_WIDTH_DAYS) -> datetime:
    """Return the start of the base bin (width in days) anchored at Unix epoch.

    - Floor to UTC midnight
    - Compute number of days since epoch, then floor to multiple of base_width_days
    - Return start datetime with tz UTC
    """
    if base_width_days <= 0:
        raise ValueError("base_width_days must be positive")
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    day0 = floor_to_utc_midnight(dt)
    delta_days = (day0 - epoch).days
    k = (delta_days // base_width_days) * base_width_days
    return epoch + timedelta(days=k)

logger = get_logger(__name__)


# -----------------------------------------------------------------------------
# Fixed-bin value collection (no merging)
# -----------------------------------------------------------------------------

def _iter_time_and_avg_elo(conn: sqlite3.Connection, batch_size: int = 100_000) -> Iterable[tuple[str, float]]:
    cur = conn.execute(
        "SELECT battle_time, avg_elo FROM matches WHERE battle_time IS NOT NULL AND avg_elo IS NOT NULL"
    )
    while True:
        rows = cur.fetchmany(batch_size)
        if not rows:
            break
        for (ts, avg) in rows:
            yield ts, float(avg)


def collect_avg_elo_by_bin(
    clean_db_path: str,
    base_width_days: int = BIN_WIDTH_DAYS,
    batch_size: int = 100_000,
) -> dict[datetime, list[float]]:
    """Return mapping of base-bin start -> list of avg_elo values (unsorted)."""
    db_path = Path(clean_db_path)
    if not db_path.exists():
        raise RuntimeError(f"Clean DB not found: {db_path}")
    values: dict[datetime, list[float]] = defaultdict(list)
    unparseable = 0
    conn = sqlite3.connect(str(db_path))
    try:
        for ts, avg in _iter_time_and_avg_elo(conn, batch_size=batch_size):
            try:
                dt = parse_battle_time_utc(ts)
            except ValueError:
                unparseable += 1
                continue
            start = base_bin_start(dt, base_width_days=base_width_days)
            values[start].append(avg)
    finally:
        conn.close()
    if unparseable:
        # An upstream timestamp format change would otherwise quietly thin every
        # bin, biasing the percentiles without failing anything.
        logger.warning(
            "%d row(s) had an unparseable battle_time and were excluded from the ECDF",
            unparseable,
        )
    return dict(values)


# -----------------------------------------------------------------------------
# ECDF utilities (midrank) and mapping
# -----------------------------------------------------------------------------

def build_bin_stats(
    avg_elo_by_bin: dict[datetime, list[float]],
    *,
    min_bin_count: int = MIN_BIN_COUNT,
) -> dict[datetime, dict[str, object]]:
    """Return {bin_start: {'values': sorted_values, 'n': int, 'coverage_ok': bool}}."""
    out: dict[datetime, dict[str, object]] = {}
    for bstart, vals in avg_elo_by_bin.items():
        sorted_vals = sorted(vals)
        n = len(sorted_vals)
        out[bstart] = {
            "values": sorted_vals,
            "n": n,
            "coverage_ok": n >= min_bin_count,
        }
    return out


def midrank_percentile(value: float, sorted_values: list[float]) -> float:
    """Return midrank percentile p in (0,1) using (rank - 0.5) / (n + 1)."""
    n = len(sorted_values)
    if n == 0:
        return 0.5
    left = bisect.bisect_left(sorted_values, value)
    right = bisect.bisect_right(sorted_values, value)
    # Average rank position for ties
    mid_rank = (left + right + 1) / 2.0
    return (mid_rank - 0.5) / (n + 1.0)


def _norm_ppf_approx(p: float) -> float:
    """Approximate inverse standard normal CDF (Acklam's method).

    Accurate to ~1e-9 across (0,1). Avoids SciPy dependency.
    """
    if p <= 0.0 or p >= 1.0:
        raise ValueError("p must be in (0, 1)")
    # Coefficients for Acklam's approximation
    a = [ -3.969683028665376e+01,
          2.209460984245205e+02,
         -2.759285104469687e+02,
          1.383577518672690e+02,
         -3.066479806614716e+01,
          2.506628277459239e+00 ]
    b = [ -5.447609879822406e+01,
          1.615858368580409e+02,
         -1.556989798598866e+02,
          6.680131188771972e+01,
         -1.328068155288572e+01 ]
    c = [ -7.784894002430293e-03,
         -3.223964580411365e-01,
         -2.400758277161838e+00,
         -2.549732539343734e+00,
          4.374664141464968e+00,
          2.938163982698783e+00 ]
    d = [ 7.784695709041462e-03,
          3.224671290700398e-01,
          2.445134137142996e+00,
          3.754408661907416e+00 ]
    pl = 0.02425
    ph = 1 - pl
    if p < pl:
        q = math.sqrt(-2*math.log(p))
        return (((((c[0]*q + c[1])*q + c[2])*q + c[3])*q + c[4])*q + c[5]) / \
               ((((d[0]*q + d[1])*q + d[2])*q + d[3])*q + 1)
    if ph < p:
        q = math.sqrt(-2*math.log(1-p))
        return -(((((c[0]*q + c[1])*q + c[2])*q + c[3])*q + c[4])*q + c[5]) / \
                 ((((d[0]*q + d[1])*q + d[2])*q + d[3])*q + 1)
    q = p - 0.5
    r = q*q
    return (((((a[0]*r + a[1])*r + a[2])*r + a[3])*r + a[4])*r + a[5]) * q / \
           (((((b[0]*r + b[1])*r + b[2])*r + b[3])*r + b[4])*r + 1)


def percentile_to_normal_score(p: float, epsilon: float) -> float:
    """Map percentile to normal score with clipping ε."""
    pp = min(1.0 - epsilon, max(epsilon, p))
    return _norm_ppf_approx(pp)


def percentile_to_logit_score(p: float, epsilon: float) -> float:
    """Map percentile to logit with clipping ε: log(p/(1-p))."""
    pp = min(1.0 - epsilon, max(epsilon, p))
    return math.log(pp / (1.0 - pp))


def collect_global_avg_elo(
    clean_db_path: str,
    batch_size: int = 100_000,
) -> list[float]:
    """Return sorted list of all avg_elo values."""
    db_path = Path(clean_db_path)
    if not db_path.exists():
        raise RuntimeError(f"Clean DB not found: {db_path}")
    vals: list[float] = []
    conn = sqlite3.connect(str(db_path))
    try:
        for _, avg in _iter_time_and_avg_elo(conn, batch_size=batch_size):
            vals.append(avg)
    finally:
        conn.close()
    vals.sort()
    return vals


