# Seed tags

Player tags a season's **first** crawl starts from, one per line, with the
leading `#`. Named `<season>.txt`, matching what `bsetl-season current` reports.

**You do not normally need to put anything here.** A season with no stored state
is seeded automatically from the strongest players of the season before it, so a
reset needs no attention:

```bash
uv run bsetl-state seeds --repo-id "$DATASET_REPO" --season season54 \
  --out seeds/season54.txt --min-elo 18 --limit 5000
```

That is the step the workflow runs on the first run of each season. Elo resets
along with the season, so those players start low and climb back — what carries
across is who they are, not what they were rated.

A committed `<season>.txt` takes precedence when one exists, which is how to
override the choice for a particular season. Later runs need neither: unvisited
tags live in the `crawl_frontier` table inside the season database, and each
scheduled run resumes from there.

Tags are public in-game identifiers, so committing them is fine.
