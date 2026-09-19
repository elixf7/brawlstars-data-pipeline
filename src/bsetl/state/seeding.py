from __future__ import annotations

import sqlite3


def sample_seed_tags_from_clean_db(
    clean_db_path: str,
    num_tags: int,
    elo_range: tuple[int, int] | None = None,
) -> list[str]:
    """Sample distinct `star_player_tag` values from the clean `matches` table.

    Parameters
    ----------
    clean_db_path : str
        Path to the clean SQLite database (with table `matches`).
    num_tags : int
        Desired number of unique seed tags to return.
    elo_range : Optional[Tuple[int, int]]
        Optional (min_elo, max_elo) range filter on `star_elo`.

    Returns
    -------
    List[str]
        A list of unique player tags (including leading '#') suitable as seeds.

    Notes
    -----
    Uses GROUP BY + ORDER BY RANDOM() + LIMIT N. On very large datasets this
    performs a scan; acceptable for occasional sampling. If you need to sample
    very frequently from 1M+ rows, consider precomputing a materialized list of
    distinct tags and sampling from that instead.
    """
    if num_tags <= 0:
        return []

    where = ["star_player_tag IS NOT NULL"]
    params: list[object] = []
    if elo_range is not None:
        min_elo, max_elo = elo_range
        where.append("star_elo BETWEEN ? AND ?")
        params.extend([min_elo, max_elo])

    where_sql = " AND ".join(where)
    sql = f"""
    SELECT star_player_tag
    FROM matches
    WHERE {where_sql}
    GROUP BY star_player_tag
    ORDER BY RANDOM()
    LIMIT ?
    """
    params.append(num_tags)

    conn = sqlite3.connect(clean_db_path)
    try:
        cur = conn.execute(sql, params)
        rows = cur.fetchall()
        return [r[0] for r in rows if r and r[0]]
    finally:
        conn.close()


def _slot_prefixes() -> list[str]:
    """`t1_b0`, `t1_b1`, ... — one per drafted brawler, from the schema itself."""
    from bsetl.transform.schema import get_brawler_column_names

    return [c[: -len("_tag")] for c in get_brawler_column_names() if c.endswith("_tag")]


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
    )


def _observations_union() -> str:
    """Every (player, elo) observation in `matches`, one slot at a time.

    A set names six players and the elo each was at, so the matches table is
    also a directory of who plays this game and how well. Reading it back out
    means visiting all six brawler slots, and every query below wants the same
    six, bounded by `:min_elo` and `:max_elo`.
    """
    return " UNION ALL ".join(
        f"SELECT {p}_tag AS tag, {p}_elo AS elo FROM matches "
        f"WHERE {p}_tag IS NOT NULL AND {p}_elo IS NOT NULL "
        f"AND {p}_elo >= :min_elo AND (:max_elo IS NULL OR {p}_elo <= :max_elo)"
        for p in _slot_prefixes()
    )


def high_elo_tags(
    clean_db_path: str,
    min_elo: float,
    limit: int,
    stale_before: str | None = None,
) -> list[str]:
    """Player tags observed at `min_elo` or above, highest first.

    Every stored set names all six players and the elo each was at, so the
    database is already a directory of who plays at the top of the ladder. That
    directory is what makes high-elo matches reachable at all: they cannot be
    searched for, but the players in them can be revisited.

    `stale_before` (an ISO timestamp) drops tags fetched since then, leaving the
    ones whose 25-battle window has had time to turn over. Without it the same
    logs are re-fetched for rows already stored.
    """
    if limit <= 0:
        return []

    union = _observations_union()
    params: dict[str, object] = {"min_elo": min_elo, "max_elo": None, "limit": limit}

    conn = sqlite3.connect(f"file:{clean_db_path}?mode=ro", uri=True)
    try:
        if not _has_table(conn, "matches"):
            return []
        join, where = "", ""
        if stale_before and _has_table(conn, "fetched_tags"):
            join = "LEFT JOIN fetched_tags f ON f.tag = s.tag"
            where = "WHERE f.fetched_utc IS NULL OR f.fetched_utc < :stale_before"
            params["stale_before"] = stale_before
        sql = f"""
        SELECT s.tag FROM ({union}) s
        {join}
        {where}
        GROUP BY s.tag
        ORDER BY MAX(s.elo) DESC
        LIMIT :limit
        """
        return [r[0] for r in conn.execute(sql, params) if r and r[0]]
    except sqlite3.DatabaseError:
        return []
    finally:
        conn.close()


#: Share of the drafting-eligible player base the high-elo floor marks out.
#: Calibrated, not chosen: season 53's hand-set floor of 18 selected the top
#: 3.30% of the 1.26M players it had seen at Mythic or above.
HIGH_ELO_SHARE = 0.035

#: Below this many known players, a share is measuring the seed list rather
#: than the ladder, and the configured floor stands.
MIN_ELIGIBLE_POPULATION = 5_000


