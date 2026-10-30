import unittest

import openpyxl

import importer_wow

HEADER = ['Table', None, 'Army', None, 'Army', 'Roll off winner and choice',
          'Points', 'Points', 'secondary', 'secondary']


def sheet(rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    return ws


def header(team_a, team_b):
    row = list(HEADER)
    row[1], row[3] = team_a, team_b
    return row


class ExtractGamesTest(unittest.TestCase):
    def test_reads_rounds_teams_and_boards(self):
        ws = sheet([
            ['Round One'],
            ['Group A'],
            header('England', 'Pirates'),
            ['A ', 'Bo', 'Ger', 'Jan', 'USA', None, 3, 0],
            ['B', 'Ann', 'Ger', 'Kim', 'USA', None, 1, 1],
            ['bonus', None, None, None, None, None, 1],
            ['Round 2'],
            header('Wales', 'England'),
            [7, 'Cy', 'Ger', 'Bo', 'USA', None, 0, 3],
        ])
        games = importer_wow._extract_games(ws)
        self.assertEqual(games, [
            (1, 1, 'Bo', 'Jan', 'W', 'England', 'Pirates'),
            (1, 2, 'Ann', 'Kim', 'D', 'England', 'Pirates'),
            (2, 7, 'Cy', 'Bo', 'L', 'Wales', 'England'),
        ])

    def test_header_with_table_number_instead_of_label(self):
        # WoW 2026 Round 1 has '19' typed over the 'Table' header cell.
        row = header('Mixed Nuts', 'Spain 2')
        row[0] = 19
        ws = sheet([row, [20, 'Bo', None, 'Granicus', None, None, 3, 0]])
        games = importer_wow._extract_games(ws, fixed_round=1)
        self.assertEqual(games, [(1, 20, 'Bo', 'Granicus', 'W', 'Mixed Nuts', 'Spain 2')])


class PlayerKeysTest(unittest.TestCase):
    def test_name_playing_twice_in_a_round_is_split_by_team(self):
        games = [
            (1, 1, 'Matt', 'Bo', 'W', 'England', 'Pirates'),
            (1, 2, 'Ann', 'Matt', 'D', 'England', 'Pirates'),
            (2, 1, 'Bo', 'Ann', 'L', 'Pirates', 'England'),
        ]
        keys = importer_wow._player_keys(games)
        self.assertEqual(keys[('Matt', 'England')], ('matt (england)', 'Matt (England)'))
        self.assertEqual(keys[('Matt', 'Pirates')], ('matt (pirates)', 'Matt (Pirates)'))
        # Names without a clash keep their plain key, so existing links survive.
        self.assertEqual(keys[('Bo', 'Pirates')], ('bo', 'Bo'))


if __name__ == '__main__':
    unittest.main()
