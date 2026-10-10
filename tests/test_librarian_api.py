from contextlib import contextmanager
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import URLError
from urllib.parse import urlsplit

from core.librarian import retrieve, Passage
from core.librarian.kiwix import KiwixClient
from core.librarian.library import LocalLibrary
from core.librarian.service import Retriever, RetrievalError

try:
    from flask import Flask
    from core.librarian.api import create_blueprint
except ImportError:
    Flask = None

FIXTURES = Path(__file__).parent / 'fixtures'


class FakeOpener:
    def __init__(self, down=False, article_down=False):
        self.down, self.article_down, self.calls = down, article_down, []

    def open(self, request, timeout):
        path = urlsplit(request.full_url).path
        self.calls.append((path, timeout))
        if self.down or (self.article_down and path != '/search'):
            raise URLError('stopped')
        name = 'kiwix-search.xml' if path == '/search' else 'sonobuoy.html'
        return io.BytesIO((FIXTURES / name).read_bytes())


class Telemetry:
    def __init__(self):
        self.events = []
        self.active = False

    @contextmanager
    def activity(self, kind, query):
        self.events.append((kind, query))
        self.active = True
        try:
            yield
        finally:
            self.active = False


class RetrievalTest(unittest.TestCase):
    def test_sonobuoy_end_to_end_and_budget(self):
        opener = FakeOpener()
        telemetry = Telemetry()
        client = KiwixClient('http://localhost:8080', opener)
        retriever = Retriever(client, telemetry=telemetry)
        rows = retriever.retrieve('How does a passive sonobuoy work?', 120)
        self.assertTrue(rows)
        self.assertTrue(all(isinstance(row, Passage) for row in rows))
        self.assertIn('hydrophone', rows[0].text)
        self.assertTrue(rows[0].url.startswith('http://localhost:8080/content/'))
        self.assertLessEqual(sum((len(row.text) + 3) // 4 for row in rows), 120)
        self.assertEqual(telemetry.events, [('search', 'How does a passive sonobuoy work?')])
        self.assertFalse(telemetry.active)
        self.assertTrue(all(timeout == 2 for _, timeout in opener.calls))
        with patch('core.librarian.service.Retriever.from_environment', return_value=retriever):
            self.assertTrue(retrieve('passive sonobuoy'))

    def test_empty_and_unavailable_are_distinct(self):
        self.assertEqual(Retriever().retrieve('sonobuoy'), [])
        client = KiwixClient('http://localhost:8080', FakeOpener())
        self.assertEqual(Retriever(client).retrieve('unicorn quasar'), [])
        self.assertEqual(Retriever(client).retrieve('why is this?'), [])
        self.assertEqual(Retriever(client).retrieve('sonobuoy', 0), [])
        for opener in (FakeOpener(down=True), FakeOpener(article_down=True)):
            telemetry = Telemetry()
            retriever = Retriever(KiwixClient('http://localhost:8080', opener), telemetry=telemetry)
            with self.assertRaisesRegex(RetrievalError, 'unavailable'):
                retriever.retrieve('sonobuoy')
            self.assertFalse(telemetry.active)

    def test_local_results_survive_kiwix_down(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'library'
            root.mkdir()
            (root / 'field.txt').write_text('A passive sonobuoy listens through a hydrophone.')
            library = LocalLibrary(root)
            library.reindex()
            retriever = Retriever(KiwixClient('http://localhost:8080', FakeOpener(down=True)), library)
            rows, errors = retriever.run('passive sonobuoy')
            self.assertEqual(rows[0].source, 'local')
            self.assertIn('unavailable', errors[0])


@unittest.skipIf(Flask is None, 'Flask not installed')
class ApiTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        root = Path(self.folder.name) / 'library'
        root.mkdir()
        (root / 'field notes.html').write_text('<script>bad()</script><p>A passive sonobuoy uses a hydrophone.</p>')
        self.library = LocalLibrary(root)
        self.library.reindex()
        self.opener = FakeOpener()
        self.retriever = Retriever(KiwixClient('http://localhost:8080', self.opener), self.library)
        app = Flask(__name__)
        app.register_blueprint(create_blueprint(retriever=self.retriever))
        self.client = app.test_client()

    def tearDown(self):
        self.folder.cleanup()

    def test_search_retrieve_and_working_local_url(self):
        response = self.client.get('/api/search?q=How+does+a+passive+sonobuoy+work?')
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data['keywords'], ['passive', 'sonobuoy'])
        self.assertEqual({r['source'] for r in data['results']}, {'kiwix', 'local'})
        local = next(row for row in data['results'] if row['source'] == 'local')
        page = self.client.get(local['url'])
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.mimetype, 'text/plain')
        self.assertEqual(page.headers['X-Content-Type-Options'], 'nosniff')
        data = self.client.get('/api/retrieve?q=passive+sonobuoy&budget_tokens=80').get_json()
        self.assertTrue(data['results'])
        self.assertLessEqual(sum((len(p['text']) + 3) // 4 for p in data['results']), 80)

    def test_validation_down_and_partial_results(self):
        for url in ('/api/search', '/api/search?q=' + 'x' * 1001,
                    '/api/retrieve?q=x&budget_tokens=-1', '/api/retrieve?q=x&budget_tokens=nan'):
            self.assertEqual(self.client.get(url).status_code, 400)
        self.opener.down = True
        data = self.client.get('/api/retrieve?q=passive+sonobuoy').get_json()
        self.assertTrue(data['partial'])
        self.assertTrue(data['warnings'])
        self.assertEqual(self.client.get('/api/retrieve?q=unicorn').status_code, 503)
        self.assertEqual(self.client.get('/api/library/../secret.txt').status_code, 404)

    def test_registration_does_not_touch_disk_or_network(self):
        from core.server import create_app
        with patch('core.librarian.service.Retriever.from_environment', side_effect=AssertionError('I/O')):
            client = create_app(None).test_client()
            self.assertEqual(client.get('/api/health').status_code, 200)
            self.assertEqual(client.get('/api/search').status_code, 400)


if __name__ == '__main__':
    unittest.main()
