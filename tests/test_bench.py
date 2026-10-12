import io
import json
from pathlib import Path
import tempfile
import unittest

from core.librarian import bench
from core.librarian.rank import Passage
from tests.test_librarian_answer import FakeClient, FakeRetriever


def passage(title, score=1.0):
    return Passage(f'{title} text', title, f'http://k/{title}', 'kiwix', score)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        self.now += 0.5
        return self.now


class TitleRetriever(FakeRetriever):
    """Returns passages for questions it knows, nothing otherwise."""

    def __init__(self, table):
        super().__init__()
        self.table = table

    def run(self, question, budget_tokens=1400):
        return [passage(t) for t in self.table.get(question, [])], []


class BenchTest(unittest.TestCase):
    def test_rank_ignores_case_and_accents(self):
        titles = ['Coffee makers', 'Glossary:Repair Café', 'Table lamps']
        self.assertEqual(bench.first_match(titles, ['repair cafe']), 2)
        self.assertEqual(bench.first_match(titles, ['Kettle']), 0)

    def test_rank_matches_whole_words(self):
        self.assertEqual(bench.first_match(['Clamp meter', 'Table lamps'], ['Lamp']), 2)
        self.assertEqual(bench.first_match(['Diffuser', 'Fuse (electrical)'], ['Fuse']), 2)
        self.assertEqual(bench.first_match(['Electric kettles'], ['Kettle']), 1)  # Plural.
        self.assertEqual(bench.first_match(['Headphones'], ['Headphone']), 1)
        self.assertEqual(bench.first_match(['Hair dryers'], ['Hair dryer']), 1)
        self.assertEqual(bench.first_match(['Phonograph', 'Smartphone'], ['Phone']), 0)

    def test_run_one_with_model(self):
        retriever = FakeRetriever([passage('Glossary:Repair Café'), passage('Glossary:Repair Café'),
                                   passage('Table lamps')])
        r = bench.run_one('What is a repair cafe?', ['Repair Café'], retriever,
                          FakeClient('Volunteers fix things [1].'), clock=Clock())
        self.assertEqual(r.titles, ['Glossary:Repair Café', 'Table lamps'])  # Deduplicated.
        self.assertEqual((r.rank, r.top3, r.found, r.uncited, r.error), (1, True, True, False, ''))
        self.assertEqual((r.retrieval_s, r.first_word_s, r.total_s), (0.5, 1.0, 1.5))
        self.assertEqual(r.prompt_tokens, 300)  # From the model's reply.
        self.assertEqual(bench.summarize([r])['max_prompt_tokens'], 300)

    def test_saying_the_sources_lack_it_first_is_honest(self):
        # From the Pi: honest about the gap, then something related.
        said = ('The part number of a toaster heating element is not mentioned in the provided '
                'sources. However, each section of the element should read a few tens of ohms [1].')
        r = bench.run_one('q', [], FakeRetriever([passage('Toasters')]), FakeClient(said), clock=Clock())
        self.assertEqual((r.found, r.says_missing, r.honest), (True, True, True))
        guess = bench.run_one('q', [], FakeRetriever([passage('Toasters')]),
                              FakeClient('Use a 0.1 Nm setting [1]. The sources do not say.'), clock=Clock())
        self.assertEqual((guess.says_missing, guess.honest), (False, False))
        # Found in review: a guess beside or after the gap is not honest.
        for text in ('The fuse rating is 10 A [1], though the sources do not mention the brand.',
                     'The sources do not specify the exact part number, but it is 1234.',
                     "The sources don't list the voltage. It is 230 V."):
            self.assertFalse(bench.run_one('q', [], FakeRetriever([passage('Toasters')]),
                                           FakeClient(text), clock=Clock()).honest, text)
        out = io.StringIO()
        bench.show_result(1, r, out)
        bench.show_result(2, guess, out)
        self.assertIn('1. honest: says the sources lack it', out.getvalue())
        self.assertIn('2. ANSWERED an out-of-scope question', out.getvalue())
        self.assertEqual(bench.summarize([r, guess])['honest_not_found_rate'], 0.5)
        partial = bench.run_one('q', [], FakeRetriever([passage('Toasters')]),
                                FakeClient('Check the element [1]. Not found in the offline sources.'),
                                clock=Clock())
        self.assertEqual((partial.found, partial.honest), (True, True))

    def test_answers_that_paste_the_sources_back_are_marked_copied(self):
        source = Passage('Seals can leak, and this is often more problematic than a worn hose.',
                         'Coffee makers', 'http://k/Coffee_makers', 'kiwix', 1.0)
        pasted = bench.run_one('q', ['Coffee maker'], FakeRetriever([source]),
                               FakeClient('[1] Coffee makers\n' + source.text), clock=Clock())
        own = bench.run_one('q', ['Coffee maker'], FakeRetriever([source]),
                            FakeClient('A worn seal is the usual cause [1].'), clock=Clock())
        self.assertEqual((pasted.copied, own.copied), (True, False))
        self.assertEqual(bench.summarize([pasted, own])['copied_rate'], 0.5)
        out = io.StringIO()
        bench.show_result(1, pasted, out)
        self.assertIn('COPIED', out.getvalue())

    def test_retrieval_only_and_errors(self):
        client = FakeClient('unused')
        r = bench.run_one('q', ['Kettle'], FakeRetriever([passage('Kettles')]), client,
                          use_model=False, clock=Clock())
        self.assertEqual((r.rank, r.first_word_s, client.calls), (1, None, []))
        summary = bench.summarize([r])
        self.assertEqual((summary['top3_hit_rate'], summary['answered_rate']), (1.0, None))
        r = bench.run_one('q', ['Kettle'], FakeRetriever(error=RuntimeError('Kiwix down')),
                          client, clock=Clock())
        self.assertEqual(r.error, 'Kiwix down')
        self.assertFalse(r.top3)

    def test_budget_reaches_retrieval(self):
        retriever = FakeRetriever([passage('Kettles')])
        bench.run_one('q', ['Kettle'], retriever, FakeClient('A [1].'), budget=700, clock=Clock())
        bench.run_one('q', ['Kettle'], retriever, FakeClient(), use_model=False, budget=500,
                      clock=Clock())
        self.assertEqual([c[1] for c in retriever.calls], [700, 500])

    def test_summary_scores_retrieval_and_honesty(self):
        retriever = TitleRetriever({'cafe?': ['Glossary:Repair Café'],
                                    'kettle?': ['Toasters', 'Coffee makers', 'Irons', 'Kettles'],
                                    'world cup?': ['Football']})
        questions = [('cafe?', ['Repair Café']), ('kettle?', ['Kettle']),
                     ('peru?', []), ('world cup?', [])]
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'bench.json'
            summary = bench.run(questions, retriever, FakeClient('An answer [1].'), out=out,
                                json_path=str(path))
            saved = json.loads(path.read_text())
        self.assertEqual(summary['top3_hit_rate'], 0.5)  # Kettles ranked 4th.
        self.assertEqual(summary['answered_rate'], 1.0)
        self.assertEqual(summary['honest_not_found_rate'], 0.5)  # Answered the World Cup one.
        self.assertEqual(saved['summary'], summary)
        self.assertEqual(len(saved['results']), 4)
        text = out.getvalue()
        self.assertIn('top-3 hit (#1)', text)
        self.assertIn('hit #4', text)
        self.assertIn('got: Toasters, Coffee makers, Irons', text)
        self.assertIn('honest: not found', text)
        self.assertIn('ANSWERED an out-of-scope question', text)
        self.assertIn('    said: An answer [1].', text)  # Shown only for answers to check.
        self.assertEqual(text.count('    said:'), 1)
        self.assertEqual(saved['results'][0]['answer'], 'An answer [1].')
        self.assertIn('Top-3 hit rate:        50%', text)

    def test_memory_watch_reads_proc(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp)
            (proc / 'meminfo').write_text('MemTotal: 3900000 kB\nMemAvailable: 2048000 kB\n')
            for pid, comm, rss in (('10', 'ollama\n', 400 * 1024), ('11', 'bash\n', 9 * 1024),
                                   ('12', 'ollama_llama_se\n', 100 * 1024),
                                   ('13', 'llama-server\n', 800 * 1024)):
                (proc / pid).mkdir()
                (proc / pid / 'comm').write_text(comm)
                (proc / pid / 'status').write_text(f'Name: x\nVmRSS: {rss} kB\n')
            (proc / 'self').mkdir()
            watch = bench.MemoryWatch(proc=proc)
            watch.sample()
            self.assertEqual((watch.min_available_mib, watch.max_ollama_mib), (2000, 1300))
            missing = bench.MemoryWatch(proc=proc / 'nope')
            with missing:
                pass
            self.assertIsNone(missing.min_available_mib)

    def test_default_questions_load(self):
        questions = bench.load_questions()
        self.assertGreaterEqual(len(questions), 20)
        self.assertTrue(any(not expect for _, expect in questions))  # Honesty checks included.
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / 'bad.json'
            bad.write_text('[{"expect": []}]')
            with self.assertRaises(ValueError):
                bench.load_questions(bad)


if __name__ == '__main__':
    unittest.main()
