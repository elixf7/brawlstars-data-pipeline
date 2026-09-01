### Clean DB Update Checklist (BrawlStars_ETL)

Purpose: produce a single-pass pipeline that writes a clean, analysis-ready SQLite DB into `data_clean/`, while keeping the battle-log pulling logic stable and optionally saving a raw copy to `data_raw/`. Also add an easy seasonal documentation process and an improved control notebook.

---

### 0) Prep and conventions

- [ ] Create directories if missing: `data_raw/`, `data_clean/`, `notebooks/`.
- [ ] Use one SQLite DB per season in `data_clean/` with name pattern: `season<id>_clean.db` (example: `season37_clean.db`). Optional raw mirror may be kept under `data_raw/Season<id>/SQLite/` for debugging only.
- [ ] Store API key via environment or `.env` (never commit keys). Notebook should read `BS_API_KEY`.
- [ ] Keep default `fetch_player_data=False` to minimize API load; allow enabling when needed.

---

### 1) Define the clean schema EXACTLY as `data_clean/build_clean_db.py`

Create a single table named `matches` with EXACT column names, types, and order as below. Each row is one best-of-3 match. This must match other codebases’ expectations.

```sql
CREATE TABLE matches (
    id                INTEGER PRIMARY KEY,
    battle_time       TEXT,
    mode              TEXT,
    map               TEXT,
    record            TEXT,
    star_brawler      TEXT,
    star_power        INTEGER,
    star_player_tag   TEXT,
    star_elo          INTEGER,
    avg_elo           REAL,
    -- Team 1 brawlers (slots 0-2, five attributes each)
    t1_b0_name TEXT, t1_b0_elo INTEGER, t1_b0_rank INTEGER, t1_b0_highest_trophies INTEGER, t1_b0_power INTEGER,
    t1_b1_name TEXT, t1_b1_elo INTEGER, t1_b1_rank INTEGER, t1_b1_highest_trophies INTEGER, t1_b1_power INTEGER,
    t1_b2_name TEXT, t1_b2_elo INTEGER, t1_b2_rank INTEGER, t1_b2_highest_trophies INTEGER, t1_b2_power INTEGER,
    -- Team 2 brawlers
    t2_b0_name TEXT, t2_b0_elo INTEGER, t2_b0_rank INTEGER, t2_b0_highest_trophies INTEGER, t2_b0_power INTEGER,
    t2_b1_name TEXT, t2_b1_elo INTEGER, t2_b1_rank INTEGER, t2_b1_highest_trophies INTEGER, t2_b1_power INTEGER,
    t2_b2_name TEXT, t2_b2_elo INTEGER, t2_b2_rank INTEGER, t2_b2_highest_trophies INTEGER, t2_b2_power INTEGER
);

CREATE INDEX idx_matches_mode  ON matches(mode);
CREATE INDEX idx_matches_time  ON matches(battle_time);
```

Additional invariants to replicate:

- Compute `avg_elo` as the mean of the six `*_elo` values present (ignore NULLs). If no `elo` values exist, `avg_elo` is NULL.
- Skip inserting rows with `avg_elo > 23` to filter out bots/corrupted rows.
- Preserve brawler name casing as present in source (dictionary notes upper-case; do not transform here).

---

### 2) Minimal, safe code changes in `DB_Data_Pull/pull_data_4.py`

Goal: keep the BFS/log fetching intact. Add a parallel “clean row” build and writer that writes DIRECTLY to `data_clean/season<id>_clean.db` with a `matches` table exactly as above. Leave the existing raw pathway available only if explicitly enabled.

- [ ] Add: `create_matches_table_if_not_exists(conn)` to create `matches` exactly as above (including indexes).
- [ ] Add: `build_clean_row(game, perspective_tag, latest_runtime, player_data_cache, fetch_player_data)` that mirrors `process_game` but returns a tuple in the EXACT column order expected by `matches` (return `None` if filtered out; enforce `avg_elo` computation and >23 filter).
- [ ] Add: `insert_rows_matches_in_chunks(db_path, rows: List[tuple], chunksize=10000)` using prepared inserts with the exact column order.
- [ ] Update: `process_tags_and_write_async(...)`
  - Preserve all parameters and behavior, especially BFS and `fetch_player_data`.
  - Add parameters: `clean_db_path: str`, `write_raw_copy: bool = False`.
  - After building `logs_dict` and `player_info_cache`, iterate games and call `build_clean_row(...)` to accumulate `clean_rows`.
  - If `write_raw_copy` is True, also write legacy raw rows as today; otherwise skip writing raw rows.
  - Always create the `matches` table and insert `clean_rows` into `clean_db_path`.
- [ ] Keep `fetch_player_data=False` as default in public API; `rank`/`highest_trophies` may be NULL when disabled.
- [ ] Add small docstrings and type hints to new functions for clarity.

Safety notes (do, but do not over-change):

- Do not change `group_ranked_matches`, `format_record`, or BFS topology (max_depth, batch_size, concurrency) beyond parameterizing.
- Keep `429` handling and request retry logic exactly as-is; consider only adding timeouts to `aiohttp.ClientSession` for robustness.

---

### 3) New season metadata capture (auto-document each dataset)

Create a light summary written per run (sidecar files only; do NOT add tables to the DB to avoid changing the DB contract):

- [ ] Function: `compute_season_metadata(clean_db_path) -> dict` that returns:
  - `season_label`, `start_time`, `end_time` (min/max `battle_time`)
  - `num_matches` (row count), `num_unique_brawlers`
  - `modes` (sorted list), `maps` (sorted list)
  - `brawler_usage_top` (top-N brawlers by occurrences across all slots)
  - `data_bytes` (DB file size), `created_utc`
