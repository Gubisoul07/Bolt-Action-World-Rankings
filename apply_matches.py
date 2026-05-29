import os, sys, unicodedata
os.chdir('/opt/rankings')
sys.path.insert(0, '/opt/rankings')
for line in open('/opt/rankings/.env').read().splitlines():
    if '=' in line:
        k, v = line.split('=', 1)
        os.environ.setdefault(k.strip(), v.strip())
from app import get_db, get_cursor, q

def norm(s):
    s = (s or '').strip().lower()
    return ''.join(c for c in unicodedata.normalize('NFD', s) if unicodedata.category(c) != 'Mn')
def first_word(s): return norm(s).split()[0] if s.strip() else ''
def last_initial(s):
    parts = norm(s).split(); return parts[-1][0] if len(parts) > 1 else ''

conn = get_db(); c = get_cursor(conn)

# Step 1: update WTC display names to full source name
c.execute(q("SELECT sc.name, sc.competitor_id FROM source_competitor sc WHERE sc.source='wtc'"))
for row in c.fetchall():
    c.execute(q('UPDATE competitor SET display_name=? WHERE id=?'), (row['name'], row['competitor_id']))
print('WTC display names updated to full names')

# Build WTC lookups
c.execute(q("SELECT sc.competitor_id, sc.name as sn, comp.display_name as dn FROM source_competitor sc JOIN competitor comp ON comp.id=sc.competitor_id WHERE sc.source='wtc'"))
wtc_all = [(r['competitor_id'], r['sn'], r['dn']) for r in c.fetchall()]
by_ns = {}; by_nd = {}; by_fi = {}; by_f = {}
for cid, sn, dn in wtc_all:
    by_ns.setdefault(norm(sn), []).append((cid, sn))
    by_nd.setdefault(norm(dn), []).append((cid, sn))
    f = first_word(sn); li = last_initial(sn)
    if f: by_f.setdefault(f, []).append((cid, sn))
    if f and li: by_fi.setdefault((f, li), []).append((cid, sn))

def find(name):
    nw = norm(name); tok = nw.split()
    h = by_ns.get(nw, [])
    if len(h) == 1: return h[0][0], h[0][1], 'exact-src'
    h = by_nd.get(nw, [])
    if len(h) == 1: return h[0][0], h[0][1], 'exact-disp'
    if len(tok) == 2 and len(tok[1]) == 1:
        h = by_fi.get((tok[0], tok[1]), [])
        if len(h) == 1: return h[0][0], h[0][1], 'first+init'
    if len(tok) == 1:
        h = by_f.get(tok[0], [])
        if len(h) == 1: return h[0][0], h[0][1], 'first-only'
    if len(tok) >= 2:
        f0, l0 = tok[0], tok[-1]
        cands = [(cid, sn) for cid, sn, dn in wtc_all
                 if norm(sn).split()[0] == f0 and len(norm(sn).split()) >= 2 and
                 (norm(sn).split()[-1].startswith(l0) or l0.startswith(norm(sn).split()[-1])
                  or (len(l0) >= 4 and norm(sn).split()[-1][:4] == l0[:4]))]
        if len(cands) == 1: return cands[0][0], cands[0][1], 'prefix-fuzzy'
    return None

# Known nickname / typo overrides: WoW source_key -> WTC competitor_id (None = keep as WoW-only)
MANUAL = {
    'aquiles':      118,   # Fernando Moreno plays as Aquiles
    'jano':         121,   # Antonio Corral plays as Jano
    'nacho':        207,   # Ignacio Garcia del Valle = Nacho
    'jedrezj':      123,   # Jedrzej Paluszynski typo
    'johny f':      176,   # Johnny Ferguson typo
    'jonnny c':     200,   # Jonny Curran typo
    'martin stork': 274,   # Martin Storck
    'martin storch':274,   # Martin Storck typo
    'martin si':    271,   # Martin Seibel abbrev
    'martin s':     271,   # Martin Seibel (WoW 2024 Team Deutschland)
    'krystof':      169,   # Krzysztof Zielński
    'krystozf':     169,
    'krysztof':     169,
    'jonny f':      176,   # Johnny Ferguson
    'rich c':       196,   # Richard Ciereszko
    'paul w':       132,   # Paul Wickens
    'al u':         154,   # Alistair Unicomb
    'phi c':        205,   # Phillip Crowcroft
    'phil c':       205,
    'colton s':     None,  # Bryan Swanson's US team – not in WTC
    'bryan s':      None,  # Bryan Swanson – not in WTC
    'm':            None,  # Too ambiguous
    'jacob w':      None,  # Likely not in WTC
    'jake t':       None,  # Not confirmed in WTC
    'tim g':        None,
}
# First-names where first-only matching is too risky (many WTC players share them)
SKIP_FIRST = {'bryan','matthew','mike','daniel','paul','phil','david','adam','matt'}

c.execute(q("SELECT id, source_key, name, competitor_id FROM source_competitor WHERE source='wow' ORDER BY name"))
wow_rows = [(r['id'], r['source_key'], r['name'], r['competitor_id']) for r in c.fetchall()]
applied = []; skipped = []
for sc_id, sk, wn, old_cid in wow_rows:
    if sk in MANUAL:
        wtc_cid = MANUAL[sk]
        if wtc_cid is None:
            skipped.append((wn, 'wow-only-keep'))
            continue
        reason = 'manual'
    else:
        res = find(wn)
        if res is None:
            skipped.append((wn, 'no-match'))
            continue
        wtc_cid, _, reason = res
        if reason == 'first-only' and first_word(wn) in SKIP_FIRST:
            skipped.append((wn, 'ambiguous-first'))
            continue
    c.execute(q('SELECT id, display_name FROM competitor WHERE id=?'), (wtc_cid,))
    wtc_comp = c.fetchone()
    if not wtc_comp:
        skipped.append((wn, f'missing-comp-{wtc_cid}'))
        continue
    if old_cid == wtc_cid:
        applied.append((wn, wtc_comp['display_name'], reason, 'already'))
        continue
    c.execute(q('UPDATE source_competitor SET competitor_id=? WHERE id=?'), (wtc_cid, sc_id))
    c.execute(q('SELECT COUNT(*) as cnt FROM source_competitor WHERE competitor_id=?'), (old_cid,))
    if c.fetchone()['cnt'] == 0:
        c.execute(q('UPDATE game SET competitor_a=? WHERE competitor_a=?'), (wtc_cid, old_cid))
        c.execute(q('UPDATE game SET competitor_b=? WHERE competitor_b=?'), (wtc_cid, old_cid))
        c.execute(q('DELETE FROM competitor WHERE id=?'), (old_cid,))
    applied.append((wn, wtc_comp['display_name'], reason, 'linked'))

conn.commit(); conn.close()
linked = [a for a in applied if a[3] == 'linked']
print(f'\nLinked {len(linked)} WoW names to WTC competitors, {len(skipped)} still need manual review')
for wn, wtcn, reason, _ in linked:
    print(f'  {wn:<24} -> {wtcn} [{reason}]')
print(f'\nStill need manual link ({len([s for s in skipped if s[1] not in ("wow-only-keep","manual-skip")])}):')
for wn, why in sorted(skipped):
    if why not in ('wow-only-keep', 'manual-skip'):
        print(f'  {wn:<24} ({why})')
