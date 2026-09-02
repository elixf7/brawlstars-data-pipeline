# ETL Pipeline Optimization Plan

## Context

This document analyzes the two main inefficiency categories in the BrawlStars ETL pipeline and proposes concrete improvements. No code changes are made here — this is a planning document.

---

## Problem 1: BFS Overlap and Redundant API Calls

### Root Cause

The BFS `visited_tags` set and `enqueued` set that prevent redundant work are **in-memory and process-scoped**. They reset on every call to `process_tags_and_write_async`. This means:

- If `queue_runs` spawns 22 subprocess runs (as in the notebook output), each run starts with a fresh `visited_tags = set()`. A tag discovered in run 3 can be fetched again in run 10.
- BFS from different seed tags naturally converge — popular high-Elo players appear in many battle logs. As the DB grows, an increasing fraction of API calls resolve to tags already fetched.
- The only protection against duplicate **data** is the SQLite `INSERT OR IGNORE` on `(battle_time, map, star_player_tag)`. This discards duplicate rows at write time but does nothing to prevent the API call from happening in the first place.

### Compounding Issue: No DB-Aware Enqueue Filter

When a tag is discovered at BFS depth+1, the code checks only:
1. Is the tag already in `visited_tags` (this run only)?
2. Is the tag already in `enqueued` (this run only)?
3. Does the Elo fall in `[elo_queue_min, elo_queue_max]`?

There is no check like: "Does the DB already have recent battle log data for this tag?" As the database grows past ~500k matches, a large fraction of newly-discovered tags are already well-represented in the DB. The BFS still queues and fetches them, burning rate-limit quota.

### Concrete Consequences

- **Efficiency degrades with DB size.** At 1M rows the overlap ratio is already significant; at 3M rows it may dominate.
- **API quota is wasted on redundant fetches** that will produce `INSERT OR IGNORE` skips.
- **Each `queue_runs` re-run of the same seed file** re-fetches every tag in the file from scratch.

### Proposed Fixes

#### Fix 1A: Persistent Fetched-Tags Table

Add a lightweight `fetched_tags` table to the clean DB:

```sql
CREATE TABLE IF NOT EXISTS fetched_tags (
    tag TEXT PRIMARY KEY,
    last_fetched_utc TEXT NOT NULL
);
```

Before enqueueing a tag during BFS expansion, check this table. Skip the tag if it was fetched within a configurable TTL (e.g., 12 or 24 hours). After a batch completes, bulk-insert all fetched tags.

**Impact**: Eliminates cross-run redundancy with a single indexed lookup per discovered tag.

#### Fix 1B: Pre-filter Seeds Against DB

Before a run starts, filter `initial_tags` to exclude any tags already present in `fetched_tags` (or that have a sufficient match count). Only seed with genuinely new tags.

This is already partially supported via `prefilter_initial_tags` but uses the expensive `/players/{tag}` endpoint for filtering. Switching to a local DB lookup is orders-of-magnitude cheaper.

#### Fix 1C: BFS Expansion Threshold

During BFS, if a tag has already been fetched this season (in `fetched_tags`), **do not expand its neighbors**. The tag itself is already represented; exploring its neighborhood again yields a high overlap fraction.

This reduces the BFS fan-out significantly once the DB is large, and is the main driver of the diminishing-returns problem described in Problem 1.

#### Fix 1D: Load Visited Tags from DB at Run Start

At the beginning of `process_tags_and_write_async`, pre-load all tags from `fetched_tags` into `visited_tags`. This makes even the first BFS iteration skip already-fetched tags, preventing re-work within the same session if the DB is warm.

---

## Problem 2: Pipeline Speed Bottlenecks

### Current Architecture

The pipeline uses `asyncio` with:
- A `Semaphore(concurrency)` (default 30) limiting parallel in-flight requests
- A sliding-window `AsyncRateLimiter` (default 1000 req/sec in the notebook)
- `aiohttp.TCPConnector` with `limit=concurrency`
- BFS loop processes one batch of up to `batch_size=1500` tags at a time, awaiting all before advancing

### Identified Bottlenecks

#### Bottleneck 2A: Sequential BFS Batches

The BFS loop is:
```
while queue:
    take up to batch_size tags
    await all fetches for this batch      # blocks here
    enqueue new tags
```

The inner `asyncio.gather()` is parallel within a batch, but the outer loop is sequential: **batch N+1 cannot start until batch N is fully complete**. With a batch of 1500 tags and even 1% of them being slow (timeouts, retries), the entire batch stalls.

**Fix**: Switch to a streaming producer-consumer model. Use an `asyncio.Queue` to decouple discovery from fetching. Worker coroutines pull from the queue continuously, write newly discovered tags back to the queue, and never block the whole batch on a slow tail request.

