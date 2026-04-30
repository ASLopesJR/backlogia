# igdb_sync.py
# Matches games in our database to IGDB entries and fetches ratings/metadata

import sqlite3
import requests
import time
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import unicodedata
from itertools import islice

from .settings import get_igdb_credentials, get_setting, IGDB_MATCH_THRESHOLD
from ..database import get_db

# IGDB API endpoints
TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
IGDB_API_URL = "https://api.igdb.com/v4"

# IGDB Popularity Type IDs (from /popularity_types endpoint)
POPULARITY_TYPE_IGDB_VISITS = 1
POPULARITY_TYPE_IGDB_WANT_TO_PLAY = 2
POPULARITY_TYPE_IGDB_PLAYING = 3
POPULARITY_TYPE_IGDB_PLAYED = 4
POPULARITY_TYPE_STEAM_PEAK_24H = 5
POPULARITY_TYPE_STEAM_POSITIVE_REVIEWS = 6

_BATCH = 10

# Global synchronization primitives
db_lock = threading.Lock()

class RateLimiter:
    """Thread-safe Token Bucket rate limiter for IGDB API (4 req/s)."""
    def __init__(self, rate=4.0, capacity=4.0):
        self.rate = rate
        self.capacity = capacity
        self.tokens = capacity
        self.last_update = time.time()
        self.lock = threading.Lock()

    def wait_for_token(self):
        """Block until a token is available."""
        while True:
            with self.lock:
                now = time.time()
                elapsed = now - self.last_update
                self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
                self.last_update = now

                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return

            time.sleep(0.1)

