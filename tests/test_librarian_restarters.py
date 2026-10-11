"""Acceptance check against real recordings from the Pi's Restarters Wiki archive.

Stand-in for the Wikipedia "passive sonobuoy" check until there is space for a Wikipedia
archive (see docs/ROADMAP.md, item 2.6). Both fixtures were captured from kiwix-serve
3.7.0 on the Pi; see tests/fixtures/README.md.
"""
import io
import unittest
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlsplit, unquote

from core.librarian.kiwix import KiwixClient
from core.librarian.service import Retriever

FIXTURES = Path(__file__).parent / 'fixtures'
BOOK = 'restarters_en_all_maxi_2026-04'
REPAIR_CAFE = f'http://localhost:8080/content/{BOOK}/Glossary%3ARepair_Caf%C3%A9'


class RecordedPi:
    """Serves the recorded search response and the one recorded article. Any other
    article was not captured, so it fails like a page the server could not deliver, unless
    `reuse` is set: then every article is the recorded one (real, but unrelated) text."""
    def __init__(self, reuse=False):
        self.paths, self.reuse = [], reuse

    def open(self, request, timeout):
        path = urlsplit(request.full_url).path
        self.paths.append(path)
        if path == '/search':
            return io.BytesIO((FIXTURES / 'kiwix-3.7.0-restarters.xml').read_bytes())
        if self.reuse or unquote(path) == f'/content/{BOOK}/Glossary:Repair_Café':
            return io.BytesIO((FIXTURES / 'kiwix-3.7.0-repair-cafe.html').read_bytes())
        raise URLError('not recorded')


class RestartersAcceptanceTest(unittest.TestCase):
    def retriever(self, reuse=False):
        return Retriever(KiwixClient('http://localhost:8080', RecordedPi(reuse), book=BOOK))

    def test_answerable_question_finds_the_right_article(self):
        rows, warnings = self.retriever().run('What is a Repair Café?')
        self.assertTrue(rows)
        self.assertEqual(rows[0].title, 'Glossary:Repair Café')
        self.assertEqual(rows[0].url, REPAIR_CAFE)  # A working offline link on the Pi.
        self.assertEqual(rows[0].source, 'kiwix')
        self.assertIn('Martine Postma', ' '.join(row.text for row in rows))
        self.assertTrue(warnings)  # The unrecorded second article is reported, not hidden.

    def test_question_outside_the_archive_returns_nothing(self):
        # What the Wikipedia check asks; Restarters has no answer, so no passages may be
        # passed to the model as if they were evidence.
        rows, warnings = self.retriever(reuse=True).run('How does a passive sonobuoy work?')
        self.assertEqual((rows, warnings), ([], []))


if __name__ == '__main__':
    unittest.main()
