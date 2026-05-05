# database_builder.py
# Combines Steam, Epic, GOG, and itch.io libraries into a central SQLite database

import sqlite3
import json
from datetime import datetime

from ..config import DATABASE_PATH
from ..utils.ratings import calculate_effective_average_rating, steam_total_reviews_from_extra


def create_database():
    """Create the SQLite database with the games table."""
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS games (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            store TEXT NOT NULL,
            store_id TEXT,

            -- Metadata
            description TEXT,
            developers TEXT,  -- JSON array
            publishers TEXT,  -- JSON array
            genres TEXT,      -- JSON array

            -- Images
            cover_image TEXT,
            background_image TEXT,
            icon TEXT,

            -- Platform info
            supported_platforms TEXT,  -- JSON array

            -- Dates
            release_date TEXT,
            created_date TEXT,
            last_modified TEXT,

            -- Stats
            playtime_hours REAL,
            playtime_label TEXT,
            critics_score REAL,
            average_rating REAL,  -- Computed average across all available ratings

            -- IGDB data
            igdb_id INTEGER,
            igdb_slug TEXT,
            igdb_rating REAL,
            igdb_rating_count INTEGER,
            aggregated_rating REAL,
            aggregated_rating_count INTEGER,
            total_rating REAL,
            total_rating_count INTEGER,
            igdb_matched_at TIMESTAMP,
            igdb_release_date INTEGER,

            -- Metacritic data
            metacritic_score INTEGER,
            metacritic_user_score REAL,
            metacritic_url TEXT,
            metacritic_slug TEXT,
            metacritic_matched_at TIMESTAMP,

            -- ProtonDB data
            protondb_tier TEXT,
            protondb_score REAL,
            protondb_confidence TEXT,
            protondb_total INTEGER,
            protondb_trending_tier TEXT,
            protondb_matched_at TIMESTAMP,

            -- Additional metadata
            summary TEXT,
            cover_url TEXT,
            cover_url_override TEXT,
            screenshots TEXT,  -- JSON array
            steam_app_id TEXT,
            nsfw BOOLEAN DEFAULT 0,
            hidden BOOLEAN DEFAULT 0,
            genres_override TEXT,  -- JSON array

            -- Additional data
            can_run_offline BOOLEAN,
            dlcs TEXT,  -- JSON array
            extra_data TEXT,  -- JSON for store-specific data

            -- Tracking
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            steam_synced_at TIMESTAMP,

            -- Soft-delete for games removed from store
            removed BOOLEAN DEFAULT 0,

            UNIQUE(store, store_id)
        )
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_games_store ON games(store)
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_games_name ON games(name)
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_games_steam_app_id ON games(steam_app_id)
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_games_igdb_id ON games(igdb_id)
    """)

    # Settings table for storing user configuration
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Collections table for user-created game collections
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS collections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Junction table for games in collections
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS collection_games (
            collection_id INTEGER NOT NULL,
            game_id INTEGER NOT NULL,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (collection_id, game_id),
            FOREIGN KEY (collection_id) REFERENCES collections(id) ON DELETE CASCADE,
            FOREIGN KEY (game_id) REFERENCES games(id) ON DELETE CASCADE
        )
    """)

    conn.commit()
    return conn


def mark_removed_games(conn, store_name, seen_store_ids):
    """Mark games as removed if they were not seen during this sync.

    Also restores previously-removed games that reappeared.
    Returns (newly_removed_count, restored_count).
    """
    cursor = conn.cursor()

    if not seen_store_ids:
        return (0, 0)

    placeholders = ",".join("?" * len(seen_store_ids))
    seen_list = list(seen_store_ids)

    # Mark games NOT in the seen set as removed
    cursor.execute(
        f"UPDATE games SET removed = 1 WHERE store = ? AND store_id NOT IN ({placeholders}) AND (removed IS NULL OR removed = 0)",
        [store_name] + seen_list
    )
    newly_removed = cursor.rowcount

    # Restore games that reappeared
    cursor.execute(
        f"UPDATE games SET removed = 0 WHERE store = ? AND store_id IN ({placeholders}) AND removed = 1",
        [store_name] + seen_list
    )
    restored = cursor.rowcount

    conn.commit()
    return (newly_removed, restored)


