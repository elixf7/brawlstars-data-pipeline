# ETL Optimization — Agent Implementation Checklist

> **For AI agents.** Each task is self-contained: it names the exact file, the exact lines to change, the exact behavior expected before and after, and the acceptance test. Complete tasks in priority order. Do not skip ahead — later tasks depend on earlier ones.

---

## Phase 1 — Persistent Tag Tracking (Fixes 1A, 1D, 1C)

These three fixes share a single new DB table and must be done together in order.

---

### Task 1.1 — Add `fetched_tags` table to `schema.py`

**File**: `data_clean/schema.py`

**What to add**: A new function `create_fetched_tags_table_if_not_exists(conn)` that creates the table and a new function `upsert_fetched_tags(conn, tags)` that bulk-inserts/updates rows.

**Table DDL**:
```sql
CREATE TABLE IF NOT EXISTS fetched_tags (
    tag         TEXT PRIMARY KEY,
    fetched_utc TEXT NOT NULL
);
```

**Upsert statement** (use `INSERT OR REPLACE`):
```sql
INSERT OR REPLACE INTO fetched_tags (tag, fetched_utc) VALUES (?, ?)
```

**Where to add in file**: After the existing `get_matches_insert_statement()` function (currently ends at line 76). Add both new functions before the final blank line.

**Acceptance test**:
```python
import sqlite3, tempfile
from data_clean.schema import create_fetched_tags_table_if_not_exists, upsert_fetched_tags
with tempfile.NamedTemporaryFile(suffix=".db") as f:
    conn = sqlite3.connect(f.name)
    create_fetched_tags_table_if_not_exists(conn)
    upsert_fetched_tags(conn, ["#AAA", "#BBB"])
    rows = conn.execute("SELECT tag FROM fetched_tags ORDER BY tag").fetchall()
    assert rows == [("#AAA",), ("#BBB",)], rows
    conn.close()
```

---

### Task 1.2 — Add `load_fetched_tags_from_db` helper to `schema.py`

**File**: `data_clean/schema.py`

**What to add**: A new function `load_fetched_tags_from_db(db_path) -> set[str]` that opens the DB, queries `fetched_tags`, returns the full set of tags. Returns an empty set if the table does not exist or the DB file does not exist.

**Logic**:
```python
def load_fetched_tags_from_db(db_path: str) -> set:
    import os
    if not os.path.exists(db_path):
        return set()
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT tag FROM fetched_tags").fetchall()
        return {r[0] for r in rows}
    except sqlite3.OperationalError:
        return set()
    finally:
        conn.close()
```

**Acceptance test**:
```python
from data_clean.schema import load_fetched_tags_from_db
result = load_fetched_tags_from_db("/nonexistent/path.db")
assert result == set()
```

---

### Task 1.3 — Add `fetched_tags_ttl_hours` parameter to `process_tags_and_write_async`

**File**: `DB_Data_Pull/pull_data_4.py`

**Function signature** (starts at line 517):
```python
async def process_tags_and_write_async(
    player_tags: List[str],
    api_key: str,
    latest_runtime: datetime,
    ...
    requests_per_second: float = 5.0,
):
```

**Change**: Add a new keyword argument `fetched_tags_ttl_hours: float = 0.0` at the end of the parameter list. A value of `0.0` means disabled (existing behavior preserved). Any positive value enables tag-skip logic.

**No other changes in this task** — just the signature and docstring. The logic is wired in Tasks 1.4 and 1.5.

---

### Task 1.4 — Pre-load `visited_tags` from DB at run start (Fix 1D)

**File**: `DB_Data_Pull/pull_data_4.py`

**Where**: Immediately after line 554 (`visited_tags = set()`).

**What to add**:
```python
# Pre-load previously fetched tags from DB to skip re-fetching across runs
if fetched_tags_ttl_hours > 0.0 and clean_db_path:
    from data_clean.schema import load_fetched_tags_from_db
    from datetime import timedelta
    _all_fetched = load_fetched_tags_from_db(clean_db_path)
    if _all_fetched:
        _cutoff = datetime.now(timezone.utc) - timedelta(hours=fetched_tags_ttl_hours)
        # Re-query with timestamp filtering if TTL < infinity; otherwise take all
        # For simplicity in first pass, load all and treat them as visited
        visited_tags.update(_all_fetched)
```

**Note for agent**: The simple first-pass approach loads all fetched tags regardless of when they were fetched. A TTL-aware implementation requires a second query against `fetched_tags` filtering by `fetched_utc`. Implement the TTL-aware version:

