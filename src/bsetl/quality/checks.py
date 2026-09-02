"""Checks that stand between a bad crawl and a published dataset.

An automated pipeline publishes whatever it produced. The interesting failures
here are not crashes — those are loud — but the quiet ones: a run that collected
a tenth of its usual volume, a schema change upstream that starts nulling a
column, a skill feature computed against the wrong season. Each check below
exists because that failure mode is real for this data.

Severity is the whole point. Things that make the dataset wrong FAIL. Things
that are merely surprising WARN, because a new brawler ships most seasons and
the pipeline should not stop for it.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from bsetl.logconfig import get_logger
from bsetl.transform.skill_config import SKILL_COLUMN, SKILL_COVERAGE_COLUMN

logger = get_logger(__name__)


class Severity(StrEnum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"


@dataclass(frozen=True)
class CheckResult:
    name: str
    severity: Severity
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Thresholds:
    """Tunable limits. Defaults suit a full season; a mid-season snapshot
    naturally has fewer rows, so `min_rows` is the one to lower."""

    min_rows: int = 1_000
    #: Fraction of rows allowed to have a NULL in a core column.
    max_core_null_rate: float = 0.01
    #: Fraction of rows allowed to have an unparseable battle_time.
    max_unparseable_time_rate: float = 0.001
    #: Elo above this is not something the ladder produces.
    max_plausible_elo: float = 23.0
    #: Minimum share of rows with a trustworthy skill_ns.
    min_skill_coverage: float = 0.80
    #: Warn when a day inside the covered range has no rows at all.
    warn_on_empty_days: bool = True
    #: Distinct brawler names below this suggests truncated ingestion.
    min_distinct_brawlers: int = 60
    #: Share of rows whose `record` cannot describe a real set. Any at all is
    #: worth a warning; a jump means set grouping has broken.
    max_malformed_record_rate: float = 0.001


CORE_COLUMNS = ("battle_time", "mode", "map", "record", "avg_elo")
KNOWN_MODES = frozenset(
    {"brawlBall", "gemGrab", "heist", "bounty", "hotZone", "knockout"}
)
RECORD_TOKENS = frozenset({"T1", "T2", "D"})


def record_is_well_formed(record: str) -> bool:
    """Whether a record could describe a real ranked set.

    A set is first-to-two-wins; draws do not count toward that, so a set can run
    past three games. What cannot happen is a game *after* one team reaches two
    wins — the set is over. Records with fewer than two wins are partial sets
    truncated by the 25-battle log window, which is normal and common: a bare
    `T1` is the second most frequent shape in the data.
    """
    tokens = record.split("-")
    if not tokens or any(t not in RECORD_TOKENS for t in tokens):
        return False
    wins = {"T1": 0, "T2": 0}
    for i, token in enumerate(tokens):
        if token == "D":
            continue
        wins[token] += 1
        if wins[token] == 2:
            return i == len(tokens) - 1
    return max(wins.values()) <= 1


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _columns(conn: sqlite3.Connection) -> set[str]:
    return {r[1] for r in conn.execute("PRAGMA table_info(matches)")}


# --------------------------------------------------------------- structure
def check_schema(conn: sqlite3.Connection, t: Thresholds) -> CheckResult:
    if not _table_exists(conn, "matches"):
        return CheckResult("schema", Severity.FAIL, "No `matches` table")
    missing = set(CORE_COLUMNS) - _columns(conn)
    if missing:
        return CheckResult("schema", Severity.FAIL,
                           f"Missing core column(s): {sorted(missing)}")
    return CheckResult("schema", Severity.OK,
                       f"{len(_columns(conn))} columns, all core fields present")


def check_dedup_index(conn: sqlite3.Connection, t: Thresholds) -> CheckResult:
    """The unique index is what makes re-crawling safe. Without it a rerun
    silently doubles the data instead of being a no-op."""
    names = {r[1] for r in conn.execute("PRAGMA index_list('matches')")}
    if "uniq_matches_key" not in names:
        return CheckResult(
            "dedup_index", Severity.FAIL,
            "Unique index on (battle_time, map, star_player_tag) is absent; "
            "re-crawling this database would duplicate rows",
        )
    return CheckResult("dedup_index", Severity.OK, "Deduplication index present")


def check_no_duplicates(conn: sqlite3.Connection, t: Thresholds) -> CheckResult:
    dupes = conn.execute(
        "SELECT COUNT(*) FROM (SELECT 1 FROM matches "
        "GROUP BY battle_time, map, star_player_tag HAVING COUNT(*) > 1)"
    ).fetchone()[0]
    if dupes:
        return CheckResult("no_duplicates", Severity.FAIL,
                           f"{dupes:,} duplicated set key(s)", {"duplicates": dupes})
    return CheckResult("no_duplicates", Severity.OK, "No duplicate sets")


# ------------------------------------------------------------------ volume
def check_row_count(conn: sqlite3.Connection, t: Thresholds) -> CheckResult:
    n = conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
    if n < t.min_rows:
        return CheckResult("row_count", Severity.FAIL,
                           f"{n:,} rows is below the floor of {t.min_rows:,}",
                           {"rows": n})
    return CheckResult("row_count", Severity.OK, f"{n:,} rows", {"rows": n})


# ------------------------------------------------------------------ content
def check_core_nulls(conn: sqlite3.Connection, t: Thresholds) -> list[CheckResult]:
    total = conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
    if not total:
        return [CheckResult("core_nulls", Severity.SKIP, "No rows")]
    sums = ", ".join(f"SUM({c} IS NULL)" for c in CORE_COLUMNS)
    counts = conn.execute(f"SELECT {sums} FROM matches").fetchone()
    out = []
    for col, nulls in zip(CORE_COLUMNS, counts, strict=True):
        rate = (nulls or 0) / total
        sev = Severity.FAIL if rate > t.max_core_null_rate else Severity.OK
        out.append(CheckResult(
            f"nulls.{col}", sev,
            f"{rate:.3%} null" + ("" if sev is Severity.OK else
                                  f", above the {t.max_core_null_rate:.1%} limit"),
            {"null_rate": round(rate, 6), "nulls": nulls or 0},
        ))
    return out


def check_battle_time_parses(conn: sqlite3.Connection, t: Thresholds) -> CheckResult:
    """Every downstream time operation assumes this format. A silent upstream
    change would thin the skill-feature bins without failing anything."""
    total = conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
    if not total:
        return CheckResult("battle_time_format", Severity.SKIP, "No rows")
    bad = conn.execute(
        "SELECT COUNT(*) FROM matches WHERE battle_time IS NULL "
        "OR length(battle_time) < 15 "
        "OR substr(battle_time, 1, 8) GLOB '*[^0-9]*'"
    ).fetchone()[0]
    rate = bad / total
    sev = Severity.FAIL if rate > t.max_unparseable_time_rate else Severity.OK
    return CheckResult("battle_time_format", sev,
                       f"{bad:,} of {total:,} timestamps unparseable ({rate:.4%})",
                       {"unparseable": bad, "rate": round(rate, 8)})


def check_elo_range(conn: sqlite3.Connection, t: Thresholds) -> CheckResult:
    row = conn.execute(
        "SELECT MIN(avg_elo), MAX(avg_elo), "
        "SUM(avg_elo > ?), SUM(avg_elo < 0) FROM matches WHERE avg_elo IS NOT NULL",
        (t.max_plausible_elo,),
    ).fetchone()
    lo, hi, above, below = row
    if lo is None:
        return CheckResult("elo_range", Severity.SKIP, "No avg_elo values")
    if above or below:
        return CheckResult(
            "elo_range", Severity.FAIL,
            f"{(above or 0) + (below or 0):,} row(s) outside 0..{t.max_plausible_elo}; "
            "the bot/corruption filter did not apply",
            {"above": above or 0, "below": below or 0, "min": lo, "max": hi},
        )
    return CheckResult("elo_range", Severity.OK,
                       f"avg_elo spans {lo:.1f}..{hi:.1f}", {"min": lo, "max": hi})


def check_record_format(conn: sqlite3.Connection, t: Thresholds) -> CheckResult:
    """Malformed records mean two adjacent sets were merged into one.

    Set grouping keys off the star-player marker that appears on a set's last
    game. When that assumption slips, two sets join and the record describes a
    match that could not have been played.
    """
    rows = conn.execute(
        "SELECT record, COUNT(*) FROM matches WHERE record IS NOT NULL GROUP BY record"
    ).fetchall()
    total = sum(c for _, c in rows)
    if not total:
        return CheckResult("record_format", Severity.SKIP, "No records")

    bad = {r: c for r, c in rows if not record_is_well_formed(r)}
    bad_rows = sum(bad.values())
    rate = bad_rows / total
    detail = {
        "distinct_shapes": len(rows),
        "malformed_shapes": len(bad),
        "malformed_rows": bad_rows,
        "rate": round(rate, 8),
        "examples": sorted(bad, key=lambda r: -bad[r])[:5],
    }
    if rate > t.max_malformed_record_rate:
        return CheckResult(
            "record_format", Severity.FAIL,
            f"{bad_rows:,} row(s) ({rate:.4%}) have a record no real set could "
            f"produce, e.g. {detail['examples'][:3]}; set grouping has likely broken",
            detail,
        )
    if bad_rows:
        return CheckResult(
            "record_format", Severity.WARN,
            f"{bad_rows:,} row(s) ({rate:.4%}) have an impossible record, e.g. "
            f"{detail['examples'][:3]} — adjacent sets merged during grouping",
            detail,
        )
    return CheckResult("record_format", Severity.OK,
                       f"{len(rows)} distinct record shapes, all valid", detail)


def check_modes(conn: sqlite3.Connection, t: Thresholds) -> CheckResult:
    seen = {r[0] for r in conn.execute(
        "SELECT DISTINCT mode FROM matches WHERE mode IS NOT NULL")}
    novel = seen - KNOWN_MODES
    if novel:
        # A rotation change is news, not a defect.
        return CheckResult("modes", Severity.WARN,
                           f"Unrecognised mode(s): {sorted(novel)}",
                           {"novel": sorted(novel), "seen": sorted(seen)})
    return CheckResult("modes", Severity.OK, f"{len(seen)} known mode(s)",
                       {"seen": sorted(seen)})


def check_brawler_coverage(conn: sqlite3.Connection, t: Thresholds) -> CheckResult:
    cols = [f"t{team}_b{slot}_name" for team in (1, 2) for slot in range(3)]
    union = " UNION ALL ".join(
        f"SELECT {c} AS n FROM matches WHERE {c} IS NOT NULL" for c in cols)
    n = conn.execute(f"SELECT COUNT(*) FROM (SELECT n FROM ({union}) GROUP BY n)").fetchone()[0]
    if n < t.min_distinct_brawlers:
        return CheckResult("brawler_coverage", Severity.FAIL,
                           f"Only {n} distinct brawlers; expected at least "
                           f"{t.min_distinct_brawlers}. Ingestion may be truncated.",
                           {"distinct": n})
    return CheckResult("brawler_coverage", Severity.OK,
                       f"{n} distinct brawlers", {"distinct": n})


# ------------------------------------------------------------ time coverage
def check_time_coverage(conn: sqlite3.Connection, t: Thresholds) -> CheckResult:
    days = [r[0] for r in conn.execute(
        "SELECT DISTINCT substr(battle_time, 1, 8) FROM matches "
        "WHERE battle_time IS NOT NULL ORDER BY 1") if r[0] and r[0].isdigit()]
    if not days:
        return CheckResult("time_coverage", Severity.SKIP, "No usable timestamps")
    first, last = datetime.strptime(days[0], "%Y%m%d"), datetime.strptime(days[-1], "%Y%m%d")
    span = (last - first).days + 1
    present = set(days)
    missing = [
        (first + timedelta(days=i)).strftime("%Y%m%d")
        for i in range(span)
        if (first + timedelta(days=i)).strftime("%Y%m%d") not in present
    ]
    detail = {"first_day": days[0], "last_day": days[-1],
              "days_covered": len(present), "days_in_span": span,
              "empty_days": missing[:10]}
    if missing and t.warn_on_empty_days:
        return CheckResult("time_coverage", Severity.WARN,
                           f"{len(missing)} day(s) inside {days[0]}..{days[-1]} have "
                           "no rows", detail)
    return CheckResult("time_coverage", Severity.OK,
                       f"{len(present)} day(s), {days[0]}..{days[-1]}", detail)


# ------------------------------------------------------------ skill feature
def check_skill_coverage(conn: sqlite3.Connection, t: Thresholds) -> CheckResult:
    cols = _columns(conn)
    if SKILL_COLUMN not in cols:
        return CheckResult("skill_coverage", Severity.SKIP,
                           f"{SKILL_COLUMN} not computed for this database")
    total, ok = conn.execute(
        f"SELECT COUNT(*), SUM(COALESCE({SKILL_COVERAGE_COLUMN}, 0)) FROM matches"
    ).fetchone()
    if not total:
        return CheckResult("skill_coverage", Severity.SKIP, "No rows")
    frac = (ok or 0) / total
    sev = Severity.OK if frac >= t.min_skill_coverage else Severity.WARN
    return CheckResult(
        "skill_coverage", sev,
        f"{frac:.1%} of rows have a trustworthy {SKILL_COLUMN}"
        + ("" if sev is Severity.OK else f", below {t.min_skill_coverage:.0%}"),
        {"coverage": round(frac, 4), "rows_ok": ok or 0},
    )


def check_skill_distribution(conn: sqlite3.Connection, t: Thresholds) -> CheckResult:
    if SKILL_COLUMN not in _columns(conn):
        return CheckResult("skill_distribution", Severity.SKIP, "Not computed")
    row = conn.execute(
        f"SELECT AVG({SKILL_COLUMN}), MIN({SKILL_COLUMN}), MAX({SKILL_COLUMN}) "
        f"FROM matches WHERE {SKILL_COVERAGE_COLUMN} = 1"
    ).fetchone()
    mean, lo, hi = row
    if mean is None:
        return CheckResult("skill_distribution", Severity.SKIP, "No covered rows")
    # A percentile-based score mapped symmetrically should centre near zero.
    # Drift means the ECDF was built over the wrong population.
    sev = Severity.OK if abs(mean) < 0.25 else Severity.WARN
    return CheckResult(
        "skill_distribution", sev,
        f"mean {mean:+.3f}, range {lo:.2f}..{hi:.2f}"
        + ("" if sev is Severity.OK else "; expected to centre near zero"),
        {"mean": round(mean, 4), "min": round(lo, 3), "max": round(hi, 3)},
    )


def check_skill_provenance(
    conn: sqlite3.Connection, t: Thresholds, season: str | None = None
) -> CheckResult:
    """The skill feature must be labelled with the season it was computed over.

    This is not hypothetical: a sidecar in this repository is stamped season42
    while its bins cover season43's dates. Nothing caught it, because nothing
    was looking.
    """
    if not _table_exists(conn, "skill_bin_metadata"):
        return CheckResult("skill_provenance", Severity.SKIP, "No skill metadata")

    seasons = [r[0] for r in conn.execute(
        "SELECT DISTINCT season FROM skill_bin_metadata WHERE season IS NOT NULL")]
    if len(seasons) > 1:
        return CheckResult("skill_provenance", Severity.FAIL,
                           f"Skill metadata claims multiple seasons: {sorted(seasons)}",
                           {"seasons": sorted(seasons)})
    if season and seasons and seasons[0] != season:
        return CheckResult(
            "skill_provenance", Severity.FAIL,
            f"Skill metadata is labelled {seasons[0]!r} but this database is "
            f"{season!r}", {"labelled": seasons[0], "expected": season},
        )

    # Bins must actually overlap the data they claim to describe.
    span = conn.execute(
        "SELECT MIN(substr(battle_time,1,8)), MAX(substr(battle_time,1,8)) FROM matches"
    ).fetchone()
    bins = conn.execute(
        "SELECT MIN(bin_start_utc), MAX(bin_end_utc) FROM skill_bin_metadata"
    ).fetchone()
    if span[0] and bins[0]:
        data_lo = f"{span[0][:4]}-{span[0][4:6]}-{span[0][6:8]}"
        data_hi = f"{span[1][:4]}-{span[1][4:6]}-{span[1][6:8]}"
        bin_lo, bin_hi = bins[0][:10], bins[1][:10]
        if bin_hi < data_lo or bin_lo > data_hi:
            return CheckResult(
                "skill_provenance", Severity.FAIL,
                f"Skill bins cover {bin_lo}..{bin_hi} but the data covers "
                f"{data_lo}..{data_hi}; the feature was computed elsewhere",
                {"bins": [bin_lo, bin_hi], "data": [data_lo, data_hi]},
            )
    return CheckResult("skill_provenance", Severity.OK,
                       f"Skill metadata consistent ({seasons[0] if seasons else 'unlabelled'})")


CHECKS = (
    check_schema,
    check_dedup_index,
    check_row_count,
    check_no_duplicates,
    check_core_nulls,
    check_battle_time_parses,
    check_elo_range,
    check_record_format,
    check_modes,
    check_brawler_coverage,
    check_time_coverage,
    check_skill_coverage,
    check_skill_distribution,
)
