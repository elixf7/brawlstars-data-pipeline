## Time-Local ECDF Skill Feature — Project Integration Checklist

Purpose: Build a simple, robust, time-aware skill feature that fixes early-season elo lag by computing a time-local ECDF of match average elo (`avg_elo`) and mapping to a symmetric, unbounded scale. This checklist adapts the generic plan to this repository’s structure and data, using fixed bins (no merging) and a coverage flag.

---

### 0) Define the scope (one-time)

- [ ] **Unit**: Use the match-level average elo column `avg_elo` in `matches`.
- [ ] **Pooling (what we group over)**: Build each bin’s ECDF from all matches in the database (i.e., no stratification by mode or map). Since ranked matchmaking randomizes features, we treat the elo distribution as the same across them.
  - Season label derived from the clean DB filename (e.g., `season42`) or via `data_clean/metadata.py`.
- [ ] **Cadence**: Recompute every 3 days (aligned to bin width) when new data lands.

Storage/columns

- [ ] Add two columns on `matches`:
  - `skill_ns` (REAL, nullable) — normalized elo from time-local ECDF + mapping.
  - `skill_ns_ok` (INTEGER {0,1}) — 1 if the bin has sufficient samples; 0 otherwise.

Implementation notes

- Use a dedicated config with versioning (e.g., `FEATURE_VERSION = "skill_ns_v1"`).
- Naming: `skill_ns` = skill score derived from time-local ECDF of `avg_elo`, mapped via Normal Scores (ns). Stable across databases; if the method/config changes materially, track that in metadata (not in the column name).
- Coverage flag column: `skill_ns_ok` (1/0).

---

### 1) Time windows (binning)

- [ ] **Bin width**: Fixed 3-day bins (configurable). Floor `battle_time` to UTC midnight. Anchor to epoch for stable boundaries.
- [ ] **Coverage rule**: Minimum per-bin count = 5,000 `avg_elo` samples (configurable). Do not widen or merge bins.
- [ ] **Bin assignment**: Precompute `time-bin` keys (database-wide; no sub-strata).
- [ ] **Low-coverage handling (no merging)**:
  - If bin count ≥ threshold: compute `skill_ns` for rows in that bin; set `skill_ns_ok = 1`.
  - If bin count < threshold: set `skill_ns = NULL` by default; set `skill_ns_ok = 0`.
  - Optional (runtime flag): allow fallback to a global (season-wide) ECDF for low-coverage bins, still marking `skill_ns_ok = 0`. Default is no fallback to preserve time-local purity.

Implementation locations

- New: `data_clean/skill_config.py` — constants (bin width, min bin count, epsilon, version, coverage column, optional fallback).
- New: `data_clean/skill_features.py` — helpers to assign bins from `matches(battle_time)` and compute fixed-bin counts (no merging).

---

### 2) Time-local ECDF per bin (database-wide pooling)

- [ ] **Ranking**: Use mid-ranks; percentiles p in (0,1) via `(rank - 0.5) / (n + 1)`.
- [ ] **Outliers**: ECDF is robust; no trimming initially.
- [ ] **Clip**: Use ε = 1e-3 to avoid exact 0/1.
- [ ] **Per output**:
  - If bin is coverage-OK: compute local percentile `p_t` for its `avg_elo` within its time-bin.
  - If bin is not coverage-OK:
    - Default: write `skill_ns = NULL`.
    - Optional (runtime flag): compute `p_t` from season-wide ECDF as a fallback but keep `skill_ns_ok = 0`.

Storage shape (choose one)

- [ ] Default: Add two columns to `matches` via a post-build `ALTER TABLE`:
  - `skill_ns` (REAL, nullable).
  - `skill_ns_ok` (INTEGER {0,1}).

Implementation locations

- `data_clean/skill_features.py`: ECDF computation per fixed time-bin over `avg_elo` (database-wide).
- `scripts/compute_skill_features.py`: DDL for the new columns/table and writes.

---

### 3) Smoothing over time

