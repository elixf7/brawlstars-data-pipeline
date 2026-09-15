"""Reaching the top of the ladder, and not drowning in state while doing it.

High-elo matches cannot be searched for — the API has no ranked leaderboard and
no bulk endpoint — so the only handle on them is the players who play them. What
follows checks the three places that handle is used: finding those players in
what has already been collected, draining the frontier in their favour, and
carrying them across a season reset.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from bsetl.ingest.budget import RunBudget
from bsetl.publish.state import prune_state, seeds_for_new_season
from bsetl.state.frontier import create_frontier_table, load_frontier, save_frontier
from bsetl.state.seeding import high_elo_tags
from bsetl.transform.schema import (
    create_fetched_tags_table_if_not_exists,
    create_matches_table_if_not_exists,
    get_matches_column_defs,
    get_matches_insert_statement,
    upsert_fetched_tags,
)

N_COLS = len(get_matches_column_defs())


def a_row(battle_time, star_tag, slots):
    """One set. `slots` is up to six (tag, elo) pairs, one per drafted brawler."""
    head = [None, battle_time, "brawlBall", "Hot Potato", "T1-T1",
            "RICO", 11, star_tag, 18, 17.5]
    body: list = []
    for i in range(6):
        tag, elo = slots[i] if i < len(slots) else (None, None)
        body += ["RICO", elo, 11, tag]
    return tuple(head + body + [None] * (N_COLS - len(head) - len(body)))


@pytest.fixture
def db(tmp_path):
    """Three sets spanning the ladder, so thresholds have something to cut."""
    p = str(tmp_path / "season.db")
    conn = sqlite3.connect(p)
    create_matches_table_if_not_exists(conn)
    conn.executemany(get_matches_insert_statement(), [
        a_row("20260901T000000.000Z", "#TOP", [("#TOP", 21), ("#HIGH", 19)]),
        a_row("20260901T010000.000Z", "#MID", [("#MID", 15), ("#HIGH", 18)]),
        a_row("20260901T020000.000Z", "#LOW", [("#LOW", 12), ("#ALSOLOW", 13)]),
    ])
    conn.commit()
    conn.close()
    return p


# ------------------------------------------------- finding the right players
def test_only_players_at_or_above_the_floor_come_back(db):
    assert set(high_elo_tags(db, 18, 100)) == {"#TOP", "#HIGH"}


def test_strongest_first(db):
    """The crawl spends its budget in this order, so it is the order that
    decides what the dataset ends up holding."""
    assert high_elo_tags(db, 12, 100)[:2] == ["#TOP", "#HIGH"]


def test_a_player_is_ranked_by_their_best_showing(db):
    """#HIGH appears at 19 and at 18. Their ceiling is what makes their log
    worth fetching, so the higher observation is the one that counts."""
    assert high_elo_tags(db, 19, 100) == ["#TOP", "#HIGH"]


def test_the_limit_is_a_ceiling_on_requests(db):
    assert len(high_elo_tags(db, 12, 2)) == 2


def test_no_limit_means_no_tags(db):
    assert high_elo_tags(db, 12, 0) == []


def test_recently_fetched_players_are_left_alone(db):
    """A battle log holds ~25 battles. Re-fetching one before the player has
    played that many more buys rows already stored."""
    conn = sqlite3.connect(db)
    create_fetched_tags_table_if_not_exists(conn)
    upsert_fetched_tags(conn, ["#TOP"], "2026-09-02T00:00:00+00:00")
    conn.commit()
    conn.close()
    assert high_elo_tags(db, 18, 100, stale_before="2026-09-01T00:00:00+00:00") == ["#HIGH"]
    # Far enough back and the window has turned over, so they are worth it again.
    assert set(high_elo_tags(db, 18, 100, stale_before="2026-09-03T00:00:00+00:00")) == \
        {"#TOP", "#HIGH"}


def test_an_empty_database_yields_nothing(tmp_path):
    p = str(tmp_path / "empty.db")
    sqlite3.connect(p).close()
    assert high_elo_tags(p, 18, 100) == []


# ----------------------------------------------------- draining it in order
def test_the_frontier_comes_back_strongest_first(tmp_path):
    p = str(tmp_path / "s.db")
    save_frontier(p, [("#LOW", 1, 13), ("#TOP", 3, 21), ("#MID", 0, 16)])
    assert [t for t, _, _ in load_frontier(p)] == ["#TOP", "#MID", "#LOW"]


def test_seeds_without_an_observed_elo_sort_last(tmp_path):
    """A seed has never been seen in a battle log. It still gets crawled — on a
    season's first run it is all there is — but it does not outrank a player
    known to be strong."""
    p = str(tmp_path / "s.db")
    save_frontier(p, [("#SEED", 0, None), ("#KNOWN", 2, 14)])
    assert [t for t, _, _ in load_frontier(p)] == ["#KNOWN", "#SEED"]


def test_depth_still_breaks_ties(tmp_path):
    p = str(tmp_path / "s.db")
    save_frontier(p, [("#DEEP", 3, 18), ("#SHALLOW", 1, 18)])
    assert [t for t, _, _ in load_frontier(p)] == ["#SHALLOW", "#DEEP"]


def _pre_elo_frontier(path, rows):
    """A crawl_frontier as it was stored before elo was recorded."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE crawl_frontier (tag TEXT PRIMARY KEY, depth INTEGER NOT NULL, "
        "enqueued_utc TEXT NOT NULL)"
    )
    conn.executemany("INSERT INTO crawl_frontier VALUES (?, ?, ?)", rows)
    conn.commit()
    conn.close()


