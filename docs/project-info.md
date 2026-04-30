# Project Information

## Tech Stack

| Layer | Technology |
|-------|-----------|
| **Backend** | FastAPI (Python) |
| **Database** | SQLite |
| **Frontend** | Jinja2 templates, vanilla JavaScript |
| **Metadata** | IGDB API integration, Metacritic, ProtonDB |
| **Deployment** | Docker + Docker Compose |

## Library Behavior Notes

- Search is escaped before SQL `LIKE` matching, so special characters (including `%`) are treated safely and predictably.
- Library filters and sorting are URL-driven (`/library?...`), which makes filtered views shareable and bookmarkable.
- Library filtering includes advanced toggles for ProtonDB tier, missing Steam AppID, missing IGDB data, streaming-only exclusion, and collection include/exclude states.
- Release date sorting supports IGDB release date fields, and the game detail page can display release dates derived from IGDB epoch timestamps in ISO format (`YYYY-MM-DD`).
- Game detail settings provide per-game metadata tools (manual Steam/IGDB sync, cover overrides via SteamGridDB, and metadata edits).
