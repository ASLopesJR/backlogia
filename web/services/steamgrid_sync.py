# steamgrid_sync.py
# Fetches SteamgridDb arts

import json
import sqlite3
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

import requests

# Rate limiting for Steam Store API
_rate_limit_lock = Lock()
_last_request_time = 0
_MIN_REQUEST_INTERVAL = 0.2 

from .settings import get_setting, STEAMGRID_API_KEY


def _rate_limited_request(url, params=None, headers=None, interval=_MIN_REQUEST_INTERVAL):
    """Make a rate-limited request to Steam Store API."""
    global _last_request_time

    with _rate_limit_lock:
        now = time.time()
        elapsed = now - _last_request_time
        if elapsed < interval:
            time.sleep(interval - elapsed)
        _last_request_time = time.time()

    try:
        response = requests.get(url, params=params, headers=headers, timeout=10)
        return response
    except requests.RequestException:
        return None

def verify_steamgrid_api_key(api_key):
    """Returns (True, None) if valid, (False, reason) if not."""
    url = "https://www.steamgriddb.com/api/v2/grids/steam/100"
    headers = {"Authorization": f"Bearer {api_key}"}
    response = _rate_limited_request(url, headers=headers)
    if not response:
        return False, "request_failed"
    if response.status_code == 401:
        return False, "invalid_api_key"
    if response.status_code == 403:
        return False, "forbidden"
    return True, None

def get_steamgrid_cover(appid=None, api_key=None,store='steam'):
    if appid is None or api_key is None:
        return None, "missing_parameters"

    url = f"https://www.steamgriddb.com/api/v2/grids/{store}/{appid}"
    headers = {"Authorization": f"Bearer {api_key}"}
    params = {"dimensions": "600x900"}  

    response = _rate_limited_request(url, params=params, headers=headers)

    if not response:
        return None, "request_failed"
    if response.status_code != 200:
        return None, f"http_{response.status_code}"

    try:
        body = response.json()
        if not body or not body.get("success"):
            return None, "not_found"

        first_match = body.get("data", [])
        if not first_match:
            return None, "not_found"
        url = first_match[0].get("url")

        return url, None

    except Exception as e:
        return None, f"parse_error: {e}"

def get_steamgrid_hero(appid=None, api_key=None, store='steam'):
    if appid is None or api_key is None:
        return None, "missing_parameters"
    if store=="epic":
        store = "egs"

    url = f"https://www.steamgriddb.com/api/v2/heroes/{store}/{appid}"
    headers = {"Authorization": f"Bearer {api_key}"}

    response = _rate_limited_request(url, headers=headers)

    if not response:
        return None, "request_failed"
    if response.status_code != 200:
        return None, f"http_{response.status_code}"

    try:
        body = response.json()
        if not body or not body.get("success"):
            return None, "not_found"

        first_match = body.get("data", [])
        if not first_match:
            return None, "not_found"
        url = first_match[0].get("url")


        return url, None

    except Exception as e:
        return None, f"parse_error: {e}"

def get_eligible_game_for_steamgrid(conn=None, id=None):

    if id is None or conn is None:
        raise(Exception("id and conn parameters are required"))

    cursor = conn.cursor()

    cursor.execute(
        "SELECT store, store_id, steam_app_id FROM games WHERE id IS ?",
        (id,)
    )

    store, store_id, steam_app_id = cursor.fetchone()

    if steam_app_id:
        return steam_app_id, "steam"
    else:
        return None, None

def get_eligible_games_for_steamgrid(conn, force=False):
    cursor = conn.cursor()

    if force:
        cursor.execute(
            "SELECT id, store, store_id, name, steam_app_id FROM games WHERE steam_app_id IS NOT NULL"
        )
    else:
        cursor.execute(
            """SELECT id, store, store_id, name, steam_app_id FROM games
               WHERE steam_app_id IS NOT NULL AND cover_url_override IS NULL"""
        )

    games_with_appid = cursor.fetchall()

    return games_with_appid

def sync_steamgrid_grids(conn, force=False, max_workers=5, progress_callback=None):
    """Fetch SteamGridDB grids for all games in the database and update image URLs.

    Args:
        conn: Database connection
        force: If True, re-fetch grids for all games; if False, only games missing grids
        max_workers: Number of threads for parallel fetching
        progress_callback: Optional callback(current, total, message)

    Returns:
        (updated, failed) counts
    """

    games = get_eligible_games_for_steamgrid(conn, force)
    total = len(games)

    if total == 0:
        return 0, 0

    print(f"Fetching SteamGridDB grids for {total} games ({max_workers} threads)...")

    updated = 0
    failed = 0
    completed = 0
    results_lock = Lock()

    db_path = conn.execute("PRAGMA database_list").fetchone()[2]

    api_key = get_setting(STEAMGRID_API_KEY)
    if not api_key:
        raise Exception("SteamGridDB API key is not set. Please set it in the settings page.")

    def fetch_and_update(row):
        game_id, store, store_id, name, appid = row
        if appid:
            grid_url, error = get_steamgrid_cover(appid=appid, api_key=api_key)
        else:
            grid_url, error = get_steamgrid_cover(store=store, appid=store_id, api_key=api_key)
        return game_id, store, store_id, name, grid_url, error

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(fetch_and_update, row): row for row in games}

        for future in as_completed(futures):
            game_id, store, store_id, name, grid_url, error = future.result()

            with results_lock:
                completed += 1
                if progress_callback:
                    progress_callback(completed, total, f"Processing: {name[:50]}...")

            if grid_url:
                # Each thread needs its own connection
                thread_conn = sqlite3.connect(db_path)
                try:
                    thread_conn.execute(
                        """UPDATE games SET
                            cover_url_override = ?,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?""",
                        (grid_url, game_id),
                    )
                    thread_conn.commit()
                finally:
                    thread_conn.close()

                with results_lock:
                    updated += 1
            else:
                print(f"game failed to fetch grid: {name} (store: {store}, store_id: {store_id}) - error: {error}")
                with results_lock:
                    failed += 1

    return updated, failed

def sync_steamgrid_by_id(conn, game_id):
        api_key = get_setting(STEAMGRID_API_KEY)
        if not api_key:
            return False, "SteamGridDB API key is not set. Please set it in the settings page."
    
        game, store = get_eligible_game_for_steamgrid(conn, game_id)
        if not game or not store:
            return False, "Game not eligible for SteamGridDB sync, missing appid"

        grid_url, error = get_steamgrid_cover(store=store, appid=game , api_key=api_key)


        if grid_url:
            conn.execute(
                """UPDATE games SET
                    cover_url_override = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?""",
                (grid_url, game_id),
            )
            conn.commit()
            return True, None

        return False, error

def sync_steamgrid(conn, force=False, max_workers=5, progress_callback=None):
    """Fetch SteamGridDB images for all Steam games in the database and update image URLs.

    Args:
        conn: Database connection
        force: If True, re-fetch images for all Steam games; if False, only games missing images
        max_workers: Number of threads for parallel fetching
        progress_callback: Optional callback(current, total, message)

    Returns:
        (updated, failed) counts
    """

    api_key = get_setting(STEAMGRID_API_KEY)
    if not api_key:
        raise Exception("SteamGridDB API key is not set. Please set it in the settings page.")

    valid, error = verify_steamgrid_api_key(api_key)
    if not valid:
        raise Exception(f"Invalid SteamGridDB API key: {error}")

    updated, failed = sync_steamgrid_grids(conn,force,max_workers, progress_callback)

    return updated, failed