def import_steam_games(conn):
    """Import games from Steam."""
    print("Importing Steam library...")
    cursor = conn.cursor()

    try:
        from ..sources.steam import get_steam_library

        games = get_steam_library()
        if not games:
            print("  No Steam games found or not authenticated")
            return 0

        count = 0
        seen_store_ids = set()
        for game in games:
            try:
                # Build cover image URL from appid
                appid = game.get("appid")
                store_id = str(appid) if appid else None
                cover_image = f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/library_600x900_2x.jpg" if appid else None
                background_image = f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/library_hero.jpg" if appid else None

                cursor.execute("""
                    INSERT INTO games (
                        name, store, store_id, steam_app_id, cover_image, background_image, icon,
                        playtime_hours, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(store, store_id) DO UPDATE SET
                        name = excluded.name,
                        steam_app_id = excluded.steam_app_id,
                        cover_image = excluded.cover_image,
                        background_image = excluded.background_image,
                        icon = excluded.icon,
                        playtime_hours = excluded.playtime_hours,
                        updated_at = excluded.updated_at
                """, (
                    game.get("name"),
                    "steam",
                    store_id,
                    store_id,
                    cover_image,
                    background_image,
                    game.get("icon_url"),
                    game.get("playtime_hours"),
                    datetime.now().isoformat()
                ))
                if store_id:
                    seen_store_ids.add(store_id)
                count += 1
            except Exception as e:
                print(f"  Error importing {game.get('name')}: {e}")

        conn.commit()
        removed, restored = mark_removed_games(conn, "steam", seen_store_ids)
        if removed:
            print(f"  Marked {removed} Steam games as removed")
        if restored:
            print(f"  Restored {restored} previously removed Steam games")
        print(f"  Imported {count} Steam games")
        return count
    except Exception as e:
        print(f"  Steam import error: {e}")
        return 0

def normalize_uid_variants(uid):
    if len(uid) == 32:
        return f"{uid[0:8]}-{uid[8:12]}-{uid[12:16]}-{uid[16:20]}-{uid[20:]}"
    return uid

def import_epic_games(conn):
    """Import games from Epic Games Store."""
    print("Importing Epic library...")
    cursor = conn.cursor()

    try:
        from ..sources.epic import get_epic_library_legendary

        games = get_epic_library_legendary()
        if not games:
            print("  No Epic games found or not authenticated")
            return 0

        count = 0
        seen_store_ids = set()
        for game in games:
            try:
                store_id = normalize_uid_variants(game.get("sku"))
                cursor.execute("""
                    INSERT INTO games (
                        name, store, store_id, description, developers,
                        supported_platforms, cover_image, release_date,
                        created_date, last_modified, can_run_offline,
                        dlcs, extra_data, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(store, store_id) DO UPDATE SET
                        name = excluded.name,
                        description = excluded.description,
                        developers = excluded.developers,
                        supported_platforms = excluded.supported_platforms,
                        cover_image = excluded.cover_image,
                        release_date = excluded.release_date,
                        created_date = excluded.created_date,
                        last_modified = excluded.last_modified,
                        can_run_offline = excluded.can_run_offline,
                        dlcs = excluded.dlcs,
                        extra_data = excluded.extra_data,
                        updated_at = excluded.updated_at
                """, (
                    game.get("name"),
                    "epic",
                    store_id,
                    game.get("description"),
                    json.dumps([game.get("developer")]) if game.get("developer") else None,
                    json.dumps(game.get("supported_platforms", [])),
                    game.get("cover_image"),
                    game.get("created_date"),
                    game.get("created_date"),
                    game.get("last_modified"),
                    game.get("can_run_offline"),
                    json.dumps(game.get("dlcs", [])),
                    json.dumps(game),
                    datetime.now().isoformat()
                ))
                if store_id:
                    seen_store_ids.add(store_id)
                count += 1
            except Exception as e:
                print(f"  Error importing {game.get('name')}: {e}")

        conn.commit()
        removed, restored = mark_removed_games(conn, "epic", seen_store_ids)
        if removed:
            print(f"  Marked {removed} Epic games as removed")
        if restored:
            print(f"  Restored {restored} previously removed Epic games")
        print(f"  Imported {count} Epic games")
        return count
    except Exception as e:
        print(f"  Epic import error: {e}")
        return 0


