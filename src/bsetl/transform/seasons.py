"""Which ranked season a match belongs to.

Since the Ranked 2.0 rework in February 2025, a season starts on the third
Thursday of each month. That makes seasons arithmetic rather than something to
infer: they are known in advance, need no threshold tuning, and are correct on
an empty database.

Getting this wrong is expensive and quiet. A database spanning a reset mixes two
elo regimes under one label, and `skill_ns` bins that straddle the boundary
compute percentiles over a bimodal population — plausible-looking numbers, no
error anywhere. The season42 fixture in this repository has exactly that.

If Supercell changes the schedule, add the affected seasons to OVERRIDES.
"""
from __future__ import annotations

import calendar
import sqlite3
from datetime import date, datetime

#: A season number and the date it began. Everything else is offset from here.
#: season49 began on 2026-04-16, the third Thursday of April 2026.
ANCHOR_NUMBER = 49
ANCHOR_START = date(2026, 4, 16)

#: Seasons that did not start on the third Thursday. Add entries here when the
#: schedule changes; no other code needs to move.
OVERRIDES: dict[int, date] = {}

_THURSDAY = 3


def third_thursday(year: int, month: int) -> date:
    """The date ranked seasons roll over in a given month."""
    days = [
        d for d in calendar.Calendar().itermonthdates(year, month)
        if d.month == month and d.weekday() == _THURSDAY
    ]
    return days[2]


def season_start(number: int) -> date:
    if number in OVERRIDES:
        return OVERRIDES[number]
    months = (ANCHOR_START.year * 12 + ANCHOR_START.month - 1) + (number - ANCHOR_NUMBER)
    return third_thursday(months // 12, months % 12 + 1)


def season_number_at(when: date) -> int:
    """The season containing `when`."""
    n = ANCHOR_NUMBER + (
        (when.year * 12 + when.month) - (ANCHOR_START.year * 12 + ANCHOR_START.month)
    )
    # A date early in a month still belongs to the previous season, since the
    # rollover is mid-month. Walk to the season that actually contains it.
    while when < season_start(n):
        n -= 1
    while when >= season_start(n + 1):
        n += 1
    return n


def season_label(number: int) -> str:
    return f"season{number}"


def season_bounds(number: int) -> tuple[date, date]:
    """Half-open [start, end) — end is the next season's start."""
    return season_start(number), season_start(number + 1)


def current_season(now: date | None = None) -> str:
    return season_label(season_number_at(now or datetime.now().date()))


def parse_battle_date(battle_time: str | None) -> date | None:
    """`20251102T002849.000Z` -> date. None when unparseable."""
    if not battle_time or len(battle_time) < 8 or not battle_time[:8].isdigit():
        return None
    try:
        return datetime.strptime(battle_time[:8], "%Y%m%d").date()
    except ValueError:
        return None


def season_for_battle_time(battle_time: str) -> str | None:
    d = parse_battle_date(battle_time)
    return None if d is None else season_label(season_number_at(d))


def season_for_database(db_path: str) -> str | None:
    """The season a database's data belongs to, from its earliest match.

    Read from the data rather than the filename: a path can say anything, and
    in this repository one of them does.
    """
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        return None
    try:
        row = conn.execute(
            "SELECT MIN(battle_time) FROM matches WHERE battle_time IS NOT NULL"
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()
    return season_for_battle_time(row[0]) if row and row[0] else None


def season_start_iso(number: int) -> str:
    """Season start as an ISO instant, for `bsetl-ingest --latest-runtime`.

    Passing this bounds ingestion to the season below: battle logs fetched just
    after a rollover still contain pre-reset matches, and those belong to the
    previous season's database, not this one.
    """
    return f"{season_start(number).isoformat()}T00:00:00Z"


def seasons_spanned(db_path: str) -> list[str]:
    """Every season present in a database. More than one means it spans a reset."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        days = [
            r[0] for r in conn.execute(
                "SELECT DISTINCT substr(battle_time,1,8) FROM matches "
                "WHERE battle_time IS NOT NULL"
            ) if r[0] and r[0].isdigit()
        ]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
    out = set()
    for day in days:
        d = parse_battle_date(day)
        if d:
            out.add(season_number_at(d))
    return [season_label(n) for n in sorted(out)]


def days_until_next_season(now: date | None = None) -> int:
    now = now or datetime.now().date()
    return (season_start(season_number_at(now) + 1) - now).days


__all__ = [
    "ANCHOR_NUMBER", "ANCHOR_START", "OVERRIDES",
    "current_season", "days_until_next_season", "parse_battle_date",
    "season_bounds", "season_for_battle_time", "season_for_database",
    "season_label", "season_number_at", "season_start", "season_start_iso",
    "seasons_spanned", "third_thursday",
]
