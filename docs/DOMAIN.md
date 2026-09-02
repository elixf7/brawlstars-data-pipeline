# The game, for people reading the data

Enough Brawl Stars to make sense of the schema. Nothing here is strategy advice;
it is the background needed to know what a row means.

## Matches, sets, and rows

Brawl Stars is a 3v3 game. Its ranked mode is played as a **set**: up to three
games on one fixed map and mode, first team to two wins. A set therefore ends
`2-0` or `2-1`, with draws possible in individual games.

**One row in `matches` is one set, not one game.** The `record` column holds the
game-by-game outcome as dash-separated tokens — `T1` for a team-1 win, `T2` for
team 2, `D` for a draw. So `T1-T1` is a 2-0 sweep and `T2-T1-T1` is a comeback
from a game down. `battle_time` is the timestamp of the *last* game in the set.

This shape follows from the source: battle logs list individual games, and the
ingester groups consecutive `soloRanked` games into sets by watching for the
star-player marker that appears on the last game of each set.

## Brawlers and the draft

Each player picks one **brawler** — a character with distinct weapons, health, and
a special ability. There are ~95 in the ranked pool, and the recent seasons in
this dataset carry 95–96.

Drafting alternates in snake order: `Blue 1 → Red 1 → Red 2 → Blue 2 → Blue 3 →
Red 3`. No duplicates within a team, but the same brawler may appear on both
sides. There are no bans.

**The API does not expose any of this.** It returns the six final brawlers and
nothing about how they were chosen — not the pick order, not which team drafted
first. The schema reflects that: six flattened `t{team}_b{slot}_*` slots with no
sequence information. Slot index is positional, not draft order.

## Modes and maps

Ranked rotates a fixed set of modes, each with its own objective:

| Mode | Objective |
| --- | --- |
| `gemGrab` | Hold 10 gems for a countdown |
| `brawlBall` | Score two goals |
| `heist` | Destroy the enemy safe |
| `bounty` | Lead on stars when time expires |
| `hotZone` | Control zones to fill a meter |
| `knockout` | Eliminate the enemy team, no respawns |

A set is played entirely on one map, so mode and map are properties of the row
rather than of individual games. Brawler strength depends heavily on both — the
map is a primary conditioning variable, not a nuisance one.

## Elo and rank

Ranked uses an elo-style rating that climbs through tiers from Bronze to Masters.
Two aspects matter for the data:

**Elo is per-brawler, not per-player.** The rating attached to a player in a
battle log belongs to the brawler they played. A strong player on an unfamiliar
brawler carries a low rating into the match.

**Elo resets each season and re-stratifies over the following weeks.** Early in a
season the population is compressed near the reset point; later it spreads out.
The same numeric elo therefore means different things at different points in the
season, which is what `skill_ns` exists to correct — see
[DESIGN.md](DESIGN.md#elo-is-not-comparable-across-a-season).

`avg_elo` averages the six brawler ratings in a set and stands in for overall
match strength. Values above 23 exceed what the ladder produces and are filtered
as bots or corrupted records.

## Seasons and the meta

Seasons run roughly a month, and a new one resets ratings. Between them, the
**meta** — which brawlers and compositions are strong — shifts almost entirely
through balance changes to individual brawlers' stats, rather than through map or
rule changes.

That has a practical consequence for anyone modeling this data: adaptation should
concentrate on brawler-level interactions, and pooling across seasons mixes
distinct balance regimes. The databases are kept per season for that reason.
