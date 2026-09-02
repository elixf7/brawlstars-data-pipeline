# Design notes

Why the pipeline is shaped the way it is. Each section states a constraint the
Brawl Stars API imposes, and what the code does about it.

For what the data means, see [DOMAIN.md](DOMAIN.md). For column-level reference,
see [DATA_DICTIONARY.md](DATA_DICTIONARY.md).

## There is no bulk endpoint

The API exposes match data only per player: `/players/{tag}/battlelog` returns
roughly that player's last 25 battles. There is no "give me recent ranked matches"
endpoint, no pagination into history, and no way to enumerate players.

So the dataset is assembled by crawling the player graph. Every ranked battle log
names all six participants, which makes each fetched log a source of new player
tags. Ingestion is a breadth-first search: fetch a player's log, extract the sets,
enqueue the other players seen in them, repeat to a bounded depth.

Two consequences follow from the 25-battle window. Re-crawling the same player
sooner than they play 25 more ranked games returns mostly rows already stored, and
a player who has stopped playing yields nothing new ever again. Both push toward
crawling *breadth* rather than revisiting.

## Crawl efficiency decays as the dataset grows

This is the central problem, and it is not obvious at small scale.

BFS from different seeds converges. Popular high-elo players appear in many
battle logs, so independent crawls rediscover the same tags. Early on this barely
matters. Once the database holds a million-plus sets, a large fraction of newly
discovered tags are already well represented, and the crawl spends its rate-limit
budget re-fetching logs whose rows will be discarded at write time.

Deduplicating at write time does not help with this. The unique index prevents
duplicate *rows*, but the API call has already been spent by then. The waste has
to be prevented before the request.

The fix is a `fetched_tags` table in the database itself, mapping each fetched
tag to a UTC timestamp. It matters that this is durable rather than in-process:
the visited set used to be scoped to a single call, so a run chunked into 500
subprocesses started each chunk with an empty set and re-fetched freely across
chunk boundaries. Now, at run start, every tag fetched within
`--fetched-tags-ttl-hours` is preloaded into the visited set, so a warm database
skips known players from the very first batch. The table is written incrementally
during the crawl, not only at the end, so a long run benefits from its own
progress and an interrupted run keeps what it learned.

## Re-crawling has to be safe

Because coverage is built by repeatedly crawling overlapping neighborhoods, the
same set will be observed many times — once from each of its six participants,
plus again on any later crawl within the battle-log window.

A set is identified by `(battle_time, map, star_player_tag)`, enforced by a unique
index, and all writes go through `INSERT OR IGNORE`. Re-ingesting is therefore
free of effect rather than merely tolerable, which is what makes an unattended
schedule viable: a run that partially fails can simply be run again.

## Two elo ranges, not one

The crawler takes `--elo-queue-min/max` and `--elo-game-min/max`, which sound
redundant and are not.

The *game* range decides which sets get stored — the slice of the ladder being
studied. The *queue* range decides which players get followed. These want
different values: to study matches around elo 12–23, it pays to follow players
somewhat higher up, because their logs are dense with games in the target range,
while following players far below it mostly yields games that will be filtered
out. Collapsing the two into one range either narrows the frontier until the
crawl starves, or widens what is stored beyond what is wanted.

A hardcoded `avg_elo > 23` filter also drops sets whose average exceeds anything
the ladder produces, which are bot or corrupted records.

## Politeness under a shared budget

Requests pass through a global token bucket — at most N acquisitions in any
one-second window — layered under an `asyncio.Semaphore` bounding requests in
flight. Rate and concurrency are separate knobs because they control different
failure modes: concurrency governs sockets and memory, rate governs the API's
opinion of you.

On a 429 the responsible task reads `Retry-After` and calls `trigger_backoff`,
which pauses *every* task through the shared limiter rather than only the one
that was rejected. Backing off individually would have the remaining workers keep
hammering an endpoint that has just asked for quiet.

## Memory has to stay bounded

A depth-2 crawl from a few hundred seeds can hold tens of thousands of battle
logs in memory before writing anything. Two mechanisms cap this:
`--flush-every-n-batches` writes accumulated rows and clears the log buffer
mid-crawl, and `bsetl-queue` splits a seed file into separate subprocesses so
each run starts from a clean heap. The subprocess split only became sensible once
`fetched_tags` made cross-process deduplication work.

## Elo is not comparable across a season

Ranked elo resets at season start and re-stratifies over the following weeks. An
average of 16 in the first days of a season describes a very different match from
an average of 16 three weeks later, when the distribution has spread out. Any
model trained on raw `avg_elo` learns a moving target, and any analysis that pools
a season conflates skill with calendar time.