class IGDBClient:
    def __init__(self):
        self.access_token = None
        self.token_expires_at = 0
        self.token_lock = threading.Lock()
        self.rate_limiter = RateLimiter(rate=4.0, capacity=4.0)
        
        creds = get_igdb_credentials()
        self.client_id = creds.get("client_id")
        self.client_secret = creds.get("client_secret")
        self._get_access_token()

    def _get_access_token(self):
        """Get access token from Twitch OAuth."""
        if not self.client_id or not self.client_secret:
            raise ValueError(
                "IGDB credentials not configured. Please set them in Settings."
            )

        response = requests.post(
            TWITCH_TOKEN_URL,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "client_credentials",
            },
        )

        if response.status_code != 200:
            raise Exception(f"Failed to get access token: {response.text}")

        data = response.json()
        self.access_token = data["access_token"]
        self.token_expires_at = time.time() + data["expires_in"] - 60

        print(f"Got IGDB access token (expires in {data['expires_in'] // 3600} hours)")

    def _ensure_token(self):
        """Ensure we have a valid access token (thread-safe)."""
        if time.time() >= self.token_expires_at:
            with self.token_lock:
                # Double-check after acquiring lock
                if time.time() >= self.token_expires_at:
                    self._get_access_token()

    def _request(self, endpoint, body):
        """Make a request to the IGDB API with rate limiting."""
        self._ensure_token()
        self.rate_limiter.wait_for_token()

        response = requests.post(
            f"{IGDB_API_URL}/{endpoint}",
            headers={
                "Client-ID": self.client_id,
                "Authorization": f"Bearer {self.access_token}",
            },
            data=body,
        )

        if response.status_code == 429:
            # Rate limited - wait and retry
            retry_after = int(response.headers.get("Retry-After", 1))
            print(f"Rate limited, waiting {retry_after}s...")
            time.sleep(retry_after)
            return self._request(endpoint, body)

        if response.status_code in (408, 504):
            # Timeout - wait and retry
            retry_after = int(response.headers.get("Retry-After", 1))
            print(f"IGDB API timeout ({response.status_code}), waiting {retry_after}s...")
            time.sleep(retry_after)
            return self._request(endpoint, body)

        if response.status_code != 200:
            print(f"IGDB API error: {response.status_code} - {response.text}")
            return None

        return response.json()

    def search_game(self, name):
        """Search for a game by name."""
        # Clean up the name for better matching
        clean_name = self._clean_game_name(name)

        # Try exact name match first (avoids DLC variants crowding results)
        body = f'''
            where name = "{clean_name}";
            fields id, name, slug, game_type, rating, rating_count, aggregated_rating,
                   aggregated_rating_count, total_rating, total_rating_count,
                   summary, storyline, first_release_date,
                   genres.name, themes.id, themes.name, platforms.name,
                   involved_companies.company.name, involved_companies.developer,
                   involved_companies.publisher,
                   cover.url, screenshots.url,
                   external_games.uid, external_games.external_game_source,
                   websites.url;
            limit 5;
        '''
        results = self._request("games", body)
        if results:
            return results

        # Fall back to fuzzy search with higher limit
        body = f'''
            search "{clean_name}";
            fields id, name, slug, game_type, rating, rating_count, aggregated_rating,
                   aggregated_rating_count, total_rating, total_rating_count,
                   summary, storyline, first_release_date,
                   genres.name, themes.id, themes.name, platforms.name,
                   involved_companies.company.name, involved_companies.developer,
                   involved_companies.publisher,
                   cover.url, screenshots.url,
                   external_games.uid, external_games.external_game_source,
                   websites.url;
            limit 15;
        '''
        return self._request("games", body)

    def get_game_by_steam_id(self, steam_id):
        """Get a game by its Steam App ID.

        Tries external_games first (fast indexed lookup), then falls back to
        websites.url substring match for games not in external_games.
        """
        # Fast path: external_games has an indexed uid column
        body = f'''
            where uid = "{steam_id}" & external_game_source = 1;
            fields game;
            limit 1;
        '''
        results = self._request("external_games", body)
        if results:
            igdb_id = results[0].get("game")
            if igdb_id:
                return self.get_game_by_id(igdb_id)

        # Fallback: websites.url substring match — better coverage but slower
        body = f'''
            where websites.url ~ *"steampowered.com/app/{steam_id}"* & game_type = (0, 4, 6, 8, 9, 10, 11);
            fields id, name, slug, game_type, rating, rating_count, aggregated_rating,
                   aggregated_rating_count, total_rating, total_rating_count,
                   summary, storyline, first_release_date, 
                   genres.name, themes.id, themes.name, platforms.name,
                   involved_companies.company.name, involved_companies.developer,
                   involved_companies.publisher,
                   cover.url, screenshots.url,
                   external_games.uid, external_games.external_game_source,
                   websites.url;
            limit 1;
        '''
        results = self._request("games", body)
        if results:
            return results[0]

        return None
    def get_game_by_slug(self, slug):
        """Get a game by its store slug."""
        body = f'''
            where slug = "{slug}" & game_type = (0, 4, 6, 8, 9, 10, 11);
            fields id, name, game_type, slug, rating, rating_count, aggregated_rating,
                   aggregated_rating_count, total_rating, total_rating_count,
                   summary, storyline, first_release_date,
                   genres.name, themes.id, themes.name, platforms.name,
                   involved_companies.company.name, involved_companies.developer,
                   involved_companies.publisher,
                   cover.url, screenshots.url,
                   external_games.uid, external_games.external_game_source,
                   websites.url;
            limit 1;
        '''
        req = self._request("games", body)

        if req:
            return req

        slug = re.sub(r'-[0-9a-f]{6}$', '', slug)
        
        body = f'''
            where slug = "{slug}" & game_type = (0, 4, 6, 8, 9, 10, 11);
            fields id, name, slug, game_type, rating, rating_count, aggregated_rating,
                   aggregated_rating_count, total_rating, total_rating_count,
                   summary, storyline, first_release_date,
                   genres.name, themes.id, themes.name, platforms.name,
                   involved_companies.company.name, involved_companies.developer,
                   involved_companies.publisher,
                   cover.url, screenshots.url,
                   external_games.uid, external_games.external_game_source,
                   websites.url;
            limit 1;
        '''
        req = self._request("games", body)

        if req:
            return req

        return {}
    def get_game_by_id(self, igdb_id):
        """Get a game by its IGDB ID."""
        results = self.get_games_by_ids([igdb_id])
        return results[0] if results else None

    def get_popular_games(self, game_ids, popularity_type=None, limit=50):
        """
        Get popularity data for specific game IDs.

        Args:
            game_ids: List of IGDB game IDs to check
            popularity_type: Optional popularity type ID to filter by
            limit: Max results to return

        Returns:
            List of popularity primitives sorted by value (highest first)
        """
        if not game_ids:
            return []

        # Build the where clause
        ids_str = ",".join(str(id) for id in game_ids)
        where_clause = f"game_id = ({ids_str})"

        if popularity_type:
            where_clause += f" & popularity_type = {popularity_type}"

        body = f'''
            where {where_clause};
            fields game_id, value, popularity_type, calculated_at;
            sort value desc;
            limit {limit};
        '''

        return self._request("popularity_primitives", body) or []



    def get_games_by_ids(self, igdb_ids):
        """Get multiple games by their IGDB IDs."""
        if not igdb_ids:
            return []

        ids_str = ",".join(str(id) for id in igdb_ids)
        body = f'''
            where id = ({ids_str});
            fields id, name, slug, game_type, rating, rating_count, aggregated_rating,
                   aggregated_rating_count, total_rating, total_rating_count,
                   summary, storyline, first_release_date,
                   genres.name, themes.id, themes.name, platforms.name,
                   involved_companies.company.name, involved_companies.developer,
                   involved_companies.publisher,
                   cover.url, screenshots.url, artworks.url, videos.video_id,
                   external_games.uid, external_games.external_game_source,
                   websites.url;
            limit 500;
        '''

        return self._request("games", body) or []

    def get_games_by_steam_ids(self, steam_ids):
        """
        Batch version of get_game_by_steam_id.
        Returns a dict mapping steam_id (str) -> game_data.
        """
        if not steam_ids:
            return {}

        final_results = {}
        remaining_ids = set(str(sid) for sid in steam_ids)

        # --- PATH 1: Fast Path (external_games endpoint) ---
        # Construct: where (uid = "123" | uid = "456") & external_game_source = 1
        uid_filter = " | ".join(f'uid = "{sid}"' for sid in remaining_ids)
        ext_body = f'''
            where ({uid_filter}) & external_game_source = 1;
            fields game, uid;
            limit {len(remaining_ids)};
        '''
        
        ext_results = self._request("external_games", ext_body) or []
        
        # We need to fetch the actual game data for these IDs
        if ext_results:
            igdb_ids = [item.get("game") for item in ext_results if item.get("game")]
            # Map uid back to the IGDB id for later correlation
            uid_to_igdb = {str(item["uid"]): item["game"] for item in ext_results}
            
            # Batch fetch full game data by IGDB IDs
            full_games = self.get_games_by_ids(igdb_ids) # Assuming you have/can make this
            
            # Correlate back to steam_id
            for steam_id, igdb_id in uid_to_igdb.items():
                for game in full_games:
                    if game["id"] == igdb_id:
                        final_results[steam_id] = game
                        remaining_ids.discard(steam_id)
                        break

        # --- PATH 2: Fallback Path (websites.url match) ---
        if remaining_ids:
            # Construct: where (websites.url ~ *"app/123"* | websites.url ~ *"app/456"*)
            url_filter = " | ".join(f'websites.url ~ *"steampowered.com/app/{sid}"*' for sid in remaining_ids)
            
            fallback_body = f'''
                where ({url_filter}) & game_type = (0, 4, 6, 8, 9, 10, 11);
                fields id, name, slug, game_type, total_rating, summary, 
                    genres.name, platforms.name, cover.url,
                    external_games.uid, external_games.external_game_source,
                    websites.url;
                limit {len(remaining_ids)};
            '''
            
            fallback_games = self._request("games", fallback_body) or []
            
            # Use your regex logic to map these back to the correct Steam ID
            for game in fallback_games:
                # We use your existing extraction logic to identify which ID this match belongs to
                extracted_id = self.extract_steam_app_id(game)
                if extracted_id and extracted_id in remaining_ids:
                    final_results[extracted_id] = game

        return final_results

    def get_games_by_epic_ids(self, skus, slugs):
        """
        skus: list of Epic Artifact/Catalog IDs (Source 26)
        slugs: list of Epic Product Slugs (for URL fallback)
        Returns a dict mapping the identifier found (sku or slug) to game data.
        """
        if not skus:
            return {}

        final_results = {}
        # Create a mapping so Path 2 knows which slug belongs to which SKU
        # We'll use this to "discard" games as we find them.
        sku_to_slug = dict(zip(skus, slugs))
        remaining_skus = set(skus)

        # --- PATH 1: Fast Path (external_games endpoint) ---
        # Query using Source 26 IDs
        uid_filter = " | ".join(f'uid = "{sku}"' for sku in remaining_skus)
        ext_body = f'''
            where ({uid_filter}) & external_game_source = 26;
            fields game, uid;
            limit {len(remaining_skus)};
        '''
        
        ext_results = self._request("external_games", ext_body) or []
        
        if ext_results:
            igdb_ids = [item.get("game") for item in ext_results if item.get("game")]
            uid_to_igdb = {str(item["uid"]): item["game"] for item in ext_results}
            
            # Batch fetch full game data
            full_games = self.get_games_by_ids(igdb_ids)
            
            for found_sku, igdb_id in uid_to_igdb.items():
                for game in full_games:
                    if game["id"] == igdb_id:
                        final_results[found_sku] = game
                        remaining_skus.discard(found_sku)
                        break

        # --- PATH 2: Fallback Path (websites.url match via Slugs) ---
        if remaining_skus:
            # Get the slugs for the games we haven't found yet
            remaining_slugs = [sku_to_slug[sku] for sku in remaining_skus if sku_to_slug[sku]]
            
            if remaining_slugs:
                url_filter = " | ".join(f'websites.url ~ *"/p/{s}" | websites.url ~ *"/product/{s}"*' for s in remaining_slugs)
                
                # Note: Included game_type 14 (Update) for games like "Bad North Jotunn"
                fallback_body = f'''
                    where ({url_filter}) & game_type = (0, 4, 6, 8, 9, 10, 11);
                    fields id, name, slug, game_type, rating, rating_count, aggregated_rating,
                       aggregated_rating_count, total_rating, total_rating_count,
                       summary, storyline, first_release_date,
                       genres.name, themes.id, themes.name, platforms.name,
                       involved_companies.company.name, involved_companies.developer,
                       involved_companies.publisher,
                       cover.url, screenshots.url,
                       external_games.uid, external_games.external_game_source,
                       websites.url;
                    limit 50;
                '''
                
                fallback_games = self._request("games", fallback_body) or []
                
                for game in fallback_games:
                    websites = game.get("websites", [])
                    
                    # Validation: Iterate through remaining games to find a strict match
                    for sku in list(remaining_skus):
                        slug = sku_to_slug[sku]
                        if not slug: continue
                        
                        # HARDENING: Use Regex to ensure slug is a full path segment
                        # This prevents 'ark' from matching 'arknights'
                        # Checks for /p/ark/ or /p/ark? or /p/ark (end of string)
                        pattern = rf"/(?:p|product)/{re.escape(slug)}(?:/|\?|$)"
                        
                        found_match = False
                        for ws in websites:
                            url = ws.get("url", "")
                            if re.search(pattern, url):
                                found_match = True
                                break
                                
                        if found_match:
                            final_results[sku] = game
                            remaining_skus.discard(sku)
                            break
            if remaining_slugs:
                url_filter = " | ".join(f'websites.url ~ *"/p/{s}" | websites.url ~ *"/product/{s}"*' for s in remaining_slugs)
                
                # Note: Included game_type 14 (Update) for games like "Bad North Jotunn"
                fallback_body = f'''
                    where ({url_filter});
                    fields id, name, slug, game_type, rating, rating_count, aggregated_rating,
                       aggregated_rating_count, total_rating, total_rating_count,
                       summary, storyline, first_release_date,
                       genres.name, themes.id, themes.name, platforms.name,
                       involved_companies.company.name, involved_companies.developer,
                       involved_companies.publisher,
                       cover.url, screenshots.url,
                       external_games.uid, external_games.external_game_source,
                       websites.url;
                    limit 50;
                '''
                
                fallback_games = self._request("games", fallback_body) or []
                
                for game in fallback_games:
                    websites = game.get("websites", [])
                    
                    # Validation: Iterate through remaining games to find a strict match
                    for sku in list(remaining_skus):
                        slug = sku_to_slug[sku]
                        if not slug: continue
                        
                        # HARDENING: Use Regex to ensure slug is a full path segment
                        # This prevents 'ark' from matching 'arknights'
                        # Checks for /p/ark/ or /p/ark? or /p/ark (end of string)
                        pattern = rf"/(?:p|product)/{re.escape(slug)}(?:/|\?|$)"
                        
                        found_match = False
                        for ws in websites:
                            url = ws.get("url", "")
                            if re.search(pattern, url):
                                found_match = True
                                break
                                
                        if found_match:
                            final_results[sku] = game
                            remaining_skus.discard(sku)
                            break
        return final_results

    def get_games_by_gog_ids(self, sids, slugs):
        """
        sids: list of GOG Artifact/Catalog IDs (Source 26)
        slugs: list of GOG Product Slugs (for URL fallback)
        Returns a dict mapping the identifier found (sku or slug) to game data.
        """
        if not sids:
            return {}

        final_results = {}
        # Create a mapping so Path 2 knows which slug belongs to which SKU
        # We'll use this to "discard" games as we find them.
        sid_to_slug = dict(zip(sids, slugs))
        remaining_sids = set(sids)

        # --- PATH 1: Fast Path (external_games endpoint) ---
        # Query using Source 26 IDs
        uid_filter = " | ".join(f'uid = "{sid}"' for sid in remaining_sids)
        ext_body = f'''
            where ({uid_filter}) & external_game_source = 5;
            fields game, uid;
            limit {len(remaining_sids)};
        '''
        
        ext_results = self._request("external_games", ext_body) or []
        
        if ext_results:
            igdb_ids = [item.get("game") for item in ext_results if item.get("game")]
            uid_to_igdb = {str(item["uid"]): item["game"] for item in ext_results}
            
            # Batch fetch full game data
            full_games = self.get_games_by_ids(igdb_ids)
            
            for found_sid, igdb_id in uid_to_igdb.items():
                for game in full_games:
                    if game["id"] == igdb_id:
                        final_results[found_sid] = game
                        remaining_sids.discard(found_sid)
                        break

        # --- PATH 2: Fallback Path (websites.url match via Slugs) ---
        if remaining_sids:
            # Get the slugs for the games we haven't found yet
            remaining_slugs = [sid_to_slug[sid] for sid in remaining_sids if sid_to_slug[sid]]
            
            if remaining_slugs:
                url_filter = " | ".join(f'websites.url ~ *"/game/{s}"*' for s in remaining_slugs)
                
                # Note: Included game_type 14 (Update) for games like "Bad North Jotunn"
                fallback_body = f'''
                    where ({url_filter}) & game_type = (0, 4, 6, 8, 9, 10, 11);
                    fields id, name, slug, game_type, rating, rating_count, aggregated_rating,
                       aggregated_rating_count, total_rating, total_rating_count,
                       summary, storyline, first_release_date,
                       genres.name, themes.id, themes.name, platforms.name,
                       involved_companies.company.name, involved_companies.developer,
                       involved_companies.publisher,
                       cover.url, screenshots.url,
                       external_games.uid, external_games.external_game_source,
                       websites.url;
                    limit {len(remaining_slugs)};
                '''
                
                fallback_games = self._request("games", fallback_body) or []
                
                for game in fallback_games:
                    for website in game.get("websites", []):
                        url = website.get("url", "")
                        # Match the found game back to the original SID/Slug
                        for sid in list(remaining_sids):
                            slug = sid_to_slug[sid]
                            if f"/p/{slug}" in url or f"/product/{slug}" in url:
                                final_results[sid] = game
                                remaining_sids.discard(sid)
                                break
        return final_results

    def batch_lookup_slugs(self, slugs):
        """Batch lookup games by derived IGDB slugs (where slug = (...)).

        Returns a dict mapping slug (str) -> game data dict.
        """
        if not slugs:
            return {}

        BATCH = 50
        slug_list = list(slugs)
        results = {}

        for start in range(0, len(slug_list), BATCH):
            chunk = slug_list[start:start + BATCH]
            slugs_str = ",".join(f'"{s}"' for s in chunk)
            body = f'''
                where slug = ({slugs_str}) ;
                fields id, name, slug, game_type, rating, rating_count, aggregated_rating,
                       aggregated_rating_count, total_rating, total_rating_count,
                       summary, storyline, first_release_date,
                       genres.name, themes.id, themes.name, platforms.name,
                       involved_companies.company.name, involved_companies.developer,
                       involved_companies.publisher,
                       cover.url, screenshots.url,
                       external_games.uid, external_games.external_game_source,
                       websites.url;
                limit {BATCH};
            '''
            games = self._request("games", body) or []
            for game in games:
                results[game["slug"]] = game
            if start + BATCH < len(slug_list):
                time.sleep(0.3)

        return results

    @staticmethod
    def derive_igdb_slug(name):
        """Derive a candidate IGDB slug from a game name.

        IGDB slugs are lowercase with non-alphanumeric chars replaced by hyphens.
        """
        if not name:
            return None
        slug = name.lower()
        slug = re.sub(r"&", "and", slug)
        slug = re.sub(r"[^a-z0-9]+", "-", slug)
        slug = slug.strip("-")
        return slug or None

    @staticmethod
    def is_nsfw(game_data):
        """Check if a game should be marked as NSFW based on IGDB data."""
        if not game_data:
            return False

        # Check for Erotic theme (ID 42)
        themes = game_data.get("themes", [])
        for theme in themes:
            if theme.get("id") == 42:  # Erotic
                return True

        return False

    @staticmethod
    def extract_steam_app_id(game_data):
        """Extract Steam App ID from IGDB external_games data.

        IGDB external_games external_game_source 1 = Steam
        Returns the Steam App ID as a string, or None if not found.
        """
        if not game_data:
            return None

        external_games = game_data.get("external_games", [])
        for ext_game in external_games:
            # external_game_source 1 = Steam
            if ext_game.get("external_game_source") == 1:
                return str(ext_game.get("uid"))

        urls = game_data.get("websites", [])
        for website in urls:
            if website.get("url", "") and "steampowered.com/app/" in website["url"]:
                match = re.search(r"/app/(\d+)", website["url"])
                if match:
                    return match.group(1)

        return None

    def _clean_game_name(self, name):
        if not name: return ""
    
        name = re.sub(r"[\(\[].*?[\)\]]", "", name)
        name = unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').decode('ascii')
        name = name.lower()
        name = re.sub(r"[^a-z0-9\s]", " ", name)
        
        return " ".join(name.split()).strip()