def import_gog_games(conn):
    """Import games from GOG Galaxy."""
    print("Importing GOG library...")
    cursor = conn.cursor()

    try:
        from ..sources.gog import get_gog_library

        games = get_gog_library()
        if not games:
            print("  No GOG games found or database not accessible")
            return 0

        count = 0
        seen_store_ids = set()
        for game in games:
            try:
                # Convert Unix timestamp to ISO date if present
                release_date = None
                if game.get("release_date"):
                    try:
                        release_date = datetime.fromtimestamp(
                            game["release_date"]
                        ).isoformat()
                    except (ValueError, TypeError):
                        pass

                # Combine genres and themes, de-duplicate (case-insensitive)
                genres = game.get("genres", [])
                themes = game.get("themes", [])
                seen = set()
                combined_tags = []
                for tag in genres + themes:
                    if tag and tag.lower() not in seen:
                        seen.add(tag.lower())
                        combined_tags.append(tag)

                store_id = game.get("product_id")
                cursor.execute("""
                    INSERT INTO games (
                        name, store, store_id, description, developers,
                        publishers, genres, cover_image, background_image,
                        icon, release_date, critics_score, extra_data, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(store, store_id) DO UPDATE SET
                        name = excluded.name,
                        description = excluded.description,
                        developers = excluded.developers,
                        publishers = excluded.publishers,
                        cover_image = excluded.cover_image,
                        background_image = excluded.background_image,
                        icon = excluded.icon,
                        release_date = excluded.release_date,
                        critics_score = excluded.critics_score,
                        extra_data = excluded.extra_data,
                        updated_at = excluded.updated_at
                """, (
                    game.get("name"),
                    "gog",
                    store_id,
                    game.get("summary"),
                    json.dumps(game.get("developers", [])),
                    json.dumps(game.get("publishers", [])),
                    json.dumps(combined_tags),
                    game.get("cover_image"),
                    game.get("background_image"),
                    game.get("icon"),
                    release_date,
                    game.get("critics_score"),
                    json.dumps(game),
                    datetime.now().isoformat()
                ))
                if store_id:
                    seen_store_ids.add(str(store_id))
                count += 1
            except Exception as e:
                print(f"  Error importing {game.get('name')}: {e}")

        conn.commit()
        removed, restored = mark_removed_games(conn, "gog", seen_store_ids)
        if removed:
            print(f"  Marked {removed} GOG games as removed")
        if restored:
            print(f"  Restored {restored} previously removed GOG games")
        print(f"  Imported {count} GOG games")
        return count
    except Exception as e:
        print(f"  GOG import error: {e}")
        return 0


def import_itch_games(conn):
    """Import games from itch.io (requires prior OAuth setup)."""
    print("Importing itch.io library...")
    cursor = conn.cursor()

    try:
        from ..sources.itch import get_auth_token, get_owned_games

        token = get_auth_token()
        if not token:
            print("  itch.io not configured or not authenticated")
            print("  Set your itch.io API key in the Settings page")
            print("  (get key at: https://itch.io/user/settings/api-keys)")
            return 0

        games = get_owned_games(token)
        if not games:
            print("  No itch.io games found")
            return 0

        count = 0
        seen_store_ids = set()
        for game in games:
            try:
                # Build platforms list
                platforms = []
                if game.get("platforms", {}).get("windows"):
                    platforms.append("Windows")
                if game.get("platforms", {}).get("mac"):
                    platforms.append("Mac")
                if game.get("platforms", {}).get("linux"):
                    platforms.append("Linux")
                if game.get("platforms", {}).get("android"):
                    platforms.append("Android")

                store_id = str(game.get("id"))
                cursor.execute("""
                    INSERT INTO games (
                        name, store, store_id, description, cover_image,
                        supported_platforms, release_date, extra_data, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(store, store_id) DO UPDATE SET
                        name = excluded.name,
                        description = excluded.description,
                        cover_image = excluded.cover_image,
                        supported_platforms = excluded.supported_platforms,
                        release_date = excluded.release_date,
                        extra_data = excluded.extra_data,
                        updated_at = excluded.updated_at
                """, (
                    game.get("title"),
                    "itch",
                    store_id,
                    game.get("short_text"),
                    game.get("cover_url"),
                    json.dumps(platforms) if platforms else None,
                    game.get("published_at"),
                    json.dumps(game),
                    datetime.now().isoformat()
                ))
                if store_id:
                    seen_store_ids.add(store_id)
                count += 1
            except Exception as e:
                print(f"  Error importing {game.get('title')}: {e}")

        conn.commit()
        removed, restored = mark_removed_games(conn, "itch", seen_store_ids)
        if removed:
            print(f"  Marked {removed} itch.io games as removed")
        if restored:
            print(f"  Restored {restored} previously removed itch.io games")
        print(f"  Imported {count} itch.io games")
        return count
    except ImportError:
        print("  itch.io module not available")
        return 0
    except Exception as e:
        print(f"  itch.io import error: {e}")
        return 0