```python
if fetched_tags_ttl_hours > 0.0 and clean_db_path:
    import os
    from data_clean.schema import load_fetched_tags_from_db
    if os.path.exists(clean_db_path):
        import sqlite3 as _sqlite3
        _cutoff = (datetime.now(timezone.utc) - __import__('datetime').timedelta(hours=fetched_tags_ttl_hours)).isoformat()
        conn_ = _sqlite3.connect(clean_db_path)
        try:
            conn_.execute("CREATE TABLE IF NOT EXISTS fetched_tags (tag TEXT PRIMARY KEY, fetched_utc TEXT NOT NULL)")
            rows_ = conn_.execute(
                "SELECT tag FROM fetched_tags WHERE fetched_utc >= ?", (_cutoff,)
            ).fetchall()
            visited_tags.update(r[0] for r in rows_)
        except Exception:
            pass
        finally:
            conn_.close()
```

**Acceptance test**: Run a crawl with 3 seed tags. Check row count in `matches`. Run the exact same crawl again with `fetched_tags_ttl_hours=24`. The second run should produce `BFS: fetching logs: 0it` (all seeds already in `visited_tags`) and make no API calls.

---

### Task 1.5 — Write fetched tags to DB after BFS sweep (Fix 1A)

**File**: `DB_Data_Pull/pull_data_4.py`

**Where**: After line 630 (`pbar_bfs.close()`) and before line 632 (`# 2-4) Enrichment`).

**What to add**:
```python
    # Persist visited tags so future runs can skip them
    if fetched_tags_ttl_hours > 0.0 and clean_db_path and visited_tags:
        from data_clean.schema import create_fetched_tags_table_if_not_exists, upsert_fetched_tags
        import sqlite3 as _sqlite3
        _now_utc = datetime.now(timezone.utc).isoformat()
        _conn = _sqlite3.connect(clean_db_path)
        try:
            create_fetched_tags_table_if_not_exists(_conn)
            upsert_fetched_tags(_conn, list(visited_tags), _now_utc)
        finally:
            _conn.close()
```

**Note**: `upsert_fetched_tags` (Task 1.1) must accept `(conn, tags: list[str], fetched_utc: str)` — update the Task 1.1 function signature accordingly. The `INSERT OR REPLACE` overwrites the timestamp on revisit, which is correct.

**Acceptance test**: After a run with `fetched_tags_ttl_hours=24`, query the DB:
```sql
SELECT COUNT(*) FROM fetched_tags;
```
Should equal the number of unique tags whose battle logs were fetched.

---

### Task 1.6 — Skip BFS expansion for already-fetched tags (Fix 1C)

**File**: `DB_Data_Pull/pull_data_4.py`

**Where**: Lines 622–626 (the BFS enqueue block):
```python
if depth < max_depth and _is_elo_in_range(elo_val, elo_queue_min, elo_queue_max):
    if nt not in visited_tags and nt not in enqueued:
        queue.append((nt, depth + 1))
        enqueued.add(nt)
```

**Change**: The existing `nt not in visited_tags` check already prevents expansion of already-fetched tags — because Task 1.4 pre-loads all recently-fetched tags into `visited_tags`. **No code change needed here** as long as Tasks 1.4 and 1.5 are implemented correctly.

**Verify**: Confirm the existing guard `if nt not in visited_tags` at line 624 is still present and unmodified. This is the fix.

---

### Task 1.7 — Expose `fetched_tags_ttl_hours` in `run_once.py`

**File**: `DB_Data_Pull/run_once.py`

**What to add**:
1. New CLI argument: `--fetched-tags-ttl-hours` (type `float`, default `0.0`)
2. Pass it through to `process_tags_and_write_async(..., fetched_tags_ttl_hours=args.fetched_tags_ttl_hours)`

**Acceptance test**: `python3 -m DB_Data_Pull.run_once --help` should show `--fetched-tags-ttl-hours`.

---

### Task 1.8 — Expose `fetched_tags_ttl_hours` in `queue_runs.py`

**File**: `scripts/queue_runs.py`

**What to add**:
1. New CLI argument in `main()`: `p.add_argument("--fetched-tags-ttl-hours", type=float, default=0.0)`
2. Pass it through to the subprocess command: `cmd += ["--fetched-tags-ttl-hours", str(args.fetched_tags_ttl_hours)]`

**Where to insert the cmd line**: After line 90 (the `elo_game_max` block), before `cmd += ["--tags"] + batch`.

---

### Task 1.9 — Update notebook config cell

**File**: `notebooks/clean_db_builder.ipynb`

