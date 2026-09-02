import asyncio
import json
import os
import sqlite3
import urllib.parse
from datetime import UTC, datetime
from typing import Any

import aiohttp

try:
    from tqdm import tqdm
except Exception:  # fallback in some notebook environments
    from tqdm.notebook import tqdm
from collections import deque

from bsetl.ingest.budget import CrawlStats, Outcome, RunBudget, StopReason
from bsetl.ingest.ratelimit import AsyncRateLimiter
from bsetl.state.frontier import load_frontier, save_frontier
from bsetl.state.runs import finish_run, start_run
from bsetl.transform.schema import (
    create_fetched_tags_table_if_not_exists,
    create_matches_table_if_not_exists,
    get_matches_insert_statement,
    upsert_fetched_tags,
)

########################
# CLEAN DB INSERTS
########################

########################
# HELPER FUNCTIONS
########################

class _BudgetSkip:
    """Returned instead of a response when the budget is spent.

    Distinct from None, which means the request happened and yielded nothing.
    A skipped tag has to go back on the frontier; an empty one must not.
    """

    __slots__ = ()


BUDGET_SKIP = _BudgetSkip()


def is_string_date_after(reference_dt, date_string):
    string_datetime = datetime.strptime(date_string, '%Y%m%dT%H%M%S.%fZ')
    string_datetime = string_datetime.replace(tzinfo=UTC)
    if reference_dt.tzinfo is None:
        reference_dt = reference_dt.replace(tzinfo=UTC)
    return string_datetime > reference_dt


def group_ranked_matches(battle_log, stats: CrawlStats | None = None):
    """
    Return a list of 'games', each game is a list of 1-2 items (battles),
    but only if they are 'soloRanked'.
    """
    games = []
    current_game = []
    if not battle_log:
        return games

    for battle in battle_log.get('items', []):
        try:
            # Only parse out battles whose type == 'soloRanked'
            if battle['battle']['type'] == "soloRanked":
                # If the starPlayer is not yet found, we keep building the same "game"
                if not current_game:
                    current_game.insert(0, battle)
                elif battle['battle'].get('starPlayer') is None:
                    current_game.insert(0, battle)
                else:
                    # If we already have starPlayer for the current game, we finalize it
                    games.append(current_game)
                    current_game = [battle]
        except Exception:
            if stats is not None:
                stats.record_parse_failure()
            continue

    return games

def format_record(game, perspective_tag):
    my_team = 1
    opponent_team = 2
    for p in game[0]['battle']['teams'][1]:
        if p['tag'] == perspective_tag:
            my_team = 2
            opponent_team = 1
            break

    record = ''
    for match in game:
        result = match['battle']['result']
        if result == 'victory':
            record += f'T{my_team}-'
        elif result == 'draw':
            record += 'D-'
        else:
            record += f'T{opponent_team}-'
    return record[:-1]


########################
# NEW HELPER FUNCTION
########################

def get_all_solo_ranked_tags_with_elos(battle_log):
    """
    Return a list of (player_tag, elo) pairs found in 'soloRanked' matches.
    Elo is taken from player['brawler']['trophies'] within each match context.
    """
    result: list[tuple[str, float | None]] = []
    if not battle_log:
        return result
    items = battle_log.get("items", [])
    for item in items:
        battle = item.get("battle", {})
        if battle.get("type") == "soloRanked":
            for team in battle.get("teams", []):
                for player in team:
                    ptag = player.get("tag")
                    elo_val = None
                    try:
                        elo_val = player.get("brawler", {}).get("trophies")
                    except Exception:
                        elo_val = None
                    if ptag:
                        result.append((ptag, elo_val))
    return result

def _is_elo_in_range(elo_value: float | None, elo_min: float | None, elo_max: float | None) -> bool:
    """Return True if elo_value is within [elo_min, elo_max] when provided."""
    if elo_value is None:
        return False
    if elo_min is not None and elo_value < elo_min:
        return False
    if elo_max is not None and elo_value > elo_max:
        return False
    return True

def _validate_range(min_val: float | None, max_val: float | None, label: str) -> None:
    """Raise ValueError if both provided and min_val > max_val."""
    if min_val is not None and max_val is not None and min_val > max_val:
        raise ValueError(f"Invalid {label}: min ({min_val}) > max ({max_val})")

########################
# PLAYER AND BRAWLER DATA EXTRACTION
########################

