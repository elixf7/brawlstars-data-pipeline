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

## Known limitations

- **Failed fetches are indistinguishable from empty ones.** After three retries
  `fetch_json_async` returns `None`, which reads the same as a player with no
  ranked games. Runs cannot currently report what fraction of the frontier was
  actually lost.
- **Parse failures are silent.** Malformed battles are skipped without being
  counted, so a systematic upstream change could shrink yield unnoticed.
- **The crawl is unbounded.** Nothing stops a run at a request budget or when
  new-row yield collapses; it runs until the frontier empties.
- **Draft order is unrecoverable.** The API returns the six final brawlers with
  no bans and no pick sequence. Sequence-dependent modeling has to infer it.
