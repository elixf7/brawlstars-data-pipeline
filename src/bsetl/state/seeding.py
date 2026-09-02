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


__all__ = ["sample_seed_tags_from_clean_db"]


