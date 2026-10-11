import unittest

from core.librarian.answer import (NOT_FOUND, PROMPT_OVERHEAD, answer, build_messages,
                                   passage_budget)
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
    num_ctx, num_predict = 2048, 400

    def __init__(self, text='', error=None):
        self.text, self.error, self.calls = text, error, []

    def chat(self, messages, *, on_text=None, detail=''):
        self.calls.append((messages, detail))
        if self.error:
            raise self.error
        if on_text:
            on_text(self.text)
        return Reply(self.text, 'qwen3:0.6b', 'stop', 300, 20, 4.0)


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
        self.assertIn('[1] Sonobuoy\nA passive sonobuoy listens', user)
        self.assertIn('[2] notes.md\nCharge the deck', user)
        self.assertTrue(user.endswith('Question: How does a passive sonobuoy work?'))

    def test_model_saying_not_found_is_reported_as_not_found(self):
        for text in ('Not found in the offline sources.', ' not found in the offline sources ', ''):
            result = answer('q', FakeRetriever([NOTES]), FakeClient(text))
            self.assertFalse(result.found)
            self.assertEqual(result.text, NOT_FOUND)

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

    def test_failures_are_errors_not_absence(self):
        with self.assertRaises(RetrievalError):
            answer('q', FakeRetriever(error=RetrievalError('Kiwix is unavailable')), FakeClient())
        with self.assertRaises(OllamaError):
            answer('q', FakeRetriever([NOTES]), FakeClient(error=OllamaError('Ollama is down')))

    def test_messages_shape(self):
        messages = build_messages('  Why?  ', [NOTES])
        self.assertEqual([m['role'] for m in messages], ['system', 'user'])
        self.assertTrue(messages[1]['content'].endswith('Question: Why?'))


if __name__ == '__main__':
    unittest.main()
