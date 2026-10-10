import io
from pathlib import Path
import unittest
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit

from core.librarian.extract import fetch_article, paragraphs
from core.librarian.kiwix import KiwixClient, KiwixError

FIXTURES = Path(__file__).parent / 'fixtures'


class FakeOpener:
    def __init__(self, data=None):
        self.data = data
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request.full_url, timeout))
        if self.data is None:
            raise URLError('Connection refused')
        return io.BytesIO(self.data)


class SourcesTest(unittest.TestCase):
    def test_modern_search_and_mixed_snippets(self):
        opener = FakeOpener((FIXTURES / 'kiwix-search.xml').read_bytes())
        client = KiwixClient('http://127.0.0.1:8080', opener, book='wikipedia_en_test')
        results = client.search('passive sonobuoy', 2)
        self.assertEqual(results[0].title, 'Sonobuoy')
        self.assertEqual(results[0].snippet, 'A passive sonobuoy listens & transmits.')
        self.assertEqual(results[1].snippet, 'Underwater sound detection.')
        self.assertEqual(results[0].url, 'http://127.0.0.1:8080/content/wikipedia_en_test/Sonobuoy')
        url, timeout = opener.requests[0]
        self.assertEqual(timeout, 2)
        self.assertEqual(parse_qs(urlsplit(url).query), {
            'pattern': ['passive sonobuoy'], 'format': ['xml'], 'pageLength': ['2'],
            'books.name': ['wikipedia_en_test']})

    def test_legacy_search_preserves_article_path(self):
        opener = FakeOpener((FIXTURES / 'kiwix-legacy.xml').read_bytes())
        client = KiwixClient('http://localhost:8080', opener, book='wikipedia_en_test',
                             book_parameter='content')
        result = client.search('sonobuoy', 1)[0]
        self.assertEqual(result.url, 'http://localhost:8080/wikipedia_en_test/A/Sonobuoy')
        self.assertEqual(parse_qs(urlsplit(opener.requests[0][0]).query)['content'],
                         ['wikipedia_en_test'])

    def test_down_malformed_oversized_and_empty(self):
        with self.assertRaisesRegex(KiwixError, 'unavailable'):
            KiwixClient('http://localhost:8080', FakeOpener()).search('x')
        for data in (b'<html>error</html>', b'<rss', b'<!DOCTYPE x><rss/>'):
            with self.assertRaises(KiwixError):
                KiwixClient('http://localhost:8080', FakeOpener(data)).search('x')
        with self.assertRaisesRegex(KiwixError, 'size limit'):
            KiwixClient('http://localhost:8080', FakeOpener(b'123456'), max_bytes=5).fetch('/x')
        client = KiwixClient('http://localhost:8080', FakeOpener(b'<rss><channel/></rss>'))
        self.assertEqual(client.search('x'), [])
        self.assertEqual(client.search(''), [])

    def test_never_follows_external_links(self):
        client = KiwixClient('http://localhost:8080', FakeOpener())
        for link in ('https://example.com/a', '//example.com/a', 'file:///etc/passwd'):
            with self.assertRaises(KiwixError):
                client.fetch(link)
        with self.assertRaises(ValueError):
            KiwixClient('https://wikipedia.org')

    def test_html_cleanup_and_fetch(self):
        data = (FIXTURES / 'sonobuoy.html').read_bytes()
        client = KiwixClient('http://localhost:8080', FakeOpener(data))
        rows = fetch_article(client, '/content/book/Sonobuoy')
        self.assertEqual(len(rows), 4)
        self.assertIn('hydrophone', rows[1])
        self.assertNotIn('[1]', ' '.join(rows))
        self.assertNotIn('Unwanted', ' '.join(rows))
        self.assertNotIn('Infobox', ' '.join(rows))
        self.assertEqual(paragraphs('<p>Water &amp; <b>sound</b>.</p><p>Next<br>line</p>'),
                         ['Water & sound.', 'Next line'])
        self.assertEqual(paragraphs('<h2>Notes</h2><p>omit</p><h2>Operation</h2><p>keep</p>'), ['keep'])


if __name__ == '__main__':
    unittest.main()
