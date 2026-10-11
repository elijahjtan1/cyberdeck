import unittest

from core.librarian.query import keywords, search_query
from core.librarian.rank import Passage, rank_passages


def passage(text, url='/local/1', source='local'):
    return Passage(text, 'Example', url, source)


class QueryTest(unittest.TestCase):
    def test_question_words_and_noise(self):
        self.assertEqual(search_query('How does a passive sonobuoy work?'), 'passive sonobuoy')
        self.assertEqual(keywords('Please explain how Alan Turing used the Enigma in 1942.'),
                         ['Alan', 'Turing', 'used', 'Enigma', '1942'])
        self.assertEqual(keywords('What are C++ C# IPv6 Qwen3 0.6B and hydro-acoustics?'),
                         ['C++', 'C#', 'IPv6', 'Qwen3', '0.6B', 'hydro-acoustics'])
        self.assertEqual(keywords('sonobuoy SONOBUOY sonar'), ['sonobuoy', 'sonar'])
        self.assertEqual(keywords('why is this?'), [])
        self.assertEqual(keywords('alpha beta gamma', limit=2), ['alpha', 'beta'])
        self.assertEqual(keywords('alpha', limit=0), [])


class RankTest(unittest.TestCase):
    def test_accents_and_case_do_not_block_a_match(self):
        cafe = Passage('Repair Café events help people mend things.', 'Repair Café', 'u', 'kiwix')
        rows = rank_passages('What is a repair cafe?', [cafe], 200)
        self.assertEqual(len(rows), 1)
        plain = rank_passages('What is a repair?', [cafe], 200)[0]
        self.assertGreater(rows[0].score, plain.score)  # "cafe" now counts too.

    def test_bm25_order_across_sources(self):
        rows = [passage('Active sonar emits a pulse.'),
                passage('A passive sonobuoy uses a hydrophone to listen underwater.', '/kiwix/sonobuoy', 'kiwix'),
                passage('A sonobuoy floats at sea. ' + 'Water is wet. ' * 20, '/local/2')]
        results = rank_passages('How does a passive sonobuoy work?', rows)
        self.assertEqual(results[0].source, 'kiwix')
        self.assertEqual(results[0].url, '/kiwix/sonobuoy')
        self.assertTrue(all(p.score > 0.25 for p in results))
        self.assertNotIn(rows[0], results)

    def test_budget_and_relevant_tail(self):
        rows = [passage(('Unrelated water. ' * 200) + 'A passive sonobuoy listens with a hydrophone.')]
        for budget in (0, 20, 80, 1400):
            results = rank_passages('passive sonobuoy', rows, budget)
            self.assertLessEqual(sum((len(p.text) + 3) // 4 for p in results), budget)
        self.assertIn('hydrophone', rank_passages('passive sonobuoy', rows, 80)[0].text)

    def test_empty_threshold_and_duplicates(self):
        self.assertEqual(rank_passages('sonobuoy', []), [])
        self.assertEqual(rank_passages('sonobuoy', [passage('Baking bread.')]), [])
        self.assertEqual(rank_passages('why is it?', [passage('sonobuoy')]), [])
        self.assertEqual(rank_passages('sonobuoy', [passage('sonobuoy')], threshold=100), [])
        rows = [passage('A passive sonobuoy listens.'), passage('A passive sonobuoy listens.', '/local/2')]
        self.assertEqual(len(rank_passages('passive sonobuoy', rows)), 1)
        with self.assertRaises(ValueError):
            rank_passages('x', rows, -1)


if __name__ == '__main__':
    unittest.main()
