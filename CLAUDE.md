# Bolt Action World Rankings

Flask web app aggregating individual player performance across multiple Bolt Action team tournaments (WTC, WoW, etc.).

## Stack
- **Backend**: Python/Flask, Jinja2 templates
- **Database**: SQLite (local dev) / PostgreSQL (production)
- **Deployment**: Gunicorn + Nginx + systemd on Hetzner VPS (same server as scoring.hala.dk)

## Key files
- `app.py` — all routes, DB helpers, and rating engine
- `importer_wtc.py` — WTC importer (fetches /api/export.json from scoring.hala.dk)
- `templates/` — Jinja2 HTML templates

## Local development
```
python -m venv venv
venv\Scripts\pip install -r requirements.txt
$env:SECRET_KEY="dev"; $env:ADMIN_PASSWORD="dev"
venv\Scripts\python app.py
```
Access at http://localhost:5001.

## Database helpers (same pattern as WTC scoring)
- `q(sql)` — converts `?` to `%s` for PostgreSQL
- `get_db()` — returns correct connection (SQLite or PostgreSQL)
- `get_cursor(conn)` — returns RealDictCursor for PostgreSQL, standard for SQLite
- `insert_returning_id(c, sql, params)` — handles RETURNING id vs lastrowid

## Rating models
Three models behind a page toggle on `/world`:
- **Iterative SoS** (default) — propagates strength across cross-tournament player pools; recommended
- **One-deep SoS** — current WTC formula, simplest
- **Sequential Elo** — chronological, K=32

Only events with `match_data_available=1` count toward ratings.

## Data flow
- WTC results: admin button fetches `https://scoring.hala.dk/api/export.json` → upserts via `importer_wtc.py`
- WoW and future sources: add a new `importer_<source>.py` following the same pattern
- Cross-tournament identity: `/admin/unmatched` screen links source_competitors to global competitors

## Environment variables
- `DATABASE_URL` — PostgreSQL connection string (if unset, uses SQLite rankings.db)
- `ADMIN_PASSWORD` — admin login password
- `SECRET_KEY` — Flask session secret