def import_humble_games(conn):
    """Import games from Humble Bundle (requires session cookie)."""
    print("Importing Humble Bundle library...")
    cursor = conn.cursor()

    try:
        from ..sources.humble import get_humble_library

        games = get_humble_library()
        if not games:
            print("  No Humble Bundle games found or not authenticated")
            print("  Set your Humble Bundle session cookie in Settings")
            return 0

        count = 0
        seen_store_ids = set()
        for game in games:
            try:
                store_id = game.get("machine_name")
                cursor.execute("""
                    INSERT INTO games (
                        name, store, store_id, cover_image, icon,
                        supported_platforms, publishers, release_date,
                        extra_data, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(store, store_id) DO UPDATE SET
                        name = excluded.name,
                        cover_image = excluded.cover_image,
                        icon = excluded.icon,
                        supported_platforms = excluded.supported_platforms,
                        publishers = excluded.publishers,
                        release_date = excluded.release_date,
                        extra_data = excluded.extra_data,
                        updated_at = excluded.updated_at
                """, (
                    game.get("human_name"),
                    "humble",
                    store_id,
                    game.get("icon"),
                    game.get("icon"),
                    json.dumps(game.get("platforms", [])),
                    json.dumps([game.get("payee")]) if game.get("payee") else None,
                    game.get("created"),
                    json.dumps(game),
                    datetime.now().isoformat()
                ))
                if store_id:
                    seen_store_ids.add(store_id)
                count += 1
            except Exception as e:
                print(f"  Error importing {game.get('human_name')}: {e}")

        conn.commit()
        removed, restored = mark_removed_games(conn, "humble", seen_store_ids)
        if removed:
            print(f"  Marked {removed} Humble Bundle games as removed")
        if restored:
            print(f"  Restored {restored} previously removed Humble Bundle games")
        print(f"  Imported {count} Humble Bundle games")
        return count
    except ImportError:
        print("  Humble Bundle module not available")
        return 0
    except Exception as e:
        print(f"  Humble Bundle import error: {e}")
        return 0


def import_battlenet_games(conn):
    """Import games from Battle.net (requires session cookie)."""
    print("Importing Battle.net library...")
    cursor = conn.cursor()

    try:
        from ..sources.battlenet import get_battlenet_library

        games = get_battlenet_library()
        if not games:
            print("  No Battle.net games found or not authenticated")
            print("  Set your Battle.net session cookie in Settings")
            return 0

        count = 0
        seen_store_ids = set()
        for game in games:
            try:
                store_id = game.get("title_id")
                cursor.execute("""
                    INSERT INTO games (
                        name, store, store_id, cover_image,
                        extra_data, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(store, store_id) DO UPDATE SET
                        name = excluded.name,
                        cover_image = excluded.cover_image,
                        extra_data = excluded.extra_data,
                        updated_at = excluded.updated_at
                """, (
                    game.get("name"),
                    "battlenet",
                    store_id,
                    game.get("cover_image"),
                    json.dumps(game.get("raw_data", {})),
                    datetime.now().isoformat()
                ))
                if store_id:
                    seen_store_ids.add(str(store_id))
                count += 1
            except Exception as e:
                print(f"  Error importing {game.get('name')}: {e}")

        conn.commit()
        removed, restored = mark_removed_games(conn, "battlenet", seen_store_ids)
        if removed:
            print(f"  Marked {removed} Battle.net games as removed")
        if restored:
            print(f"  Restored {restored} previously removed Battle.net games")
        print(f"  Imported {count} Battle.net games")
        return count
    except ImportError:
        print("  Battle.net module not available")
        return 0
    except Exception as e:
        print(f"  Battle.net import error: {e}")
        return 0


