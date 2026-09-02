## Brawl Stars Ranked — Clean Database Data Dictionary

This document describes the analysis-ready SQLite schema produced under `data/seasons/`. It is designed to be stable across seasons and to serve as a reference for both current and future datasets.

- Source build orchestration: the `bsetl-ingest` and `bsetl-queue` commands (see the README).
- Core schema definition: `src/bsetl/transform/schema.py`.
- Time-local ECDF skill feature: the `bsetl-skill-features` command, with config in `src/bsetl/transform/skill_config.py` and helpers in `src/bsetl/transform/skill_features.py`.

### Scope and naming

- Clean DB per season and version (example): `data/seasons/season42/v1_clean.db`.
- Clean DB with skill feature (example): `data/seasons/season42/season42_combined_skill_ns.db`.
- Skill feature sidecar JSON (example): `data/seasons/season42/season42_skill_ns_metadata.json`.
- Season summary sidecar JSON (example): `data/seasons/season42/season42_v1_metadata.json`.

All timestamps are UTC. Column names are stable and lower snake-case, with per-team/per-slot patterns documented below.

## Tables overview

- `matches` (wide, one row per ranked set): Core match metadata, star-player summary, per-team brawler fields, and optional derived features like `skill_ns`.
- `skill_bin_metadata` (when skill feature is computed): Summary rows describing the time-bin coverage and configuration used for feature computation.

## Table: `matches`

One row per ranked set (the last game’s timestamp for the set). All columns are NULL-able to accommodate missing data from the API.

- Primary key: `id` (INTEGER)
- Recommended uniqueness for de-duplication: `(battle_time, map, star_player_tag)`
  - Note: In some historical builds the unique index may not exist; deduplication logic is applied at write time.

### Event metadata

- `id` (INTEGER, PRIMARY KEY): Locally assigned sequential identifier for the set.
- `battle_time` (TEXT): ISO-8601 UTC timestamp of the last game in the set (e.g., `20251102T002849.000Z`).
- `mode` (TEXT): Ranked mode identifier, e.g., `brawlBall`, `gemGrab`, `hotZone`, `bounty`, `heist`, `knockout`.
- `map` (TEXT): Map name used for the entire set.
- `record` (TEXT): Game-by-game results in the set: tokens are `T1` (Team 1 win), `T2` (Team 2 win), `D` (draw). Examples: `T1-T1`, `T2-T1-T1`, `T1-T2-T2`.

### Star player fields (from the last game’s MVP tuple)

These are parsed from the four-tuple `Star_Player = [BrawlerName, PowerLevel, PlayerTag, BrawlerElo]`:

- `star_brawler` (TEXT): MVP brawler name (e.g., `RICO`, `CROW`).
- `star_power` (INTEGER): MVP brawler power level.
- `star_player_tag` (TEXT): Player tag string (e.g., `#8Q8YC8VJ0`).
- `star_elo` (INTEGER): MVP’s brawler Elo at match time.

### Team brawler fields (flattened)

For each team `t ∈ {1,2}` and brawler slot `b ∈ {0,1,2}`:

- `t{t}_b{b}_name` (TEXT): Brawler name.
- `t{t}_b{b}_elo` (INTEGER): Brawler Elo (ranked rating) at match time.
- `t{t}_b{b}_rank` (INTEGER): Ranked tier at match time (Elo-style rank tier; see game docs).
- `t{t}_b{b}_highest_trophies` (INTEGER): Highest trophies recorded for the player on this brawler.
- `t{t}_b{b}_power` (INTEGER): Brawler power level.

Notes:

- No duplicates within a team in the final 3 picks; the same brawler may appear on both sides.
- Pick order and bans are not exposed by the public API and are not recorded.
- Missing slots are represented as NULLs.

### Derived fields on `matches`

- `avg_elo` (REAL): Mean Elo across the six brawlers on the two teams for the set.

  - Computation: collect all non-NULL `t{t}_b{b}_elo` values across both teams and average them. If none are present, `avg_elo` is NULL.
  - Safety filter: rows with `avg_elo > 23` are skipped during ingestion (to avoid obviously corrupted or bot data).

- `skill_ns` (REAL): Time-local ECDF-normalized skill score derived from `avg_elo` (added by `scripts/compute_skill_features.py`).

  - See “Skill feature computation” below for details. Absent if the feature hasn’t been computed.

- `skill_ns_ok` (INTEGER): Coverage flag for `skill_ns` (1 = computed using a sufficiently populated time bin; 0 = low coverage or missing context). Absent if the feature hasn’t been computed.

