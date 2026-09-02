# Test fixtures

## `season_sample.db`

3,023 ranked sets sampled evenly across season43 (drawn from a database
mislabelled season42 — its matches postdate the 2025-10-16 reset), carrying the real schema and
the `skill_ns` columns plus the `skill_bin_metadata` provenance table. Real
brawler names, modes, maps, records, elo values, and timestamps — all 95
brawlers and all 6 modes appear, spread over 16 days so time-binned code sees
many bins.

**Player tags are pseudonymous.** Each real tag is replaced by a deterministic
BLAKE2s digest, so uniqueness and the `(battle_time, map, star_player_tag)`
dedup key behave exactly as in production while no real in-game identifiers are
committed.

Built with the current schema, so unlike season42 itself it carries the unique
index. It exists so the transform, quality, and export paths are exercised
against realistically shaped data rather than only hand-built rows — synthetic
fixtures agree with whatever assumptions wrote them, which is precisely the
class of bug worth catching.