def import_ea_games(conn):
    """Import games from EA (requires session cookies)."""
    print("Importing EA library...")
    cursor = conn.cursor()

    try:
        from ..sources.ea import get_ea_library

        games = get_ea_library()
        if not games:
            print("  No EA games found or not authenticated")
            print("  Get a new EA bearer token using the script in Settings")
            return 0

        count = 0
        seen_store_ids = set()
        for game in games:
            try:
                # Build developers/publishers JSON arrays
                developers = [game.get("developer")] if game.get("developer") else None
                publishers = [game.get("publisher")] if game.get("publisher") else None

                store_id = game.get("offer_id")
                cursor.execute("""
                    INSERT INTO games (
                        name, store, store_id, cover_image,
                        developers, publishers, release_date,
                        extra_data, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(store, store_id) DO UPDATE SET
                        name = excluded.name,
                        cover_image = excluded.cover_image,
                        developers = excluded.developers,
                        publishers = excluded.publishers,
                        release_date = excluded.release_date,
                        extra_data = excluded.extra_data,
                        updated_at = excluded.updated_at
                """, (
                    game.get("name"),
                    "ea",
                    store_id,
                    game.get("cover_image"),
                    json.dumps(developers) if developers else None,
                    json.dumps(publishers) if publishers else None,
                    game.get("release_date"),
                    json.dumps(game.get("raw_data", {})),
                    datetime.now().isoformat()
                ))
                if store_id:
                    seen_store_ids.add(store_id)
                count += 1
            except Exception as e:
                print(f"  Error importing {game.get('name')}: {e}")

        conn.commit()
        removed, restored = mark_removed_games(conn, "ea", seen_store_ids)
        if removed:
            print(f"  Marked {removed} EA games as removed")
        if restored:
            print(f"  Restored {restored} previously removed EA games")
        print(f"  Imported {count} EA games")
        return count
    except ImportError:
        print("  EA module not available")
        return 0
    except Exception as e:
        print(f"  EA import error: {e}")
        return 0


def import_amazon_games(conn):
    """Import games from Amazon Games (local database or API token)."""
    print("Importing Amazon Games library...")
    cursor = conn.cursor()

    try:
        from ..sources.amazon import get_amazon_library

        games = get_amazon_library()
        if not games:
            print("  No Amazon games found or not configured")
            print("  Set up Amazon Games in Settings (local database or API token)")
            return 0

        count = 0
        seen_store_ids = set()
        for game in games:
            try:
                # Build developers/publishers JSON arrays
                developers = [game.get("developer")] if game.get("developer") else None
                publishers = [game.get("publisher")] if game.get("publisher") else None

                store_id = game.get("product_id")
                cursor.execute("""
                    INSERT INTO games (
                        name, store, store_id, cover_image, icon,
                        developers, publishers, extra_data, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(store, store_id) DO UPDATE SET
                        name = excluded.name,
                        cover_image = excluded.cover_image,
                        icon = excluded.icon,
                        developers = excluded.developers,
                        publishers = excluded.publishers,
                        extra_data = excluded.extra_data,
                        updated_at = excluded.updated_at
                """, (
                    game.get("name"),
                    "amazon",
                    store_id,
                    game.get("icon_url"),
                    game.get("icon_url"),
                    json.dumps(developers) if developers else None,
                    json.dumps(publishers) if publishers else None,
                    json.dumps(game.get("raw_data", {})),
                    datetime.now().isoformat()
                ))
                if store_id:
                    seen_store_ids.add(store_id)
                count += 1
            except Exception as e:
                print(f"  Error importing {game.get('name')}: {e}")

        conn.commit()
        removed, restored = mark_removed_games(conn, "amazon", seen_store_ids)
        if removed:
            print(f"  Marked {removed} Amazon games as removed")
        if restored:
            print(f"  Restored {restored} previously removed Amazon games")
        print(f"  Imported {count} Amazon games")
        return count
    except ImportError:
        print("  Amazon Games module not available")
        return 0
    except Exception as e:
        print(f"  Amazon Games import error: {e}")
        return 0


