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

    prefixes = _slot_prefixes()
    union = " UNION ALL ".join(
        f"SELECT {p}_tag AS tag, {p}_elo AS elo FROM matches "
        f"WHERE {p}_tag IS NOT NULL AND {p}_elo >= :min_elo"
        for p in prefixes
    )
    params: dict[str, object] = {"min_elo": min_elo, "limit": limit}

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


__all__ = ["high_elo_tags", "sample_seed_tags_from_clean_db"]