- [ ] Default: None. No temporal smoothing or bin merging is applied to compute `skill_ns`.

Implementation locations

- N/A for main feature computation. Keep implementation simple and time-local.

---

### 4) Map percentile to symmetric, unbounded scale

- [ ] **Default**: Normal scores (van der Waerden): `Φ^{-1}(clip(p_t, ε, 1-ε))`.
- [ ] **Alternative**: Logit mapping for heavier tails.
- [ ] **Output**: `skill_ns` per match.

Implementation locations

- `data_clean/skill_features.py`: Mapping functions and application on `p_t`.
- `scripts/compute_skill_features.py`: Persist to storage shape chosen in Step 2.

---

### 6) Hyperparameters (defaults)

- [ ] Bin width: 3 days (no widening/merging).
- [ ] Minimum bin count: 5,000.
- [ ] Clipping ε: 1e-3.
- [ ] Mapping: Normal scores.
- [ ] Fallback (optional): disabled by default; `global_season_ecdf` for low-coverage bins if enabled.

Implementation locations

- `data_clean/skill_config.py`: Centralize and freeze a config snapshot for this version.

---

### 7) Integration into the ETL pipeline

- [ ] Keep `data_clean/build_clean_db.py` unchanged; run feature computation as a post-build step on the clean DB (e.g., `data_clean/season42/v1_clean.db`).
- [ ] Create `scripts/compute_skill_features.py` to:
  - [ ] Add two columns to `matches`: `skill_ns` (REAL, nullable) and `skill_ns_ok` (INTEGER {0,1}).
  - [ ] Compute database-wide per-bin ECDFs over `avg_elo` (fixed bins, no merging).
  - [ ] Write `skill_ns` for coverage-OK bins; write NULL for low-coverage bins by default.
  - [ ] Set `skill_ns_ok` accordingly (1/0).
  - [ ] Persist bin-level metadata (see Step 7b).

7b) Metadata and auditability

- [ ] Create table `skill_bin_metadata` with: `feature_version`, `season`, `bin_start_utc`, `bin_end_utc`, `n_samples`, `coverage_ok`, `epsilon`, `bin_width_days`, `fallback_used`, `created_utc`.
- [ ] Write JSON sidecar: `{season}_skill_ns_metadata.json` adjacent to the DB (use pattern in `data_clean/metadata.py`).

Automation

- [ ] Optionally hook into `scripts/queue_runs.py` to run after each DB refresh.

---

### 9) Monitoring (guardrails)

- [ ] Volume alerts: warn if bin count < threshold; report number/fraction of low-coverage bins per season.

---

### 10) When to revisit choices

- [ ] Early-season still noisy → shorten bin width (e.g., 3 → 2 days) or slightly increase ε.
- [ ] Too many low-coverage bins → consider enabling fallback to season-wide ECDF (keep `skill_ns_ok = 0`).
- [ ] Extreme tails dominate → switch mapping from normal scores to logit.

Versioning

- [ ] Keep the column name stable as `skill_ns`. If the method/config changes materially, record a new `feature_version` in metadata (and optionally a new JSON sidecar), without changing the column name.

---

### Deliverables (initial implementation)

- [ ] `data_clean/skill_config.py` — constants, defaults, version string, coverage column, fallback option.
- [ ] `data_clean/skill_features.py` — fixed-binning utilities (no merging), ECDF, mapping, coverage checks.
- [ ] `scripts/compute_skill_features.py` — DB I/O, DDL for new columns/table, writes, metadata logging.
- [ ] `data_clean/diagnostics.py` — optional helpers for validation/monitoring.
- [ ] `notebooks/skill_feature_validation.ipynb` — lightweight validation and plots.

Done when

- [ ] Two columns on `matches`: `skill_ns` (nullable) and `skill_ns_ok` (1/0) are populated.
- [ ] `skill_bin_metadata` table and JSON sidecar are written with parameters, counts, coverage flags (and fallback markers if used).
- [ ] Validation produces monotone decile win rates and stable distribution metrics across bins; acceptable number of low-coverage bins is documented.