def extract_genres_and_themes(igdb_data):
    """Extract genres and themes from IGDB data as a combined list of tag names."""
    tags = []

    # Extract genres (e.g., "Action", "RPG", "Adventure")
    if igdb_data.get("genres"):
        for genre in igdb_data["genres"]:
            if genre.get("name"):
                tags.append(genre["name"])

    # Extract themes (e.g., "Fantasy", "Sci-fi", "Horror")
    if igdb_data.get("themes"):
        for theme in igdb_data["themes"]:
            # Skip the "Erotic" theme (ID 42) - handled separately via NSFW flag
            if theme.get("id") == 42:
                continue
            if theme.get("name"):
                tags.append(theme["name"])

    return tags


def merge_and_dedupe_genres(existing_genres_json, new_genres):
    """
    Merge existing genres with new genres and de-duplicate.

    Args:
        existing_genres_json: JSON string of existing genres (or None)
        new_genres: List of new genre/theme names to add

    Returns:
        JSON string of merged and de-duplicated genres
    """
    # Parse existing genres
    existing = []
    if existing_genres_json:
        try:
            existing = json.loads(existing_genres_json)
            if not isinstance(existing, list):
                existing = []
        except (json.JSONDecodeError, TypeError):
            existing = []

    # Combine and de-duplicate (case-insensitive, preserving original case)
    seen = set()
    merged = []

    for genre in existing + new_genres:
        if not genre:
            continue
        genre_lower = genre.lower().strip()
        if genre_lower not in seen:
            seen.add(genre_lower)
            merged.append(genre.strip())

    return json.dumps(merged) if merged else None


