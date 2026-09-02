# Brawl Stars Ranked Telemetry ETL

An automated pipeline that ingests ranked match data from the public Brawl Stars
API and turns it into an analysis-ready dataset for model training.

It crawls the player graph breadth-first from a set of seed tags, reconstructs
complete ranked sets from battle logs, and writes them into a wide, one-row-per-set
SQLite schema with idempotent inserts. A derived skill feature normalizes match
strength against the elo distribution local in time, which corrects for the
league-wide elo drift that makes raw elo incomparable across a season.

Roughly 2.7M ranked sets per season, ~600MB per database.

> **Status.** The extraction, schema, and skill-feature stages are complete and in
> use. Scheduled execution, the quality gate, and dataset publishing are being
> built out — see [`docs/design/`](docs/design/) for the plans.

## How it fits together

```
Brawl Stars API
      │  async BFS over the player graph, rate-limit aware
      ▼
  ingest/          crawler.py · ratelimit.py
      │  reconstructed sets, elo-gated
      ▼
  transform/       schema.py · skill_features.py · metadata.py
      │  wide matches table + skill_ns + metadata sidecar
      ▼
  season database  ──▶  downstream model training (LogR-MCTS)
```

| Package | Holds |
| --- | --- |
| `bsetl.ingest` | The crawler and its request pacing |
| `bsetl.transform` | Schema, the `skill_ns` feature, dataset metadata |
| `bsetl.state` | Durable pipeline state — seed sampling, fetched-tag tracking |
| `bsetl.quality` | Data quality checks that gate publication |
| `bsetl.cli` | Command-line entry points |

## Setup

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

Get a key from the [developer portal](https://developer.brawlstars.com) and export it.
Keys are locked to the IP that created them, so a key made at home returns 403
anywhere else.

```bash
export BS_API_KEY="your-key"
```

## Running it

**One crawl.** Walks outward from the seed tags to `--max-depth`, keeping sets whose
average elo falls in the game range and only following players in the queue range.

```bash
uv run bsetl-ingest --tags '#9UUU9QVU' --clean-db-path data/seasons/season50/v1.db --max-depth 2 --elo-queue-min 13 --elo-queue-max 23 --elo-game-min 12 --elo-game-max 23 --fetched-tags-ttl-hours 24
```

**Many short crawls.** Chunks a seed file into separate subprocesses so memory stays
bounded on long runs.

```bash
uv run bsetl-queue --tags-file data/seasons/season50/seed_tags.txt --per-run-tags 5 --max-runs 500 --clean-db-path data/seasons/season50/v1.db --elo-game-min 12 --elo-game-max 23 --fetched-tags-ttl-hours 24
```

**Compute the skill feature.** Adds `skill_ns` and `skill_ns_ok` to `matches` in place,
plus a `skill_bin_metadata` audit table recording each bin's sample count and config.

```bash
uv run bsetl-skill-features --clean-db-path data/seasons/season50/v1.db --bin-width-days 3 --min-bin-count 100000 --mapping logit
```

Then explore what landed with [`notebooks/explore_season.ipynb`](notebooks/explore_season.ipynb)
(`uv sync --extra notebook` first). The notebook does not run the pipeline — that is
what the CLI is for.

## What's in the data

Full column reference: [`docs/DATA_DICTIONARY.md`](docs/DATA_DICTIONARY.md).

One row per ranked set — up to three games on a fixed map, first team to two wins.
Core fields cover time, mode, map, and the game-by-game record; the six drafted
brawlers are flattened into `t{team}_b{slot}_*` columns; `avg_elo` is the mean elo
across the six players, and `skill_ns` is its time-local normalization.

Deduplication is enforced by a unique index on `(battle_time, map, star_player_tag)`,
so re-crawling the same players is safe and cheap.

**Limitations worth knowing.** The API exposes only the final six brawlers — no bans
and no pick order, so draft sequence cannot be recovered. Battle logs hold only a
player's last ~25 battles, which sets how often re-crawling a player is worthwhile.
Only `soloRanked` battles are ingested.

## Credentials and security

The repository stores no credentials. There is exactly one way to supply a key
locally: the `BS_API_KEY` environment variable.

Automated runs never use a stored key at all. The scheduled job signs in to the
developer portal with credentials held as repository secrets, mints a key scoped to
the runner's own IP, and revokes it when the run ends — so no long-lived key exists
to leak.

- Never pass a key as a command-line argument; it lands in shell history and the
  process table. Both CLIs read it from the environment, and child processes inherit
  it rather than receiving it as an argument.
- `.env` is git-ignored. `.env.example` documents the variables.
- Season databases are git-ignored — they are hundreds of megabytes and belong in the
  published dataset, not in version control.

## License

MIT — see [LICENSE](LICENSE). Not affiliated with or endorsed by Supercell; fan
content made under Supercell's
[Fan Content Policy](https://supercell.com/en/fan-content-policy/).