def get_player_data(json_player):
    return {
        'tag': json_player['tag'],
        'name': json_player['name'],
        'highestTrophies': json_player['highestTrophies'],
        'expLevel': json_player['expLevel'],
        'expPoints': json_player['expPoints'],
        '3vs3Victories': json_player['3vs3Victories']
    }

def get_brawler_data(json_player, brawler_name, elo, power_level):
    json_brawlers = json_player.get('brawlers', [])
    target_brawler_json = None
    for b in json_brawlers:
        if b['name'] == brawler_name:
            target_brawler_json = b
            break
    if not target_brawler_json:
        return {
            'name': brawler_name,
            'elo': elo,
            'rank': None,
            'highestTrophies': None,
            'power': power_level
        }

    return {
        'name': brawler_name,
        'elo': elo,
        'rank': target_brawler_json['rank'],
        'highestTrophies': target_brawler_json['highestTrophies'],
        'power': target_brawler_json['power'],
    }

def process_team(team, player_data_cache):
    brawlers = []
    players = []
    for (brawler_name, power_level, tag, elo) in team:
        json_player = player_data_cache.get(tag)
        if json_player and isinstance(json_player, dict):
            brawler_data = get_brawler_data(json_player, brawler_name, elo, power_level)
            player_data = get_player_data(json_player)
        else:
            brawler_data = {
                'name': brawler_name,
                'elo': elo,
                'rank': None,
                'highestTrophies': None,
                'power': power_level
            }
            player_data = {
                'tag': tag,
                'name': None,
                'highestTrophies': None,
                'expLevel': None,
                'expPoints': None,
                '3vs3Victories': None
            }

        brawlers.append(brawler_data)
        players.append(player_data)
    return brawlers, players

def get_teams(match):
    teams_json = match['battle']['teams']
    teams = []
    for t in teams_json:
        team_data = []
        for player in t:
            brawler = player['brawler']
            team_data.append((brawler['name'], brawler['power'], player['tag'], brawler['trophies']))
        teams.append(team_data)
    return teams

def _pad_brawler_list(brawlers):
    """Ensure a list of 3 brawler dicts with the required keys (fill with None)."""
    arr = list(brawlers) if brawlers else []
    while len(arr) < 3:
        arr.append({
            'name': None,
            'elo': None,
            'rank': None,
            'highestTrophies': None,
            'power': None,
        })
    return arr[:3]

def _compute_avg_elo(team1_brawlers, team2_brawlers) -> float | None:
    """Compute mean of available elo values across both teams; return None if none."""
    values = []
    for b in team1_brawlers + team2_brawlers:
        v = b.get('elo') if isinstance(b, dict) else None
        if v is not None:
            values.append(v)
    if not values:
        return None
    return sum(values) / len(values)

def build_clean_row(
    game,
    perspective_tag: str,
    latest_runtime: datetime,
    player_data_cache: dict,
    fetch_player_data: bool,
    elo_game_min: float | None,
    elo_game_max: float | None
) -> tuple[Any, ...] | None:
    """
    Return a tuple of 40 values matching the `matches` column order or None.
    Applies avg_elo computation and filters out rows by elo_game_min/elo_game_max and avg_elo > 23.
    """
    if not game:
        return None

    # Require star player present to finalize the set
    if game[-1]['battle'].get('starPlayer') is None:
        return None

    battle_time = game[-1]['battleTime']
    if not is_string_date_after(latest_runtime, battle_time):
        return None

    event = game[0]['event']
    mode = event['mode']
    map_name = event['map']
    record = format_record(game, perspective_tag)

    teams = get_teams(game[0])
    star_player = game[-1]['battle']['starPlayer']
    star_brawler_name = star_player['brawler']['name']
    star_power = star_player['brawler']['power']
    star_tag = star_player['tag']
    star_elo = star_player['brawler']['trophies']

    team1_brawlers, _ = process_team(teams[0], player_data_cache)
    team2_brawlers, _ = process_team(teams[1], player_data_cache)

    team1_brawlers = _pad_brawler_list(team1_brawlers)
    team2_brawlers = _pad_brawler_list(team2_brawlers)

    avg_elo = _compute_avg_elo(team1_brawlers, team2_brawlers)
    # Filter by provided game-level elo range if present
    if avg_elo is None:
        return None
    if elo_game_min is not None and avg_elo < elo_game_min:
        return None
    if elo_game_max is not None and avg_elo > elo_game_max:
        return None
    # Original cap
    if avg_elo is not None and avg_elo > 23:
        return None

    # id first (let SQLite assign if None)
    out = [
        None,
        battle_time,
        mode,
        map_name,
        record,
        star_brawler_name,
        star_power,
        star_tag,
        star_elo,
        avg_elo,
    ]

    for b in team1_brawlers + team2_brawlers:
        out.extend([
            b.get('name'),
            b.get('elo'),
            b.get('rank'),
            b.get('highestTrophies'),
            b.get('power'),
        ])

    return tuple(out)

