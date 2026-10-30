import hmac
import os
import secrets
from functools import wraps

from flask import (Flask, abort, flash, redirect, render_template, request,
                   session, url_for)

from db import get_cursor, get_db, init_db, insert_returning_id, q, relink_source_competitor
from importer_wow import WOW_EDITIONS
from ratings import ELO_K, MIN_EDITIONS, MIN_GAMES, compute_ratings


def require_env(name):
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f'{name} environment variable is required and must be set')
    return value


app = Flask(__name__)
app.secret_key = require_env('SECRET_KEY')
app.config.update(SESSION_COOKIE_SAMESITE='Lax')
ADMIN_PASSWORD = require_env('ADMIN_PASSWORD')

WTC_EXPORT_URL = 'https://scoring.hala.dk/api/export.json'

init_db()

# ---------------------------------------------------------------------------
# Auth / CSRF helpers
# ---------------------------------------------------------------------------

def require_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


def csrf_token():
    if 'csrf_token' not in session:
        session['csrf_token'] = secrets.token_hex(32)
    return session['csrf_token']


app.jinja_env.globals['csrf_token'] = csrf_token


@app.before_request
def csrf_protect():
    if request.method == 'POST':
        token = session.get('csrf_token')
        if not token or not hmac.compare_digest(token, request.form.get('csrf_token', '')):
            abort(400, 'Invalid or missing CSRF token — reload the page and try again.')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        password = request.form.get('password', '')
        if hmac.compare_digest(password.encode(), ADMIN_PASSWORD.encode()):
            session['logged_in'] = True
            return redirect(url_for('index'))
        flash('Wrong password.', 'error')
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
    # Each game appears twice in the UNION ALL (once per side), hence / 2.
    c.execute('''SELECT se.source, se.year, se.name, se.match_data_available,
                        COALESCE(p.player_count, 0) as player_count,
                        COALESCE(p.game_count, 0) as game_count
                 FROM source_edition se
                 LEFT JOIN (
                     SELECT source_edition_id,
                            COUNT(DISTINCT cid) as player_count,
                            COUNT(*) / 2 as game_count
                     FROM (SELECT source_edition_id, competitor_a as cid FROM game
                           UNION ALL
                           SELECT source_edition_id, competitor_b FROM game) sides
                     GROUP BY source_edition_id
                 ) p ON p.source_edition_id = se.id
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
                               min_games=MIN_GAMES, min_editions=MIN_EDITIONS, elo_k=ELO_K)

    rated_ids = list(rated_editions.keys())
    placeholders = ','.join(['?' for _ in rated_ids])
    c.execute(q(f'''SELECT g.*, se.source, se.year
                    FROM game g
                    JOIN source_edition se ON se.id = g.source_edition_id
                    WHERE g.source_edition_id IN ({placeholders})'''), rated_ids)
    games = c.fetchall()

    c.execute('SELECT id, display_name FROM competitor')
    competitors = {row['id']: row['display_name'] for row in c.fetchall()}

    # Build per-competitor edition list from actual games played.
    # Using games (not source_competitor→source_edition join) avoids showing
    # every edition of a source to players who only played in one of them.
    c.execute(q(f'''SELECT DISTINCT g.competitor_a as cid, se.source, se.year, se.name as edition_name
                    FROM game g JOIN source_edition se ON se.id = g.source_edition_id
                    WHERE g.source_edition_id IN ({placeholders})
                    UNION
                    SELECT DISTINCT g.competitor_b, se.source, se.year, se.name
                    FROM game g JOIN source_edition se ON se.id = g.source_edition_id
                    WHERE g.source_edition_id IN ({placeholders})'''),
              rated_ids + rated_ids)
    comp_editions = {}
    for row in c.fetchall():
        cid = row['cid']
        comp_editions.setdefault(cid, [])
        entry = {'source': row['source'], 'year': row['year'], 'name': row['edition_name']}
        if entry not in comp_editions[cid]:
            comp_editions[cid].append(entry)

    c.execute('''SELECT DISTINCT source FROM source_edition ORDER BY source''')
    available_sources = [r['source'] for r in c.fetchall()]

    conn.close()

    rows = compute_ratings(games, competitors, comp_editions, model=model)
    qualified   = sorted([r for r in rows if r['qualified']],
                         key=lambda x: (x['rating'], x['point_pct'], x['dice_kills']),
                         reverse=True)
    provisional = sorted([r for r in rows if not r['qualified']],
                         key=lambda x: (x['rating'], x['point_pct']), reverse=True)

    return render_template('world.html', qualified=qualified, provisional=provisional,
                           model=model, sources=sources, available_sources=available_sources,
                           min_games=MIN_GAMES, min_editions=MIN_EDITIONS, elo_k=ELO_K)


# ---------------------------------------------------------------------------
# Admin: imports
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
                           unmatched_count=unmatched_count,
                           wtc_export_url=WTC_EXPORT_URL,
                           wow_years=sorted(WOW_EDITIONS))


def _flash_import_stats(label, stats):
    flash(f"{label} import done — {stats['editions']} new edition(s), "
          f"{stats['new_competitors']} new competitors, "
          f"{stats['games']} games imported (previously {stats['games_before']}).")


@app.route('/admin/import/wtc', methods=['POST'])
@require_admin
def admin_import_wtc():
    import requests as req
    url = request.form.get('url', WTC_EXPORT_URL).strip()
    try:
        resp = req.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        flash(f'Fetch failed: {e}', 'error')
        return redirect(url_for('admin'))

    from importer_wtc import import_wtc
    try:
        stats = import_wtc(data)
    except Exception as e:
        flash(f'WTC import failed: {e}', 'error')
        return redirect(url_for('admin'))
    _flash_import_stats('WTC', stats)
    return redirect(url_for('admin'))


@app.route('/admin/import/wow/<int:year>', methods=['POST'])
@require_admin
def admin_import_wow(year):
    if year not in WOW_EDITIONS:
        flash(f'Unknown WoW year: {year}', 'error')
        return redirect(url_for('admin'))
    from importer_wow import import_wow
    try:
        stats = import_wow(year)
    except Exception as e:
        flash(f'WoW {year} import failed: {e}', 'error')
        return redirect(url_for('admin'))
    _flash_import_stats(f'WoW {year}', stats)
    return redirect(url_for('admin'))


# ---------------------------------------------------------------------------
# Admin: identity matching
# ---------------------------------------------------------------------------

@app.route('/admin/unmatched')
@require_admin
def admin_unmatched():
    conn = get_db()
    c = get_cursor(conn)
    c.execute('''SELECT sc.id, sc.source, sc.source_key, sc.name
                 FROM source_competitor sc
                 WHERE sc.competitor_id IS NULL
                 ORDER BY sc.name''')
    unmatched = c.fetchall()
    c.execute('SELECT id, display_name FROM competitor ORDER BY display_name')
    all_competitors = c.fetchall()
    conn.close()
    return render_template('admin_unmatched.html',
                           unmatched=unmatched, all_competitors=all_competitors)


@app.route('/admin/wow_players')
@require_admin
def admin_wow_players():
    conn = get_db()
    c = get_cursor(conn)
    # Unreviewed WoW source_competitors with games, whose linked competitor has
    # no WTC source_competitor
    c.execute('''SELECT sc.id, sc.source_key, sc.name, sc.competitor_id, comp.display_name
                 FROM source_competitor sc
                 JOIN competitor comp ON comp.id = sc.competitor_id
                 WHERE sc.source = 'wow'
                 AND sc.reviewed = 0
                 AND EXISTS (SELECT 1 FROM game g WHERE g.sc_a = sc.id OR g.sc_b = sc.id)
                 AND NOT EXISTS (
                     SELECT 1 FROM source_competitor sc2
                     WHERE sc2.competitor_id = sc.competitor_id AND sc2.source = 'wtc'
                 )
                 ORDER BY sc.name''')
    wow_players = c.fetchall()
    # All competitors for the dropdown, flagging WTC-linked ones
    c.execute('''SELECT c.id, c.display_name,
                        (SELECT COUNT(*) FROM source_competitor sc
                         WHERE sc.competitor_id = c.id AND sc.source = 'wtc') as has_wtc
                 FROM competitor c
                 ORDER BY c.display_name''')
    all_competitors = c.fetchall()
    conn.close()
    return render_template('admin_wow_players.html',
                           wow_players=wow_players, all_competitors=all_competitors)


@app.route('/admin/wow_merge', methods=['POST'])
@require_admin
def admin_wow_merge():
    sc_id  = int(request.form['sc_id'])
    action = request.form['action']  # 'link' | 'keep'
    conn = get_db()
    c = get_cursor(conn)
    if action == 'keep':
        c.execute(q('UPDATE source_competitor SET reviewed=1 WHERE id=?'), (sc_id,))
        flash('Kept as WoW-only competitor.')
    else:
        relink_source_competitor(c, sc_id, int(request.form['competitor_id']))
        flash('Linked.')
    conn.commit()
    conn.close()
    return redirect(url_for('admin_wow_players'))


@app.route('/admin/link', methods=['POST'])
@require_admin
def admin_link():
    """Link a source_competitor to an existing competitor, or create a new one."""
    sc_id  = int(request.form['sc_id'])
    action = request.form['action']   # 'link' | 'new'
    conn = get_db()
    c = get_cursor(conn)
    if action == 'link':
        comp_id = int(request.form['competitor_id'])
    else:
        name = request.form['new_name'].strip()
        if not name:
            conn.close()
            flash('Enter a name for the new competitor.', 'error')
            return redirect(url_for('admin_unmatched'))
        comp_id = insert_returning_id(
            c, q('INSERT INTO competitor (display_name) VALUES (?)'), (name,))
    relink_source_competitor(c, sc_id, comp_id)
    conn.commit()
    conn.close()
    return redirect(url_for('admin_unmatched'))


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    app.run(debug=True, port=5001)
