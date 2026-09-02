"""The shape a ranked set's record can take.

Lives here rather than in the quality package because both ingestion and the
gate need it: ingestion drops records that describe an impossible set, and the
gate watches the rate at which that happens.
"""
from __future__ import annotations

RECORD_TOKENS = frozenset({"T1", "T2", "D"})


def record_is_well_formed(record: str | None) -> bool:
    """Whether a record could describe a real ranked set.

    A set is first-to-two-wins. Draws do not count toward the two, so a set can
    legitimately run past three games: `D-T1-T1` is normal. What cannot happen
    is a game *after* one team reaches two wins — the set is over.

    Records with fewer than two wins are partial sets, cut off by the ~25-battle
    log window. These are normal and common: a bare `T1` or `T2` is roughly a
    quarter of all rows, so treating them as malformed would discard a huge
    slice of legitimate data.
    """
    if not record:
        return False
    tokens = record.split("-")
    if any(t not in RECORD_TOKENS for t in tokens):
        return False
    wins = {"T1": 0, "T2": 0}
    for i, token in enumerate(tokens):
        if token == "D":
            continue
        wins[token] += 1
        if wins[token] == 2:
            return i == len(tokens) - 1
    return max(wins.values()) <= 1
