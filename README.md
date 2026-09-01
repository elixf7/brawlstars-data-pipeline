### Brawl Stars Ranked Data Extractor

This repository provides a minimal, production-friendly pipeline to extract ranked match data from the Brawl Stars public API efficiently and store it in SQLite for future use. It focuses purely on data ingestion and storage; all unrelated research and model-training code has been removed.

The extractor performs a breadth-first crawl over player tags found in ranked matches, fetches battle logs and (optionally) player profiles, and writes normalized rows into a single SQLite table. It includes resilient rate-limit handling, chunked async I/O, and utilities for deduplication and indexing.

### Key Features

- **Async, rate-limit-aware HTTP**: Uses `aiohttp` with a shared semaphore and `Retry-After` backoff for `429` responses.
- **Targeted ranked data**: Filters for `soloRanked` matches and reconstructs full set results and participant info.
- **BFS crawl**: Discovers additional player tags from ranked matches to broaden coverage (bounded by `max_depth`).
- **SQLite storage**: Appends rows efficiently in chunks; utilities provided for indexing and deduplication.
- **Optional player profile enrichment**: Toggle `fetch_player_data` to include per-player metadata.

### Project Structure

- `DB_Data_Pull/pull_data_4.py`: Core extractor, SQLite utilities, and helpers.

### Prerequisites

