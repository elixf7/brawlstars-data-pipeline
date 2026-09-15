"""Which ranked season a match belongs to.

Since the Ranked 2.0 rework in February 2025, a season starts on the third
Thursday of each month, at 09:00 UTC. That makes seasons arithmetic rather than
something to infer: they are known in advance, need no threshold tuning, and are
correct on an empty database.

The time of day is not decoration. Treating the boundary as midnight pulls the
nine hours of pre-reset play on reset day into the new season — matches at the
previous season's elo, landing in the first `skill_ns` bin of the next.

Getting this wrong is expensive and quiet. A database spanning a reset mixes two
elo regimes under one label, and `skill_ns` bins that straddle the boundary
compute percentiles over a bimodal population — plausible-looking numbers, no
error anywhere. The season42 fixture in this repository has exactly that.

If Supercell changes the schedule, add the affected seasons to OVERRIDES.
"""
from __future__ import annotations

import calendar
import sqlite3
from datetime import UTC, date, datetime, time

#: A season number and the date it began. Everything else is offset from here.
#: season49 began on 2026-04-16, the third Thursday of April 2026.
ANCHOR_NUMBER = 49
ANCHOR_START = date(2026, 4, 16)

#: Seasons that did not start on the third Thursday. Add entries here when the
#: schedule changes; no other code needs to move.
OVERRIDES: dict[int, date] = {}

_THURSDAY = 3

#: Hour (UTC) at which ranked resets on its start date. Supercell announces the
#: rollover as 9 AM UTC, and the data agrees: across the October 2025 reset, the
#: mean average elo of a match runs 15-16 through the 15th and is 2.1 by 09:00
#: on the 16th — the whole ladder back at the floor.
RESET_UTC_HOUR = 9


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


def season_start_instant(number: int) -> datetime:
    """The exact moment a season begins."""
    return datetime.combine(season_start(number), time(RESET_UTC_HOUR), tzinfo=UTC)


def season_number_at(when: date | datetime) -> int:
    """The season containing `when`.

    A datetime is resolved against the reset instant, a plain date against the
    reset day. Pass a datetime for anything that decides what a match belongs
    to, or which season is live — on reset day those differ by nine hours, and
    a date cannot tell the two sides of the boundary apart.
    """
    # datetime is a subclass of date, so it has to be checked first.
    if isinstance(when, datetime):
        at: date | datetime = when if when.tzinfo else when.replace(tzinfo=UTC)
        boundary = season_start_instant
        day = when.date()
    else:
        at, boundary, day = when, season_start, when

    n = ANCHOR_NUMBER + (
        (day.year * 12 + day.month) - (ANCHOR_START.year * 12 + ANCHOR_START.month)
    )
    # A date early in a month still belongs to the previous season, since the
    # rollover is mid-month. Walk to the season that actually contains it.
    while at < boundary(n):
        n -= 1
    while at >= boundary(n + 1):
        n += 1
    return n


def season_label(number: int) -> str:
    return f"season{number}"


def season_bounds(number: int) -> tuple[date, date]:
    """Half-open [start, end) — end is the next season's start."""
    return season_start(number), season_start(number + 1)


def current_season(now: date | datetime | None = None) -> str:
    """The season live right now, in UTC.

    Resolved to the instant, not the day: a run that fires on reset morning
    before 09:00 belongs to the season that is ending, and must keep collecting
    it rather than opening an empty database for a season that has not started.
    """
    return season_label(season_number_at(now or datetime.now(UTC)))


def parse_battle_instant(battle_time: str | None) -> datetime | None:
    """`20251102T002849.000Z` -> an aware datetime. None when unparseable.

    Falls back to midnight when only a date is present, which is the safe
    direction: an undated reset-day match reads as the season that is ending.
    """
    d = parse_battle_date(battle_time)
    if d is None:
        return None
    if battle_time and len(battle_time) >= 15 and battle_time[9:15].isdigit():
        try:
            return datetime.strptime(battle_time[:15], "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
        except ValueError:
            pass
    return datetime.combine(d, time(0), tzinfo=UTC)


def parse_battle_date(battle_time: str | None) -> date | None:
    """`20251102T002849.000Z` -> date. None when unparseable."""
    if not battle_time or len(battle_time) < 8 or not battle_time[:8].isdigit():
        return None
    try:
        return datetime.strptime(battle_time[:8], "%Y%m%d").date()
    except ValueError:
        return None


def season_for_battle_time(battle_time: str) -> str | None:
    at = parse_battle_instant(battle_time)
    return None if at is None else season_label(season_number_at(at))


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
    return season_start_instant(number).strftime("%Y-%m-%dT%H:%M:%SZ")


def season_bounds_stamps(number: int) -> tuple[str, str]:
    """Half-open [start, end) as `battle_time` strings.

    `battle_time` is fixed-width (`20251102T002849.000Z`), so lexicographic
    comparison against these is chronological comparison, and SQLite can answer
    a range over them straight from the index.
    """
    fmt = "%Y%m%dT%H%M%S.000Z"
    return (season_start_instant(number).strftime(fmt),
            season_start_instant(number + 1).strftime(fmt))


def seasons_spanned(db_path: str) -> list[str]:
    """Every season present in a database. More than one means it spans a reset.

    Read from the earliest and latest match rather than from every distinct day.
    Seasons are contiguous intervals, so anything between two matches of the
    same season is also in it — and a day string cannot say which side of a
    09:00 reset a match fell on, which is exactly the case this has to get
    right.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT MIN(battle_time), MAX(battle_time) FROM matches "
            "WHERE battle_time IS NOT NULL"
        ).fetchone()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
    if not row or not row[0]:
        return []
    first, last = (parse_battle_instant(row[0]), parse_battle_instant(row[1]))
    if first is None or last is None:
        return []
    lo, hi = season_number_at(first), season_number_at(last)
    return [season_label(n) for n in range(lo, hi + 1)]


def days_until_next_season(now: date | datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    day = now.date() if isinstance(now, datetime) else now
    return (season_start(season_number_at(now) + 1) - day).days


__all__ = [
    "ANCHOR_NUMBER", "ANCHOR_START", "OVERRIDES",
    "current_season", "days_until_next_season", "parse_battle_date",
    "parse_battle_instant", "RESET_UTC_HOUR", "season_start_instant",
    "season_bounds", "season_for_battle_time", "season_for_database",
    "season_label", "season_number_at", "season_start", "season_start_iso",
    "seasons_spanned", "third_thursday",
]
