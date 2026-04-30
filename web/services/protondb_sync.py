# protondb_sync.py
# Fetches ProtonDB Linux/Steam Deck compatibility data for Steam games

import sqlite3
import requests
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed


class ProtonDBClient:
    """Client for fetching game data from ProtonDB."""

    BASE_URL = "https://www.protondb.com/api/v1/reports/summaries"

    def __init__(self, min_request_interval=0.5):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json",
        })
        self.last_request_time = 0
        self.min_request_interval = min_request_interval
        self._lock = threading.Lock()

    def _rate_limit(self):
        """Ensure we don't make requests too quickly (thread-safe)."""
        with self._lock:
            elapsed = time.time() - self.last_request_time
            if elapsed < self.min_request_interval:
                time.sleep(self.min_request_interval - elapsed)
            self.last_request_time = time.time()

    def _make_request(self, url):
        """Make a rate-limited request."""
        self._rate_limit()
        try:
            response = self.session.get(url, timeout=15)
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return response
        except requests.RequestException as e:
            print(f"Request error: {e}")
            return None

    def get_game_by_steam_id(self, steam_id):
        """
        Get ProtonDB data for a game by its Steam ID.

        Returns dict with: tier, score, confidence, total, trending_tier, best_reported_tier
        """
        url = f"{self.BASE_URL}/{steam_id}.json"
        response = self._make_request(url)

        if not response:
            return None

        try:
            data = response.json()
            return {
                "tier": data.get("tier"),
                "score": data.get("score"),
                "confidence": data.get("confidence"),
                "total": data.get("total"),
                "trending_tier": data.get("trendingTier"),
                "best_reported_tier": data.get("bestReportedTier"),
            }
        except Exception as e:
            print(f"Error parsing ProtonDB response: {e}")
            return None


def _process_single_game(client, game_id, steam_id):
    """
    Process a single game for ProtonDB data.
    Returns a tuple of (game_id, success, result_dict or error_message).
    """
    try:
        data = client.get_game_by_steam_id(steam_id)

        if not data:
            return (game_id, False, "No data found")

        if not data.get("tier"):
            return (game_id, False, "No tier data")

        return (game_id, True, data)

    except Exception as e:
        return (game_id, False, f"Error: {e}")


def sync_games(conn, client, force=False, max_workers=5, progress_callback=None):
    """Sync games with ProtonDB using multithreading.

    Syncs all games that have a Steam ID - either from being owned on Steam (store_id)
    or from IGDB external_games data (steam_app_id).

    Args:
        conn: Database connection
        client: ProtonDBClient instance
        force: If True, resync all games with Steam IDs; if False, only sync unmatched games
        max_workers: Number of parallel workers
        progress_callback: Optional callback function(current, total, message) for progress updates
    """
    cursor = conn.cursor()

    # Get all games that have a Steam ID (either from store_id or steam_app_id from IGDB)
    # Use COALESCE to prefer steam_app_id (from IGDB) over store_id (for Steam-owned games)
    # This allows non-Steam games with IGDB data to get ProtonDB info
    if force:
        cursor.execute(
            """SELECT id,
                      COALESCE(steam_app_id, CASE WHEN store = 'steam' THEN store_id END) as steam_id,
                      name
               FROM games
               WHERE (steam_app_id IS NOT NULL OR (store = 'steam' AND store_id IS NOT NULL))
               AND (hidden IS NULL OR hidden = 0)
               ORDER BY name"""
        )
    else:
        cursor.execute(
            """SELECT id,
                      COALESCE(steam_app_id, CASE WHEN store = 'steam' THEN store_id END) as steam_id,
                      name
               FROM games
               WHERE (steam_app_id IS NOT NULL OR (store = 'steam' AND store_id IS NOT NULL))
               AND protondb_tier IS NULL
               AND (hidden IS NULL OR hidden = 0)
               ORDER BY name"""
        )

    games = cursor.fetchall()

    total = len(games)
    print(f"Processing {total} Steam games for ProtonDB data with {max_workers} workers...")

    matched = 0
    failed = 0
    completed = 0
    results_lock = threading.Lock()

    def update_database(game_id, result):
        """Update the database with the result."""
        cursor.execute(
            """UPDATE games SET
                protondb_tier = ?,
                protondb_score = ?,
                protondb_confidence = ?,
                protondb_total = ?,
                protondb_trending_tier = ?,
                protondb_matched_at = CURRENT_TIMESTAMP
            WHERE id = ?""",
            (
                result["tier"],
                result["score"],
                result["confidence"],
                result["total"],
                result["trending_tier"],
                game_id,
            ),
        )

    def mark_not_found(game_id):
        """Mark game as searched but not found (protondb_tier = 'unknown')."""
        cursor.execute(
            """UPDATE games SET
                protondb_tier = 'unknown',
                protondb_matched_at = CURRENT_TIMESTAMP
            WHERE id = ?""",
            (game_id,),
        )

    # Process games in parallel
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Submit all tasks
        future_to_game = {
            executor.submit(_process_single_game, client, game_id, steam_id): (game_id, steam_id, name)
            for game_id, steam_id, name in games
        }

        # Process results as they complete
        for future in as_completed(future_to_game):
            game_id, steam_id, name = future_to_game[future]
            completed += 1

            # Report progress
            if progress_callback:
                progress_callback(completed, total, f"Processing: {name[:50]}...")

            try:
                result_game_id, success, result = future.result()

                if success:
                    # Update database (SQLite operations need to be serialized)
                    with results_lock:
                        update_database(result_game_id, result)
                        conn.commit()
                        matched += 1

                    tier = result.get("tier", "unknown")
                    total_reports = result.get("total", 0)
                    print(f"[{completed}/{total}] {name} → {tier} ({total_reports} reports)")
                else:
                    # Mark as searched but not found
                    with results_lock:
                        mark_not_found(game_id)
                        conn.commit()
                        failed += 1
                    print(f"[{completed}/{total}] {name} → {result}")

            except Exception as e:
                # Mark as searched but not found on exception
                with results_lock:
                    mark_not_found(game_id)
                    conn.commit()
                    failed += 1
                print(f"[{completed}/{total}] {name} → Exception: {e}")

    return matched, failed


def get_stats(conn):
    """Get ProtonDB sync statistics."""
    cursor = conn.cursor()

    # Count total Steam games
    cursor.execute("SELECT COUNT(*) FROM games WHERE store = 'steam'")
    total_steam = cursor.fetchone()[0]

    # Count matched games (protondb_tier is not null and not 'unknown')
    cursor.execute(
        "SELECT COUNT(*) FROM games WHERE protondb_tier IS NOT NULL AND protondb_tier != 'unknown'"
    )
    matched = cursor.fetchone()[0]

    # Count by tier
    cursor.execute(
        """SELECT protondb_tier, COUNT(*) FROM games
           WHERE protondb_tier IS NOT NULL AND protondb_tier != 'unknown'
           GROUP BY protondb_tier
           ORDER BY COUNT(*) DESC"""
    )
    tier_counts = dict(cursor.fetchall())

    return {
        "total_steam": total_steam,
        "matched": matched,
        "match_rate": (matched / total_steam * 100) if total_steam > 0 else 0,
        "tier_counts": tier_counts,
    }
