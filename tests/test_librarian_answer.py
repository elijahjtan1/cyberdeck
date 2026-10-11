import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from core.librarian import answer as answer_module
from core.librarian.answer import (ANSWER_CUE, NOT_FOUND, PROMPT_OVERHEAD, answer, build_messages,
                                   check_citations, copied_share, passage_budget,
                                   service_kiwix_settings)
from core.librarian.ollama import OllamaError, Reply
from core.librarian.rank import Passage
from core.librarian.service import RetrievalError

SONOBUOY = Passage('A passive sonobuoy listens with a hydrophone and radios the sound.',
                   'Sonobuoy', 'http://127.0.0.1:8080/content/wiki/Sonobuoy', 'kiwix', 3.2)
NOTES = Passage('Charge the deck before field use.', 'notes.md', '/api/library/notes.md', 'local', 1.1)


class FakeRetriever:
    def __init__(self, passages=(), warnings=(), error=None):
        self.passages, self.warnings, self.error = list(passages), list(warnings), error
        self.calls = []

    def run(self, question, budget_tokens=1400):
        self.calls.append((question, budget_tokens))
        if self.error:
            raise self.error
        return self.passages, self.warnings


class FakeClient:
    num_ctx, num_predict, model = 2048, 400, 'qwen3:0.6b'

    def __init__(self, text='', error=None):
        self.text, self.error, self.calls = text, error, []

    def iter_chat(self, messages, *, detail=''):
        self.calls.append((messages, detail))
        if self.error:
            raise self.error
        if self.text:
            yield self.text
        yield Reply(self.text, 'qwen3:0.6b', 'stop', 300, 20, 4.0)