### Indexes

- `idx_matches_mode` on (`mode`)
- `idx_matches_time` on (`battle_time`)
- (Recommended) `uniq_matches_key` on (`battle_time`, `map`, `star_player_tag`) for deduplication; may not exist in every historical DB.

## Table: `skill_bin_metadata`

One row per time bin when `scripts/compute_skill_features.py` is run. These rows document the coverage and parameters used to compute `skill_ns`.

- `feature_version` (TEXT): Version string for the feature pipeline (e.g., `skill_ns_v1`).
- `season` (TEXT): Season label (e.g., `season42`).
- `bin_start_utc` (TEXT): ISO-8601 UTC start of the bin.
- `bin_end_utc` (TEXT): ISO-8601 UTC end of the bin (exclusive).
- `n_samples` (INTEGER): Number of `avg_elo` samples in the bin.
- `coverage_ok` (INTEGER): 1 if `n_samples >= min_bin_count`, else 0.
- `epsilon` (REAL): Clipping parameter for percentile-to-score mapping.
- `bin_width_days` (INTEGER): Fixed bin width in days.
- `fallback_used` (INTEGER): 1 if a fallback ECDF (e.g., season-global) was used for the bin, else 0.
- `created_utc` (TEXT): ISO-8601 UTC timestamp when the metadata row was written.

Indexes:

- `idx_skill_meta_season` on (`season`)

## Skill feature computation (time-local ECDF of `avg_elo`)

The optional `skill_ns` feature expresses a row’s `avg_elo` as a normalized score relative to other matches in a nearby time window. This helps adapt to meta/time effects.

Configuration defaults (see `src/bsetl/transform/skill_config.py`):

- `BIN_WIDTH_DAYS = 3`
- `MIN_BIN_COUNT = 5_000` (overridden at runtime in your notebook/example run to `100_000`)
- `EPSILON = 1e-3`
- Mapping `mapping ∈ {normal, logit}` (your example run uses `logit`)
- Fallback strategy `fallback_strategy ∈ {none, global_season_ecdf}` (your example run: `none`)
- Feature version: `skill_ns_v1`

Steps (implemented in `scripts/compute_skill_features.py`):

1. Assign each row to a fixed-width time bin using `battle_time` in UTC. Bin starts are anchored at 00:00:00 UTC and aligned to epoch in steps of `BIN_WIDTH_DAYS` (see `base_bin_start()`).
2. For each bin, collect all non-NULL `avg_elo` values and sort them; compute coverage: `coverage_ok = (n_samples >= MIN_BIN_COUNT)`.
3. For a row with `avg_elo = x` in a bin with sorted values `V`, compute midrank percentile:
   - `p = (rank_mid - 0.5) / (n + 1)`, where `rank_mid` averages the tie range of `x` in `V`.
4. Map percentile to score with ε-clipping (`ε = EPSILON`):
   - If `mapping = "normal"`: `skill_ns = Φ⁻¹(clip(p, ε, 1-ε))` (inverse standard normal)
   - If `mapping = "logit"`: `skill_ns = log(clip(p, ε, 1-ε) / (1 - clip(p, ε, 1-ε)))`
5. Write `skill_ns` and `skill_ns_ok` to `matches`. If `coverage_ok = 0` and no fallback is enabled, `skill_ns` is NULL.
6. Persist per-bin metadata in `skill_bin_metadata` and a JSON sidecar for reproducibility.

Notes:

- Coverage flag `skill_ns_ok = 1` indicates that the score was computed from a bin meeting the coverage threshold. Prefer filtering to `skill_ns_ok = 1` for high-quality analysis.
- With `fallback_strategy = "global_season_ecdf"`, rows in low-coverage bins get `skill_ns` from the season-global ECDF; `skill_ns_ok` still reflects the local coverage (0), and `fallback_used = 1` is recorded in metadata.

## Modes, maps, and enumerations

Canonical ranked modes typically include (season-dependent): `bounty`, `brawlBall`, `gemGrab`, `heist`, `hotZone`, `knockout`. Maps are strings and vary by mode and season rotation. See the season sidecar JSON (e.g., `season42_v1_metadata.json`) for the set of modes and maps observed in a given dataset.

## Domain notes and limitations