`skill_ns` normalizes each match against the elo distribution *local in time*:

1. Sets are assigned to fixed 3-day bins anchored at the Unix epoch, so bin
   boundaries are stable across databases and rebuilds.
2. Within a bin, a match's `avg_elo` is converted to a percentile by its midrank,
   `(rank - 0.5) / (n + 1)`, which handles the heavy ties that integer elo
   produces.
3. The percentile is clipped to `[ε, 1-ε]` and mapped to an unbounded symmetric
   scale — normal scores by default, logit optionally for heavier tails.

Bins are never widened or merged. A bin below `--min-bin-count` samples gets
`skill_ns = NULL` and `skill_ns_ok = 0` instead, because a percentile computed
from too few observations is a confident-looking guess. Consumers filter on
`skill_ns_ok = 1`; the alternative — quietly substituting a season-wide ECDF —
would defeat the purpose while looking like it worked. That substitution exists
behind `--fallback-strategy global_season_ecdf`, still flagged, for cases where
coverage is known to be poor.

The inverse normal CDF is Acklam's approximation rather than a SciPy dependency,
which keeps the runtime install to three packages.

## Runs have to be auditable after the fact

Every skill-feature computation writes a `skill_bin_metadata` row per bin —
sample count, coverage flag, bin bounds, ε, bin width, whether fallback was used,
and when it ran — plus a JSON sidecar next to the database. A dataset whose
provenance cannot be reconstructed is hard to trust and harder to debug when a
downstream model behaves oddly.

The feature is versioned as `skill_ns_v1`, held in `skill_config.py`. The column
name stays stable; a material change to the method bumps the version recorded in
metadata rather than renaming the column, so downstream code does not break and
old data stays interpretable.

## A scheduled run has to end by itself

A crawl used to run until the frontier emptied, which on this graph is
effectively never. That is fine when a person is watching and can press Ctrl-C.
It is not fine on a schedule, where the run shares a fixed CI window with a
short-lived API key that gets revoked on the way out.

Three things end a run, and it records which one did:

- **Request budget** — a hard ceiling on API calls, so cost is predictable.
- **Time budget** — wall clock, so the run fits its window.
- **Yield collapse** — the interesting one. Rows inserted per 1000 requests is
  measured over a trailing window, and the run stops when it falls below a
  floor. Because BFS neighborhoods saturate, this is usually what fires first:
  it ends the crawl at the point where it stopped being worth continuing,
  rather than at an arbitrary count.

Yield is measured on rows *actually inserted*, which `INSERT OR IGNORE` makes
very different from rows attempted once the database is warm. Measuring
attempts would show a healthy crawl right up until the disk stopped changing.

The budget is enforced at the request layer, not just between batches, so a
large in-flight batch cannot overshoot it. A request declined for budget returns
a distinct sentinel rather than `None`: `None` means the request happened and
found nothing, whereas a declined tag was never asked about and has to go back
on the frontier. Conflating them would silently drop a slice of the frontier on
every bounded run.

## Stopping early only helps if the next run continues

Bounding a run is pointless if the next one starts over. When a run stops, the
unvisited frontier — every tag discovered but not yet fetched — is written to a
`crawl_frontier` table in the season database, and the next run loads it before
consulting its seed tags. Pending tags are restored shallowest-first, so
resuming continues the breadth-first sweep rather than diving into whatever
happened to be deepest when the clock ran out.

This is what makes frequent small runs equivalent to one long crawl, which is
the whole premise of running on a schedule.

Every run also writes a row to `pipeline_runs`: its configuration, how it
stopped, requests made, rows inserted, HTTP outcomes broken out by kind, parse
failures, and frontier size before and after. The row is written as `running`
before the first request and updated on the way out, so a process that dies
leaves evidence rather than nothing.

## The key cannot be stored

Supercell keys are CIDR-locked — the permitted address is baked into the token
itself. A key minted at home returns 403 from a CI runner, whose address is
neither known ahead of time nor stable between runs. So the usual arrangement,
a long-lived key held as a repository secret, cannot work here at all.

What is stored instead is a *credential*. At the start of a run the pipeline
signs in to the developer portal, mints a key scoped to whatever address it is
calling from, uses it, and revokes it in a `finally` block. The key exists for
one run, never touches disk, and is worthless to anyone who obtains it from
anywhere else.

The caller's address comes from the login response itself, which returns a
`temporaryAPIToken` whose JWT payload carries the CIDR the portal observed.
Asking a third-party IP-echo service would introduce an outside dependency in
the authentication path and could disagree with what the portal actually sees
behind egress NAT. The CIDR is located by searching the `limits` array for the
entry that has one rather than by index, since the order is not guaranteed.