**Cell id**: `d6de3f2c` (the "Queue many short runs" cell)

**What to add**: A new variable `fetched_tags_ttl_hours = 24.0` near the other crawler knobs, and pass it into the `cmd` list:
```python
"--fetched-tags-ttl-hours", str(fetched_tags_ttl_hours),
```

---

## Phase 2 — Memory: Incremental DB Flush (Fix 2D)

This phase addresses the memory growth problem. It must be done after Phase 1 because the flush also writes `fetched_tags` mid-run.

---

### Task 2.1 — Extract row-processing into a helper function

**File**: `DB_Data_Pull/pull_data_4.py`

**What to extract**: Lines 657–665 (the loop that builds `clean_rows`) into a standalone function:

```python
def _build_clean_rows_from_logs(
    logs_dict: dict,
    latest_runtime,
    player_info_cache: dict,
    fetch_player_data: bool,
    elo_game_min,
    elo_game_max,
) -> list:
    rows = []
    for t in logs_dict:
        for g in group_ranked_matches(logs_dict[t]):
            row = build_clean_row(g, t, latest_runtime, player_info_cache, fetch_player_data, elo_game_min, elo_game_max)
            if row:
                rows.append(row)
    return rows
```

Place this function just before `process_tags_and_write_async` (before line 517). Replace the inline loop at lines 657–665 with a call to `_build_clean_rows_from_logs(...)`.

**No behavior change** — pure refactor to enable Task 2.2.

---

### Task 2.2 — Add `flush_every_n_batches` parameter and incremental flush

**File**: `DB_Data_Pull/pull_data_4.py`

**New parameter**: Add `flush_every_n_batches: int = 0` to `process_tags_and_write_async`. `0` disables incremental flushing (backward-compatible default).

**New variables** (add after line 554, alongside `visited_tags = set()`):
```python
_batch_count = 0
_pending_rows: list = []
_pending_visited: set = set()
```

**Inside the BFS while loop**, after `pbar_bfs.update(len(tasks))` (after line 628):
```python
_batch_count += 1
_pending_visited.update(tag for (tag, _) in tasks)

if flush_every_n_batches > 0 and _batch_count % flush_every_n_batches == 0:
    # Build rows from logs fetched so far
    _flush_logs = {t: logs_dict[t] for t in _pending_visited if t in logs_dict}
    _flush_rows = _build_clean_rows_from_logs(
        _flush_logs, latest_runtime, player_info_cache,
        fetch_player_data, elo_game_min, elo_game_max
    )
    if _flush_rows and clean_db_path:
        await asyncio.to_thread(insert_rows_matches_in_chunks, clean_db_path, _flush_rows, 10000)
    # Write fetched tags mid-run
    if fetched_tags_ttl_hours > 0.0 and clean_db_path and _pending_visited:
        from data_clean.schema import create_fetched_tags_table_if_not_exists, upsert_fetched_tags
        import sqlite3 as _sqlite3
        _now_utc = datetime.now(timezone.utc).isoformat()
        _conn = _sqlite3.connect(clean_db_path)
        try:
            create_fetched_tags_table_if_not_exists(_conn)
            upsert_fetched_tags(_conn, list(_pending_visited), _now_utc)
        finally:
            _conn.close()
    # Free processed logs from memory
    for t in _pending_visited:
        logs_dict.pop(t, None)
    _pending_visited.clear()
```

**End-of-BFS flush**: The existing `clean_rows` list at lines 633–675 handles any remaining rows not yet flushed. Ensure `_build_clean_rows_from_logs` is called on the remaining `logs_dict` entries only (those not yet flushed).

**Acceptance test**: Run with `flush_every_n_batches=5`. Monitor RSS memory in Activity Monitor — it should plateau rather than grow linearly. DB row count at end should equal a non-incremental run on the same seeds.

---

### Task 2.3 — Expose `flush_every_n_batches` in `run_once.py` and `queue_runs.py`

Same pattern as Task 1.7 and 1.8:
- `run_once.py`: add `--flush-every-n-batches` (int, default 0)
- `queue_runs.py`: add `--flush-every-n-batches` (int, default 0) and pass through to subprocess cmd

---

## Phase 3 — Speed: Flatten Player Info Fetch (Fix 2B)

Only relevant when `fetch_player_data=True`. Low risk, low complexity.

---

### Task 3.1 — Remove sequential chunking in player info phase

**File**: `DB_Data_Pull/pull_data_4.py`

