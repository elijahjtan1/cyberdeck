import os
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from core.librarian.library import LibraryError, LocalLibrary


class LibraryTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name) / 'library'
        self.root.mkdir()
        self.library = LocalLibrary(self.root)

    def tearDown(self):
        self.folder.cleanup()

    def test_formats_reindex_update_and_delete(self):
        a = self.root / 'Passive sonar.txt'
        a.write_text('A passive sonobuoy uses a hydrophone.\n\nIt transmits audio by radio.', encoding='utf-8')
        (self.root / 'notes.md').write_text('# Sonobuoy\n\nPassive acoustic listening.', encoding='utf-8')
        (self.root / 'article.html').write_text('<nav><p>navigation</p></nav><p>A sonobuoy at sea.</p>', encoding='utf-8')
        (self.root / 'ignored.pdf').write_text('sonobuoy')
        self.assertEqual(self.library.reindex()['changed'], 3)
        rows = self.library.search('sonobuoy')
        self.assertEqual(len(rows), 3)
        self.assertTrue(any('Passive%20sonar.txt' in r.url for r in rows))
        self.assertEqual(self.library.search('navigation'), [])
        before = self.library.db_path.stat().st_mtime_ns
        self.assertEqual(self.library.reindex(), {'changed': 0, 'removed': 0, 'skipped': []})
        self.assertEqual(self.library.db_path.stat().st_mtime_ns, before)
        old = a.stat()
        a.write_text('Whales vocalize underwater.', encoding='utf-8')
        os.utime(a, ns=(old.st_atime_ns, old.st_mtime_ns + 2_000_000_000))
        self.assertEqual(self.library.reindex()['changed'], 1)
        self.assertEqual(len(self.library.search('sonobuoy')), 2)
        self.assertEqual(len(self.library.search('whales')), 1)
        a.unlink()
        self.assertEqual(self.library.search('whales'), [])
        self.assertEqual(self.library.reindex()['removed'], 1)
        self.assertEqual(self.library.search('whales'), [])

    def test_empty_quotes_and_corrupt_index(self):
        self.assertEqual(self.library.search('sonobuoy'), [])
        (self.root / 'notes.md').write_text('C++ IPv6 sonar', encoding='utf-8')
        self.library.reindex()
        self.assertEqual(self.library.search('???'), [])
        self.assertEqual(len(self.library.search('"sonar" OR "*"')), 1)
        self.library.db_path.write_bytes(b'not sqlite')
        with self.assertRaisesRegex(LibraryError, 'index is unavailable'):
            self.library.search('sonar')

    def test_outside_paths_and_size_limits(self):
        outside = Path(self.folder.name) / 'secret.txt'
        outside.write_text('outside')
        for name in ('../secret.txt', str(outside), 'notes.pdf'):
            with self.assertRaises(LibraryError):
                self.library.read_document(name)
        (self.root / 'large.txt').write_text('x' * 101)
        library = LocalLibrary(self.root, max_bytes=100)
        self.assertEqual(len(library.reindex()['skipped']), 1)
        self.assertEqual(library.search('x'), [])
        with self.assertRaises(LibraryError):
            library.read_document('large.txt')

    def test_same_size_changed_mtime_and_non_utf8(self):
        path = self.root / 'note.txt'
        path.write_text('sonar')
        self.library.reindex()
        info = path.stat()
        path.write_text('radio')
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 10_000_000))
        self.assertEqual(self.library.reindex()['changed'], 1)
        self.assertEqual(self.library.search('sonar'), [])
        self.assertEqual(len(self.library.search('radio')), 1)
        path.write_bytes(b'\xff')
        self.assertEqual(self.library.reindex()['removed'], 1)
        self.assertEqual(self.library.search('radio'), [])

    def test_nested_paths_and_plain_html_reading(self):
        nested = self.root / 'field notes'
        nested.mkdir()
        path = nested / 'A & B.html'
        path.write_text('<script>alert(1)</script><p>Sonobuoy listening.</p>', encoding='utf-8')
        self.library.reindex()
        row = self.library.search('sonobuoy')[0]
        self.assertEqual(row.url, '/api/library/field%20notes/A%20%26%20B.html')
        self.assertNotIn('alert', row.text)
        self.assertIn('<script>', self.library.read_document('field notes/A & B.html'))

    def test_sqlite_connections_close(self):
        (self.root / 'a.txt').write_text('sonobuoy')
        self.library.reindex()
        self.library.search('sonobuoy')
        with closing(sqlite3.connect(self.library.db_path)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM files').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