def insert_rows_matches_in_chunks(db_path: str, rows: list[tuple[Any, ...]], chunksize: int = 10000) -> int:
    """Bulk insert into matches; return the number of rows actually inserted.

    Writes use INSERT OR IGNORE, so attempted and inserted diverge sharply once
    the database is warm. total_changes measures the latter, which is the only
    figure that says whether the requests were worth making.
    """
    if not rows:
        return 0
    # Ensure parent directory exists for the SQLite file path
    try:
        parent_dir = os.path.dirname(os.path.abspath(db_path))
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)
    except Exception:
        # Fall through; sqlite will raise a clearer error if path is invalid
        pass

    conn = sqlite3.connect(db_path)
    try:
        create_matches_table_if_not_exists(conn)
        insert_sql = get_matches_insert_statement()
        before = conn.total_changes
        for i in range(0, len(rows), chunksize):
            chunk = rows[i:i+chunksize]
            conn.executemany(insert_sql, chunk)
            conn.commit()
        return conn.total_changes - before
    finally:
        conn.close()

def process_game(game, perspective_tag, latest_runtime, player_data_cache, fetch_player_data):
    """
    Same logic as your original, but we pass `fetch_player_data`. 
    If it's False, only the 'Team1_Players' and 'Team2_Players' become None.
    Everything else is built normally.
    """
    if not game:
        return None

    # If starPlayer is None, skip
    if game[-1]['battle'].get('starPlayer') is None:
        return None

    battle_time = game[-1]['battleTime']
    if not is_string_date_after(latest_runtime, battle_time):
        return None

    event = game[0]['event']
    mode = event['mode']
    map_name = event['map']
    record = format_record(game, perspective_tag)

    teams = get_teams(game[0])
    star_player = game[-1]['battle']['starPlayer']
    star_player_tuple = (
        star_player['brawler']['name'], 
        star_player['brawler']['power'], 
        star_player['tag'], 
        star_player['brawler']['trophies']
    )
    star_player_str = json.dumps(star_player_tuple)

    # process_team uses `player_data_cache`
    team1_brawlers, team1_players = process_team(teams[0], player_data_cache)
    team2_brawlers, team2_players = process_team(teams[1], player_data_cache)

    team1_brawlers_str = json.dumps(team1_brawlers)
    team1_players_str = json.dumps(team1_players)
    team2_brawlers_str = json.dumps(team2_brawlers)
    team2_players_str = json.dumps(team2_players)

    # If NOT fetching player data, override the players columns:
    if not fetch_player_data:
        team1_players_str = None
        team2_players_str = None

    return (
        battle_time,
        mode,
        map_name,
        record,
        star_player_str,
        team1_brawlers_str,
        team1_players_str,
        team2_brawlers_str,
        team2_players_str
    )

########################
# ASYNC API FUNCTIONS
########################