def apply_igdb_data(conn, game_id, igdb_game, existing_genres=None):
    """Apply IGDB game data to the database for a given game_id.

    Args:
        conn: Database connection
        game_id: The local game ID
        igdb_game: IGDB game data dict
        existing_genres: Optional pre-fetched genres JSON string; fetched from DB if None
    """
    with db_lock:
        cursor = conn.cursor()

        # Extract cover URL
        cover_url = None
        if igdb_game.get("cover"):
            cover_url = igdb_game["cover"].get("url", "")
            cover_url = cover_url.replace("t_thumb", "t_cover_big")
            if cover_url and not cover_url.startswith("http"):
                cover_url = "https:" + cover_url

        # Extract up to 5 screenshot URLs
        screenshots = []
        if igdb_game.get("screenshots"):
            for screenshot in igdb_game["screenshots"][:5]:
                url = screenshot.get("url", "")
                url = url.replace("t_thumb", "t_screenshot_big")
                if url and not url.startswith("http"):
                    url = "https:" + url
                screenshots.append(url)

        # Check if game is NSFW
        is_nsfw = IGDBClient.is_nsfw(igdb_game)

        # Extract Steam App ID from IGDB external_games
        steam_app_id = IGDBClient.extract_steam_app_id(igdb_game)

        # Fetch existing genres if not provided, and check if already steam-synced
        if existing_genres is None:
            cursor.execute("SELECT genres, steam_synced_at FROM games WHERE id = ?", (game_id,))
            row = cursor.fetchone()
            existing_genres = row[0] if row else None
            steam_synced = bool(row[1]) if row else False
        else:
            cursor.execute("SELECT steam_synced_at FROM games WHERE id = ?", (game_id,))
            row = cursor.fetchone()
            steam_synced = bool(row[0]) if row else False

        # Extract genres and themes from IGDB and merge with existing
        igdb_tags = extract_genres_and_themes(igdb_game)
        merged_genres = merge_and_dedupe_genres(existing_genres, igdb_tags)

        # If Steam has already synced this game, preserve its summary/cover/screenshots.
        # Otherwise IGDB data takes priority and overwrites directly.
        if steam_synced:
            summary_expr = "summary = COALESCE(summary, ?)"
            cover_expr   = "cover_url = COALESCE(cover_url, ?)"
            shots_expr   = "screenshots = COALESCE(screenshots, ?)"
        else:
            summary_expr = "summary = ?"
            cover_expr   = "cover_url = ?"
            shots_expr   = "screenshots = ?"
        params = (
                igdb_game.get("id"),
                igdb_game.get("slug"),
                igdb_game.get("rating"),
                igdb_game.get("rating_count"),
                igdb_game.get("aggregated_rating"),
                igdb_game.get("aggregated_rating_count"),
                igdb_game.get("total_rating"),
                igdb_game.get("total_rating_count"),
                igdb_game.get("summary"),
                cover_url,
                json.dumps(screenshots) if screenshots else None,
                1 if is_nsfw else 0,
                merged_genres,
                steam_app_id,
                igdb_game.get("first_release_date"),
<<<<<<< HEAD
                json.dumps(igdb_game) if igdb_game else None,
=======
>>>>>>> a3f69c5 (feat(sync): parallelize IGDB metadata synchronization)
                game_id,
            )

        query = f"""UPDATE games SET
                igdb_id = ?,
                igdb_slug = ?,
                igdb_rating = ?,
                igdb_rating_count = ?,
                aggregated_rating = ?,
                aggregated_rating_count = ?,
                total_rating = ?,
                total_rating_count = ?,
                {summary_expr},
                {cover_expr},
                {shots_expr},
                igdb_matched_at = CURRENT_TIMESTAMP,
                nsfw = ?,
                genres = ?,
                steam_app_id = COALESCE(?, steam_app_id),
<<<<<<< HEAD
                igdb_release_date = ?,
                igdb_debug_info = ?
=======
                igdb_release_date = ?
>>>>>>> a3f69c5 (feat(sync): parallelize IGDB metadata synchronization)
                WHERE id = ?"""
        try:
            cursor.execute( query, params)
            conn.commit()
        except Exception as db_err:
            print(f"Database Error: {db_err}")
            print(f"Param count: {len(params)}")
            conn.rollback()
            raise

