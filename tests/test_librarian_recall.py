"""Recall fixes found with `deck bench` on the Pi's Restarters archive."""
import unittest

from core.librarian.extract import join_short, paragraphs
from core.librarian.kiwix import KiwixError, SearchResult
from core.librarian.query import keywords, match_key
from core.librarian.rank import Passage, rank_passages
from core.librarian.service import Retriever


class FakeKiwix:
    """kiwix-serve style: every word of the query must appear, "OR" included."""

    def __init__(self, articles, fail_after=None):
        self.articles, self.fail_after, self.queries = articles, fail_after, []

    def search(self, query, limit=5):
        self.queries.append(query)
        if self.fail_after is not None and len(self.queries) > self.fail_after:
            raise KiwixError('Kiwix is unavailable')
        rows = [SearchResult(title, f'http://k/{title}', '') for title, text in self.articles
                if all(w in text.split() for w in query.split())]
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

    def test_word_pairs_find_what_the_full_question_misses(self):
        kiwix = FakeKiwix([('Table lamps', 'table lamp bulb holder'),
                           ('Light bulbs', 'lamp flickers bulb')])
        found = Retriever(kiwix=kiwix).kiwix_search('table lamp flickers check')
        self.assertEqual([r.title for r in found], ['Table lamps', 'Light bulbs'])
        self.assertEqual(kiwix.queries, ['table lamp flickers check', 'table lamp', 'lamp flickers',
                                         'flickers check', 'table', 'lamp', 'flickers', 'check'])

    def test_one_broad_pair_does_not_take_every_slot(self):
        # "test fuse" matches many articles; "fuse plug" still gets its best one in.
        articles = [(f'Testing {n}', 'test fuse') for n in range(8)]
        kiwix = FakeKiwix(articles + [('Glossary:Fuse', 'fuse plug')])
        found = [r.title for r in Retriever(kiwix=kiwix).kiwix_search('test fuse plug')]
        self.assertEqual(found, ['Testing 0', 'Glossary:Fuse', 'Testing 1', 'Testing 2', 'Testing 3'])
        self.assertEqual(kiwix.queries, ['test fuse plug', 'test fuse', 'fuse plug'])

    def test_translated_pages_do_not_take_slots(self):
        kiwix = FakeKiwix([('Table lamps/fr', 'lamp'), ('Table lamps/en', 'lamp'),
                           ('Table lamps', 'lamp'), ('Table lamps/nl', 'lamp'),
                           ('Glossary:AC/DC', 'lamp')])
        found = Retriever(kiwix=kiwix).kiwix_search('lamp')
        self.assertEqual([r.title for r in found], ['Table lamps/en', 'Glossary:AC/DC'])

    def test_enough_results_skip_the_fallback(self):
        kiwix = FakeKiwix([(f'Kettle {n}', 'kettle switch') for n in range(6)])
        self.assertEqual(len(Retriever(kiwix=kiwix).kiwix_search('kettle switch')), 5)
        self.assertEqual(kiwix.queries, ['kettle switch'])

    def test_fallback_failure_keeps_earlier_hits(self):
        kiwix = FakeKiwix([('Kettles', 'kettle switch')], fail_after=1)
        self.assertEqual([r.title for r in Retriever(kiwix=kiwix).kiwix_search('kettle switch')],
                         ['Kettles'])
        kiwix = FakeKiwix([('Kettles', 'kettle')], fail_after=0)
        with self.assertRaises(KiwixError):
            Retriever(kiwix=kiwix).kiwix_search('kettle lid')
        self.assertEqual(len(kiwix.queries), 1)  # Down: no fallback searches.


if __name__ == '__main__':
    unittest.main()


class PiTraceTest(unittest.TestCase):
    """Shapes from the Pi trace of the bench misses (Restarters archive, 400-token budget)."""

    def test_title_match_beats_a_passing_mention(self):
        # "How to use a multimeter" was Kiwix's third result but ranked behind Toasters.
        toaster = Passage('Unplug it and check the timer board. Check the switch contacts with a '
                          'multimeter on its continuity range; use the lowest range if it has no beeper.',
                          'Toasters', 't', 'kiwix')
        howto = Passage('A continuity range is very useful. It beeps when the probes find a low '
                        'resistance path, so you can check a cable without looking at the display.',
                        'How to use a multimeter', 'h', 'kiwix')
        filler = [Passage(f'Paragraph {n} about kettles, lids and descaling.', f'Filler {n}', f'f{n}', 'kiwix')
                  for n in range(8)]
        rows = rank_passages('How do I use a multimeter to check continuity?', [toaster, howto] + filler, 400)
        self.assertEqual([r.title for r in rows], ['How to use a multimeter', 'Toasters'])

    def test_short_paragraphs_join_the_next(self):
        # Coffee makers: seven one-liners (20-66 characters) took all the model's sources.
        rows = ['Coffee makers can often be fixed.', 'Coffee makers come in 3 separate types:',
                'Filter coffee makers', 'Those using capsules or pods',
                'Espresso machines, similar to those in cafes.', 'L' * 300, 'Last short line.']
        joined = join_short(rows)
        self.assertEqual(len(joined), 1)
        self.assertTrue(joined[0].startswith('Coffee makers can often be fixed. Coffee makers come'))
        self.assertIn('cafes. ' + 'L' * 300, joined[0])  # A run of short ones joins the next long one.
        self.assertTrue(joined[0].endswith('L' * 300 + ' Last short line.'))  # A short tail joins the last.
        self.assertEqual(join_short(['A' * 250, 'B' * 250]), ['A' * 250, 'B' * 250])
        self.assertEqual(join_short(['A' * 250, 'tail']), ['A' * 250 + ' tail'])
        self.assertEqual(join_short([]), [])

    def test_link_lists_are_dropped(self):
        html = ('<ul><li><a href="Spare_parts">Spare parts</a></li>'
                '<li><a href="Case_Studies">Case Studies</a> - our favourite famous fixes</li></ul>'
                '<p>Check the <a href="Fuse">fuse</a> in the plug first.</p>')
        self.assertEqual(paragraphs(html), ['Case Studies - our favourite famous fixes',
                                            'Check the fuse in the plug first.'])

    def test_main_page_takes_no_slot(self):
        kiwix = FakeKiwix([('Main Page/nl', 'coffee maker'), ('Main Page', 'coffee maker'),
                           ('Main Page/en', 'coffee maker'), ('Coffee makers', 'coffee maker')])
        found = Retriever(kiwix=kiwix).kiwix_search('coffee maker')
        self.assertEqual([r.title for r in found], ['Coffee makers'])