async def fetch_json_async(
    url, headers, session, semaphore, retries=3, delay=.005,
    rate_limiter: AsyncRateLimiter | None = None,
    stats: CrawlStats | None = None,
):
    """Fetch one JSON document, recording how it went.

    Returns the decoded body, None if the request was made and yielded nothing
    usable, or BUDGET_SKIP if no request was made because the budget is spent.
    Distinguishing the last two is what lets a bounded run put unvisited tags
    back on the frontier instead of silently dropping them.
    """
    if stats is not None and stats.should_stop() is not None:
        return BUDGET_SKIP

    async with semaphore:
        for _ in range(retries):
            await asyncio.sleep(delay)
            try:
                if rate_limiter is not None:
                    await rate_limiter.acquire()
                async with session.get(url, headers=headers) as response:
                    if response.status == 200:
                        if stats is not None:
                            stats.record_request(Outcome.OK)
                        return await response.json()
                    elif response.status == 429:
                        if stats is not None:
                            stats.record_request(Outcome.RATE_LIMITED)
                        try:
                            retry_after = float(response.headers.get("Retry-After", 0.5))
                        except Exception:
                            retry_after = 0.5
                        retry_after = max(0.5, retry_after)
                        # Pause every worker, not just this one: the API has
                        # asked for quiet, and the rest are about to be told so.
                        if rate_limiter is not None:
                            rate_limiter.trigger_backoff(retry_after)
                        await asyncio.sleep(retry_after)
                    else:
                        text = await response.text()
                        not_found = False
                        try:
                            not_found = json.loads(text).get("reason") == "notFound"
                        except json.JSONDecodeError:
                            pass
                        if stats is not None:
                            stats.record_request(
                                Outcome.NOT_FOUND if not_found else Outcome.ERROR
                            )
                        if not_found:
                            # A tag with no accessible profile. Expected and common.
                            return None
                        print(f"HTTP {response.status}: {text}")
                        await asyncio.sleep(1)
            except Exception as e:
                if stats is not None:
                    stats.record_request(Outcome.ERROR)
                print(f"Exception fetching {url}: {e}")
                await asyncio.sleep(1)
    return None

async def fetch_battle_log_async(player_tag, api_key, session, semaphore, rate_limiter: AsyncRateLimiter | None = None, stats: CrawlStats | None = None):
    headers = {'Accept': 'application/json', 'Accept-Encoding': 'gzip', 'Authorization': f'Bearer {api_key}'}
    encoded_tag = urllib.parse.quote(player_tag)
    url = f'https://api.brawlstars.com/v1/players/{encoded_tag}/battlelog'
    return await fetch_json_async(url, headers, session, semaphore, rate_limiter=rate_limiter, stats=stats)

async def fetch_player_info_async(player_tag, api_key, session, semaphore, rate_limiter: AsyncRateLimiter | None = None, stats: CrawlStats | None = None):
    headers = {'Accept': 'application/json', 'Accept-Encoding': 'gzip', 'Authorization': f'Bearer {api_key}'}
    encoded_tag = urllib.parse.quote(player_tag)
    url = f'https://api.brawlstars.com/v1/players/{encoded_tag}'
    return await fetch_json_async(url, headers, session, semaphore, rate_limiter=rate_limiter, stats=stats)

def getUpdatedIP(api_key):
    import requests
    headers = {
        'Accept': 'application/json',
        'Authorization': f'Bearer {api_key}',
    }
    encoded_tag = urllib.parse.quote('#9UUU9QVU')
    url = f'https://api.brawlstars.com/v1/players/{encoded_tag}'

    # Make the GET request using the session
    response = requests.get(url, headers=headers)

    # Check the response status
    if response.status_code == 200:
        print("IP Adress is Valid")
    else:
        print(response.text)

########################
# MAIN PROCESS FUNCTION WITH SINGLE PROGRESS BAR
########################

def _build_clean_rows_from_logs(
    logs_dict: dict,
    latest_runtime,
    player_info_cache: dict,
    fetch_player_data: bool,
    elo_game_min,
    elo_game_max,
    stats: CrawlStats | None = None,
) -> list:
    rows = []
    for t in logs_dict:
        for g in group_ranked_matches(logs_dict[t], stats):
            row = build_clean_row(
                g, t, latest_runtime, player_info_cache,
                fetch_player_data, elo_game_min, elo_game_max,
            )
            if row:
                rows.append(row)
    return rows