def calculate_match_score(client, game_name, igdb_result):
    if not igdb_result or not game_name:
        return 0

    igdb_name = client._clean_game_name(igdb_result.get("name", ""))
    our_name = client._clean_game_name(game_name)
    
    our_tokens = set(our_name.split())
    igdb_tokens = set(igdb_name.split())
    
    if not our_tokens: return 0

    intersection = our_tokens.intersection(igdb_tokens)
    containment = len(intersection) / len(our_tokens)
    
    # Strictly reject if any part of our name is missing from IGDB result
    if containment < 1.0:
        return 0

    if len(our_tokens) == 1 or len(our_name) < 10:
        if not igdb_name.startswith(our_name):
            return 0

    union = our_tokens.union(igdb_tokens)
    jaccard = len(intersection) / len(union)
    
    game_type = igdb_result.get("game_type", 0)
    
    if our_name == igdb_name:
        # Perfect match is always 100
        score = 100.0
    elif game_type == 0:
        # It's the main game but with a subtitle (e.g., Warsaw -> Warsaw Rising)
        # Score ranges from 70-95 based on how much "extra" text is in the title
        score = 70.0 + (jaccard * 25.0)
    elif game_type in [3, 8, 9, 10, 11, 12]:
        # It's a Bundle/Remake/Remaster (Acceptable fallback)
        # Score ranges from 40-65
        score = 40.0 + (jaccard * 25.0)
    else:
        # DLCs/Episodes/Etc.
        score = jaccard * 30.0

    return min(score, 100.0)

