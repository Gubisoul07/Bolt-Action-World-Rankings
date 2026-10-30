import unittest

import ratings


def game(a, b, outcome, edition=1, rnd=1, year=2025):
    return {'competitor_a': a, 'competitor_b': b, 'outcome': outcome,
            'dice_kills_a': 0, 'dice_kills_b': 0, 'source_edition_id': edition,
            'round_order': rnd, 'year': year, 'source': 'wtc'}


def by_name(rows):
    return {r['name']: r for r in rows}


NAMES = {i: f'P{i}' for i in range(1, 10)}


class IterativeSoSTest(unittest.TestCase):
    def test_isolated_pair_is_stable(self):
        # Two players who only met each other used to flip between
        # 1500/500 and 1000/1000 depending on the pass count.
        rows = by_name(ratings.compute_ratings([game(1, 2, 'W')], NAMES, {}))
        self.assertEqual(rows['P1']['rating'], 1250)
        self.assertEqual(rows['P2']['rating'], 750)

    def test_result_does_not_depend_on_pass_cap(self):
        games = [game(1, 2, 'W', rnd=1), game(3, 4, 'D', rnd=1),
                 game(1, 3, 'L', rnd=2), game(2, 4, 'W', rnd=2),
                 game(1, 4, 'W', rnd=3), game(2, 3, 'L', rnd=3),
                 game(5, 6, 'W', rnd=1)]
        default = by_name(ratings.compute_ratings(games, NAMES, {}))
        original = ratings.ITERATIVE_MAX_PASS
        try:
            ratings.ITERATIVE_MAX_PASS = original * 4 + 1
            longer = by_name(ratings.compute_ratings(games, NAMES, {}))
        finally:
            ratings.ITERATIVE_MAX_PASS = original
        self.assertEqual({n: r['rating'] for n, r in default.items()},
                         {n: r['rating'] for n, r in longer.items()})

    def test_pool_mean_stays_at_1000(self):
        games = [game(1, 2, 'W'), game(2, 3, 'W', rnd=2), game(3, 1, 'D', rnd=3),
                 game(1, 4, 'W', rnd=4), game(4, 2, 'L', rnd=5)]
        rows = ratings.compute_ratings(games, NAMES, {})
        played = sum(r['played'] for r in rows)
        weighted = sum(r['rating'] * r['played'] for r in rows) / played
        self.assertAlmostEqual(weighted, 1000, delta=1)

    def test_draw_is_even(self):
        rows = by_name(ratings.compute_ratings([game(1, 2, 'D')], NAMES, {}))
        self.assertEqual(rows['P1']['rating'], rows['P2']['rating'])


class EloTest(unittest.TestCase):
    def test_winner_gains_what_loser_loses(self):
        rows = by_name(ratings.compute_ratings([game(1, 2, 'W')], NAMES, {}, model='elo'))
        self.assertEqual(rows['P1']['rating'], 1000 + ratings.ELO_K // 2)
        self.assertEqual(rows['P2']['rating'], 1000 - ratings.ELO_K // 2)


class QualificationTest(unittest.TestCase):
    def test_two_editions_qualify(self):
        games = [game(1, 2, 'W', edition=1), game(1, 2, 'L', edition=2)]
        rows = by_name(ratings.compute_ratings(games, NAMES, {}))
        self.assertTrue(rows['P1']['qualified'])

    def test_few_games_in_one_edition_is_provisional(self):
        rows = by_name(ratings.compute_ratings([game(1, 2, 'W')], NAMES, {}))
        self.assertFalse(rows['P1']['qualified'])


if __name__ == '__main__':
    unittest.main()
