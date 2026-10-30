"""Cross-tournament rating engine.  Pure functions — no DB access."""

MIN_GAMES    = 12   # ~2 full WTC events
MIN_EDITIONS = 2
ELO_K        = 32

# Iterative SoS: stop when no adjusted rate moves more than TOL between passes.
ITERATIVE_TOL      = 1e-6
ITERATIVE_MAX_PASS = 5000


def compute_ratings(games, competitors, comp_editions, *, model='iterative', lam=1.0):
    """Compute cross-tournament player ratings.

    games       — list of game rows (dicts with competitor_a, competitor_b, outcome,
                  dice_kills_a/b, source, year, source_edition_id)
    competitors — {id: display_name}
    comp_editions — {competitor_id: [{source, year, name}, ...]}

    Returns a list of row dicts ready for the template.
    Three models:
      'onedeep'  — SoS = mean(opponents' raw rate).  Current WTC formula.
      'iterative' — SoS computed from opponents' adjusted rate, iterated to
                    convergence.  Best for cross-pool connectivity.
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
        return compute_elo(games, competitors, comp_editions, wdl, dk, edition_set)

    # One-deep SoS (baseline).
    def _sos(cid, adj_rate):
        opp_list = opps.get(cid, [])
        opp_rates = [adj_rate.get(o, 0.5) for o in opp_list]
        return (sum(opp_rates) / len(opp_rates)) if opp_rates else 0.5

    if model == 'iterative':
        perf_rate = _iterate_sos(rate, _sos, lam)
    else:  # onedeep
        perf_rate = {cid: rate[cid] + lam * (_sos(cid, rate) - 0.5) for cid in rate}

    rows = []
    for cid in rate:
        w, d, l = wdl[cid]
        g = w + d + l
        sos = _sos(cid, rate)   # always show one-deep SoS in table regardless of model
        perf = perf_rate[cid]
        rows.append(_row(cid, competitors, comp_editions, wdl, dk, edition_set,
                         rating=round(1000 + 1000 * (perf - 0.5)), sos=round(sos, 3)))
    return rows


def _iterate_sos(rate, sos_fn, lam):
    """Solve adj = rate + lam·(SoS(adj) − 0.5) by damped fixed-point iteration.

    Undamped, the iteration oscillates forever on bipartite pockets of the
    opponent graph (e.g. two players who only met each other).  Averaging each
    pass with the previous one keeps the same fixed point but removes the
    oscillation, and preserves each pool's mean so the result is unique.
    """
    adj = dict(rate)
    for _ in range(ITERATIVE_MAX_PASS):
        new_adj = {cid: 0.5 * adj[cid] + 0.5 * (rate[cid] + lam * (sos_fn(cid, adj) - 0.5))
                   for cid in rate}
        delta = max((abs(new_adj[cid] - adj[cid]) for cid in rate), default=0.0)
        adj = new_adj
        if delta < ITERATIVE_TOL:
            break
    return adj


def compute_elo(games, competitors, comp_editions, wdl, dk, edition_set, K=ELO_K):
    """Sequential Elo, ordered by (year, round_order)."""
    elo = {}
    for cid in wdl:
        elo[cid] = 1000.0

    for g in sorted(games, key=lambda x: (x['year'], x['source_edition_id'], x['round_order'])):
        a, b = g['competitor_a'], g['competitor_b']
        ra, rb = elo.get(a, 1000.0), elo.get(b, 1000.0)
        ea = 1 / (1 + 10 ** ((rb - ra) / 400))
        o = g['outcome']
        sa = 1.0 if o == 'W' else (0.5 if o == 'D' else 0.0)
        elo[a] = ra + K * (sa - ea)
        elo[b] = rb + K * ((1 - sa) - (1 - ea))

    return [_row(cid, competitors, comp_editions, wdl, dk, edition_set,
                 rating=round(elo.get(cid, 1000)), sos=0.0)  # SoS N/A for Elo
            for cid in wdl]


def _row(cid, competitors, comp_editions, wdl, dk, edition_set, *, rating, sos):
    w, d, l = wdl[cid]
    g = w + d + l
    editions_played = comp_editions.get(cid, [])
    editions_played.sort(key=lambda e: e['year'] or 0, reverse=True)
    return {
        'name':        competitors.get(cid, f'#{cid}'),
        'editions':    editions_played,
        'played':      g,
        'wins':        w, 'draws': d, 'losses': l,
        'win_pct':     round(100 * (w / g), 1) if g else 0.0,
        'point_pct':   round(100 * ((w + 0.5 * d) / g), 1) if g else 0.0,
        'sos':         sos,
        'rating':      rating,
        'dice_kills':  dk.get(cid, 0),
        'qualified':   (len(edition_set.get(cid, set())) >= MIN_EDITIONS or g >= MIN_GAMES),
    }
