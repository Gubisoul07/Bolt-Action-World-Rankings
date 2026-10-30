import os
import tempfile
import unittest

import db


class RelinkTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        self._orig_path = db.SQLITE_PATH
        db.SQLITE_PATH = self.path
        db.init_db()
        self.conn = db.get_db()
        self.c = db.get_cursor(self.conn)
        for cid, name in ((1, 'Matt Smith'), (2, 'matt s'), (4, 'Opponent')):
            self.c.execute('INSERT INTO competitor (id, display_name) VALUES (?, ?)', (cid, name))
        # Two WoW spellings currently share the auto-created competitor 2.
        for sc_id, source, key, cid in ((10, 'wtc', '312', 1), (20, 'wow', 'matt s', 2),
                                        (21, 'wow', 'matty', 2), (40, 'wow', 'opponent', 4)):
            self.c.execute('''INSERT INTO source_competitor (id, source, source_key, name, competitor_id)
                              VALUES (?, ?, ?, ?, ?)''', (sc_id, source, key, key, cid))
        self.c.execute('''INSERT INTO source_edition (id, source, source_edition_id, year, name)
                          VALUES (1, 'wow', 2025, 2025, 'WoW 2025')''')
        for gid, sc_a in ((100, 20), (101, 21)):
            self.c.execute('''INSERT INTO game (id, source_edition_id, round_order, board_number,
                                  competitor_a, competitor_b, sc_a, sc_b, outcome)
                              VALUES (?, 1, ?, 1, 2, 4, ?, 40, 'W')''', (gid, gid, sc_a))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        db.SQLITE_PATH = self._orig_path
        os.remove(self.path)

    def competitor_of_game(self, gid):
        self.c.execute('SELECT competitor_a FROM game WHERE id=?', (gid,))
        return self.c.fetchone()['competitor_a']

    def competitor_exists(self, cid):
        self.c.execute('SELECT COUNT(*) as cnt FROM competitor WHERE id=?', (cid,))
        return self.c.fetchone()['cnt'] == 1

    def test_relink_moves_only_that_source_competitors_games(self):
        db.relink_source_competitor(self.c, 20, 1)
        self.assertEqual(self.competitor_of_game(100), 1)
        self.assertEqual(self.competitor_of_game(101), 2)   # 'matty' not moved yet
        self.assertTrue(self.competitor_exists(2))

        db.relink_source_competitor(self.c, 21, 1)
        self.assertEqual(self.competitor_of_game(101), 1)
        self.assertFalse(self.competitor_exists(2))         # orphan cleaned up

    def test_relink_marks_reviewed(self):
        db.relink_source_competitor(self.c, 20, 1)
        self.c.execute('SELECT reviewed FROM source_competitor WHERE id=20')
        self.assertEqual(self.c.fetchone()['reviewed'], 1)

    def test_legacy_games_without_sc_follow_orphaned_competitor(self):
        self.c.execute('UPDATE game SET sc_a = NULL')
        db.relink_source_competitor(self.c, 20, 1)
        db.relink_source_competitor(self.c, 21, 1)
        self.assertEqual(self.competitor_of_game(100), 1)
        self.assertEqual(self.competitor_of_game(101), 1)

    def test_backfill_only_fills_unambiguous_rows(self):
        self.c.execute('UPDATE game SET sc_a = NULL, sc_b = NULL')
        self.conn.commit()
        db._backfill_game_source_competitors()
        self.c.execute('SELECT sc_a, sc_b FROM game ORDER BY id')
        rows = [tuple(r) for r in self.c.fetchall()]
        # Competitor 2 has two WoW source_competitors → ambiguous; 4 has one.
        self.assertEqual(rows, [(None, 40), (None, 40)])


if __name__ == '__main__':
    unittest.main()
