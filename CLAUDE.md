# Bolt Action World Rankings

Flask web app aggregating individual player performance across multiple Bolt Action team tournaments (WTC, WoW, etc.).

## Stack
- **Backend**: Python/Flask, Jinja2 templates
- **Database**: SQLite (local dev) / PostgreSQL (production)
- **Deployment**: Gunicorn + Nginx + systemd on Hetzner VPS (same server as scoring.hala.dk)

## Key files
- `app.py` — routes, auth and CSRF
- `db.py` — DB helpers, schema/migrations (`init_db`), identity helper `relink_source_competitor`
- `ratings.py` — rating engine (pure functions, no DB access)
- `importer_wtc.py` — WTC importer (fetches /api/export.json from scoring.hala.dk)
- `importer_wow.py` — WoW importer (reads Excel files in `Data/`; years configured in `WOW_EDITIONS`)
- `scripts/` — one-off maintenance scripts (e.g. `apply_matches.py`, the WoW→WTC auto-linker)
- `templates/` — Jinja2 HTML templates (shared CSS classes live in `base.html`)
- `tests/` — unit tests: `venv\Scripts\python -m unittest discover -s tests`

## Local development
```
python -m venv venv
venv\Scripts\pip install -r requirements.txt
$env:SECRET_KEY="dev"; $env:ADMIN_PASSWORD="dev"
venv\Scripts\python app.py
```
Access at http://localhost:5001.

## Database helpers (same pattern as WTC scoring, in `db.py`)
- `q(sql)` — converts `?` to `%s` for PostgreSQL
- `get_db()` — returns correct connection (SQLite or PostgreSQL)
- `get_cursor(conn)` — returns RealDictCursor for PostgreSQL, standard for SQLite
- `insert_returning_id(c, sql, params)` — handles RETURNING id vs lastrowid
- Schema changes to existing tables go through `_safe_migrate('ALTER TABLE ...')` in `init_db()`
- All POST forms must include `<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">`

## Rating models
Three models behind a page toggle on `/world`:
- **Iterative SoS** (default) — propagates strength across cross-tournament player pools; recommended
- **One-deep SoS** — current WTC formula, simplest
- **Sequential Elo** — chronological, K=32

Only events with `match_data_available=1` count toward ratings.

## Data flow
- WTC results: admin button fetches `https://scoring.hala.dk/api/export.json` → upserts via `importer_wtc.py`
- WoW and future sources: add a new `importer_<source>.py` following the same pattern
- Importers replace an edition's games on every re-import (no stale games after corrections)
- Each `game` records which `source_competitor` played it (`sc_a`/`sc_b`); `competitor_a`/`b` are a cache
  updated by `relink_source_competitor`. Always relink through that helper, never by updating ids directly.
- Cross-tournament identity: `/admin/wow_players` links WoW names to WTC competitors;
  `/admin/unmatched` handles source_competitors with no competitor at all
- WoW names that appear twice in one round are split by team ("Matt (England)")

## Environment variables
- `DATABASE_URL` — PostgreSQL connection string (if unset, uses SQLite rankings.db)
- `ADMIN_PASSWORD` — admin login password
- `SECRET_KEY` — Flask session secret
- `SQLITE_PATH` — optional; SQLite file path (default: `rankings.db` next to `db.py`)