def import_xbox_games(conn):
    """Import games from Xbox (owned + Game Pass)."""
    print("Importing Xbox library...")
    cursor = conn.cursor()

    try:
        from ..sources.xbox import get_xbox_library

        games = get_xbox_library()
        if not games:
            print("  No Xbox games found or not configured")
            print("  Add your XSTS token in Settings to import owned games")
            print("  Select Game Pass plan in Settings to import catalog")
            return 0

        count = 0
        seen_store_ids = set()
        for game in games:
            try:
                # Build developers/publishers JSON arrays
                developers = [game.get("developer")] if game.get("developer") else None
                publishers = [game.get("publisher")] if game.get("publisher") else None

                # Store streaming flag and other Xbox-specific data in extra_data
                extra_data = {
                    "is_streaming": game.get("is_streaming", False),
                    "acquisition_type": game.get("acquisition_type"),
                    "title_id": game.get("title_id"),
                    "pfn": game.get("pfn"),
                }

                store_id = game.get("store_id")
                cursor.execute("""
                    INSERT INTO games (
                        name, store, store_id, cover_image,
                        developers, publishers, release_date,
                        extra_data, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(store, store_id) DO UPDATE SET
                        name = excluded.name,
                        cover_image = excluded.cover_image,
                        developers = excluded.developers,
                        publishers = excluded.publishers,
                        release_date = excluded.release_date,
                        extra_data = excluded.extra_data,
                        updated_at = excluded.updated_at
                """, (
                    game.get("name"),
                    "xbox",
                    store_id,
                    game.get("cover_image"),
                    json.dumps(developers) if developers else None,
                    json.dumps(publishers) if publishers else None,
                    game.get("release_date"),
                    json.dumps(extra_data),
                    datetime.now().isoformat()
                ))
                if store_id:
                    seen_store_ids.add(store_id)
                count += 1
            except Exception as e:
                print(f"  Error importing {game.get('name')}: {e}")

        conn.commit()
        removed, restored = mark_removed_games(conn, "xbox", seen_store_ids)
        if removed:
            print(f"  Marked {removed} Xbox games as removed")
        if restored:
            print(f"  Restored {restored} previously removed Xbox games")
        print(f"  Imported {count} Xbox games")
        return count
    except ImportError:
        print("  Xbox module not available")
        return 0
    except Exception as e:
        print(f"  Xbox import error: {e}")
        return 0


def import_local_games(conn):
    """Import games from local folders."""
    print("Importing local games...")
    cursor = conn.cursor()

    try:
        from ..sources.local import get_local_library

        games = get_local_library()
        if not games:
            print("  No local games found or not configured")
            print("  Set LOCAL_GAMES_PATHS in Settings (comma-separated folder paths)")
            return 0

        count = 0
        seen_store_ids = set()
        for game in games:
            try:
                # Build developers/genres JSON arrays if provided
                developers = game.get("developers")
                genres = game.get("genres")

                # Store folder path and any override data in extra_data
                extra_data = {
                    "folder_path": game.get("folder_path"),
                }
                if game.get("igdb_id"):
                    extra_data["manual_igdb_id"] = game.get("igdb_id")

                store_id = game.get("store_id")
                cursor.execute("""
                    INSERT INTO games (
                        name, store, store_id, description, cover_image,
                        developers, genres, release_date, extra_data, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(store, store_id) DO UPDATE SET
                        name = excluded.name,
                        description = excluded.description,
                        cover_image = excluded.cover_image,
                        developers = excluded.developers,
                        genres = excluded.genres,
                        release_date = excluded.release_date,
                        extra_data = excluded.extra_data,
                        updated_at = excluded.updated_at
                """, (
                    game.get("name"),
                    "local",
                    store_id,
                    game.get("description"),
                    game.get("cover_image"),
                    json.dumps(developers) if developers else None,
                    json.dumps(genres) if genres else None,
                    game.get("release_date"),
                    json.dumps(extra_data),
                    datetime.now().isoformat()
                ))
                if store_id:
                    seen_store_ids.add(store_id)
                count += 1
            except Exception as e:
                print(f"  Error importing {game.get('name')}: {e}")

        conn.commit()
        removed, restored = mark_removed_games(conn, "local", seen_store_ids)
        if removed:
            print(f"  Marked {removed} local games as removed")
        if restored:
            print(f"  Restored {restored} previously removed local games")
        print(f"  Imported {count} local games")
        return count
    except ImportError:
        print("  Local games module not available")
        return 0
    except Exception as e:
        print(f"  Local games import error: {e}")
        return 0


