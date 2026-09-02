# data/

Pipeline output. Everything under `data/seasons/` is git-ignored — season
databases run 250–900 MB, and generated sidecars are reproducible from them.

```
data/seasons/<season>/
    <name>.db                      SQLite, one row per ranked set
    <season>_v1_metadata.json      dataset summary written at build time
    <season>_skill_ns_metadata.json  per-bin skill-feature provenance
    seed_tags.txt                  seed player tags for the crawl
```

Create a season by pointing the CLI at a path under here:

```bash
uv run bsetl-ingest --tags '#9UUU9QVU' --clean-db-path data/seasons/season50/v1.db
```

Exports built for publication land beside them:

```
data/exports/<season>/
    README.md                      generated dataset card
    metadata.json                  season summary plus export stats
    data/battle_date=YYYY-MM-DD/   Parquet, one file per day
    <season>.db                    optional SQLite copy, pipeline state stripped
```

```bash
uv run bsetl-export --clean-db-path data/seasons/season50/v1.db --out-dir data/exports/season50
```

Schema reference is in [`docs/DATA_DICTIONARY.md`](../docs/DATA_DICTIONARY.md).
Published datasets are the distribution channel for the data itself; this
directory is a local working area.
