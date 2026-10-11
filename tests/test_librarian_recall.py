"""Recall fixes found with `deck bench` on the Pi's Restarters archive."""
import unittest

from core.librarian.kiwix import KiwixError, SearchResult
from core.librarian.query import keywords, match_key
from core.librarian.rank import Passage, rank_passages
from core.librarian.service import Retriever


class FakeKiwix:
    """kiwix-serve style: a plain query needs every word; 'a OR b' needs any."""

    def __init__(self, articles, or_error=False):
        self.articles, self.or_error, self.queries = articles, or_error, []

    def search(self, query, limit=5):
        self.queries.append(query)
        if ' OR ' in query:
            if self.or_error:
                raise KiwixError('Kiwix is unavailable')
            words, match = query.split(' OR '), any
        else:
            words, match = query.split(), all
        rows = [SearchResult(title, f'http://k/{title}', '') for title, text in self.articles
                if match(w in text for w in words)]
        return rows[:limit]


class RecallTest(unittest.TestCase):
    def test_question_noise_is_not_a_keyword(self):
        self.assertEqual(keywords("Why won't my kettle switch on?"), ['kettle', 'switch'])
        self.assertEqual(keywords('Why does my hair dryer keep cutting out?'),
                         ['hair', 'dryer', 'keep', 'cutting'])

    def test_plurals_match(self):
        self.assertEqual([match_key(w) for w in ('Lamps', 'batteries', 'glass', 'status', 'flickers')],
                         ['lamp', 'battery', 'glass', 'status', 'flicker'])
        lamps = Passage('Table lamps often flicker when the bulb holder is loose.', 'Table lamps', 'u', 'kiwix')
        self.assertEqual(len(rank_passages('My table lamp flickers', [lamps], 200)), 1)

    def test_title_words_count_toward_a_match(self):
        body = Passage('If the bulb flickers, tighten it in its holder.',
                       'Table lamps', 'u', 'kiwix')
        other = Passage('Flickers can come from a loose plug.', 'Spare parts', 'v', 'kiwix')
        rows = rank_passages('My table lamp flickers. What should I check?', [other, body], 200)
        self.assertEqual([r.title for r in rows], ['Table lamps'])  # Without the title, 1 of 4 words.
        self.assertEqual(rows[0].text, body.text)  # The title is matched, not added to the text.

    def test_any_word_search_tops_up_an_empty_all_words_search(self):
        kiwix = FakeKiwix([('Coffee makers', 'coffee maker descale pump'),
                           ('Kettles', 'kettle element')])
        found = Retriever(kiwix=kiwix).kiwix_search('coffee maker leak')
        self.assertEqual([r.title for r in found], ['Coffee makers'])
        self.assertEqual(kiwix.queries, ['coffee maker leak', 'coffee OR maker OR leak'])

    def test_full_or_single_word_searches_skip_the_fallback(self):
        kiwix = FakeKiwix([(f'Kettle {n}', 'kettle') for n in range(6)])
        retriever = Retriever(kiwix=kiwix)
        self.assertEqual(len(retriever.kiwix_search('kettle')), 5)
        self.assertEqual(kiwix.queries, ['kettle'])

    def test_fallback_failure_keeps_earlier_hits(self):
        kiwix = FakeKiwix([('Kettles', 'kettle switch')], or_error=True)
        self.assertEqual([r.title for r in Retriever(kiwix=kiwix).kiwix_search('kettle switch')],
                         ['Kettles'])
        with self.assertRaises(KiwixError):
            Retriever(kiwix=kiwix).kiwix_search('kettle lid')


if __name__ == '__main__':
    unittest.main()