Two failure modes get explicit handling. The portal caps an account at ten
keys, and a run killed between minting and revoking leaks one; a handful of
crashes would wedge the pipeline until someone cleared keys by hand. So each
run first sweeps keys carrying the managed name prefix, which are by definition
disposable, before minting its own. `bsetl-key sweep` does the same on demand.
That sweep assumes one pipeline per prefix — concurrent runs sharing a prefix
would revoke each other's keys, so they need distinct ones.

The key is never rendered. `ProvisionedKey.__repr__` redacts it, because the
realistic leak is not someone printing it deliberately — it is a traceback, a
debug log, or a CI transcript that happens to include an object.

## Running unattended on an ephemeral runner

The schedule is frequent short crawls rather than one long seasonal batch. Each
run resumes the previous run's frontier, so a series of them behaves as one
continuous crawl while each stays well inside a CI window. It also means a
failed run costs one interval rather than a season.

Three things have to be true for that to work.

**State must outlive the runner.** A CI runner keeps nothing. But a bounded
crawl is only worthwhile if the next run continues it: the frontier is what it
picks up, `fetched_tags` is what stops it re-fetching, and the unique index is
what makes overlapping crawls idempotent. Start from an empty database and all
three guarantees are gone. So the working database is stored beside the
published dataset under a `state/` prefix — outside the `data/**` glob the
dataset config matches, so it is versioned by the same mechanism as the data
without ever being presented as part of it.

The database is pushed back even when the run fails the quality gate or crashes
outright. A failed run still advanced the frontier and the fetched-tag ledger,
and discarding that would make the next run pay again for work already done.

**Runs must not overlap.** Two concurrent crawls would resume the same frontier
and duplicate every request, and the key sweep assumes one pipeline per name
prefix — parallel runs would revoke each other's keys mid-crawl. The workflow
takes a concurrency group with `cancel-in-progress: false`, so a run that
overruns its slot delays the next one rather than being killed halfway.

**Output must be machine-readable.** The run summary is JSON on stdout and
everything else is on stderr, so the workflow can capture one without parsing
around the other.

## Impossible records are dropped, and the dropping is watched

A record describing a game after one team already won the set did not happen;
it is two adjacent sets merged during grouping. Those rows are now dropped at
ingest rather than published, since a consumer has no way to tell them from real
matches.

Dropping silently would be worse than keeping them, though. A trickle is
routine — season42 has 0.0144% — but a large share means set grouping has broken
and everything that run produced is suspect, however clean the surviving rows
look. So the count travels into the run record, the crawl logs an error when the
rate crosses a threshold, and a quality check reads it back from the run history
and fails. The evidence has to live in the run record precisely because the rows
themselves are gone.

## Seasons are arithmetic

Ranked resets on the third Thursday of each month, unchanged since the Ranked
2.0 rework of February 2025. So the pipeline computes the current season from
the calendar rather than being told, and rollover cannot be forgotten.

The alternative was detecting resets statistically. That is possible — within a
season the same player's elo drifts *upward*, with under 1% of players dropping
more than one elo over a five-day gap, so a reset moving everyone down at once
stands out enormously. But it needs a threshold, a trailing window, and enough
paired players before it can say anything, and it can only ever report a
boundary after the fact. A calendar rule needs none of that and is known in
advance. If Supercell changes the schedule, `OVERRIDES` in
`transform/seasons.py` takes the affected seasons; nothing else moves.

Two consequences follow.

**A database holds one season.** A database spanning a reset mixes two elo
regimes under one label, and any `skill_ns` bin crossing the boundary computes
percentiles over a bimodal population — numbers that look entirely plausible and
are not. A quality check fails a database whose matches fall in more than one
season. The season42 database in this repository is exactly that case: its
matches run 2025-10-10 to 2025-11-06, straddling the 2025-10-16 reset, with
almost all the volume after it.

**Ingestion is bounded below by the season start.** Battle logs fetched just
after a rollover still contain pre-reset matches, which belong to the previous
season. The scheduled run passes the season start as `--latest-runtime`, so
those are filtered rather than mixed in.

The season a database holds is read from its earliest match, not from its
filename. A path can say anything, and in this repository one of them does.

## Something has to decide whether the output is fit to publish

An automated pipeline publishes whatever it produced. The failures worth
worrying about are not crashes — those are loud and stop the run — but the quiet
ones: a crawl that collected a tenth of its usual volume, an upstream change
that starts nulling a column, a skill feature computed against the wrong season.
Each of those produces a dataset that looks fine and is wrong.

