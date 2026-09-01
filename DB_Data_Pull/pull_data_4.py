import aiohttp
import asyncio
import urllib.parse
import sqlite3
import json
import time
import os
from datetime import datetime, timezone
from typing import List, Optional, Tuple, Any
try:
    from tqdm import tqdm
except Exception:  # fallback in some notebook environments
    from tqdm.notebook import tqdm
from collections import deque

from data_clean.schema import (
    create_matches_table_if_not_exists,
    get_matches_insert_statement,
    create_fetched_tags_table_if_not_exists,
    upsert_fetched_tags,
)

########################
# CLEAN DB INSERTS
########################

########################
# HELPER FUNCTIONS
########################

def is_string_date_after(reference_dt, date_string):
    string_datetime = datetime.strptime(date_string, '%Y%m%dT%H%M%S.%fZ')
    string_datetime = string_datetime.replace(tzinfo=timezone.utc)
    if reference_dt.tzinfo is None:
        reference_dt = reference_dt.replace(tzinfo=timezone.utc)
    return string_datetime > reference_dt

class AsyncRateLimiter:
    """
    Simple global requests-per-second limiter with shared 429 backoff.
    Ensures at most `requests_per_second` acquisitions within any 1s window.
    A 429 triggers a global cooldown so all tasks pause before retrying.
    """
    def __init__(self, requests_per_second: float):
        self.requests_per_second = max(float(requests_per_second or 1.0), 0.1)
        self._events = deque()
        self._lock = asyncio.Lock()
        self._backoff_until = 0.0

    async def acquire(self):
        while True:
            # Respect global backoff if active
            now = time.monotonic()
            if now < self._backoff_until:
                await asyncio.sleep(self._backoff_until - now)

            async with self._lock:
                now = time.monotonic()
                # Drop timestamps older than 1s window
                while self._events and (now - self._events[0]) >= 1.0:
                    self._events.popleft()
                if len(self._events) < self.requests_per_second:
                    self._events.append(now)
                    return
                # Need to wait until the oldest acquisition falls out of the 1s window
                earliest = self._events[0]
                sleep_for = max(0.0, (earliest + 1.0) - now)
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)

    def trigger_backoff(self, seconds: float):
        target = time.monotonic() + max(0.0, float(seconds))
        # Extend backoff if longer than current
        if target > self._backoff_until:
            self._backoff_until = target

def group_ranked_matches(battle_log):
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
        except:
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

def get_player_tag_list(battle_log, exclusion_list):
    tags = []
    if battle_log is None:
        return tags
    for battle in battle_log.get('items', []):
        try:
            for team in battle['battle']['teams']:
                for player in team:
                    tag = player['tag']
                    if tag not in exclusion_list:
                        tags.append(tag)
        except:
            continue
    return list(set(tags))

########################
# NEW HELPER FUNCTION
########################

def get_all_solo_ranked_tags_with_elos(battle_log):
    """
    Return a list of (player_tag, elo) pairs found in 'soloRanked' matches.
    Elo is taken from player['brawler']['trophies'] within each match context.
    """
    result: List[Tuple[str, Optional[float]]] = []
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

def _is_elo_in_range(elo_value: Optional[float], elo_min: Optional[float], elo_max: Optional[float]) -> bool:
    """Return True if elo_value is within [elo_min, elo_max] when provided."""
    if elo_value is None:
        return False
    if elo_min is not None and elo_value < elo_min:
        return False
    if elo_max is not None and elo_value > elo_max:
        return False
    return True

def _validate_range(min_val: Optional[float], max_val: Optional[float], label: str) -> None:
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

def _compute_avg_elo(team1_brawlers, team2_brawlers) -> Optional[float]:
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
    elo_game_min: Optional[float],
    elo_game_max: Optional[float]
) -> Optional[Tuple[Any, ...]]:
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

def insert_rows_matches_in_chunks(db_path: str, rows: List[Tuple[Any, ...]], chunksize: int = 10000) -> None:
    """Bulk insert tuples into matches using fixed 40-column INSERT order."""
    if not rows:
        return
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
        for i in range(0, len(rows), chunksize):
            chunk = rows[i:i+chunksize]
            conn.executemany(insert_sql, chunk)
            conn.commit()
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

async def fetch_json_async(url, headers, session, semaphore, retries=3, delay=.005, rate_limiter: Optional[AsyncRateLimiter] = None):
    async with semaphore:
        for _ in range(retries):
            await asyncio.sleep(delay)
            try:
                if rate_limiter is not None:
                    await rate_limiter.acquire()
                async with session.get(url, headers=headers) as response:
                    if response.status == 200:
                        return await response.json()
                    elif response.status == 429:
                        try:
                            retry_after = float(response.headers.get("Retry-After", 0.5))
                        except Exception:
                            retry_after = 0.5
                        retry_after = max(0.5, retry_after)
                        # print(f"Rate limited (429). Retrying after {retry_after}s")
                        if rate_limiter is not None:
                            rate_limiter.trigger_backoff(retry_after)
                        await asyncio.sleep(retry_after)
                    else:
                        # 404 or other HTTP errors
                        # We'll let it print, but we won't fail the entire process
                        text = await response.text()
                        try:
                            data = json.loads(text)
                            # Check if reason is present and if it's not 'notFound'
                            if data.get('reason') != 'notFound':
                                print(f"HTTP {response.status}: {text}")
                        except json.JSONDecodeError:
                            # If it can't be decoded as JSON, just print it
                            print(f"HTTP {response.status}: {text}")
                        await asyncio.sleep(1)
            except Exception as e:
                print(f"Exception fetching {url}: {e}")
                await asyncio.sleep(1)
    return None

