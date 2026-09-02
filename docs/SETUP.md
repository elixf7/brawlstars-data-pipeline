# Setup

Everything the pipeline needs, in the order it needs it. About 20 minutes.

Nothing here is stored in the repository. Two accounts, one repository variable,
three secrets, and one seed file.

---

## 1. Clean up the old API keys

Four keys were previously committed to a local file. They are not in this
repository's git history, but they existed unprotected on disk and are still
live until revoked.

Go to [developer.brawlstars.com](https://developer.brawlstars.com) → **My Account**
and revoke every key you did not deliberately create. They are identifiable by IP:

| Created | Locked to |
| --- | --- |
| 2024-12-08 | `136.47.193.6` |
| 2025-10-28 | `76.36.228.122` |
| 2025-11-01 | `76.36.244.22` |
| 2026-03-16 | `136.56.124.154` |

**Do not create replacements.** The pipeline mints its own keys per run. You only
need a personal key if you plan to run crawls by hand, and even then you can make
one at the time.

The account allows 10 keys total, so clearing these also frees slots the pipeline
will use.

---

## 2. Confirm your portal login works

This is the credential the pipeline actually uses, and it is worth proving before
anything depends on it.

```bash
export BS_DEV_EMAIL="you@example.com"
export BS_DEV_PASSWORD="your-portal-password"

uv run bsetl-key check
```

Expected:

```
Provisioned and verified:
  host ip : 76.36.244.22
  key id  : a1b2c3...
  key name: bsetl-auto-20260902T...
  (key material is never printed)
Revoked. Credentials work; scheduled runs can mint their own keys.
```

That minted a real key, verified it, and revoked it. If it fails with
`Invalid email or password`, fix that now — every scheduled run starts here.

`uv run bsetl-key list` shows your remaining keys and how this machine appears to
the portal.

---

## 3. Create the Hugging Face dataset

The Hub holds two things: the published dataset, and the working database between
runs. One repository serves both.

1. Sign up at [huggingface.co](https://huggingface.co) if needed.
2. Create a dataset: **New → Dataset**. Name it something like
   `brawlstars-ranked`. Private is fine to start.
3. Create a token: **Settings → Access Tokens → New token**, type **Write**,
   scoped to that dataset. Copy it — it is shown once.

Your repo id is `<your-username>/brawlstars-ranked`.

---

## 4. Configure the GitHub repository

One variable and three secrets. From the command line:

```bash
gh variable set BSETL_DATASET_REPO --body "your-username/brawlstars-ranked"

gh secret set BS_DEV_EMAIL       # portal email
gh secret set BS_DEV_PASSWORD    # portal password
gh secret set HF_TOKEN           # Hugging Face write token
```

Or in the browser: **Settings → Secrets and variables → Actions**, with variables
and secrets on separate tabs.

| Name | Kind | Value |
| --- | --- | --- |
| `BSETL_DATASET_REPO` | Variable | `your-username/brawlstars-ranked` |
| `BS_DEV_EMAIL` | Secret | Developer portal email |
| `BS_DEV_PASSWORD` | Secret | Developer portal password |
| `HF_TOKEN` | Secret | Hugging Face write token |

**There is no season setting.** Ranked resets on the third Thursday of each month,
so the pipeline works out the current season itself and rolls over unattended.

```bash
uv run bsetl-season current
```

---

## 5. Provide seed tags for the first crawl

A season's first run needs somewhere to start. Afterwards each run resumes the
stored frontier, so this is once per season — and once the pipeline is running,
later seasons can be seeded from the previous one.

Find your own player tag in-game (profile, top left, e.g. `#9UUU9QVU`), then:

```bash
mkdir -p seeds
printf '#YOURTAG\n' > "seeds/$(uv run bsetl-season current --format label).txt"
```

A handful of tags is enough — breadth-first expansion reaches thousands of players
within one run. If you have an older season database, sample from it instead for
better spread:

```python
from bsetl.state import sample_seed_tags_from_clean_db
tags = sample_seed_tags_from_clean_db("path/to/old.db", num_tags=500, elo_range=(15, 23))
```

Commit the file — player tags are public in-game identifiers.

```bash
git add seeds/ && git commit -m "Add seed tags" && git push
```

---

## 6. Enable the status page (optional)

**Settings → Pages → Source: GitHub Actions.** Each run then publishes coverage,
quality results, and run history.

On a private repository this requires a paid plan. Without it the deploy step
fails harmlessly and everything else still runs.

---

## 7. Run it

Do not wait for the schedule the first time — watch one run end to end.

**Actions → Ingest → Run workflow.** Set `publish` to false for a dry run if you
want to see the crawl and gate without pushing a dataset.

```bash
gh workflow run pipeline.yml
gh run watch
```

What should happen:

| Step | What it means |
| --- | --- |
| Resolve season | Prints the current season and its start date |
| Restore working database | "no stored state; starting fresh" on the first run |
| Crawl | Ends on `request_budget`, `time_budget`, or `yield_collapsed` |
| Quality gate | Fails the run rather than publishing bad data |
| Export | Parquet partitioned by day, plus a dataset card |
| Publish | Dataset appears on the Hub |
| Store working database | The frontier the next run resumes from |

Then it runs every six hours on its own.

---

## Afterwards

**Adjust the cadence** with the `cron` line in
[`.github/workflows/pipeline.yml`](../.github/workflows/pipeline.yml), and the
size of each run with `--max-requests` and `--max-seconds`.

**Watch the first rollover.** `uv run bsetl-season current` shows how long you
have. At the boundary the pipeline starts a fresh database automatically; nothing
to do.

**GitHub disables scheduled workflows** on repositories with no activity for 60
days. A push or a manual run resets that.

**If a run fails,** the working database is still stored, so nothing is lost — the
next run resumes from the same frontier. The run summary artifact and the
`pipeline_runs` table in the database record what happened and why it stopped.

**If keys accumulate** because runs died before cleanup, `uv run bsetl-key sweep`
reclaims them.