def _sync_steam_batch(client, steam_games_dict):
    conn = get_db()
    matched = 0
    searched = 0
    fallback = []
    try:
        appids = list(steam_games_dict.keys())
        total_batches = (len(appids) + _BATCH - 1) // _BATCH
        for start in range(0, len(appids), _BATCH):
            chunk = appids[start:start + _BATCH]
            games_matched = client.get_games_by_steam_ids(chunk)
            for appid in chunk:
                gid, name, store, existing_genres, rd, sid, _ = steam_games_dict[appid]
                game_data = games_matched.get(str(appid))
                if game_data:
                    apply_igdb_data(conn, gid, game_data, existing_genres=existing_genres)
                    matched += 1
                else:
                    fallback.append((gid, name, store, existing_genres, rd, sid, None))
                searched += 1
            time.sleep(0.1)
    finally:
        conn.close()
    return matched, searched, fallback

def _sync_gog_batch(client, gog_data_dict):
    conn = get_db()
    matched = 0
    searched = 0
    fallback = []
    try:
        all_sids = list(gog_data_dict.keys())
        all_slugs = [gog_data_dict[s]["slug"] for s in all_sids]
        for start in range(0, len(all_sids), _BATCH):
            batch_sids = all_sids[start : start + _BATCH]
            batch_slugs = all_slugs[start : start + _BATCH]
            results = client.get_games_by_gog_ids(batch_sids, batch_slugs)
            for sid in batch_sids:
                local_game = gog_data_dict[sid]
                game_data = results.get(sid)
                if game_data:
                    apply_igdb_data(conn, local_game["gid"], game_data, existing_genres=local_game["genres"])
                    matched += 1
                else:
                    fallback.append((local_game["gid"], local_game["name"], "gog", local_game["genres"], local_game["rd"], sid, None))
                searched += 1
            time.sleep(0.1)
    finally:
        conn.close()
    return matched, searched, fallback