def test_a_frontier_stored_before_elo_was_recorded_still_loads(tmp_path):
    """Read straight from the old shape, with no migration first — that is the
    order a real run does it in, and a frontier is hours of paid-for requests.
    Selecting a column it predates raises, and the tags would be dropped in
    silence: the most expensive failure available here."""
    p = str(tmp_path / "s.db")
    _pre_elo_frontier(p, [("#OLD", 1, "2026-09-01T00:00:00"),
                          ("#OLDER", 0, "2026-09-01T00:00:00")])
    assert load_frontier(p) == [("#OLDER", 0, None), ("#OLD", 1, None)]


def test_an_old_frontier_survives_the_round_trip_and_gains_elo(tmp_path):
    p = str(tmp_path / "s.db")
    _pre_elo_frontier(p, [("#OLD", 1, "2026-09-01T00:00:00")])
    kept = load_frontier(p)
    save_frontier(p, [*kept, ("#NEW", 2, 20)])
    assert load_frontier(p) == [("#NEW", 2, 20), ("#OLD", 1, None)]


def test_migrating_the_table_keeps_the_rows(tmp_path):
    p = str(tmp_path / "s.db")
    _pre_elo_frontier(p, [("#OLD", 1, "2026-09-01T00:00:00")])
    conn = sqlite3.connect(p)
    create_frontier_table(conn)
    assert "elo" in {r[1] for r in conn.execute("PRAGMA table_info(crawl_frontier)")}
    conn.close()
    assert load_frontier(p) == [("#OLD", 1, None)]


# ------------------------------------------------------- carrying it forward
class _FakeApi:
    """Stands in for HfApi. Records what would have been deleted."""

    files = [
        "README.md",
        "metadata.json",
        "data/season=season54/data.parquet",
        "state/season52.db",
        "state/season53.db",
        "state/season54.db",
        "state/bsdraft_registry.db",
    ]

    def __init__(self, *a, **k):
        _FakeApi.deleted = []

    def list_repo_files(self, **k):
        return list(self.files)

    def delete_file(self, path_in_repo, **k):
        _FakeApi.deleted.append(path_in_repo)


@pytest.fixture
def fake_hub(monkeypatch):
    import huggingface_hub
    monkeypatch.setenv("HF_TOKEN", "hf_fake")
    monkeypatch.setattr(huggingface_hub, "HfApi", _FakeApi)
    return _FakeApi


def test_pruning_keeps_the_current_season_and_the_one_before_it(fake_hub):
    """The previous season is not sentimentality — it is what the next season's
    seeds are sampled from."""
    removed = prune_state("repo", ["season54", "season53"])
    assert removed == ["state/season52.db"]


def test_pruning_never_touches_anything_but_season_state(fake_hub):
    """Other files live under state/ too, and none of them are this module's
    to delete."""
    prune_state("repo", ["season54"])
    assert "state/bsdraft_registry.db" not in fake_hub.deleted
    assert set(fake_hub.deleted) == {"state/season52.db", "state/season53.db"}