- Python 3.10+
- A Brawl Stars API key from the developer portal: [Brawl Stars Developer](https://developer.brawlstars.com/)
- Ensure your current machine IP is allowed for the key in the developer portal.

Install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Conventions (Phase 0)

- Environment variable for API access: prefer `BS_API_KEY` (falls back to `BRAWLSTARS_API_KEY` if set).
- Create the following directories before running:
  - `data_raw/`
  - `data_clean/`
  - `notebooks/`
- Seasonal clean DB files will follow: `data_clean/season<id>_clean.db` (e.g., `season37_clean.db`). Raw mirrors are optional and, if used, should live under `data_raw/Season<id>/SQLite/`.

You can use `utils.env_utils.ensure_directories([...])` to create the directories if missing, and `utils.env_utils.get_api_key()` to read the API key.

### Database Schema

The extractor writes rows into a SQLite table you specify (e.g., `games`). Columns:

- `id` INTEGER PRIMARY KEY AUTOINCREMENT
- `Battle_Time` TEXT (ISO-8601 time of the last game in the set, e.g., `20250310T025416.000Z`)
- `Mode` TEXT
- `Map` TEXT
- `Record` TEXT (e.g., `T1-T1`, `T2-T1`, `T1-D-T2`)
- `Star_Player` TEXT (JSON-encoded tuple `[BrawlerName, PowerLevel, PlayerTag, BrawlerElo]`)
- `Team1_Brawlers` TEXT (JSON list of 3 dicts)
- `Team1_Players` TEXT (JSON list of 3 dicts; may be `NULL` when `fetch_player_data=False`)
- `Team2_Brawlers` TEXT (JSON list of 3 dicts)
- `Team2_Players` TEXT (JSON list of 3 dicts; may be `NULL` when `fetch_player_data=False`)

An index helper is provided on `(Battle_Time, Map, Star_Player)` to speed up deduplication.

### Quick Start

1. Export your API key or place it securely in your environment manager.

```bash
export BS_API_KEY="REDACTED"
```

2. Run the extractor from a short Python script (example below). It will crawl ranked matches starting from your seed tags and write to a SQLite DB.

```python
import os
import asyncio
from datetime import datetime, timezone

from DB_Data_Pull.pull_data_4 import (
    process_tags_and_write_async,
    create_index,
    remove_duplicates_by_columns,
    pull_random_tags,
    getUpdatedIP,
)
from utils.env_utils import get_api_key, ensure_directories

API_KEY = get_api_key()  # prefers BS_API_KEY, falls back to BRAWLSTARS_API_KEY

# Optional: verify the API key and IP allow-listing works from this machine
# getUpdatedIP(API_KEY)

# Seed tags to start crawling from (must include the leading '#')
initial_tags = ["#9UUU9QVU"]  # replace or expand as desired

# Only ingest sets strictly after this timestamp (UTC)
latest_runtime = datetime(2024, 1, 1, tzinfo=timezone.utc)

ensure_directories(["data_raw", "data_clean", "notebooks"])  # Phase 0: ensure basic dirs
db_path = "brawlstars.db"
table_name = "games"

async def main():
    await process_tags_and_write_async(
        player_tags=initial_tags,
        api_key=API_KEY,
        latest_runtime=latest_runtime,
        db_path=db_path,
        table_name=table_name,
        max_depth=2,        # crawl depth over discovered tags
        batch_size=1500,    # batch size for async fan-out
        concurrency=40,     # concurrent HTTP requests; tune to your network and rate limits
        fetch_player_data=False,  # Phase 0 default: minimize API load (toggle to True if needed)
    )

    # Optional hygiene: add index and deduplicate
    create_index(db_path, table_name)
    remove_duplicates_by_columns(db_path, table_name)

if __name__ == "__main__":
    asyncio.run(main())
```

3. Optionally, once you have an existing DB, you can draw additional seed tags from prior rows to broaden future crawls:

```python
from DB_Data_Pull.pull_data_4 import pull_random_tags

seed_tags = pull_random_tags(
    db_path="brawlstars.db",
    table_name="games",
    num_tags=500,
    elo_range=(10, 19),  # target mid-to-high elo
)
```

### Core API Reference

All functions live in `DB_Data_Pull/pull_data_4.py`.

- **process_tags_and_write_async(player_tags, api_key, latest_runtime, db_path, table_name, max_depth=2, batch_size=1500, concurrency=40, fetch_player_data=True)**

  - Default `fetch_player_data` is now `False` to minimize API load; set to `True` to enrich per-player profile fields.

  - Async end-to-end pipeline. Crawls ranked battle logs via BFS over discovered player tags, formats rows, and appends to SQLite.
  - Honors HTTP 429 via `Retry-After`; prints non-fatal HTTP errors but keeps going.

- **create_index(db_path, table_name)**

  - Adds an index on `(Battle_Time, Map, Star_Player)`.

- **remove_duplicates_by_columns(db_path, table_name)**

  - Deduplicates rows by `(Battle_Time, Map, Star_Player)` keeping the smallest `id`.

- **clear_table_in_db(db_path, table_name)**

  - Truncate and reset autoincrement counter.

- **pull_random_tags(db_path, table_name, num_tags, elo_range=(0, 19), search_range='default', columns=...)**

  - Sample seed tags from existing rows to extend crawls (based on star player entries).

- **getUpdatedIP(api_key)**
  - Quick check to verify your API key + IP allow-list is valid from this machine.

### Performance and Tuning

- **Concurrency**: Start with `concurrency=30-60` and adjust based on your network and observed API rate limits. The extractor backs off on `429` using the server-provided `Retry-After` header.
- **Crawl breadth**: Increase `max_depth` to discover more players from ranked lobbies. Depth 2 is a good starting point; higher depths expand exponentially.
- **Batching**: `batch_size` controls fan-out granularity. Larger batches can improve throughput but use more memory.

### Data Notes and Limitations

- Ranked sets are reconstructed from `soloRanked` matches only.
- The Supercell public API does not expose the ban list or exact pick order; only the final six chosen Brawlers are returned.
- Player profile enrichment depends on `fetch_player_data` (set to `False` to speed up runs or avoid profile calls).

### Troubleshooting

- **401/403 errors**: Ensure your API key is correct and the current machine IP is added to the key in the developer portal.
- **429 rate limits**: Reduce `concurrency` or increase crawl intervals; extractor respects `Retry-After` but you may still need to tune.
- **404 notFound**: Some profiles or logs may be unavailable; the extractor logs and continues.

### Credentials and security

The repository stores no credentials of any kind. There is exactly one way to
supply a key locally:

```bash
export BS_API_KEY="your-key-from-developer.brawlstars.com"
```

Keys are CIDR-locked to a single IP, embedded in the token itself, so a key
created at home will return 403 from anywhere else. This is why automated runs
do not use a stored key at all: the scheduled job signs in to the developer
portal with credentials held as repository secrets, mints a key scoped to the
runner's own IP, and revokes it when the run finishes. Nothing long-lived
exists to leak.

- Never pass a key as a command-line argument — it lands in shell history and
  the process table. Both CLIs read it from the environment and pass it to
  child processes by inheritance only.
- `.env` is git-ignored; `.env.example` documents the variables.
- Rotate keys periodically and remove unused allow-listed IPs from the portal.

### License

This repository is trimmed to serve a single purpose: ingest ranked data from the Brawl Stars public API into SQLite. Use responsibly and in accordance with Supercell's terms.