async def fetch_battle_log_async(player_tag, api_key, session, semaphore, rate_limiter: Optional[AsyncRateLimiter] = None):
    headers = {'Accept': 'application/json', 'Accept-Encoding': 'gzip', 'Authorization': f'Bearer {api_key}'}
    encoded_tag = urllib.parse.quote(player_tag)
    url = f'https://api.brawlstars.com/v1/players/{encoded_tag}/battlelog'
    return await fetch_json_async(url, headers, session, semaphore, rate_limiter=rate_limiter)

async def fetch_player_info_async(player_tag, api_key, session, semaphore, rate_limiter: Optional[AsyncRateLimiter] = None):
    headers = {'Accept': 'application/json', 'Accept-Encoding': 'gzip', 'Authorization': f'Bearer {api_key}'}
    encoded_tag = urllib.parse.quote(player_tag)
    url = f'https://api.brawlstars.com/v1/players/{encoded_tag}'
    return await fetch_json_async(url, headers, session, semaphore, rate_limiter=rate_limiter)

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
) -> list:
    rows = []
    for t in logs_dict:
        for g in group_ranked_matches(logs_dict[t]):
            row = build_clean_row(
                g, t, latest_runtime, player_info_cache,
                fetch_player_data, elo_game_min, elo_game_max,
            )
            if row:
                rows.append(row)
    return rows


