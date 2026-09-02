# Seed tags

Player tags a season's **first** crawl starts from, one per line, with the
leading `#`. Named `<season>.txt`, matching what `bsetl-season current` reports.

Later runs do not need this file. Once a crawl has run, unvisited tags live in
the `crawl_frontier` table inside the season database, and each scheduled run
resumes from there — the workflow logs a note and carries on if the file is
absent.

A useful set of seeds is a few hundred tags spread across the target elo band.
Sample them from a previous season rather than inventing them:

```python
from bsetl.state import sample_seed_tags_from_clean_db

tags = sample_seed_tags_from_clean_db(
    "data/seasons/season49/v1.db", num_tags=500, elo_range=(15, 23)
)
Path("seeds/season50.txt").write_text("\n".join(tags) + "\n")
```

Tags are public in-game identifiers, so committing them is fine.