class AnswerTest(unittest.TestCase):
    def test_nothing_retrieved_skips_the_model(self):
        client = FakeClient('should not be used')
        result = answer('What is a sonobuoy?', FakeRetriever(warnings=['Kiwix down']), client)
        self.assertEqual(result.text, NOT_FOUND)
        self.assertFalse(result.found)
        self.assertEqual(result.warnings, ['Kiwix down'])
        self.assertIsNone(result.reply)
        self.assertEqual(client.calls, [])

    def test_grounded_answer_with_numbered_sources(self):
        client = FakeClient('It listens with a hydrophone and radios the sound [1].')
        retriever = FakeRetriever([SONOBUOY, NOTES])
        seen = []
        result = answer('How does a passive sonobuoy work?', retriever, client, on_text=seen.append)
        self.assertTrue(result.found)
        self.assertEqual(result.text, 'It listens with a hydrophone and radios the sound [1].')
        self.assertEqual(result.sources, [SONOBUOY, NOTES])
        self.assertEqual(seen, [result.text])
        messages, detail = client.calls[0]
        self.assertEqual(detail, 'How does a passive sonobuoy work?')
        self.assertIn(NOT_FOUND, messages[0]['content'])
        user = messages[1]['content']
        self.assertIn('Source [1]: Sonobuoy\nA passive sonobuoy listens', user)
        self.assertIn('Source [2]: notes.md\nCharge the deck', user)
        self.assertTrue(user.endswith('Question: How does a passive sonobuoy work?\n\n' + ANSWER_CUE))

    def test_model_saying_not_found_is_reported_as_not_found(self):
        for text in ('Not found in the offline sources.', ' not found in the offline sources ', ''):
            result = answer('q', FakeRetriever([NOTES]), FakeClient(text))
            self.assertFalse(result.found)
            self.assertEqual(result.text, NOT_FOUND)

    def test_not_found_with_a_citation_or_decoration_is_still_not_found(self):
        # Told to cite every fact, a small model may cite its not-found sentence too.
        for text in ('Not found in the offline sources. [1]', 'Not found in the offline sources [1].',
                     'Not found in the offline sources. [1][2]', 'Not found in the offline sources. [1]. [2]',
                     '**Not found in the offline sources.**', '"Not found in the offline sources."',
                     'Not found in the offline sources.\n\nThe sources only discuss notes.'):
            result = answer('q', FakeRetriever([NOTES]), FakeClient(text))
            self.assertFalse(result.found, text)
            self.assertEqual((result.text, result.cited), (NOT_FOUND, []))

    def test_not_found_in_the_models_own_words_is_not_found(self):
        # Both from the Pi, for questions the Restarters wiki can't answer.
        for text in ('The sources do not provide information about the torque required for pentalobe '
                     'screwdrivers on an iPhone 12. Therefore, the answer is not found in the offline sources.',
                     'The part number of a toaster heating element is not provided in the sources. [1]',
                     'No information about this is given in the sources [1].'):
            result = answer('q', FakeRetriever([NOTES]), FakeClient(text))
            self.assertFalse(result.found, text)
            self.assertEqual((result.text, result.cited), (NOT_FOUND, []))

    def test_answers_that_mention_missing_details_are_still_answers(self):
        for text in ('The sources do not give a part number, but the element is a nichrome coil [1].',
                     'Charge the deck before use [1]. The sources do not mention the voltage.',
                     'Do not use the deck in rain [1].',
                     'The fuse is not provided with the kit.'):
            result = answer('q', FakeRetriever([NOTES]), FakeClient(text))
            self.assertTrue(result.found, text)

    def test_cited_facts_after_not_found_are_an_answer(self):
        for text in ('Not found in the offline sources. But charge the deck first [1].',
                     'The deck is not found in the offline sources list, but charge it [1].'):
            result = answer('q', FakeRetriever([NOTES]), FakeClient(text))
            self.assertTrue(result.found, text)
            self.assertEqual(result.cited, [1])

    def test_budget_leaves_room_for_prompt_and_answer(self):
        client, retriever = FakeClient('ok [1]'), FakeRetriever([NOTES])
        answer('q', retriever, client, budget_tokens=5000)
        self.assertEqual(retriever.calls[0][1], 2048 - 400 - PROMPT_OVERHEAD)
        answer('q', retriever, client, budget_tokens=600)
        self.assertEqual(retriever.calls[1][1], 600)
        client.num_ctx = 512
        self.assertEqual(passage_budget(client), 0)
        with self.assertRaisesRegex(ValueError, 'num_ctx'):
            answer('q', retriever, client)  # A config error, never a false "not found".

    def test_paragraphs_of_one_article_are_one_source(self):
        more = Passage('It drifts and transmits for hours.', 'Sonobuoy', SONOBUOY.url, 'kiwix', 2.0)
        client = FakeClient('It listens [1] and charges [2], see [3].')
        result = answer('q', FakeRetriever([SONOBUOY, NOTES, more]), client)
        self.assertEqual([p.title for p in result.sources], ['Sonobuoy', 'notes.md'])
        self.assertEqual(result.sources[0].text, f'{SONOBUOY.text}\n\n{more.text}')
        self.assertEqual(result.sources[0].score, SONOBUOY.score)  # The best paragraph's.
        prompt = client.calls[0][0][1]['content']
        self.assertIn('Source [2]: notes.md', prompt)
        self.assertNotIn('[3]', prompt)
        self.assertEqual(result.dropped, [3])

    def test_merged_paragraphs_keep_page_order(self):
        later = Passage('It radios the sound back.', 'Sonobuoy', SONOBUOY.url, 'kiwix', 3.0, order=7)
        earlier = Passage('A sonobuoy is a small buoy.', 'Sonobuoy', SONOBUOY.url, 'kiwix', 1.0, order=2)
        result = answer('q', FakeRetriever([later, earlier]), FakeClient('ok [1]'))
        self.assertEqual(result.sources[0].text, f'{earlier.text}\n\n{later.text}')
        self.assertEqual(result.sources[0].score, 3.0)

    def test_budget_from_environment(self):
        retriever = FakeRetriever([NOTES])
        with mock.patch.dict(os.environ, {'CYBERDECK_LIBRARIAN_BUDGET': '5000'}):
            answer('q', retriever, FakeClient('ok [1]'))
        with mock.patch.dict(os.environ, {}, clear=True):
            answer('q', retriever, FakeClient('ok [1]'))
        self.assertEqual([c[1] for c in retriever.calls], [1398, 400])  # 5000 capped by the context.
        for bad in ('lots', '50'):
            with mock.patch.dict(os.environ, {'CYBERDECK_LIBRARIAN_BUDGET': bad}):
                with self.assertRaisesRegex(ValueError, 'CYBERDECK_LIBRARIAN_BUDGET'):
                    answer('q', retriever, FakeClient('ok [1]'))

    def test_failures_are_errors_not_absence(self):
        with self.assertRaises(RetrievalError):
            answer('q', FakeRetriever(error=RetrievalError('Kiwix is unavailable')), FakeClient())
        with self.assertRaises(OllamaError):
            answer('q', FakeRetriever([NOTES]), FakeClient(error=OllamaError('Ollama is down')))

    def test_citations_to_missing_sources_are_dropped(self):
        client = FakeClient('It uses a hydrophone [1]. It also flies [4]. Charge it [2, 9].')
        result = answer('q', FakeRetriever([SONOBUOY, NOTES]), client)
        self.assertEqual(result.text, 'It uses a hydrophone [1]. It also flies. Charge it [2].')
        self.assertEqual((result.cited, result.dropped), ([1, 2], [4, 9]))
        self.assertFalse(result.uncited)

    def test_answer_without_citations_is_flagged(self):
        result = answer('q', FakeRetriever([SONOBUOY]), FakeClient('It floats [3].'))
        self.assertTrue(result.found)
        self.assertTrue(result.uncited)
        self.assertEqual((result.text, result.cited, result.dropped), ('It floats.', [], [3]))
        # Not-found answers are not "uncited"; there is nothing to verify.
        self.assertFalse(answer('q', FakeRetriever(), FakeClient()).uncited)

    def test_check_citations_edge_cases(self):
        self.assertEqual(check_citations('See [note], [0] and [2].', 2),
                         ('See [note], and [2].', [2], [0]))
        self.assertEqual(check_citations('A [2,1, 2]. B [1].', 2), ('A [2, 1]. B [1].', [2, 1], []))
        self.assertEqual(check_citations('plain', 3), ('plain', [], []))

    def test_service_kiwix_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            unit = Path(tmp) / 'cyberdeck-core.service'
            unit.write_text('[Service]\nEnvironment=CYBERDECK_KIWIX_URL=http://127.0.0.1:8080\n'
                            'Environment=CYBERDECK_KIWIX_BOOK=restarters\nEnvironment=OTHER=x\n')
            self.assertEqual(service_kiwix_settings(unit),
                             {'CYBERDECK_KIWIX_URL': 'http://127.0.0.1:8080',
                              'CYBERDECK_KIWIX_BOOK': 'restarters'})
            self.assertEqual(service_kiwix_settings(Path(tmp) / 'missing'), {})

    def test_command_borrows_service_settings_and_explains(self):
        retriever = FakeRetriever([SONOBUOY])
        retriever.kiwix = None
        out, err = io.StringIO(), io.StringIO()
        saved = {'CYBERDECK_KIWIX_URL': 'http://127.0.0.1:8080'}
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(answer_module, 'service_kiwix_settings', return_value=saved), \
                mock.patch('core.librarian.service.Retriever.from_environment', return_value=retriever), \
                mock.patch('core.librarian.ollama.OllamaClient.from_environment',
                           return_value=FakeClient('Hydrophone [1].')), \
                mock.patch('sys.stdout', out), mock.patch('sys.stderr', err):
            self.assertEqual(answer_module.main(['What is a sonobuoy?', '--explain']), 0)
            self.assertEqual(os.environ['CYBERDECK_KIWIX_URL'], 'http://127.0.0.1:8080')
        self.assertIn("service's Kiwix settings", err.getvalue())
        self.assertIn('keywords: sonobuoy', err.getvalue())
        self.assertIn('[1] score 3.20 Sonobuoy', err.getvalue())
        self.assertIn('Sources:\n[1] Sonobuoy', out.getvalue())

    def test_command_warns_when_kiwix_is_off(self):
        err = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(answer_module, 'service_kiwix_settings', return_value={}), \
                mock.patch('core.librarian.service.Retriever.from_environment',
                           return_value=FakeRetriever()), \
                mock.patch('core.librarian.ollama.OllamaClient.from_environment',
                           return_value=FakeClient()), \
                mock.patch('sys.stdout', io.StringIO()), mock.patch('sys.stderr', err):
            answer_module.main(['q'])
        self.assertIn('Kiwix is off', err.getvalue())

    def test_messages_shape(self):
        messages = build_messages('  Why?  ', [NOTES])
        self.assertEqual([m['role'] for m in messages], ['system', 'user'])
        self.assertIn('Question: Why?\n\n', messages[1]['content'])
        self.assertTrue(messages[1]['content'].endswith(NOT_FOUND))  # The last thing it reads.

    def test_copied_share_spots_pasted_sources(self):
        lamp = Passage('Seals can leak, and this is often more problematic. Cleaning might resolve '
                       'the problem, or there might occasionally be an O-ring you can replace.',
                       'Mixers', 'http://k/Mixers', 'kiwix')
        pasted = ('[1] Mixers\nSeals can leak, and this is often more problematic. Cleaning '
                  'might resolve the problem, or there might occasionally be an O-ring you can replace.')
        own = 'A worn seal is the usual cause [1]. Cleaning it may help, or replace the O-ring [1].'
        self.assertGreater(copied_share(pasted, [lamp]), .9)
        self.assertEqual(copied_share(own, [lamp]), 0.0)
        self.assertEqual(copied_share('Too short [1].', [lamp]), 0.0)


if __name__ == '__main__':
    unittest.main()