#### Bottleneck 2B: Player Info Fetch is Sequential Chunks

When `fetch_player_data=True`, the player info phase fetches all discovered tags in sequential `batch_size` chunks. With 50k discovered tags and `batch_size=1500`, this is 33 sequential rounds even though each round itself is parallel.

**Fix**: Flatten the player info fetch into a single `asyncio.gather()` call, relying solely on the semaphore for concurrency control. The sequential chunking is unnecessary given the semaphore already limits parallelism.

#### Bottleneck 2C: Clean-Row Building is Single-Threaded

After fetching, `build_clean_rows()` parses and transforms match records synchronously before the DB write. This runs on the event loop thread and blocks while processing potentially thousands of records.

**Fix**: Move `build_clean_rows()` into `asyncio.to_thread()` alongside the DB write, or pre-process rows incrementally as battle logs arrive rather than accumulating all of them first.

#### Bottleneck 2D: DB Writes are Batched Only at End of BFS

`insert_rows_matches_in_chunks()` runs once after the full BFS sweep completes. This means:
- All clean rows must be kept in memory until the BFS is done
- A crash mid-run loses all progress

**Fix**: Flush to the DB incrementally (every N rows or every K batches). This also enables the `fetched_tags` table to be updated mid-run, which cascades into Fix 1D (more tags marked visited sooner).

#### Bottleneck 2E: aiohttp Session Created Per BFS Phase

A new `aiohttp.ClientSession` (and `TCPConnector`) is created for the player info fetch phase (separate from the battle log phase). TCP connection setup overhead applies to all connections in the new session.

**Fix**: Reuse a single session and connector for the entire run. The connector's connection pool will reuse open TCP connections, reducing handshake overhead.

#### Bottleneck 2F: Subprocess Overhead in queue_runs

Each batch in `queue_runs` launches a new Python subprocess. This incurs:
- Python startup time (~200-500ms)
- Module import overhead (aiohttp, pandas, sqlite3, etc.)
- A new DB connection open/close cycle

With 22 batches (as in the notebook run), this overhead is real but modest. At `--max-runs 500` with very small `--per-run-tags`, it can add up.

**Fix**: If cross-run tag deduplication is solved (Fix 1A/1B), consider increasing `--per-run-tags` to reduce the number of subprocess launches while keeping total API calls the same. Alternatively, implement `queue_runs` as an in-process loop rather than subprocesses, sharing a single Python process and DB connection.

---

## Recommended Priority Order

| Priority | Fix | Effort | Impact |
|----------|-----|--------|--------|
| 1 | **1A**: Persistent `fetched_tags` table | Medium | High — eliminates cross-run redundancy |
| 2 | **1D**: Pre-load visited tags from DB at run start | Low | High — effective immediately for warm DBs |
| 3 | **1C**: Skip BFS expansion for already-fetched tags | Low | High — directly addresses diminishing returns |
| 4 | **2A**: Producer-consumer async queue | High | Medium — reduces tail latency stalls |
| 5 | **2D**: Incremental DB flush | Medium | Medium — crash resilience + enables Fix 1D mid-run |
| 6 | **1B**: Pre-filter seeds against DB | Low | Medium — cleaner seed selection |
| 7 | **2B**: Flatten player info fetch | Low | Low-Medium — only relevant when `fetch_player_data=True` |
| 8 | **2C**: Offload row building to thread | Low | Low — currently not a measured bottleneck |
| 9 | **2E**: Reuse aiohttp session | Low | Low — minor connection overhead |
| 10 | **2F**: Reduce subprocess launches | Medium | Low — overhead is small at current batch counts |

---

## Key Invariant to Preserve

The SQLite unique index on `(battle_time, map, star_player_tag)` with `INSERT OR IGNORE` is a solid correctness guarantee and should not be removed even after adding `fetched_tags`. It remains the final defense against duplicate rows regardless of how upstream deduplication improves.

---

## Open Questions Before Implementing

1. **TTL for `fetched_tags`**: How stale is "too stale"? A player's battle log rolls over (only last 25 battles returned by the API). If a player is very active, their log from 6 hours ago may miss new games. What staleness window is acceptable given the season-level analysis goals?

2. **`fetched_tags` write timing**: Should tags be written to `fetched_tags` immediately after a successful fetch (mid-run), or only after the clean rows are written to `matches`? Writing early risks marking a tag as fetched even if the match rows were lost to a crash.

3. **Does `prefilter_initial_tags` use case still apply?** If `fetched_tags` replaces the current pre-filter, the `/players/{tag}` Elo-based pre-filter can be dropped, saving additional API calls.

4. **Producer-consumer complexity**: Fix 2A requires restructuring the BFS loop substantially. Is the current tail-latency stall actually measurable given the large batch sizes used? Worth profiling a run with timing logs before investing in this refactor.
