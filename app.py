import os
import sqlite3
import logging

from flask import Flask, jsonify, render_template, request, redirect, url_for, session, flash

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY') or (_ for _ in ()).throw(
    RuntimeError('SECRET_KEY environment variable is required and must be set')
)
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD') or (_ for _ in ()).throw(
    RuntimeError('ADMIN_PASSWORD environment variable is required and must be set')
)

DATABASE_URL = os.environ.get('DATABASE_URL')

# ---------------------------------------------------------------------------
# DB helpers  (mirrors WTC scoring q/get_db/get_cursor pattern)
# ---------------------------------------------------------------------------

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
    return sqlite3.connect('rankings.db')


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
    c.execute(f'''CREATE TABLE IF NOT EXISTS source_competitor (
        id            {id_col},
        source        TEXT NOT NULL,
        source_key    TEXT NOT NULL,
        name          TEXT NOT NULL,
        competitor_id INTEGER,
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
    # perspective (W/D/L).  rated is derived from the parent source_edition's
    # match_data_available at query time — this column caches it for fast reads.
    c.execute(f'''CREATE TABLE IF NOT EXISTS game (
        id                {id_col},
        source_edition_id INTEGER NOT NULL,
        round_order       INTEGER NOT NULL,
        board_number      INTEGER,
        competitor_a      INTEGER NOT NULL,
        competitor_b      INTEGER NOT NULL,
        outcome           TEXT NOT NULL,
        dice_kills_a      INTEGER DEFAULT 0,
        dice_kills_b      INTEGER DEFAULT 0,
        UNIQUE (source_edition_id, round_order, board_number, competitor_a, competitor_b)
    )''')

    conn.commit()
    conn.close()


init_db()

# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def require_admin(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if request.form.get('password') == ADMIN_PASSWORD:
            session['logged_in'] = True
            return redirect(url_for('index'))
        flash('Wrong password.')
    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))


# ---------------------------------------------------------------------------
# Public pages
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    conn = get_db()
    c = get_cursor(conn)
    c.execute('''SELECT se.source, se.year, se.name, se.match_data_available,
                        COUNT(DISTINCT g.competitor_a) as player_count,
                        COUNT(g.id) as game_count
                 FROM source_edition se
                 LEFT JOIN game g ON g.source_edition_id = se.id
                 GROUP BY se.id, se.source, se.year, se.name, se.match_data_available
                 ORDER BY se.year DESC, se.source''')
    editions = c.fetchall()
    c.execute('SELECT COUNT(*) as cnt FROM competitor')
    competitor_count = c.fetchone()['cnt']
    conn.close()
    return render_template('index.html', editions=editions, competitor_count=competitor_count)


@app.route('/world')
def world_rankings():
    model = request.args.get('model', 'iterative')
    if model not in ('iterative', 'onedeep', 'elo'):
        model = 'iterative'

    sources = request.args.getlist('source')  # [] means all

    conn = get_db()
    c = get_cursor(conn)

    # Load all rated editions (and which sources they belong to).
    if sources:
        placeholders = ','.join(['?' for _ in sources])
        c.execute(q(f'''SELECT id, source, year, name
                        FROM source_edition
                        WHERE match_data_available = 1
                        AND source IN ({placeholders})
                        ORDER BY year'''), sources)
    else:
        c.execute('''SELECT id, source, year, name
                     FROM source_edition
                     WHERE match_data_available = 1
                     ORDER BY year''')
    rated_editions = {row['id']: row for row in c.fetchall()}

    if not rated_editions:
        conn.close()
        return render_template('world.html', qualified=[], provisional=[],
                               model=model, sources=sources, available_sources=[],
                               min_games=MIN_GAMES, min_editions=MIN_EDITIONS)

    rated_ids = list(rated_editions.keys())
    placeholders = ','.join(['?' for _ in rated_ids])
    c.execute(q(f'''SELECT g.*, se.source, se.year
                    FROM game g
                    JOIN source_edition se ON se.id = g.source_edition_id
                    WHERE g.source_edition_id IN ({placeholders})'''), rated_ids)
    games = c.fetchall()

    c.execute('SELECT id, display_name FROM competitor')
    competitors = {row['id']: row['display_name'] for row in c.fetchall()}

    c.execute('''SELECT sc.competitor_id, se.source, se.year, se.name as edition_name
                 FROM source_competitor sc
                 JOIN source_edition se ON se.source = sc.source
                 WHERE sc.competitor_id IS NOT NULL''')
    # Build per-competitor edition list (all editions, not just rated).
    comp_editions = {}
    for row in c.fetchall():
        cid = row['competitor_id']
        comp_editions.setdefault(cid, [])
        entry = {'source': row['source'], 'year': row['year'], 'name': row['edition_name']}
        if entry not in comp_editions[cid]:
            comp_editions[cid].append(entry)

    c.execute('''SELECT DISTINCT source FROM source_edition ORDER BY source''')
    available_sources = [r['source'] for r in c.fetchall()]

    conn.close()

    rows = _compute_ratings(games, competitors, comp_editions, model=model)
    qualified   = sorted([r for r in rows if r['qualified']],
                         key=lambda x: (x['rating'], x['point_pct'], x['dice_kills']),
                         reverse=True)
    provisional = sorted([r for r in rows if not r['qualified']],
                         key=lambda x: (x['rating'], x['point_pct']), reverse=True)

    return render_template('world.html', qualified=qualified, provisional=provisional,
                           model=model, sources=sources, available_sources=available_sources,
                           min_games=MIN_GAMES, min_editions=MIN_EDITIONS)


MIN_GAMES    = 12   # ~2 full WTC events
MIN_EDITIONS = 2


def _compute_ratings(games, competitors, comp_editions, *, model='iterative', lam=1.0):
    """Compute cross-tournament player ratings.

    games       — list of game rows (dicts with competitor_a, competitor_b, outcome,
                  dice_kills_a/b, source, year, source_edition_id)
    competitors — {id: display_name}
    comp_editions — {competitor_id: [{source, year, name}, ...]}

    Returns a list of row dicts ready for the template.
    Three models:
      'onedeep'  — SoS = mean(opponents' raw rate).  Current WTC formula.
      'iterative' — SoS computed from opponents' adjusted rate, iterated to
                    convergence (~10 passes).  Best for cross-pool connectivity.
      'elo'      — sequential Elo, ordered by (year, round_order).
    """
    # Accumulate per-competitor W/D/L, dice kills, and opponent lists.
    wdl     = {}   # {comp_id: [w, d, l]}
    dk      = {}   # {comp_id: dice_kills}
    opps    = {}   # {comp_id: [opponent_comp_id, ...]}
    edition_set = {}  # {comp_id: set(source_edition_id)}

    for g in games:
        a, b = g['competitor_a'], g['competitor_b']
        for cid in (a, b):
            wdl.setdefault(cid,  [0, 0, 0])
            dk.setdefault(cid,   0)
            opps.setdefault(cid, [])
            edition_set.setdefault(cid, set())
        opps[a].append(b)
        opps[b].append(a)
        edition_set[a].add(g['source_edition_id'])
        edition_set[b].add(g['source_edition_id'])
        dk[a] += g['dice_kills_a'] or 0
        dk[b] += g['dice_kills_b'] or 0
        o = g['outcome']   # W/D/L from a's view
        if o == 'W':
            wdl[a][0] += 1; wdl[b][2] += 1
        elif o == 'D':
            wdl[a][1] += 1; wdl[b][1] += 1
        else:
            wdl[a][2] += 1; wdl[b][0] += 1

    # Raw win-equivalent rate r for every competitor seen.
    rate = {}
    for cid, (w, d, l) in wdl.items():
        g = w + d + l
        rate[cid] = ((w + 0.5 * d) / g) if g else 0.0

    if model == 'elo':
        return _compute_elo(games, competitors, comp_editions, wdl, dk, edition_set)

    # One-deep SoS (baseline).
    def _sos(cid, adj_rate):
        opp_list = opps.get(cid, [])
        opp_rates = [adj_rate.get(o, 0.5) for o in opp_list]
        return (sum(opp_rates) / len(opp_rates)) if opp_rates else 0.5

    if model == 'iterative':
        adj = dict(rate)
        for _ in range(10):
            new_adj = {}
            for cid in rate:
                sos = _sos(cid, adj)
                new_adj[cid] = rate[cid] + lam * (sos - 0.5)
            adj = new_adj
        perf_rate = adj
    else:  # onedeep
        perf_rate = {cid: rate[cid] + lam * (_sos(cid, rate) - 0.5) for cid in rate}

    rows = []
    for cid in rate:
        w, d, l = wdl[cid]
        g = w + d + l
        sos = _sos(cid, rate)   # always show one-deep SoS in table regardless of model
        perf = perf_rate[cid]
        editions_played = comp_editions.get(cid, [])
        editions_played.sort(key=lambda e: e['year'] or 0, reverse=True)
        rows.append({
            'name':        competitors.get(cid, f'#{cid}'),
            'editions':    editions_played,
            'played':      g,
            'wins':        w, 'draws': d, 'losses': l,
            'win_pct':     round(100 * (w / g), 1) if g else 0.0,
            'point_pct':   round(100 * rate[cid], 1),
            'sos':         round(sos, 3),
            'rating':      round(1000 + 1000 * (perf - 0.5)),
            'dice_kills':  dk.get(cid, 0),
            'qualified':   (len(edition_set.get(cid, set())) >= MIN_EDITIONS or g >= MIN_GAMES),
        })
    return rows


def _compute_elo(games, competitors, comp_editions, wdl, dk, edition_set, K=32):
    """Sequential Elo, ordered by (year, round_order)."""
    elo = {}
    for cid in wdl:
        elo[cid] = 1000.0

    for g in sorted(games, key=lambda x: (x['year'], x['round_order'])):
        a, b = g['competitor_a'], g['competitor_b']
        ra, rb = elo.get(a, 1000.0), elo.get(b, 1000.0)
        ea = 1 / (1 + 10 ** ((rb - ra) / 400))
        o = g['outcome']
        sa = 1.0 if o == 'W' else (0.5 if o == 'D' else 0.0)
        elo[a] = ra + K * (sa - ea)
        elo[b] = rb + K * ((1 - sa) - (1 - ea))

    rows = []
    for cid, (w, d, l) in wdl.items():
        g = w + d + l
        editions_played = comp_editions.get(cid, [])
        editions_played.sort(key=lambda e: e['year'] or 0, reverse=True)
        rows.append({
            'name':        competitors.get(cid, f'#{cid}'),
            'editions':    editions_played,
            'played':      g,
            'wins':        w, 'draws': d, 'losses': l,
            'win_pct':     round(100 * (w / g), 1) if g else 0.0,
            'point_pct':   round(100 * ((w + 0.5 * d) / g), 1) if g else 0.0,
            'sos':         0.0,  # N/A for Elo
            'rating':      round(elo.get(cid, 1000)),
            'dice_kills':  dk.get(cid, 0),
            'qualified':   (len(edition_set.get(cid, set())) >= MIN_EDITIONS or g >= MIN_GAMES),
        })
    return rows


# ---------------------------------------------------------------------------
# Admin: WTC import
# ---------------------------------------------------------------------------

@app.route('/admin')
@require_admin
def admin():
    conn = get_db()
    c = get_cursor(conn)
    c.execute('''SELECT se.*, COUNT(g.id) as game_count
                 FROM source_edition se
                 LEFT JOIN game g ON g.source_edition_id = se.id
                 GROUP BY se.id
                 ORDER BY se.year DESC''')
    editions = c.fetchall()
    c.execute('SELECT COUNT(*) as cnt FROM competitor')
    competitor_count = c.fetchone()['cnt']
    c.execute('''SELECT COUNT(*) as cnt FROM source_competitor
                 WHERE competitor_id IS NULL''')
    unmatched_count = c.fetchone()['cnt']
    conn.close()
    return render_template('admin.html', editions=editions,
                           competitor_count=competitor_count,
                           unmatched_count=unmatched_count)


@app.route('/admin/import/wtc', methods=['POST'])
@require_admin
def admin_import_wtc():
    import requests as req
    url = request.form.get('url', 'https://scoring.hala.dk/api/export.json').strip()
    try:
        resp = req.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        flash(f'Fetch failed: {e}')
        return redirect(url_for('admin'))

    from importer_wtc import import_wtc
    stats = import_wtc(data)
    flash(f"WTC import done — {stats['editions']} edition(s), "
          f"{stats['players']} players, {stats['games']} games, "
          f"{stats['new_competitors']} new competitors.")
    return redirect(url_for('admin'))


# ---------------------------------------------------------------------------
# Admin: identity matching
# ---------------------------------------------------------------------------

@app.route('/admin/unmatched')
@require_admin
def admin_unmatched():
    conn = get_db()
    c = get_cursor(conn)
    c.execute(q('''SELECT sc.id, sc.source, sc.source_key, sc.name
                   FROM source_competitor sc
                   WHERE sc.competitor_id IS NULL
                   ORDER BY sc.name'''))
    unmatched = c.fetchall()
    c.execute('SELECT id, display_name FROM competitor ORDER BY display_name')
    all_competitors = c.fetchall()
    conn.close()
    return render_template('admin_unmatched.html',
                           unmatched=unmatched, all_competitors=all_competitors)


@app.route('/admin/link', methods=['POST'])
@require_admin
def admin_link():
    """Link a source_competitor to an existing competitor, or create a new one."""
    sc_id       = int(request.form['sc_id'])
    action      = request.form['action']   # 'link' | 'new'
    conn = get_db()
    c = get_cursor(conn)
    if action == 'link':
        comp_id = int(request.form['competitor_id'])
    else:
        name = request.form['new_name'].strip()
        comp_id = insert_returning_id(
            c, q('INSERT INTO competitor (display_name) VALUES (?)'), (name,))
    c.execute(q('UPDATE source_competitor SET competitor_id=? WHERE id=?'), (comp_id, sc_id))
    conn.commit()
    conn.close()
    return redirect(url_for('admin_unmatched'))


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    app.run(debug=True, port=5001)
