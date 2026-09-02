"""Dataset card generation.

A dataset nobody can interpret is not published, only uploaded. The card is
generated from the same metadata the export computes, so the numbers on it
cannot drift from the files beside it.
"""
from __future__ import annotations

from typing import Any

_FRONTMATTER = """---
license: mit
language:
  - en
pretty_name: {pretty_name}
size_categories:
  - {size_category}
task_categories:
  - tabular-classification
tags:
  - games
  - esports
  - telemetry
  - brawl-stars
configs:
  - config_name: default
    data_files: "data/**/*.parquet"
---
"""


def _size_category(rows: int) -> str:
    for limit, label in ((1_000, "n<1K"), (10_000, "1K<n<10K"), (100_000, "10K<n<100K"),
                         (1_000_000, "100K<n<1M"), (10_000_000, "1M<n<10M")):
        if rows < limit:
            return label
    return "10M<n<100M"


def _fmt_time(ts: str | None) -> str:
    """20251102T002849.000Z -> 2025-11-02."""
    if not ts or len(ts) < 8 or not ts[:8].isdigit():
        return "unknown"
    return f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}"


def render_dataset_card(
    meta: dict[str, Any],
    *,
    export: dict[str, Any] | None = None,
    repo_id: str | None = None,
    source_repo: str = "https://github.com/elixf7/brawlstars-data-pipeline",
) -> str:
    rows = int(meta.get("num_matches") or 0)
    modes = meta.get("modes") or []
    maps = meta.get("maps") or []
    season = meta.get("season_label") or "unknown"
    export = export or {}

    top = meta.get("brawler_usage_top") or []
    top_rows = "\n".join(
        f"| {name} | {count:,} |" for name, count in top[:10]
    ) or "| — | — |"

    parts = [
        _FRONTMATTER.format(
            pretty_name=f"Brawl Stars Ranked Matches ({season})",
            size_category=_size_category(rows),
        ),
        f"""
# Brawl Stars Ranked Matches — {season}

Ranked match telemetry from the public Brawl Stars API, one row per **ranked
set**. A set is up to three games on one fixed map and mode, first team to two
wins; `record` holds the game-by-game outcome.

Collected and published automatically by [an open-source ETL pipeline]({source_repo}).

## At a glance

| | |
| --- | --- |
| Ranked sets | {rows:,} |
| Covering | {_fmt_time(meta.get('start_time'))} to {_fmt_time(meta.get('end_time'))} |
| Distinct brawlers | {meta.get('num_unique_brawlers', 'unknown')} |
| Modes | {len(modes)} |
| Maps | {len(maps)} |
| Format | Parquet, partitioned by `battle_date` |
""",
    ]

    if export:
        parts.append(
            f"| Files | {export.get('files', '?')} |\n"
            f"| Size | {export.get('parquet_bytes', 0) / 1e6:.0f} MB "
            f"({export.get('compression_ratio', 0):.1f}x smaller than the source database) |\n"
        )

    parts.append(f"""
## Loading

```python
from datasets import load_dataset

ds = load_dataset("{repo_id or 'your-name/brawlstars-ranked'}", split="train")
```

Partitioning by day means a time slice can be read without scanning the season:

```python
import pyarrow.dataset as ds_
table = ds_.dataset("data", partitioning="hive").to_table(
    filter=ds_.field("battle_date") >= "2025-11-10"
)
```

## Columns

One row per ranked set. Core fields cover time, mode, map, and the record; the
six drafted brawlers are flattened into `t{{team}}_b{{slot}}_*` columns.

| Column | Meaning |
| --- | --- |
| `battle_time` | UTC timestamp of the last game in the set |
| `mode`, `map` | Fixed for the whole set |
| `record` | Game-by-game result, e.g. `T1-T1`, `T2-T1-T1` |
| `star_*` | The final game's MVP: brawler, power, tag, elo |
| `avg_elo` | Mean elo across the six players |
| `skill_ns` | `avg_elo` normalized against the elo distribution local in time |
| `skill_ns_ok` | 1 when the row's time bin had enough samples to trust `skill_ns` |
| `t{{1,2}}_b{{0,1,2}}_*` | Per-brawler name, elo, rank, highest trophies, power |

Filter on `skill_ns_ok = 1` for analyses that depend on the skill feature.

### Why `skill_ns` exists

Ranked elo resets each season and re-stratifies over the following weeks, so an
average of 16 in week one describes a very different match from an average of 16
in week three. `skill_ns` converts each set's `avg_elo` to a percentile within a
fixed 3-day time bin and maps it to a symmetric unbounded scale, making matches
comparable across the season.

## Modes and maps

**Modes:** {', '.join(f'`{m}`' for m in modes) if modes else 'unknown'}

**Maps:** {', '.join(maps) if maps else 'unknown'}

## Most-picked brawlers

| Brawler | Picks |
| --- | --- |
{top_rows}

## Limitations

- **No draft order and no bans.** The API returns the six final brawlers with no
  pick sequence, and the slot index is positional, not draft order.
- **Not a uniform sample.** Rows are gathered by crawling the player graph
  breadth-first from seed players, filtered to a target elo band. Popular and
  higher-rated players are over-represented relative to the whole population.
- **One season per dataset.** Balance changes shift the meta between seasons, so
  pooling them mixes distinct regimes.
- **Elo is per-brawler**, not per-player: a strong player on an unfamiliar
  brawler carries a low rating into the match.

## Provenance

Built from `soloRanked` battles only. Deduplicated on
`(battle_time, map, star_player_tag)`. Sets with an average elo above 23 are
dropped as bot or corrupted records. Full pipeline documentation and design
notes are in the [source repository]({source_repo}).

## License and attribution

MIT for the pipeline and this dataset compilation. Derived from the public Brawl
Stars API. Not affiliated with, endorsed, sponsored, or specifically approved by
Supercell; fan content made under Supercell's
[Fan Content Policy](https://supercell.com/en/fan-content-policy/).
""")
    return "".join(parts)