def elo_population(
    clean_db_path: str, min_elo: float = 0.0, max_elo: float | None = None
) -> dict[int, int]:
    """How many distinct players have been seen at each elo, at their best.

    A player is counted once, at their highest observed elo: that ceiling is
    what makes their battle log worth fetching, and counting every observation
    instead would weight the histogram by how much each player plays.
    """
    conn = sqlite3.connect(f"file:{clean_db_path}?mode=ro", uri=True)
    try:
        if not _has_table(conn, "matches"):
            return {}
        rows = conn.execute(
            f"SELECT m, COUNT(*) FROM "
            f"(SELECT tag, MAX(elo) AS m FROM ({_observations_union()}) GROUP BY tag) "
            f"GROUP BY m",
            {"min_elo": min_elo, "max_elo": max_elo},
        ).fetchall()
        return {int(e): int(n) for e, n in rows if e is not None}
    except sqlite3.DatabaseError:
        return {}
    finally:
        conn.close()


def adaptive_high_elo_floor(
    clean_db_path: str,
    configured: float,
    *,
    eligible_from: float | None = None,
    share: float = HIGH_ELO_SHARE,
    min_population: int = MIN_ELIGIBLE_POPULATION,
) -> float:
    """The elo that marks the top `share` of the ladder *as it stands today*.

    `--high-elo-floor` decides which players are followed past the depth cap
    and which are re-queued each run, and it is the whole reason the frontier
    keeps growing rather than dying at terminal depth. A fixed number cannot do
    that job across a season boundary. Ranked's reset drops everyone about six
    minor ranks and the ladder re-spreads over the following weeks, so the same
    numeric elo describes a different slice of the population depending on when
    it is read — which is precisely why `skill_ns` exists for the data, and the
    crawl needs the same correction for the same reason.

    Season 54 is what a fixed floor costs. Two days in, a floor of 18 matched
    57 players in the entire database; six were re-queued, nothing was exempt
    from the depth cap, the frontier drained to zero, and the run stopped with
    62% of its request budget unspent while still returning 1.9 sets per
    request. A season's opening days cannot be crawled later, so those rows are
    simply gone.

    So the floor is derived from the database rather than configured: the
    lowest elo whose share of the eligible population is still within `share`.
    On season 53 that reproduces 18 exactly; on season 54 at two days old it
    gives 16, which is 2,485 players rather than 57.

    `eligible_from` is the elo the crawl follows players from — below Mythic
    there is no drafting, so those players are not part of the ladder being
    measured and must not dilute the share. The floor never falls below it:
    exempting players the crawl would not follow anyway means nothing.

    Falls back to `configured` when the database cannot answer — an empty one
    on a season's first run, or too few players for a share to mean anything.
    """
    floor = 0.0 if eligible_from is None else float(eligible_from)
    if share <= 0.0:
        return configured
    population = elo_population(clean_db_path, min_elo=floor)
    total = sum(population.values())
    if total < min_population:
        return configured

    cumulative = 0
    answer: int | None = None
    for elo in sorted(population, reverse=True):
        cumulative += population[elo]
        if cumulative / total > share:
            break
        answer = elo
    if answer is None:
        # The top bucket alone is already wider than the share. Nothing below
        # it can be called the top of the ladder, so the ceiling is the floor.
        answer = max(population)
    return float(max(answer, floor))


def refill_tags(
    clean_db_path: str,
    min_elo: float,
    limit: int,
    *,
    max_elo: float | None = None,
    stale_before: str | None = None,
    exclude: set[str] | None = None,
) -> list[tuple[str, int]]:
    """(tag, elo) pairs worth crawling that this run has not touched, best first.

    The same directory `high_elo_tags` reads, asked a broader question: not
    "who is at the top of the ladder" but "who is left". It exists for the
    moment a frontier empties with budget to spare — see the crawler's refill.

    `exclude` is what the run has already queued or fetched, and it is applied
    here rather than in SQL because it is a live set of six figures. The query
    is therefore streamed and abandoned as soon as `limit` new tags are found,
    which on a warm database is long before the end of it.
    """
    if limit <= 0:
        return []

    conn = sqlite3.connect(f"file:{clean_db_path}?mode=ro", uri=True)
    try:
        if not _has_table(conn, "matches"):
            return []
        params: dict[str, object] = {"min_elo": min_elo, "max_elo": max_elo}
        join, where = "", ""
        if stale_before and _has_table(conn, "fetched_tags"):
            join = "LEFT JOIN fetched_tags f ON f.tag = s.tag"
            where = "WHERE f.fetched_utc IS NULL OR f.fetched_utc < :stale_before"
            params["stale_before"] = stale_before
        sql = f"""
        SELECT s.tag, MAX(s.elo) AS m FROM ({_observations_union()}) s
        {join}
        {where}
        GROUP BY s.tag
        ORDER BY m DESC
        """
        skip = exclude or set()
        found: list[tuple[str, int]] = []
        for tag, elo in conn.execute(sql, params):
            if not tag or tag in skip:
                continue
            found.append((tag, int(elo)))
            if len(found) >= limit:
                break
        return found
    except sqlite3.DatabaseError:
        return []
    finally:
        conn.close()


__all__ = [
    "HIGH_ELO_SHARE",
    "MIN_ELIGIBLE_POPULATION",
    "adaptive_high_elo_floor",
    "elo_population",
    "high_elo_tags",
    "refill_tags",
    "sample_seed_tags_from_clean_db",
]


