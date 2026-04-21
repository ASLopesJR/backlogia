# steam_sync.py
# Fetches Steam review scores and updates games in the database

import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

import requests

from .settings import get_steam_credentials

# Rate limiting for Steam Store API
_rate_limit_lock = Lock()
_last_request_time = 0
_MIN_REQUEST_INTERVAL = 0.2  # 200ms between requests (5 req/sec max)


def _rate_limited_request(url, params=None):
    """Make a rate-limited request to Steam Store API."""
    global _last_request_time

    with _rate_limit_lock:
        now = time.time()
        elapsed = now - _last_request_time
        if elapsed < _MIN_REQUEST_INTERVAL:
            time.sleep(_MIN_REQUEST_INTERVAL - elapsed)
        _last_request_time = time.time()

    try:
        response = requests.get(url, params=params, timeout=10)
        return response
    except requests.RequestException:
        return None

def get_steam_store_info(appid):
    """Fetch store info for a Steam game.

    Returns a dict with:
    - screenshots: percentage of positive reviews (0-100)
    - summary: text description (e.g., "Very Positive")
    - cover_url: total number of reviews
    """
    url = f"https://store.steampowered.com/api/appdetails?appids={appid}"

    response = _rate_limited_request(url)
    if not response or response.status_code != 200:
        return None

    try:
        data = response.json().get(f"{appid}")
        summary = data.get("data", {}).get("detailed_description")
        print(summary)

        return {
            "summary": summary,
        }
    except (ValueError, KeyError):
        return None


def get_steam_review_score(appid):
    """Fetch review score for a Steam game.

    Returns a dict with:
    - review_score: percentage of positive reviews (0-100)
    - review_desc: text description (e.g., "Very Positive")
    - total_reviews: total number of reviews
    """
    url = f"https://store.steampowered.com/appreviews/{appid}"
    params = {
        "json": 1,
        "language": "all",
        "purchase_type": "all"
    }

    response = _rate_limited_request(url, params)
    if not response or response.status_code != 200:
        return None

    try:
        data = response.json()
        summary = data.get("query_summary", {})

        total_positive = summary.get("total_positive", 0)
        total_negative = summary.get("total_negative", 0)
        total_reviews = total_positive + total_negative

        if total_reviews == 0:
            return None

        review_score = round((total_positive / total_reviews) * 100, 1)
        review_desc = summary.get("review_score_desc", "")

        return {
            "review_score": review_score,
            "review_desc": review_desc,
            "total_reviews": total_reviews,
        }
    except (ValueError, KeyError):
        return None


def sync_steam_reviews(conn, force=False, max_workers=5, progress_callback=None):
    """Fetch Steam review scores for all Steam games in the database and update critics_score.

    Args:
        conn: Database connection
        force: If True, re-fetch reviews for all Steam games; if False, only games missing critics_score
        max_workers: Number of threads for parallel fetching
        progress_callback: Optional callback(current, total, message)

    Returns:
        (updated, failed) counts
    """
    cursor = conn.cursor()

    if force:
        cursor.execute(
            "SELECT id, store_id, name FROM games WHERE store = 'steam' AND store_id IS NOT NULL"
        )
    else:
        cursor.execute(
            """SELECT id, store_id, name FROM games
               WHERE store = 'steam' AND store_id IS NOT NULL AND critics_score IS NULL"""
        )

    games = cursor.fetchall()
    total = len(games)

    if total == 0:
        return 0, 0

    print(f"Fetching Steam reviews for {total} games ({max_workers} threads)...")

    updated = 0
    failed = 0
    completed = 0
    results_lock = Lock()

    db_path = conn.execute("PRAGMA database_list").fetchone()[2]

    def fetch_and_update(row):
        game_id, store_id, name = row
        reviews = get_steam_review_score(store_id)
        return game_id, name, reviews

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(fetch_and_update, row): row for row in games}

        for future in as_completed(futures):
            game_id, name, reviews = future.result()

            with results_lock:
                completed += 1
                if progress_callback:
                    progress_callback(completed, total, f"Processing: {name[:50]}...")

            if reviews:
                # Each thread needs its own connection
                thread_conn = sqlite3.connect(db_path)
                thread_conn.execute(
                    """UPDATE games SET
                        critics_score = ?,
                        extra_data = json_set(COALESCE(extra_data, '{}'), '$.review_desc', ?, '$.total_reviews', ?),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?""",
                    (reviews["review_score"], reviews["review_desc"], reviews["total_reviews"], game_id),
                )
                thread_conn.commit()
                thread_conn.close()

                with results_lock:
                    updated += 1
            else:
                with results_lock:
                    failed += 1

    return updated, failed

def sync_steam(conn, force=False, max_workers=5, progress_callback=None):
    """Fetch Steam review scores for all Steam games in the database and update critics_score.

    Args:
        conn: Database connection
        force: If True, re-fetch reviews for all Steam games; if False, only games missing critics_score
        max_workers: Number of threads for parallel fetching
        progress_callback: Optional callback(current, total, message)

    Returns:
        (updated, failed) counts
    """

    updated, failed = sync_steam_reviews(conn, force, max_workers, progress_callback)
    get_steam_store_info(239140)

    return updated, failed
