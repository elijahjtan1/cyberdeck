import io
import json
import unittest
from unittest import mock
from urllib.error import HTTPError, URLError

from core import cli
from core.librarian.ollama import OllamaError
from tests.test_librarian_answer import NOTES, SONOBUOY, FakeClient, FakeRetriever

try:
    from flask import Flask
    from core.librarian.api import create_blueprint
except ImportError:
    Flask = None


def app_for(retriever, client):
    app = Flask('test')
    app.register_blueprint(create_blueprint(retriever=retriever, client=client))
    return app.test_client()


@unittest.skipIf(Flask is None, 'Flask is not installed')
class AskApiTest(unittest.TestCase):
    def test_json_answer(self):
        web = app_for(FakeRetriever([SONOBUOY, NOTES]), FakeClient('Hydrophone [1]. Wings [7].'))
        data = web.get('/api/ask?q=How+does+a+sonobuoy+work').get_json()
        self.assertEqual(data['answer'], 'Hydrophone [1]. Wings.')
        self.assertEqual((data['found'], data['uncited'], data['cited'], data['dropped']),
                         (True, False, [1], [7]))
        self.assertEqual([s['n'] for s in data['sources']], [1, 2])
        self.assertEqual(data['sources'][0]['title'], 'Sonobuoy')
        self.assertEqual((data['model'], data['seconds']), ('qwen3:0.6b', 4.0))

    def test_not_found_skips_model(self):
        client = FakeClient('unused')
        data = app_for(FakeRetriever(), client).get('/api/ask?q=unknown').get_json()
        self.assertEqual((data['found'], data['answer'], data['model']),
                         (False, 'Not found in the offline sources.', None))
        self.assertEqual(client.calls, [])

    def test_stream(self):
        web = app_for(FakeRetriever([SONOBUOY]), FakeClient('Hydrophone [1].'))
        response = web.get('/api/ask?q=sonobuoy&stream=1')
        self.assertEqual(response.mimetype, 'application/x-ndjson')
        rows = [json.loads(line) for line in response.get_data(as_text=True).splitlines()]
        self.assertEqual([r['event'] for r in rows], ['passages', 'text', 'done'])
        self.assertEqual(rows[0]['keywords'], ['sonobuoy'])
        self.assertEqual(rows[0]['model'], 'qwen3:0.6b')
        self.assertEqual(rows[1]['text'], 'Hydrophone [1].')
        self.assertTrue(rows[2]['found'])

    def test_errors_are_not_not_found(self):
        web = app_for(FakeRetriever([SONOBUOY]), FakeClient(error=OllamaError('Ollama is down')))
        response = web.get('/api/ask?q=sonobuoy')
        self.assertEqual((response.status_code, response.get_json()['error']), (503, 'Ollama is down'))
        rows = web.get('/api/ask?q=sonobuoy&stream=1').get_data(as_text=True).splitlines()
        self.assertEqual(json.loads(rows[-1]), {'event': 'error', 'error': 'Ollama is down'})
        self.assertEqual(web.get('/api/ask?q=').status_code, 400)

    def test_one_question_at_a_time_and_lock_released(self):
        web = app_for(FakeRetriever([SONOBUOY]), FakeClient('Hydrophone [1].'))
        response = web.get('/api/ask?q=sonobuoy&stream=1', buffered=False)
        busy = web.get('/api/ask?q=sonobuoy')
        self.assertEqual(busy.status_code, 503)
        self.assertIn('Another question', busy.get_json()['error'])
        response.close()  # Client left before reading anything.
        self.assertEqual(web.get('/api/ask?q=sonobuoy').status_code, 200)


class FakeResponse(io.BytesIO):
    pass


class FakeOpener:
    def __init__(self, body=b'', error=None):
        self.body, self.error, self.urls = body, error, []

    def open(self, url, timeout):
        self.urls.append(url)
        if self.error:
            raise self.error
        return FakeResponse(self.body)


def ndjson(*rows):
    return b''.join(json.dumps(r).encode() + b'\n' for r in rows)


DONE = {'event': 'done', 'found': True, 'uncited': False, 'cited': [1], 'dropped': [],
        'warnings': [], 'sources': [{'n': 1, 'title': 'Sonobuoy', 'source': 'kiwix',
                                     'url': 'http://k/Sonobuoy', 'score': 3.2}]}
PASSAGES = {'event': 'passages', 'keywords': ['sonobuoy'], 'model': 'qwen3:0.6b', 'warnings': [],
            'passages': [{'n': 1, 'title': 'Sonobuoy', 'score': 3.2}]}