def _sync_amazon_batch(client, amazon_data_dict):
    conn = get_db()
    matched = 0
    searched = 0
    fallback = []
    try:
        valid_sids = [s for s in amazon_data_dict.keys() if amazon_data_dict[s]["steam_appid"]]
        # Non-steam amazon games go straight to fallback
        for sid in amazon_data_dict:
            if not amazon_data_dict[sid]["steam_appid"]:
                local_game = amazon_data_dict[sid]
                fallback.append((local_game["gid"], local_game["name"], "amazon", local_game["genres"], local_game["rd"], sid, None))
                searched += 1

        all_steam_appids = [amazon_data_dict[s]["steam_appid"] for s in valid_sids]
        for start in range(0, len(valid_sids), _BATCH):
            batch_sids = valid_sids[start : start + _BATCH]
            batch_steam_appids = all_steam_appids[start : start + _BATCH]
            results = client.get_games_by_steam_ids(batch_steam_appids)
            for sid in batch_sids:
                local_game = amazon_data_dict[sid]
                game_data = results.get(str(local_game["steam_appid"]))
                if game_data:
                    apply_igdb_data(conn, local_game["gid"], game_data, existing_genres=local_game["genres"])
                    matched += 1
                else:
                    fallback.append((local_game["gid"], local_game["name"], "amazon", local_game["genres"], local_game["rd"], sid, None))
                searched += 1
            time.sleep(0.1)
    finally:
        conn.close()
    return matched, searched, fallback

def _sync_epic_batch(client, epic_data_dict):
    conn = get_db()
    matched = 0
    searched = 0
    fallback = []
    try:
        all_sids = list(epic_data_dict.keys())
        all_slugs = [epic_data_dict[s]["slug"] for s in all_sids]
        for start in range(0, len(all_sids), _BATCH):
            batch_sids = all_sids[start : start + _BATCH]
            batch_slugs = all_slugs[start : start + _BATCH]
            results = client.get_games_by_epic_ids(batch_sids, batch_slugs)
            for sid in batch_sids:
                local_game = epic_data_dict[sid]
                game_data = results.get(sid)
                if game_data:
                    apply_igdb_data(conn, local_game["gid"], game_data, existing_genres=local_game["genres"])
                    matched += 1
                else:
                    fallback.append((local_game["gid"], local_game["name"], "epic", local_game["genres"], local_game["rd"], sid, None))
                searched += 1
            time.sleep(0.1)
    finally:
        conn.close()
    return matched, searched, fallback

def chunk_dict(data, size):
    it = iter(data)
    for i in range(0, len(data), size):
        yield {k: data[k] for k in islice(it, size)}

def sync_games(conn, client, limit=None, force=False, progress_callback=None):
    """Sync games with IGDB using parallel store lookups."""
    cursor = conn.cursor()

    if force:
        cursor.execute(
            "SELECT id, name, store, genres, release_date, store_id, extra_data, steam_app_id FROM games WHERE name IS NOT NULL ORDER BY name"
        )
    else:
        cursor.execute(
            """SELECT id, name, store, genres, release_date, store_id, extra_data, steam_app_id FROM games
               WHERE name IS NOT NULL AND igdb_id IS NULL
               ORDER BY name"""
        )

    games = cursor.fetchall()
    if limit:
        games = games[:limit]

    total = len(games)
    if total == 0:
        return 0, 0
        
    print(f"Processing {total} games...")

    matched = 0
    searched = 0
    fallback_pool = []

    # Prepare store-specific data dictionaries
    steam_dict = {}
    gog_dict = {}
    amazon_dict = {}
    epic_dict = {}
    other_games = []

    for gid, name, store, genres, rd, sid, extra_raw, appid in games:
<<<<<<< HEAD
        if store == "steam" or appid:
=======
        if store == "steam":
