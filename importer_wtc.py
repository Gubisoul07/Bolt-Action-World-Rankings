"""WTC importer — pulls from scoring.hala.dk/api/export.json and upserts into
the rankings DB.

Called by app.py's /admin/import/wtc route; can also be run standalone:
  python importer_wtc.py [url]
"""
import sys
import requests

from db import get_db, get_cursor, init_db, insert_returning_id, q


def import_wtc(data: dict) -> dict:
    """Upsert one WTC export payload.  Returns stats dict.

    Games are replaced per edition, so pairings corrected or removed in WTC
    disappear here too.
    """
    conn = get_db()
    c    = get_cursor(conn)

    stats = {'editions': 0, 'players': 0, 'games': 0, 'games_before': 0, 'new_competitors': 0}

    for edition in data.get('editions', []):
        eid_src = edition['id']
        year    = edition['year']
        name    = edition['name']
        mda     = 1 if edition.get('match_data_available', True) else 0

        # Upsert source_edition.
        c.execute(q('''SELECT id FROM source_edition
                       WHERE source=? AND source_edition_id=?'''), ('wtc', eid_src))
        row = c.fetchone()
        if row:
            sed_id = row['id']
            c.execute(q('''UPDATE source_edition
                           SET year=?, name=?, match_data_available=?
                           WHERE id=?'''), (year, name, mda, sed_id))
        else:
            sed_id = insert_returning_id(c, q(
                '''INSERT INTO source_edition
                   (source, source_edition_id, year, name, match_data_available)
                   VALUES (?, ?, ?, ?, ?)'''),
                ('wtc', eid_src, year, name, mda))
            stats['editions'] += 1

        # Map source_competitor: source_key = str(person_id) from WTC.
        # Players without a person_id are skipped (not yet deduped in WTC admin).
        player_map = {}   # WTC player_id → (source_competitor_id, competitor_id)
        for p in edition.get('players', []):
            wtc_player_id = p['id']
            person_id     = p.get('person_id')
            if not person_id:
                continue   # unresolved in WTC — skip, import again after WTC admin dedup
            source_key = str(person_id)
            name_str   = p['name']

            c.execute(q('''SELECT id, competitor_id FROM source_competitor
                           WHERE source=? AND source_key=?'''), ('wtc', source_key))
            sc_row = c.fetchone()
            if sc_row:
                sc_id   = sc_row['id']
                comp_id = sc_row['competitor_id']
                # Keep name fresh.  WTC names are canonical display names.
                c.execute(q('UPDATE source_competitor SET name=? WHERE id=?'),
                          (name_str, sc_id))
                if comp_id is not None:
                    c.execute(q('UPDATE competitor SET display_name=? WHERE id=?'),
                              (name_str, comp_id))
            else:
                # Auto-create a competitor for new WTC persons.  Cross-source
                # identity (WTC↔WoW) is resolved later via the admin UI.
                comp_id = insert_returning_id(
                    c, q('INSERT INTO competitor (display_name) VALUES (?)'), (name_str,))
                sc_id = insert_returning_id(c, q(
                    '''INSERT INTO source_competitor
                       (source, source_key, name, competitor_id)
                       VALUES (?, ?, ?, ?)'''),
                    ('wtc', source_key, name_str, comp_id))
                stats['new_competitors'] += 1
                stats['players'] += 1

            if comp_id is not None:
                player_map[wtc_player_id] = (sc_id, comp_id)

        # Replace games — only if match_data_available and the export has
        # pairings (an empty list must not wipe a previously imported edition).
        pairings = edition.get('pairings', [])
        if not mda or not pairings:
            continue

        c.execute(q('SELECT COUNT(*) as cnt FROM game WHERE source_edition_id=?'), (sed_id,))
        stats['games_before'] += c.fetchone()['cnt']
        c.execute(q('DELETE FROM game WHERE source_edition_id=?'), (sed_id,))

        seen = set()
        for pairing in pairings:
            pa = player_map.get(pairing['player_a'])
            pb = player_map.get(pairing['player_b'])
            if pa is None or pb is None:
                continue   # one side has no person_id yet — skip

            outcome      = pairing.get('outcome')    # W/D/L from player_a's view
            round_order  = pairing['round']
            board_number = pairing.get('board_number')
            dk_a = pairing.get('dice_kills_a') or 0
            dk_b = pairing.get('dice_kills_b') or 0

            if not outcome:
                continue
            key = (round_order, board_number, pa[1], pb[1])
            if key in seen:
                continue
            seen.add(key)

            c.execute(q(
                '''INSERT INTO game
                   (source_edition_id, round_order, board_number,
                    competitor_a, competitor_b, sc_a, sc_b,
                    outcome, dice_kills_a, dice_kills_b)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)'''),
                (sed_id, round_order, board_number, pa[1], pb[1], pa[0], pb[0],
                 outcome, dk_a, dk_b))
            stats['games'] += 1

    conn.commit()
    conn.close()
    return stats


if __name__ == '__main__':
    url = sys.argv[1] if len(sys.argv) > 1 else 'https://scoring.hala.dk/api/export.json'
    print(f'Fetching {url} …')
    data = requests.get(url, timeout=15).json()
    init_db()
    result = import_wtc(data)
    print(result)