async def process_tags_and_write_async(
    player_tags: list[str],
    api_key: str,
    latest_runtime: datetime,
    max_depth: int = 2,
    batch_size: int = 1500,
    concurrency: int = 40,
    fetch_player_data: bool = False,
    clean_db_path: str | None = None,
    elo_queue_min: float | None = None,
    elo_queue_max: float | None = None,
    elo_game_min: float | None = None,
    elo_game_max: float | None = None,
    prefilter_initial_tags: bool = False,
    requests_per_second: float = 5.0,
    fetched_tags_ttl_hours: float = 0.0,
    flush_every_n_batches: int = 0,
    budget: RunBudget | None = None,
    resume: bool = True,
    record_run: bool = True,
) -> CrawlStats:
    """Depth-limited BFS over ranked battle logs, bounded by `budget`.

    Enqueue policy: follow a discovered player only if their observed elo falls
    in [elo_queue_min, elo_queue_max]. Build policy: keep a set only if its
    average elo falls in [elo_game_min, elo_game_max]. These are deliberately
    separate — see docs/DESIGN.md.

    The run resumes from and writes back a persistent frontier, so a sequence of
    short bounded runs behaves as one long crawl. Returns the run's CrawlStats.
    """
    _validate_range(elo_queue_min, elo_queue_max, "elo_queue range")
    _validate_range(elo_game_min, elo_game_max, "elo_game range")

    stats = CrawlStats(budget=budget or RunBudget())
    stop_reason: StopReason | None = None
    status = "failed"
    queue: deque = deque()
    requeue: list[tuple[str, int]] = []

    pending: list[tuple[str, int]] = []
    if resume and clean_db_path:
        pending = load_frontier(clean_db_path)

    run_id = None
    if clean_db_path and record_run:
        run_id = start_run(
            clean_db_path,
            {
                "seed_tags": len(player_tags),
                "max_depth": max_depth,
                "batch_size": batch_size,
                "concurrency": concurrency,
                "requests_per_second": requests_per_second,
                "fetch_player_data": fetch_player_data,
                "elo_queue": [elo_queue_min, elo_queue_max],
                "elo_game": [elo_game_min, elo_game_max],
                "fetched_tags_ttl_hours": fetched_tags_ttl_hours,
                "flush_every_n_batches": flush_every_n_batches,
                "budget": vars(stats.budget),
                "resumed_frontier": len(pending),
            },
            frontier_before=len(pending),
        )

    logs_dict: dict = {}
    discovered_tags_set: set = set()
    visited_tags: set = set()
    _batch_count = 0

    # Skip tags fetched recently enough that their 25-battle window has not
    # meaningfully turned over.
    if fetched_tags_ttl_hours > 0.0 and clean_db_path and os.path.exists(clean_db_path):
        from datetime import timedelta
        _cutoff = (datetime.now(UTC) - timedelta(hours=fetched_tags_ttl_hours)).isoformat()
        _preload_conn = sqlite3.connect(clean_db_path)
        try:
            _preload_conn.execute(
                "CREATE TABLE IF NOT EXISTS fetched_tags (tag TEXT PRIMARY KEY, fetched_utc TEXT NOT NULL)"
            )
            visited_tags.update(
                r[0] for r in _preload_conn.execute(
                    "SELECT tag FROM fetched_tags WHERE fetched_utc >= ?", (_cutoff,)
                )
            )
        except Exception:
            pass
        finally:
            _preload_conn.close()

    async def _flush_logs() -> None:
        """Write accumulated logs as rows and release the buffer."""
        nonlocal logs_dict
        if not logs_dict:
            return
        rows = _build_clean_rows_from_logs(
            logs_dict, latest_runtime, {}, False, elo_game_min, elo_game_max, stats
        )
        if rows and clean_db_path:
            inserted = await asyncio.to_thread(
                insert_rows_matches_in_chunks, clean_db_path, rows, 10000
            )
            stats.record_rows(inserted)
        if fetched_tags_ttl_hours > 0.0 and clean_db_path:
            _now = datetime.now(UTC).isoformat()
            os.makedirs(os.path.dirname(os.path.abspath(clean_db_path)), exist_ok=True)
            _c = sqlite3.connect(clean_db_path)
            try:
                create_fetched_tags_table_if_not_exists(_c)
                upsert_fetched_tags(_c, list(logs_dict.keys()), _now)
            finally:
                _c.close()
        logs_dict = {}

    semaphore = asyncio.Semaphore(concurrency)
    rate_limiter = AsyncRateLimiter(requests_per_second=requests_per_second)
    connector = aiohttp.TCPConnector(limit=concurrency, limit_per_host=concurrency, ttl_dns_cache=300)
    timeout = aiohttp.ClientTimeout(total=30)

    try:
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            initial_tags = list(player_tags)
            player_info_cache: dict = {}

            if prefilter_initial_tags and (elo_queue_min is not None or elo_queue_max is not None):
                infos = await asyncio.gather(*[
                    fetch_player_info_async(t, api_key, session, semaphore, rate_limiter, stats)
                    for t in initial_tags
                ])
                filtered = []
                for t, info_json in zip(initial_tags, infos, strict=True):
                    if not info_json or info_json is BUDGET_SKIP:
                        continue
                    player_info_cache[t] = info_json
                    try:
                        in_range = any(
                            _is_elo_in_range(b.get("trophies"), elo_queue_min, elo_queue_max)
                            for b in info_json.get("brawlers", [])
                        )
                    except Exception:
                        in_range = False
                    if in_range:
                        filtered.append(t)
                initial_tags = filtered

            # Resumed frontier first, then any new seeds not already pending.
            queue = deque(pending)
            enqueued = {t for t, _ in pending}
            for t in initial_tags:
                if t not in enqueued:
                    queue.append((t, 0))
                    enqueued.add(t)

            pbar_bfs = tqdm(desc="BFS: fetching logs", total=0, dynamic_ncols=True)

            while queue:
                stop_reason = stats.should_stop()
                if stop_reason is not None:
                    break

                batch = []
                while queue and len(batch) < batch_size:
                    batch.append(queue.popleft())

                tasks = []
                for (tag, depth) in batch:
                    if tag not in visited_tags:
                        visited_tags.add(tag)
                        tasks.append((tag, depth))
                if not tasks:
                    continue

                results = await asyncio.gather(*[
                    fetch_battle_log_async(t, api_key, session, semaphore, rate_limiter, stats)
                    for (t, _) in tasks
                ])

                for (tag, depth), battle_log in zip(tasks, results, strict=True):
                    if battle_log is BUDGET_SKIP:
                        # No request was made. Put it back rather than lose it.
                        visited_tags.discard(tag)
                        requeue.append((tag, depth))
                        continue
                    if battle_log:
                        logs_dict[tag] = battle_log
                        for nt, elo_val in get_all_solo_ranked_tags_with_elos(battle_log):
                            discovered_tags_set.add(nt)
                            if depth < max_depth and _is_elo_in_range(elo_val, elo_queue_min, elo_queue_max):
                                if nt not in visited_tags and nt not in enqueued:
                                    queue.append((nt, depth + 1))
                                    enqueued.add(nt)

                pbar_bfs.update(len(tasks))
                _batch_count += 1

                if (
                    flush_every_n_batches > 0
                    and not fetch_player_data
                    and _batch_count % flush_every_n_batches == 0
                ):
                    await _flush_logs()
            else:
                stop_reason = StopReason.FRONTIER_EXHAUSTED

            pbar_bfs.close()

            if fetch_player_data:
                to_fetch = [t for t in discovered_tags_set if t not in player_info_cache]
                pbar_info = tqdm(total=len(to_fetch), desc="Fetching player info", dynamic_ncols=True)
                results_info = await asyncio.gather(*[
                    fetch_player_info_async(t, api_key, session, semaphore, rate_limiter, stats)
                    for t in to_fetch
                ])
                for t, info_json in zip(to_fetch, results_info, strict=True):
                    if info_json and info_json is not BUDGET_SKIP:
                        player_info_cache[t] = info_json
                pbar_info.update(len(to_fetch))
                pbar_info.close()

        if fetched_tags_ttl_hours > 0.0 and clean_db_path and visited_tags:
            _now = datetime.now(UTC).isoformat()
            os.makedirs(os.path.dirname(os.path.abspath(clean_db_path)), exist_ok=True)
            _c = sqlite3.connect(clean_db_path)
            try:
                create_fetched_tags_table_if_not_exists(_c)
                upsert_fetched_tags(_c, list(visited_tags), _now)
            finally:
                _c.close()

        clean_rows = _build_clean_rows_from_logs(
            logs_dict, latest_runtime, player_info_cache, fetch_player_data,
            elo_game_min, elo_game_max, stats,
        )
        if clean_db_path and clean_rows:
            inserted = await asyncio.to_thread(
                insert_rows_matches_in_chunks, clean_db_path, clean_rows, 10000
            )
            stats.record_rows(inserted)
        status = "ok"

    except BaseException:
        stop_reason = StopReason.FAILED
        raise
    finally:
        remaining = requeue + list(queue)
        if clean_db_path:
            try:
                save_frontier(clean_db_path, remaining)
            except Exception as e:
                print(f"Could not save frontier: {e}")
            if run_id is not None:
                try:
                    finish_run(
                        clean_db_path, run_id,
                        status=status,
                        stop_reason=None if stop_reason is None else str(stop_reason),
                        stats=stats.summary(),
                        frontier_after=len(remaining),
                    )
                except Exception as e:
                    print(f"Could not record run: {e}")

    return stats