- Ranked sets are best-of-3; `record` encodes the games in order, but the draft order and team picking order are not exposed.
- The public API does not expose bans or per-pick ordering—only final teams.
- The same brawler can appear on both teams; no duplicates within a single team.
- `avg_elo` is not a latent skill estimate; it is the simple mean of available brawler Elo values for the set and is time-local by construction.
- Meta shifts are primarily balance changes; adapting across seasons should focus on brawler interactions rather than map changes.

## Nulls, quality filters, and edge cases

- Columns are NULL-able and reflect source API completeness.
- `avg_elo` is NULL if no brawler Elo values are available for the row.
- Rows with `avg_elo > 23` are excluded during ingestion to avoid corrupted/bot-like entries.
- `battle_time` parsing supports `YYYYMMDDTHHMMSS.fffZ`, `YYYYMMDDTHHMMSSZ`, and ISO-8601 with `Z` → `+00:00` normalization.

## Reproducibility and versioning

- Skill feature version: `skill_ns_v1`. If computation or config changes, this version increments.
- Each run writes a JSON sidecar summarizing configuration, coverage, and time bins (e.g., `season42_skill_ns_metadata.json`).
- Season summary sidecar (e.g., `season42_v1_metadata.json`) includes counts, time range, modes, maps, and size statistics.

## Typical queries

Only use `skill_ns` rows with adequate coverage:

```sql
SELECT mode, map, COUNT(*) AS n_rows
FROM matches
WHERE skill_ns_ok = 1
GROUP BY mode, map
ORDER BY n_rows DESC;
```

Top brawlers by recent period and mode (filter by Elo range and quality):

```sql
SELECT mode, t1_b0_name AS brawler, COUNT(*) AS picks
FROM matches
WHERE battle_time >= '20251101T000000.000Z'
  AND avg_elo BETWEEN 15 AND 23
  AND skill_ns_ok = 1
GROUP BY mode, brawler
ORDER BY picks DESC
LIMIT 50;
```

Join rows to their metadata bins (by bin membership):

```sql
-- Example: count rows per metadata bin where coverage_ok = 1
SELECT m_count.season, m_count.bin_start_utc, m_count.bin_end_utc, m_count.n_rows
FROM (
  SELECT sbm.season,
         sbm.bin_start_utc,
         sbm.bin_end_utc,
         COUNT(*) AS n_rows
  FROM matches AS m
  JOIN skill_bin_metadata AS sbm
    ON datetime(substr(m.battle_time,1,4)||'-'||substr(m.battle_time,5,2)||'-'||substr(m.battle_time,7,2)||'T00:00:00Z')
       >= datetime(sbm.bin_start_utc)
   AND datetime(substr(m.battle_time,1,4)||'-'||substr(m.battle_time,5,2)||'-'||substr(m.battle_time,7,2)||'T00:00:00Z')
       <  datetime(sbm.bin_end_utc)
  WHERE sbm.coverage_ok = 1
  GROUP BY sbm.season, sbm.bin_start_utc, sbm.bin_end_utc
) AS m_count
ORDER BY m_count.bin_start_utc;
```

## Building or extending for future seasons

1. Crawl into a new season database with `bsetl-ingest`, or `bsetl-queue` to chunk a
   seed file into bounded subprocess runs. Seed tags can be sampled from an existing
   season via `bsetl.state.seeding.sample_seed_tags_from_clean_db`.
2. Compute the skill feature (optional) with `bsetl-skill-features`:
   - `--clean-db-path`, `--bin-width-days`, `--min-bin-count`, `--epsilon`,
     `--mapping {normal,logit}`, `--fallback-strategy {none,global_season_ecdf}`,
     `--season` (optional).
   - Adds `skill_ns` and `skill_ns_ok` to `matches`, creates or updates
     `skill_bin_metadata`, and writes the sidecar JSON.
3. Write the season summary sidecar with `bsetl.transform.write_season_metadata`.

See the README for full command examples.

## Current database snapshot (season42)

For `/data/seasons/season42/season42_combined_skill_ns.db` (as inspected):

- Tables: `matches` (~2.72M rows), `skill_bin_metadata` (10 rows)
- `matches` columns include all core fields above plus `skill_ns`, `skill_ns_ok`
- Indexes: `idx_matches_time`, `idx_matches_mode` (unique key may be absent depending on the build path)
- Skill sidecar (example settings): `bin_width_days=3`, `min_bin_count=100000`, `epsilon=0.001`, `mapping='logit'`, `fallback='none'`

Use `skill_ns_ok = 1` for high-quality analyses and prefer restricting to realistic Elo ranges (e.g., 12–23) for rank-tier–relevant slices.