>>>>>>> a3f69c5 (feat(sync): parallelize IGDB metadata synchronization)
            steam_dict[str(sid if store == "steam" else appid)] = (gid, name, store, genres, rd, sid, extra_raw)
        elif store == "gog":
            slug = None
            if extra_raw:
                try:
                    m = re.search(r"/game/([^/?#]+)", json.loads(extra_raw).get("store_url", ""))
                    if m: slug = m.group(1)
                except: pass
            gog_dict[str(sid)] = {"gid": gid, "name": name, "slug": slug, "genres": genres, "rd": rd}
        elif store == "amazon":
            steam_appid = None
            if extra_raw:
                try:
                    data = json.loads(extra_raw)
                    url = data.get("product", {}).get("productDetail", {}).get("details", {}).get("websites", {}).get("STEAM")
                    if url:
                        m = re.search(r"/app/(\d+)", url)
                        if m: steam_appid = m.group(1)
                except: pass
            amazon_dict[str(sid)] = {"gid": gid, "name": name, "steam_appid": steam_appid, "genres": genres, "rd": rd}
        elif store == "epic":
            slug = None
            if extra_raw:
                try: slug = json.loads(extra_raw).get("product_slug")
                except: pass
            epic_dict[str(sid)] = {"gid": gid, "name": name, "slug": slug, "genres": genres, "rd": rd}
        else:
            other_games.append((gid, name, store, genres, rd, sid, extra_raw))

    # Dispatch parallel tasks
    tasks = []
    with ThreadPoolExecutor(max_workers=4) as executor:
        # Create chunks for each store
        for chunk in chunk_dict(steam_dict, _BATCH):
            tasks.append(executor.submit(_sync_steam_batch, client, chunk))
        for chunk in chunk_dict(epic_dict, _BATCH):
            tasks.append(executor.submit(_sync_epic_batch, client, chunk))
        for chunk in chunk_dict(gog_dict, _BATCH):
            tasks.append(executor.submit(_sync_gog_batch, client, chunk))
        for chunk in chunk_dict(amazon_dict, _BATCH):
            tasks.append(executor.submit(_sync_amazon_batch, client, chunk))

        for future in as_completed(tasks):
            m, s, fb = future.result()
            matched += m
            searched += s
            fallback_pool.extend(fb)
            if progress_callback:
                progress_callback(searched, total, f"Parallel batch sync in progress")

    # Add other_games to fallback pool
    fallback_pool.extend(other_games)

    print(f"Fallback pool: {fallback_pool}")
    # Slug fallback (batch)
    slug_map = {}
    no_slug_games = []
    for gid, name, store, existing_genres, rd, sid, _ in fallback_pool:
        candidate = IGDBClient.derive_igdb_slug(client._clean_game_name(name))
        if candidate:
            slug_map.setdefault(candidate, []).append((gid, name, existing_genres, rd))
        else:
            no_slug_games.append((gid, name, store, existing_genres, rd, sid, None))

    if slug_map:
        print(f"Batch slug lookup for {len(slug_map)} unique slugs...")
        slug_to_game = client.batch_lookup_slugs(slug_map.keys())
        for candidate_slug, game_list in slug_map.items():
            game_data = slug_to_game.get(candidate_slug)
            for gid, name, existing_genres, rd in game_list:
                if game_data:
                    apply_igdb_data(conn, gid, game_data, existing_genres=existing_genres)
                    matched += 1
                else:
                    no_slug_games.append((gid, name, None, existing_genres, rd, None, None))

    # Sequential name search for remaining
    min_match_score = int(get_setting(IGDB_MATCH_THRESHOLD, "100"))
    seq_total = len(no_slug_games)
    failed = 0
    if seq_total:
        print(f"Name search for {seq_total} remaining unmatched games...")

    for i, (gid, name, store, existing_genres, rd, sid, _) in enumerate(no_slug_games):
        if progress_callback:
            progress_callback(total - seq_total + i + 1, total, f"Processing: {name[:50]}...")
            
        try:
            results = client.search_game(name)
            best_match = None
            best_score = 0
            if results:
                for result in results:
                    score = calculate_match_score(client, name, result)
                    if score > best_score:
                        best_score = score
                        best_match = result

            if best_match and best_score >= min_match_score:
                apply_igdb_data(conn, gid, best_match, existing_genres=existing_genres)
                matched += 1
                print(f"  [{i+1}/{seq_total}] matched: {name} (score: {best_score:.0f})")
            else:
                with db_lock:
                    cursor.execute("UPDATE games SET igdb_id = 0, igdb_matched_at = CURRENT_TIMESTAMP WHERE id = ?", (gid,))
                    conn.commit()
                failed += 1
                print(f"  [{i+1}/{seq_total}] no match: {name}")
        except Exception as e:
            print(f"Error searching {name}: {e}")
            failed += 1

    return matched, failed


def get_stats(conn):
    """Get IGDB sync statistics."""
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM games")
    total = cursor.fetchone()[0]

    # Count matched games (igdb_id > 0, not counting 0 which means "not found")
    cursor.execute("SELECT COUNT(*) FROM games WHERE igdb_id IS NOT NULL AND igdb_id > 0")
    matched = cursor.fetchone()[0]

    cursor.execute(
        "SELECT AVG(total_rating) FROM games WHERE total_rating IS NOT NULL"
    )
    avg_rating = cursor.fetchone()[0]

    cursor.execute(
        """SELECT name, total_rating FROM games
           WHERE total_rating IS NOT NULL
           ORDER BY total_rating DESC LIMIT 5"""
    )
    top_rated = cursor.fetchall()

    return {
        "total": total,
        "matched": matched,
        "match_rate": (matched / total * 100) if total > 0 else 0,
        "avg_rating": avg_rating,
        "top_rated": top_rated,
    }