`bsetl-check` runs a set of checks over a season database and exits non-zero if
any fails. `bsetl-export` runs the same gate first and refuses to build a
dataset that fails it, so the check cannot be forgotten; `--skip-checks`
overrides deliberately. The report is embedded in the exported metadata, so a
published season carries the evidence it was checked.

Severity is the substance of the design. Things that make the data wrong FAIL:
a missing unique index, duplicate sets, impossible elo, a collapsed brawler
pool, timestamps that will not parse, skill metadata labelled with a season that
is not this one. Things that are merely surprising WARN: an unrecognised mode,
because a rotation change is news rather than a defect, and gaps in daily
coverage, which are usually just a run that did not happen. A gate that fails on
novelty gets switched off.

Two checks are worth singling out.

**Skill provenance.** The skill feature must be labelled with the season it was
computed over, and its bins must overlap the data they claim to describe. This
is not hypothetical — a sidecar in this repository is stamped `season42` while
its bins cover season43's dates. Nothing caught it because nothing was looking.

**Record shape.** A set is first-to-two-wins, and draws do not count toward the
two, so a set can legitimately run past three games. What cannot happen is a
game *after* one team reaches two wins. Records that violate this are two
adjacent sets merged into one, which happens when the star-player marker used to
delimit sets does not appear where grouping expects it. In season42 that affects
393 rows, 0.0144%. Small enough to warn rather than block, but a jump in that
rate means grouping has broken and the records have stopped meaning what they
say.

## SQLite to work in, Parquet to hand out

These are different jobs, and one format does not do both well.

The crawl needs a transactional store with a unique constraint. Idempotent
re-crawling depends entirely on `INSERT OR IGNORE` against the unique index, and
the frontier, fetched-tag ledger, and run history need to commit atomically
alongside the rows they describe. Parquet offers none of that: no uniqueness, no
transactions, no in-place update, which the skill feature also needs when it
backfills `skill_ns`. Writing Parquet directly from the crawler would mean
reimplementing deduplication by hand and splitting operational state into a
second store.

Publishing has the opposite requirements. Nobody consuming this wants a
600 MB file they must have a SQLite driver to open, and nearly every consumer
reads a subset — a date range, a few columns. So the export projects `matches`
into Parquet partitioned by day, which on season42 is 2.7M rows in 66 MB rather
than 623 MB, a factor of 9.4, with a day readable without touching the rest.

Two details make the exported files safe to combine across seasons. The Arrow
schema is built from SQLite's *declared* column types rather than inferred from
the data, because a column that happens to be entirely NULL in one season would
otherwise be typed `null` and refuse to concatenate with a season where it is
populated. And one writer is held open per day across read batches, so a day
spanning a batch boundary lands in one file instead of fragmenting.

The export is a projection, not a move. It carries `matches` and the
skill-feature provenance and leaves the operational tables behind — they say
nothing about the game and everything about our crawl. `bsetl-export
--with-sqlite` produces the same projection as a SQLite file, vacuumed, for
consumers that already expect one.

## A run has to account for itself

Nobody watches a scheduled run, so its log is the only account of what happened.
Library code logs and never prints, at levels a reader can filter: routine
progress at INFO, things worth a human's attention at WARNING.

Logs go to stderr, which keeps stdout clean for the JSON run summary that
`bsetl-ingest` prints on exit — so a caller can parse the result without
stripping log lines out of it. Progress bars are drawn only when stderr is a
terminal; tqdm redraws with carriage returns, which a CI log renders as
thousands of unreadable lines.

Failures that used to be invisible now say so. Unparseable battles are counted
and reported. Rows whose `battle_time` cannot be parsed are counted before the
ECDF is built, because an upstream format change would otherwise quietly thin
every bin and bias the percentiles without failing anything.

One durability point: rows are normally written once at the end of a run, so a
crash after thousands of requests would discard all of them. The failure path
now salvages what was fetched before re-raising, so an interrupted run loses
time but not data.

## Known limitations

- **Draft order is unrecoverable.** The API returns the six final brawlers with
  no bans and no pick sequence. Sequence-dependent modeling has to infer it.
- **Merged sets are detected, not repaired.** The gate reports records that no
  real set could produce, but ingestion still writes them; a consumer wanting
  clean records should filter on the same rule.
- **Seeding is still manual.** A new season starts from hand-supplied tags or a
  sample of an old season's star players; nothing picks them automatically.
- **Yield collapse is global, not per-neighborhood.** The crawl stops when
  overall yield falls, but cannot currently redirect toward a more productive
  region of the graph instead.
- **Player enrichment is still all-or-nothing.** With `fetch_player_data`
  enabled, profile fetches happen in one pass after the crawl, outside the
  incremental flush, so that path holds more in memory than the default one.