def test_a_committed_seed_file_is_never_overwritten(tmp_path):
    """Automation is the default, not a seizure of control."""
    out = tmp_path / "season54.txt"
    out.write_text("#MINE\n#OURS\n")
    assert seeds_for_new_season("repo", "season54", str(out), min_elo=18, limit=10) == 2
    assert out.read_text() == "#MINE\n#OURS\n"


def test_a_new_season_is_seeded_from_the_previous_one(tmp_path, db, monkeypatch):
    import bsetl.publish.state as st

    def fake_download(repo_id, season, dest, token=None):
        if season != "season53":
            return False
        import shutil
        shutil.copy(db, dest)
        return True

    monkeypatch.setattr(st, "download_state", fake_download)
    out = tmp_path / "season54.txt"
    n = seeds_for_new_season("repo", "season54", str(out), min_elo=18, limit=10)
    assert n == 2
    assert out.read_text().split() == ["#TOP", "#HIGH"]


def test_seeding_looks_further_back_when_a_season_is_missing(tmp_path, db, monkeypatch):
    """A season that was never crawled must not break the one after it."""
    import bsetl.publish.state as st

    seen = []

    def fake_download(repo_id, season, dest, token=None):
        seen.append(season)
        if season != "season52":
            return False
        import shutil
        shutil.copy(db, dest)
        return True

    monkeypatch.setattr(st, "download_state", fake_download)
    out = tmp_path / "season54.txt"
    assert seeds_for_new_season("repo", "season54", str(out), min_elo=18, limit=10) == 2
    assert seen == ["season53", "season52"]


def test_seeding_fails_loudly_when_there_is_nothing_to_seed_from(tmp_path, monkeypatch):
    """Better here, with a reason, than in the crawl with 'no seed tags'."""
    import bsetl.publish.state as st
    from bsetl.publish.hub import PublishError

    monkeypatch.setattr(st, "download_state", lambda *a, **k: False)
    with pytest.raises(PublishError, match="Cannot seed season54"):
        seeds_for_new_season("repo", "season54", str(tmp_path / "x.txt"),
                             min_elo=18, limit=10)


# ----------------------------------------- following strong players past the cap
class _Log:
    """A battle log naming one strong player and one ordinary one."""

    body = json.dumps({"items": [{"battle": {
        "type": "soloRanked",
        "result": "victory",
        "teams": [
            [{"tag": "#STRONG", "brawler": {"name": "RICO", "power": 11, "trophies": 19}}],
            [{"tag": "#WEAK", "brawler": {"name": "BO", "power": 11, "trophies": 14}}],
        ],
    }}]})

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def get(self, url, headers=None):
        return _Response(200, self.body)


class _Response:
    def __init__(self, status, body):
        self.status, self._body, self.headers = status, body, {}

    async def json(self):
        return json.loads(self._body)

    async def text(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


async def _crawl_one(tmp_path, monkeypatch, **kw):
    """Crawl a single root at the depth cap, and report what was left queued."""
    import datetime as dt

    from bsetl.ingest import crawler

    monkeypatch.setattr(crawler.aiohttp, "ClientSession", _Log)
    monkeypatch.setattr(crawler.aiohttp, "TCPConnector", lambda **k: None)

    db = str(tmp_path / "s.db")
    await crawler.process_tags_and_write_async(
        player_tags=["#ROOT"],
        api_key="x",
        latest_runtime=dt.datetime(2020, 1, 1, tzinfo=dt.UTC),
        clean_db_path=db,
        max_depth=0,          # the root is already as deep as the crawl goes
        elo_queue_min=13, elo_queue_max=23,
        requests_per_second=10_000,
        budget=RunBudget(max_requests=1),
        **kw,
    )
    return {t for t, _, _ in load_frontier(db)}


@pytest.mark.asyncio
async def test_a_strong_player_is_followed_past_the_depth_cap(tmp_path, monkeypatch):
    """The cap stops the crawl drifting into the bulk of the ladder. Reaching a
    strong player is the opposite of drift, so it is a reason to keep going —
    this is what stops the frontier dying at terminal depth with the top of the
    ladder still unexplored."""
    assert await _crawl_one(tmp_path, monkeypatch, high_elo_floor=18) == {"#STRONG"}


@pytest.mark.asyncio
async def test_without_a_floor_the_cap_is_absolute(tmp_path, monkeypatch):
    assert await _crawl_one(tmp_path, monkeypatch, high_elo_floor=None) == set()
