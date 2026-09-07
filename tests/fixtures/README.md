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

The six per-slot `t{t}_b{b}_tag` columns are synthetic. They were added to this
fixture after the fact, and the sets it was drawn from predate the crawl that
collects them, so there is nothing real to carry over. Each set's star tag is
placed in one slot — chosen by digest — and the remaining five are derived from
the set key, which reproduces the property the pipeline relies on: every slot is
identified, and `star_player_tag` joins to exactly one of them. Everything else
in the fixture is real.

Built with the current schema, so unlike season42 itself it carries the unique
index. It exists so the transform, quality, and export paths are exercised
against realistically shaped data rather than only hand-built rows — synthetic
fixtures agree with whatever assumptions wrote them, which is precisely the
class of bug worth catching.