async def process_tags_and_write_async(
    player_tags: List[str],
    api_key: str,
    latest_runtime: datetime,
    max_depth: int = 2,
    batch_size: int = 1500,
    concurrency: int = 40,
    fetch_player_data: bool = False,
    clean_db_path: Optional[str] = None,
    elo_queue_min: Optional[float] = None,
    elo_queue_max: Optional[float] = None,
    elo_game_min: Optional[float] = None,
    elo_game_max: Optional[float] = None,
    prefilter_initial_tags: bool = False,
    requests_per_second: float = 5.0,
    fetched_tags_ttl_hours: float = 0.0,
    flush_every_n_batches: int = 0,
):
    """
    Depth-limited BFS on 'soloRanked' matches:
      - Enqueue policy (queue gating): Only enqueue tags whose observed elo
        falls within [elo_queue_min, elo_queue_max] when provided.
      - Build policy (game gating): Only persist ranked games whose avg elo
        falls within [elo_game_min, elo_game_max] when provided.
      - If fetch_player_data=True, fetch player info for all discovered tags
        (so no null fields). Otherwise, set player columns to None.
      - If prefilter_initial_tags=True and elo_queue_* provided, initial
        player_tags are filtered via the player info endpoint before BFS.
    """

    # Validate ranges
    _validate_range(elo_queue_min, elo_queue_max, "elo_queue range")
    _validate_range(elo_game_min, elo_game_max, "elo_game range")

    # No raw path; clean DB table is created on insert

    # BFS Data
    logs_dict = {}
    discovered_tags_set = set()
    visited_tags = set()
    _batch_count = 0

    # Pre-load recently-fetched tags so BFS skips them across runs (Fix 1D)
    if fetched_tags_ttl_hours > 0.0 and clean_db_path and os.path.exists(clean_db_path):
        from datetime import timedelta
        _cutoff = (datetime.now(timezone.utc) - timedelta(hours=fetched_tags_ttl_hours)).isoformat()
        _preload_conn = sqlite3.connect(clean_db_path)
        try:
            _preload_conn.execute(
                "CREATE TABLE IF NOT EXISTS fetched_tags (tag TEXT PRIMARY KEY, fetched_utc TEXT NOT NULL)"
            )
            _preload_rows = _preload_conn.execute(
                "SELECT tag FROM fetched_tags WHERE fetched_utc >= ?", (_cutoff,)
            ).fetchall()
            visited_tags.update(r[0] for r in _preload_rows)
        except Exception:
            pass
        finally:
            _preload_conn.close()

    semaphore = asyncio.Semaphore(concurrency)
    rate_limiter = AsyncRateLimiter(requests_per_second=requests_per_second)
    connector = aiohttp.TCPConnector(limit=concurrency, limit_per_host=concurrency, ttl_dns_cache=300)
    timeout = aiohttp.ClientTimeout(total=30)

    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:

        # Optionally prefilter initial tags by elo using player info
        initial_tags = list(player_tags)
        player_info_cache = {}
        if prefilter_initial_tags and (elo_queue_min is not None or elo_queue_max is not None):
            coros = [fetch_player_info_async(t, api_key, session, semaphore, rate_limiter) for t in initial_tags]
            infos = await asyncio.gather(*coros)
            filtered = []
            for t, info_json in zip(initial_tags, infos):
                if info_json:
                    player_info_cache[t] = info_json
                    try:
                        brawlers = info_json.get('brawlers', [])
                        in_range = any(_is_elo_in_range(b.get('trophies'), elo_queue_min, elo_queue_max) for b in brawlers)
                    except Exception:
                        in_range = False
                    if in_range:
                        filtered.append(t)
            initial_tags = filtered

        # Prepare BFS queue and de-dup trackers
        queue = deque((t, 0) for t in initial_tags)
        enqueued = set(initial_tags)

        # PROGRESS BAR: BFS logs
        pbar_bfs = tqdm(desc="BFS: fetching logs", total=0, dynamic_ncols=True)

        while queue:
            # Build a batch
            batch = []
            while queue and len(batch) < batch_size:
                item = queue.popleft()
                batch.append(item)

            # Filter out already-fetched tags
            tasks = []
            for (tag, depth) in batch:
                if tag not in visited_tags:
                    visited_tags.add(tag)
                    tasks.append((tag, depth))

            if not tasks:
                continue

            # Fetch logs in parallel
            fetch_coros = [
                fetch_battle_log_async(t, api_key, session, semaphore, rate_limiter)
                for (t, _) in tasks
            ]
            results = await asyncio.gather(*fetch_coros)

            # Process each log
            for (tag, depth), battle_log in zip(tasks, results):
                if battle_log:
                    logs_dict[tag] = battle_log

                    # Gather player tags with elos from 'soloRanked' matches
                    new_pairs = get_all_solo_ranked_tags_with_elos(battle_log)
                    for nt, elo_val in new_pairs:
                        discovered_tags_set.add(nt)
                        # BFS expansion only if depth < max_depth and queue elo matches range
                        if depth < max_depth and _is_elo_in_range(elo_val, elo_queue_min, elo_queue_max):
                            if nt not in visited_tags and nt not in enqueued:
                                queue.append((nt, depth + 1))
                                enqueued.add(nt)

            pbar_bfs.update(len(tasks))
            _batch_count += 1

            # Incremental flush: write accumulated rows to DB and free memory (Fix 2D)
            # Only runs when fetch_player_data=False; player info isn't available mid-BFS.
            if (
                flush_every_n_batches > 0
                and not fetch_player_data
                and _batch_count % flush_every_n_batches == 0
                and logs_dict
            ):
                _flush_rows = _build_clean_rows_from_logs(
                    logs_dict, latest_runtime, {}, False, elo_game_min, elo_game_max
                )
                if _flush_rows and clean_db_path:
                    await asyncio.to_thread(
                        insert_rows_matches_in_chunks, clean_db_path, _flush_rows, 10000
                    )
                if fetched_tags_ttl_hours > 0.0 and clean_db_path:
                    _now_utc = datetime.now(timezone.utc).isoformat()
                    os.makedirs(os.path.dirname(os.path.abspath(clean_db_path)), exist_ok=True)
                    _mid_conn = sqlite3.connect(clean_db_path)
                    try:
                        create_fetched_tags_table_if_not_exists(_mid_conn)
                        upsert_fetched_tags(_mid_conn, list(logs_dict.keys()), _now_utc)
                    finally:
                        _mid_conn.close()
                logs_dict.clear()

        pbar_bfs.close()

        # 2) (OPTIONAL) Fetch player info reusing the same session (Fix 2E, 2B)
        if fetch_player_data:
            to_fetch = [t for t in discovered_tags_set if t not in player_info_cache]
            pbar_info = tqdm(total=len(to_fetch), desc="Fetching player info", dynamic_ncols=True)
            tasks_info = [
                fetch_player_info_async(t, api_key, session, semaphore, rate_limiter)
                for t in to_fetch
            ]
            results_info = await asyncio.gather(*tasks_info)
            for t, info_json in zip(to_fetch, results_info):
                if info_json:
                    player_info_cache[t] = info_json
            pbar_info.update(len(to_fetch))
            pbar_info.close()

    # Persist fetched tags so future runs can skip them (Fix 1A)
    if fetched_tags_ttl_hours > 0.0 and clean_db_path and visited_tags:
        _now_utc = datetime.now(timezone.utc).isoformat()
        os.makedirs(os.path.dirname(os.path.abspath(clean_db_path)), exist_ok=True)
        _persist_conn = sqlite3.connect(clean_db_path)
        try:
            create_fetched_tags_table_if_not_exists(_persist_conn)
            upsert_fetched_tags(_persist_conn, list(visited_tags), _now_utc)
        finally:
            _persist_conn.close()

    # 3-4) Build rows from remaining logs and write to DB
    clean_rows: list = []
    try:
        clean_rows = _build_clean_rows_from_logs(
            logs_dict, latest_runtime, player_info_cache, fetch_player_data, elo_game_min, elo_game_max
        )
    finally:
        # Always attempt the write even if row-building raises
        if clean_db_path and clean_rows:
            await asyncio.to_thread(
                insert_rows_matches_in_chunks,
                clean_db_path,
                clean_rows,
                10000,
            )

########################
# No raw utilities remain
########################
