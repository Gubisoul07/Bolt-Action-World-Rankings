"""DB helpers (mirrors WTC scoring q/get_db/get_cursor pattern), schema and
identity helpers shared by app.py, the importers and scripts.
"""
import logging
import os
import sqlite3

DATABASE_URL = os.environ.get('DATABASE_URL')
SQLITE_PATH  = os.environ.get('SQLITE_PATH') or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'rankings.db')


def q(sql):
    """Convert ? placeholders to %s for PostgreSQL; leave as-is for SQLite."""
    if DATABASE_URL:
        return sql.replace('?', '%s')
    return sql


def get_db():
    if DATABASE_URL:
        import psycopg2
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = False
        return conn
    return sqlite3.connect(SQLITE_PATH)


def get_cursor(conn):
    if DATABASE_URL:
        import psycopg2.extras
        return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    conn.row_factory = sqlite3.Row
    return conn.cursor()


def insert_returning_id(c, sql, params):
    if DATABASE_URL:
        c.execute(sql + ' RETURNING id', params)
        return c.fetchone()['id']
    c.execute(sql, params)
    return c.lastrowid


# ---------------------------------------------------------------------------
# Schema initialisation
# ---------------------------------------------------------------------------

def _safe_migrate(sql):
    _conn = get_db()
    try:
        _conn.cursor().execute(sql)
        _conn.commit()
    except Exception as e:
        msg = str(e).lower()
        if 'duplicate column' not in msg and 'already exists' not in msg:
            logging.getLogger(__name__).error('Migration failed: %s', e)
            raise
    finally:
        _conn.close()


def init_db():
    conn = get_db()
    c = get_cursor(conn)

    if DATABASE_URL:
        id_col = 'SERIAL PRIMARY KEY'
        ts_now = 'TIMESTAMP DEFAULT NOW()'
    else:
        id_col = 'INTEGER PRIMARY KEY AUTOINCREMENT'
        ts_now = 'TIMESTAMP DEFAULT CURRENT_TIMESTAMP'

    # Global identity — one row per real-world person.
    c.execute(f'''CREATE TABLE IF NOT EXISTS competitor (
        id          {id_col},
        display_name TEXT NOT NULL,
        created_at  {ts_now}
    )''')

    # Source-specific identity record.  Maps a (source, source_key) pair to a
    # competitor.  source_key is the WTC person_id (as text) for WTC imports,
    # or the player name slug for sources without an authoritative ID.
    # reviewed = 1 once an admin has confirmed (or kept) the link.
    c.execute(f'''CREATE TABLE IF NOT EXISTS source_competitor (
        id            {id_col},
        source        TEXT NOT NULL,
        source_key    TEXT NOT NULL,
        name          TEXT NOT NULL,
        competitor_id INTEGER,
        reviewed      INTEGER NOT NULL DEFAULT 0,
        created_at    {ts_now},
        UNIQUE (source, source_key)
    )''')

    # One row per imported edition (WTC 2024, WoW 2025, …).
    # match_data_available = 0 means only aggregate data exists — the edition
    # is shown for the historical record but excluded from ratings.
    c.execute(f'''CREATE TABLE IF NOT EXISTS source_edition (
        id                   {id_col},
        source               TEXT NOT NULL,
        source_edition_id    INTEGER NOT NULL,
        year                 INTEGER NOT NULL,
        name                 TEXT NOT NULL,
        match_data_available INTEGER NOT NULL DEFAULT 1,
        imported_at          {ts_now},
        UNIQUE (source, source_edition_id)
    )''')

    # One row per individual board game.  outcome is always from competitor_a's
    # perspective (W/D/L).  sc_a/sc_b record which source_competitor played the
    # game; competitor_a/b cache their current competitor_id so ratings don't
    # need a join, and are rewritten whenever a source_competitor is relinked.
    c.execute(f'''CREATE TABLE IF NOT EXISTS game (
        id                {id_col},
        source_edition_id INTEGER NOT NULL,
        round_order       INTEGER NOT NULL,
        board_number      INTEGER,
        competitor_a      INTEGER NOT NULL,
        competitor_b      INTEGER NOT NULL,
        sc_a              INTEGER,
        sc_b              INTEGER,
        outcome           TEXT NOT NULL,
        dice_kills_a      INTEGER DEFAULT 0,
        dice_kills_b      INTEGER DEFAULT 0,
        UNIQUE (source_edition_id, round_order, board_number, competitor_a, competitor_b)
    )''')

    conn.commit()
    conn.close()

    # Columns added after the first deploy.
    _safe_migrate('ALTER TABLE source_competitor ADD COLUMN reviewed INTEGER NOT NULL DEFAULT 0')
    _safe_migrate('ALTER TABLE game ADD COLUMN sc_a INTEGER')
    _safe_migrate('ALTER TABLE game ADD COLUMN sc_b INTEGER')
    _backfill_game_source_competitors()


def _backfill_game_source_competitors():
    """Fill sc_a/sc_b for games imported before those columns existed, where
    the competitor has exactly one source_competitor in the edition's source.
    Ambiguous rows stay NULL until the edition is re-imported."""
    conn = get_db()
    c = get_cursor(conn)
    for side in ('a', 'b'):
        c.execute(f'''UPDATE game SET sc_{side} = (
                          SELECT CASE WHEN COUNT(*) = 1 THEN MIN(sc.id) END
                          FROM source_competitor sc
                          JOIN source_edition se ON se.source = sc.source
                          WHERE se.id = game.source_edition_id
                          AND sc.competitor_id = game.competitor_{side})
                      WHERE sc_{side} IS NULL''')
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Identity helpers
# ---------------------------------------------------------------------------

def relink_source_competitor(c, sc_id, competitor_id):
    """Point source_competitor *sc_id* at *competitor_id*, move its games with
    it, and delete the previous competitor if nothing references it any more.
    Caller commits."""
    c.execute(q('SELECT competitor_id FROM source_competitor WHERE id=?'), (sc_id,))
    old_cid = c.fetchone()['competitor_id']
    c.execute(q('UPDATE source_competitor SET competitor_id=?, reviewed=1 WHERE id=?'),
              (competitor_id, sc_id))
    if old_cid == competitor_id:
        return
    c.execute(q('UPDATE game SET competitor_a=? WHERE sc_a=?'), (competitor_id, sc_id))
    c.execute(q('UPDATE game SET competitor_b=? WHERE sc_b=?'), (competitor_id, sc_id))
    if old_cid is None:
        return
    c.execute(q('SELECT COUNT(*) as cnt FROM source_competitor WHERE competitor_id=?'), (old_cid,))
    if c.fetchone()['cnt'] == 0:
        # Legacy games without sc_a/sc_b can only be attributed via the old id.
        c.execute(q('UPDATE game SET competitor_a=? WHERE competitor_a=?'), (competitor_id, old_cid))
        c.execute(q('UPDATE game SET competitor_b=? WHERE competitor_b=?'), (competitor_id, old_cid))
        c.execute(q('DELETE FROM competitor WHERE id=?'), (old_cid,))