class DeckCommandTest(unittest.TestCase):
    def run_show(self, events, explain=False):
        out, err = io.StringIO(), io.StringIO()
        status = cli.show(iter(events), explain, out, err)
        return status, out.getvalue(), err.getvalue()

    def test_backend_events(self):
        opener = FakeOpener(ndjson(PASSAGES, {'event': 'text', 'text': 'Hi [1].'}, DONE))
        rows = list(cli.backend_events('what is it?', 'http://127.0.0.1:8765', opener))
        self.assertEqual([r['event'] for r in rows], ['passages', 'text', 'done'])
        self.assertIn('/api/ask?q=what+is+it%3F&stream=1', opener.urls[0])

    def test_backend_missing_or_old(self):
        for error in (URLError(ConnectionRefusedError()),
                      HTTPError('u', 404, 'Not Found', {}, io.BytesIO(b''))):
            with self.assertRaises(cli.BackendMissing):
                next(cli.backend_events('q', 'http://127.0.0.1:8765', FakeOpener(error=error)))

    def test_backend_busy_is_an_error_event(self):
        error = HTTPError('u', 503, 'Busy', {}, io.BytesIO(b'{"error":"Another question is being answered"}'))
        rows = list(cli.backend_events('q', 'http://127.0.0.1:8765', FakeOpener(error=error)))
        self.assertEqual(rows, [{'event': 'error', 'error': 'Another question is being answered'}])

    def test_show_answer(self):
        status, out, err = self.run_show([PASSAGES, {'event': 'text', 'text': 'Hi [1].'}, DONE], True)
        self.assertEqual(status, 0)
        self.assertEqual(out, 'Hi [1].\nSources:\n[1] Sonobuoy (kiwix) http://k/Sonobuoy\n')
        self.assertIn('Reading 1 passage(s) with qwen3:0.6b...', err)
        self.assertIn('[1] score 3.20 Sonobuoy', err)

    def test_unknown_citations_never_appear_while_streaming(self):
        pieces = ['Buoys listen', ' [', '4', ']. They', ' float [1', ', 9', ']', '.']
        events = [PASSAGES, *({'event': 'text', 'text': t} for t in pieces), dict(DONE, dropped=[4, 9])]
        err = io.StringIO()

        class Recorder(io.StringIO):
            def write(self, text):
                self.seen = getattr(self, 'seen', '') + text
                return super().write(text)

        out = Recorder()
        cli.show(iter(events), False, out, err)
        streamed = out.seen.split('\n')[0]
        self.assertEqual(streamed, 'Buoys listen. They float [1].')
        self.assertIn('Removed citations to sources that were not given: [4], [9]', out.seen)

    def test_show_not_found_and_unverified(self):
        empty = dict(PASSAGES, passages=[])
        done = dict(DONE, found=False, cited=[], sources=[])
        status, out, err = self.run_show([empty, done])
        self.assertEqual((status, out), (0, 'Not found in the offline sources.\n'))
        self.assertNotIn('Reading', err)
        done = dict(DONE, uncited=True, cited=[], dropped=[4])
        _, out, _ = self.run_show([PASSAGES, {'event': 'text', 'text': 'Guess.'}, done])
        self.assertIn('Removed citations to sources that were not given: [4]', out)
        self.assertIn('UNVERIFIED', out)
        self.assertIn('Searched:\n[1] Sonobuoy', out)
        self.assertNotIn('QUOTED', out)
        _, out, _ = self.run_show([PASSAGES, {'event': 'text', 'text': 'Pasted [1].'}, dict(DONE, copied=True)])
        self.assertIn('QUOTED: mostly copied word for word from the sources.', out)

    def test_show_errors_and_ctrl_c(self):
        status, _, err = self.run_show([PASSAGES, {'event': 'error', 'error': 'Ollama is down'}])
        self.assertEqual((status, err.strip().splitlines()[-1]), (1, 'error: Ollama is down'))
        self.assertEqual(self.run_show([PASSAGES])[0], 1)  # Stream ended without done.

        closed = []

        def interrupted():
            try:
                yield PASSAGES
                raise KeyboardInterrupt
            finally:
                closed.append(True)

        status, _, err = self.run_show(interrupted())
        self.assertEqual(status, 130)
        self.assertIn('Stopped.', err)
        self.assertNotIn('Traceback', err)
        self.assertEqual(closed, [True])

    def test_falls_back_to_local_when_backend_is_down(self):
        events = cli.backend_events('q', 'http://127.0.0.1:8765', FakeOpener(error=URLError('refused')))
        err = io.StringIO()
        with mock.patch.object(cli, 'backend_events', return_value=events), \
                mock.patch.object(cli, 'ask_local', return_value=0) as local, \
                mock.patch('sys.stderr', err):
            self.assertEqual(cli.main(['ask', 'what', 'is', 'it?']), 0)
        local.assert_called_once_with('what is it?', False)
        self.assertIn('Answering here because the backend is not running', err.getvalue())

    def test_local_events_match_backend_shape(self):
        rows = list(cli.local_events('sonobuoy', FakeRetriever([SONOBUOY]), FakeClient('Hi [1].')))
        self.assertEqual([r['event'] for r in rows], ['passages', 'text', 'done'])
        self.assertEqual(rows[0]['passages'][0]['n'], 1)
        rows = list(cli.local_events('q', FakeRetriever([SONOBUOY]), FakeClient(error=OllamaError('down'))))
        self.assertEqual(rows[-1], {'event': 'error', 'error': 'down'})


if __name__ == '__main__':
    unittest.main()