**Current code** (lines 641–653):
```python
async with aiohttp.ClientSession(...) as session_info:
    for i in range(0, len(discovered_tags_list), batch_size):
        subset = discovered_tags_list[i:i+batch_size]
        to_fetch = [t for t in subset if t not in player_info_cache]
        tasks_info = [fetch_player_info_async(...) for t in to_fetch]
        results_info = await asyncio.gather(*tasks_info)
        ...
        pbar_info.update(len(subset))
```

**Replace with**: A single `asyncio.gather()` over all tags at once. The semaphore already limits concurrency:

```python
async with aiohttp.ClientSession(...) as session_info:
    to_fetch = [t for t in discovered_tags_list if t not in player_info_cache]
    tasks_info = [
        fetch_player_info_async(t, api_key, session_info, semaphore, rate_limiter)
        for t in to_fetch
    ]
    results_info = await asyncio.gather(*tasks_info)
    for t, info_json in zip(to_fetch, results_info):
        if info_json:
            player_info_cache[t] = info_json
    pbar_info.update(len(to_fetch))
```

**Acceptance test**: Run with `fetch_player_data=True` on a small seed. Verify `player_info_cache` has entries and `matches` rows have non-null brawler columns.

---

### Task 3.2 — Reuse aiohttp session for player info phase (Fix 2E)

**File**: `DB_Data_Pull/pull_data_4.py`

**Current issue**: The player info phase (line 639) opens a new `aiohttp.ClientSession` with a new `TCPConnector`.

**Change**: Remove the inner `connector2` and `session_info` creation. Instead, pass the **existing** `session` (opened at line 561) into the player info coroutines. The `async with aiohttp.ClientSession(...)` block at lines 639–654 becomes:

```python
# Reuse the existing session from BFS phase
to_fetch = [t for t in discovered_tags_list if t not in player_info_cache]
tasks_info = [
    fetch_player_info_async(t, api_key, session, semaphore, rate_limiter)
    for t in to_fetch
]
results_info = await asyncio.gather(*tasks_info)
for t, info_json in zip(to_fetch, results_info):
    if info_json:
        player_info_cache[t] = info_json
pbar_info.update(len(to_fetch))
```

**Note**: This requires moving the player info fetch block **inside** the `async with aiohttp.ClientSession(...) as session:` block (currently at line 561). Currently the `clean_rows` / enrichment section is outside that block (line 632). Move it inside, before `pbar_bfs.close()` or restructure the block boundary.

**Acceptance test**: Same as Task 3.1. Confirm no `RuntimeError: Session is closed` exceptions.

---

## Appendix: Key Code Locations

| Component | File | Lines |
|-----------|------|-------|
| `process_tags_and_write_async` signature | `DB_Data_Pull/pull_data_4.py` | 517–532 |
| `visited_tags` / `enqueued` init | `DB_Data_Pull/pull_data_4.py` | 552–554 |
| BFS queue init | `DB_Data_Pull/pull_data_4.py` | 583–584 |
| BFS while loop | `DB_Data_Pull/pull_data_4.py` | 589–630 |
| BFS enqueue guard | `DB_Data_Pull/pull_data_4.py` | 622–626 |
| `pbar_bfs.close()` | `DB_Data_Pull/pull_data_4.py` | 630 |
| Enrichment + row build | `DB_Data_Pull/pull_data_4.py` | 632–665 |
| Final DB write | `DB_Data_Pull/pull_data_4.py` | 666–675 |
| `insert_rows_matches_in_chunks` | `DB_Data_Pull/pull_data_4.py` | 359–381 |
| Player info fetch phase | `DB_Data_Pull/pull_data_4.py` | 636–654 |
| `create_matches_table_if_not_exists` | `data_clean/schema.py` | 47–65 |
| `get_matches_insert_statement` | `data_clean/schema.py` | 68–76 |
| `run_once.py` entry point | `DB_Data_Pull/run_once.py` | 57–102 |
| `queue_runs.py` main loop | `scripts/queue_runs.py` | 63–101 |
| Notebook crawler knobs cell | `notebooks/clean_db_builder.ipynb` | cell `d6de3f2c` |

---

## Appendix: Invariants — Do Not Break

- `INSERT OR IGNORE` on `matches` with unique index `(battle_time, map, star_player_tag)` must remain unchanged. This is the final dedup safety net.
- `visited_tags` must still be populated from actual fetch operations (Task 1.4 pre-seeds it; the BFS loop at line 600 adds to it during the run).
- `fetched_tags_ttl_hours=0.0` must be a fully backward-compatible no-op — all new code paths must be guarded by `if fetched_tags_ttl_hours > 0.0`.
- `flush_every_n_batches=0` must be a fully backward-compatible no-op.
