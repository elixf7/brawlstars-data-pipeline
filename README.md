# Brawl Stars Data Pipeline

Collects ranked match data from the Brawl Stars API and publishes it as a
versioned dataset, on a schedule, without anyone watching.

Around 570,000 ranked sets per season, republished twice a week to
[Hugging Face](https://huggingface.co/datasets/EliF77/brawlstars-ranked).

[![Ingest](https://github.com/elixf7/brawlstars-data-pipeline/actions/workflows/pipeline.yml/badge.svg)](https://github.com/elixf7/brawlstars-data-pipeline/actions/workflows/pipeline.yml)
[![CI](https://github.com/elixf7/brawlstars-data-pipeline/actions/workflows/ci.yml/badge.svg)](https://github.com/elixf7/brawlstars-data-pipeline/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**[Live status →](https://elixf7.github.io/brawlstars-data-pipeline/)** ·
**[Dataset →](https://huggingface.co/datasets/EliF77/brawlstars-ranked)** ·
**[What uses it →](https://github.com/elixf7/brawlstars-draft-agent)**

---

## The problem

The Brawl Stars API has no endpoint for recent matches. You can ask about one
player at a time, and you get roughly their last 25 battles. There is no way to
list players, no pagination into history, and no bulk export.

So a dataset has to be assembled rather than downloaded. Every ranked battle
names all six participants, which makes each fetched battle log a source of new
players to look up. The pipeline crawls outward from a handful of seed players,
storing what it finds and following the players it meets.

Three constraints shape everything else:

- **Keys are locked to an IP address.** The permitted address is inside the
  token, so a key created on a laptop is refused from a CI runner.
- **The API is rate limited**, and the battle log window is short — re-visiting
  a player too soon returns matches already stored.
- **Crawl efficiency falls as the dataset grows.** Independent crawls converge
  on the same popular players, so an increasing share of requests return data
  already held.

## What it does

```
Brawl Stars API
      │   breadth-first crawl over the player graph
      ▼
   ingest ──────── bounded by requests, time, and yield
      │
      ▼
 season database ── SQLite: matches, crawl frontier, run history
      │
      ├──▶ skill feature ── normalises rating against its own moment in time
      │
      ├──▶ quality gate ─── 18 checks; failure blocks publication
      │
      ▼
   published ────── Parquet on Hugging Face, partitioned by season and day
```

Twice a week the pipeline restores the working database
from the Hub, crawls until a budget stops it, recomputes the derived skill
feature, runs the quality gate, publishes if it passes, and stores the database
back for the next run.

### Ingestion

A breadth-first crawl with two separate elo filters: one deciding which matches
to keep, another deciding which players to follow. They want different values —
following slightly stronger players yields denser coverage of the band being
collected, while collapsing them into one range either starves the crawl or
widens what gets stored.

Matches are identified by `(battle_time, map, star_player_tag)` under a unique
index, so re-crawling is a no-op rather than a source of duplicates. That is
what makes an unattended schedule safe: a run that fails halfway can simply run
again.

### Bounded runs

A crawl stops on a request ceiling, a time ceiling, or when it stops finding
anything new — and records which. Yield is measured on rows *actually inserted*
over a trailing window, not rows attempted, because deduplication makes those
diverge sharply once the database is warm.

Unvisited players are written to a frontier table and reloaded by the next run,
so a series of short runs behaves as one continuous crawl.

### The skill feature

Ranked ratings reset each season and re-spread over the following weeks, so the
same numeric rating means different things at different points in a season.
`skill_ns` converts each match's average rating into its percentile *within a
three-day window* and maps that onto a symmetric scale, which is comparable
across the whole season.

Bins with too few samples are marked untrustworthy rather than filled in, and
consumers filter on that flag.

### Credentials

Since keys are locked to an IP the runner does not know in advance, no key is
stored. Each run signs in to the developer portal, mints a key scoped to its own
address, uses it, and revokes it on the way out. The address comes from the login
response itself, which returns a token carrying the address the portal observed.

Keys left behind by interrupted runs are reclaimed automatically, since the
account allows only ten.

### The quality gate

Eighteen checks run before anything is published, and a failure stops it.
Severity is the design: things that make the data *wrong* fail — a missing
deduplication index, duplicate matches, impossible ratings, a collapsed character
roster, skill metadata labelled with the wrong season. Things that are merely
*surprising* warn, because a new game mode is a rotation change rather than a
defect, and a gate that fails on novelty gets switched off.

### Seasons

Ranked resets on the third Thursday of each month. The pipeline computes the
current season from the calendar rather than being configured with it, so
rollover is automatic. A database is not allowed to span a reset: mixing two
rating regimes under one label would corrupt the skill feature, which normalises
within time windows.

## Using the data

```python
from datasets import load_dataset

ds = load_dataset("EliF77/brawlstars-ranked", split="train")
```

One row per **set** — up to three games on a fixed map, first to two wins.
Partitioned by season and day, so a single week reads without scanning the rest.

| Column | Meaning |
| --- | --- |
| `battle_time` | UTC timestamp of the final game in the set |
| `mode`, `map` | Fixed for the whole set |
| `record` | Game-by-game result, e.g. `T1-T1`, `T2-T1-T1` |
| `t{1,2}_b{0,1,2}_*` | The six drafted characters and their ratings |
| `avg_elo` | Mean rating across the six players |
| `skill_ns` | `avg_elo` normalised against its own moment in the season |
| `skill_ns_ok` | Whether that normalisation is trustworthy for this row |

Around 2.7M sets in a full season, roughly 600 MB as SQLite and 66 MB as
Parquet. Full column reference in [`docs/DATA_DICTIONARY.md`](docs/DATA_DICTIONARY.md).

**Worth knowing before modelling on it.** The API exposes the six final
characters but no pick order and no bans. Battle logs hold only a player's
recent matches, so partial sets are normal — a bare `T1` is about a quarter of
rows. And the sample is not uniform: crawling outward from seed players within an
elo band over-represents active and higher-rated players.

## Running it yourself

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
export BS_API_KEY="your-key"        # from developer.brawlstars.com

uv run bsetl-ingest --tags '#9UUU9QVU' --clean-db-path data/seasons/s54/v1.db \
  --max-requests 20000 --max-seconds 2400 --min-rows-per-1k-requests 40
uv run bsetl-check  --clean-db-path data/seasons/s54/v1.db
uv run bsetl-export --clean-db-path data/seasons/s54/v1.db --out-dir data/exports/s54
```

Logs go to stderr and the run summary is JSON on stdout, so the two separate
cleanly. Every run records itself — configuration, stop reason, requests, rows
inserted, HTTP outcomes, frontier size before and after:

```python
from bsetl.state import recent_runs
recent_runs("data/seasons/s54/v1.db", limit=5)
```

Full setup for the automated version, including credentials, is in
[`docs/SETUP.md`](docs/SETUP.md).

## Commands

| | |
| --- | --- |
| `bsetl-ingest` | Run one bounded crawl |
| `bsetl-check` | Quality gate; exits non-zero on failure |
| `bsetl-export` | Parquet plus a dataset card |
| `bsetl-publish` | Upload to the Hub |
| `bsetl-season` | Which season is running, and when the next starts |
| `bsetl-key` | Mint, list, and reclaim API keys |
| `bsetl-state` | Move the working database to and from the Hub |
| `bsetl-report` | Render the status page |

## Documentation

| | |
| --- | --- |
| [`docs/DATA_DICTIONARY.md`](docs/DATA_DICTIONARY.md) | Every column, table, and index |
| [`docs/DESIGN.md`](docs/DESIGN.md) | Why the pipeline is built this way |
| [`docs/DOMAIN.md`](docs/DOMAIN.md) | Enough Brawl Stars to read the data |
| [`docs/SETUP.md`](docs/SETUP.md) | Running the automated pipeline yourself |

## License

MIT — see [LICENSE](LICENSE). Not affiliated with or endorsed by Supercell; fan
content made under Supercell's
[Fan Content Policy](https://supercell.com/en/fan-content-policy/).