- [ ] Persist to:
  - `data_clean/season<id>_metadata.json`
  - Optional: `data_clean/season<id>_summary.md` for human-friendly notes
- [ ] Provide a small helper callable from the notebook: `write_season_metadata(clean_db_path, season_label)`.

---

### 4) New control notebook (replace `db_builder.ipynb` with improved flow)

Create `notebooks/clean_db_builder.ipynb` with these sections:

1. Intro and safety (how to set `BS_API_KEY`, what will be written, defaults)
2. Config
   - `season_label`, `latest_runtime` (UTC), `max_depth`, `batch_size`, `concurrency`, `fetch_player_data=False`

- Paths: `clean_db_path` (e.g., `data_clean/season37_clean.db`), optional `raw_db_path` if `write_raw_copy=True`

3. Connect and pull
   - Generate initial player seed set (reuse `pull_random_tags` or a custom list)
   - Run `process_tags_and_write_async(..., clean_db_path=..., write_raw_copy=False)`
4. Deduplicate and index (if needed; clean table inserts should avoid duplicates by unique composite or post-pass `DELETE` with GROUP BY similar to the existing helper)
5. Season metadata
   - Run `write_season_metadata` and display quick summaries
6. Quick validation cells
   - Sanity checks on nulls, counts, date ranges, example head

Notes:

- Remove plain-text keys from notebooks; read from `os.environ["BS_API_KEY"]`.
- Keep notebooks small; heavy logic stays in Python modules.

---

### 5) Updated data dictionary (for `matches`)

Core

- `id` (INTEGER PRIMARY KEY): sequential id carried over from raw table.
- `battle_time` (TEXT): ISO-8601 UTC timestamp of last game in the match.
- `mode` (TEXT): Game mode.
- `map` (TEXT): Map name.
- `record` (TEXT): Sequence of results (e.g., `T1-T2-T1`).
- `star_brawler` (TEXT): MVP brawler name.
- `star_power` (INTEGER): MVP brawler power.
- `star_player_tag` (TEXT): MVP player tag.
- `star_elo` (INTEGER): MVP brawler elo.
- `avg_elo` (REAL): Mean of six brawler elos (ignore NULLs).

Per brawler fields for `t{team}_b{slot}_*` (team∈{1,2}, slot∈{0,1,2}):

- `name` (TEXT), `elo` (INTEGER), `rank` (INTEGER, NULLABLE), `highest_trophies` (INTEGER, NULLABLE), `power` (INTEGER)

---

### 6) Seasonal run procedure (repeatable)

When a new season starts:

- [ ] Update `season_label` and `latest_runtime` in the notebook/config.
- [ ] Choose seed tags (reuse `pull_random_tags` against prior season or a curated list).
- [ ] Run the notebook; verify row counts flow upward during BFS fetch.
- [ ] After completion, run metadata writer and review `metadata.json`.
- [ ] Commit and tag the run (e.g., git tag `season-37-clean-ready`).

Artifacts created:

- `data_clean/season<id>_clean.db` with table `matches` only.
- Optional: raw DB under `data_raw/Season<id>/SQLite/` if `write_raw_copy=True`.
- `data_clean/season<id>_metadata.json` + optional `season<id>_summary.md`.

---

### 7) Efficiency and robustness (small improvements only)

- [ ] Add docstrings and type hints to new functions (`create_clean_table_if_not_exists`, `build_clean_row`, `insert_rows_clean_in_chunks`).
- [ ] Optionally set `aiohttp` timeouts via session/connector; keep retry + `429` logic intact.
- [ ] Reuse a single SQLite connection per insert batch where practical.
- [ ] Add indices described in Step 1; keep `remove_duplicates_by_columns` available if needed.
- [ ] Consider `PRAGMA journal_mode=WAL;` and `synchronous=NORMAL` for faster bulk inserts when running locally.

---

### 8) Acceptance checks (EXACT contract guard)

- [ ] Clean DB builds without manual second pass; file appears under `data_clean/`.
- [ ] DB contains ONLY one table named `matches` (no additional tables created by this pipeline).
- [ ] `PRAGMA table_info('matches')` exactly matches column names, types, and order specified in Step 1 (40 columns).
- [ ] `idx_matches_mode` and `idx_matches_time` indexes exist.
- [ ] `avg_elo` computation and `avg_elo > 23` filter applied.
- [ ] With `fetch_player_data=False`, `rank`/`highest_trophies` may be NULL but columns exist; enabling it fills values when available.
- [ ] `record` logic matches original and is stable.

---

### 9) Backward compatibility and migration notes

- Keep the legacy raw writer behind `write_raw_copy=True` for debugging only.
- New notebook supersedes `db_builder.ipynb` but you can keep it for reference.
- Downstream notebooks/analyses continue to target `matches` in `season<id>_clean.db` (no changes required).

---

### 10) Implementation sketch (minimal code touchpoints)

Add (names illustrative):

```python
def create_matches_table_if_not_exists(conn):
    """Create matches schema exactly as in Step 1 and add indexes."""

def build_clean_row(game, perspective_tag, latest_runtime, player_data_cache, fetch_player_data):
    """Return tuple of 40 values in the exact column order or None (apply avg_elo computation and >23 filter)."""

def insert_rows_matches_in_chunks(db_path, rows, chunksize=10000):
    """Bulk insert tuples into matches using a fixed 40-column INSERT in that order."""
```

Integrate into the main async:

```python
await process_tags_and_write_async(
    player_tags=seed_tags,
    api_key=os.environ["BS_API_KEY"],
    latest_runtime=season_start_utc,
    db_path=raw_db_path,          # only if write_raw_copy=True
    table_name="game_table",     # legacy raw table
    clean_db_path=clean_db_path,  # always write
    write_raw_copy=False,
    fetch_player_data=False,
)
```