def migrate_database(conn):
    """Ensure all columns and indices exist in the database."""
    cursor = conn.cursor()
    
    # Check existing columns
    cursor.execute("PRAGMA table_info(games)")
    existing_columns = {row[1] for row in cursor.fetchall()}

    # List of all expected columns (name, type)
    expected_columns = [
        ("playtime_label", "TEXT"),
        ("average_rating", "REAL"),
        ("igdb_id", "INTEGER"),
        ("igdb_slug", "TEXT"),
        ("igdb_rating", "REAL"),
        ("igdb_rating_count", "INTEGER"),
        ("aggregated_rating", "REAL"),
        ("aggregated_rating_count", "INTEGER"),
        ("total_rating", "REAL"),
        ("total_rating_count", "INTEGER"),
        ("igdb_matched_at", "TIMESTAMP"),
        ("igdb_release_date", "INTEGER"),
        ("metacritic_score", "INTEGER"),
        ("metacritic_user_score", "REAL"),
        ("metacritic_url", "TEXT"),
        ("metacritic_slug", "TEXT"),
        ("metacritic_matched_at", "TIMESTAMP"),
        ("protondb_tier", "TEXT"),
        ("protondb_score", "REAL"),
        ("protondb_confidence", "TEXT"),
        ("protondb_total", "INTEGER"),
        ("protondb_trending_tier", "TEXT"),
        ("protondb_matched_at", "TIMESTAMP"),
        ("summary", "TEXT"),
        ("cover_url", "TEXT"),
        ("cover_url_override", "TEXT"),
        ("screenshots", "TEXT"),
        ("steam_app_id", "TEXT"),
        ("nsfw", "BOOLEAN DEFAULT 0"),
        ("hidden", "BOOLEAN DEFAULT 0"),
        ("genres_override", "TEXT"),
        ("steam_synced_at", "TIMESTAMP"),
        ("removed", "BOOLEAN DEFAULT 0"),
    ]

    for col_name, col_type in expected_columns:
        if col_name not in existing_columns:
            try:
                cursor.execute(f"ALTER TABLE games ADD COLUMN {col_name} {col_type}")
                print(f"Added column: {col_name}")
            except sqlite3.OperationalError as e:
                # Column might already exist but wasn't caught by PRAGMA for some reason
                print(f"Error adding column {col_name}: {e}")

    # Add indices
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_games_steam_app_id ON games(steam_app_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_games_igdb_id ON games(igdb_id)")
    
    conn.commit()


def calculate_average_rating(
    critics_score=None,
    igdb_rating=None,
    igdb_rating_count=None,
    aggregated_rating=None,
    aggregated_rating_count=None,
    total_rating=None,
    total_rating_count=None,
    metacritic_score=None,
    metacritic_user_score=None,
    steam_total_reviews=None,
):
    """
    Calculate the average rating across all available ratings.
    All ratings are normalized to 0-100 scale.
    Returns None if no ratings are available.
    """
    return calculate_effective_average_rating(
        critics_score=critics_score,
        steam_total_reviews=steam_total_reviews,
        igdb_rating=igdb_rating,
        igdb_rating_count=igdb_rating_count,
        aggregated_rating=aggregated_rating,
        aggregated_rating_count=aggregated_rating_count,
        total_rating=total_rating,
        total_rating_count=total_rating_count,
        metacritic_score=metacritic_score,
        metacritic_user_score=metacritic_user_score,
    )


def update_average_rating(conn, game_id):
    """
    Fetch all ratings for a game and update its average_rating.
    Call this after updating any rating field for a game.
    """
    cursor = conn.cursor()

    # First ensure the column exists
    migrate_database(conn)

    # Fetch all rating fields for this game
    cursor.execute(
        """SELECT critics_score, igdb_rating, igdb_rating_count,
                  aggregated_rating, aggregated_rating_count,
                  total_rating, total_rating_count,
                  metacritic_score, metacritic_user_score, extra_data
           FROM games WHERE id = ?""",
        (game_id,),
    )
    row = cursor.fetchone()
    if not row:
        return None

    avg = calculate_average_rating(
        critics_score=row[0],
        igdb_rating=row[1],
        igdb_rating_count=row[2],
        aggregated_rating=row[3],
        aggregated_rating_count=row[4],
        total_rating=row[5],
        total_rating_count=row[6],
        metacritic_score=row[7],
        metacritic_user_score=row[8],
        steam_total_reviews=steam_total_reviews_from_extra(row[9]),
    )

    cursor.execute(
        "UPDATE games SET average_rating = ? WHERE id = ?",
        (avg, game_id),
    )
    conn.commit()
    return avg


def get_stats(conn):
    """Get database statistics."""
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM games")
    total = cursor.fetchone()[0]

    cursor.execute("SELECT store, COUNT(*) FROM games GROUP BY store")
    by_store = dict(cursor.fetchall())

    return {"total": total, "by_store": by_store}
