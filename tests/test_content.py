import hashlib
import http.client
import io
import json
import sys
import tempfile
import unittest
from collections import namedtuple
from pathlib import Path
from unittest import mock
from urllib.error import URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import content

Usage = namedtuple('Usage', 'total used free')
BASE = content.BASE


class Response(io.BytesIO):
    def __init__(self, body, status=200):
        super().__init__(body)
        self.status = status


class Broken(Response):
    """Sends some bytes, then fails the way a dropped connection does."""

    def __init__(self, body, error, status):
        super().__init__(body, status)
        self.error = error

    def read(self, size=-1):
        block = super().read(size)
        if not block:
            raise self.error
        return block


class FakeKiwix:
    """Serves listings, metalinks and file bodies; records Range headers."""

    def __init__(self, files, folder='other', meta_sha=True):
        self.files = files  # name -> bytes
        self.folder = folder
        self.meta_sha = meta_sha
        self.ranges = []
        self.break_with = None  # Exception raised after the body, or 'short' to end early.

    def open(self, request, timeout=None):
        url = request.full_url
        for folder in ('other', 'zimit', 'wikipedia'):
            if url == BASE + folder + '/':
                names = self.files if folder == self.folder else {}
                return Response(''.join('<a href="%s">%s</a>\n' % (n, n) for n in names).encode())
        name = url.rsplit('/', 1)[1]
        if name.endswith('.meta4'):
            body = self.files[name[:-6]]
            sha = ('<hash type="sha-256">%s</hash>' % hashlib.sha256(body).hexdigest()
                   if self.meta_sha else '<hash type="md5">x</hash>')
            return Response(('<?xml version="1.0"?><metalink xmlns="urn:ietf:params:xml:ns:'
                             'metalink"><file name="%s"><size>%d</size>%s</file></metalink>'
                             % (name[:-6], len(body), sha)).encode())
        if name.endswith('.sha256'):
            body = self.files[name[:-7]]
            return Response(('%s  %s\n' % (hashlib.sha256(body).hexdigest(), name)).encode())
        if name not in self.files:
            raise URLError('not found')
        body = self.files[name]
        byte_range = request.get_header('Range')
        self.ranges.append(byte_range)
        start = int(byte_range[6:-1]) if byte_range else 0
        status = 206 if byte_range else 200
        if self.break_with == 'short':
            return Response(body[start:start + 3], status)
        if self.break_with:
            return Broken(body[start:start + 3], self.break_with, status)
        return Response(body[start:], status)


SOURCE = {'prefix': 'zimgit-water_en', 'dir': 'other', 'tiers': [0]}


class ContentTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.dest = Path(self.directory.name)
        self.out = io.StringIO()
        self.roomy = mock.patch.object(content.shutil, 'disk_usage',
                                       return_value=Usage(10**9, 0, 10**9))
        self.roomy.start()

    def tearDown(self):
        self.roomy.stop()
        self.directory.cleanup()

    def kiwix(self, fake):
        return content.Kiwix(opener=fake)

    def test_picks_newest_dated_file_and_ignores_other_prefixes(self):
        fake = FakeKiwix({'zimgit-water_en_2024-01.zim': b'old', 'zimgit-water_en_2025-03.zim': b'new',
                          'zimgit-water_en_extra_2026-01.zim': b'no'})
        latest = self.kiwix(fake).latest(SOURCE)
        self.assertEqual(latest['file'], 'zimgit-water_en_2025-03.zim')
        self.assertEqual(latest['size'], 3)

    def test_finds_book_moved_to_fallback_folder(self):
        fake = FakeKiwix({'zimgit-water_en_2025-03.zim': b'new'}, folder='zimit')
        self.assertEqual(self.kiwix(fake).latest(SOURCE)['url'], BASE + 'zimit/zimgit-water_en_2025-03.zim')

    def test_reads_sha256_file_when_metalink_has_none(self):
        fake = FakeKiwix({'zimgit-water_en_2025-03.zim': b'new'}, meta_sha=False)
        latest = self.kiwix(fake).latest(SOURCE)
        self.assertEqual(latest['sha256'], hashlib.sha256(b'new').hexdigest())

    def test_fetch_downloads_verifies_and_records_manifest(self):
        fake = FakeKiwix({'zimgit-water_en_2025-03.zim': b'water' * 1000})
        content.fetch(self.kiwix(fake), [SOURCE], self.dest, self.out)
        self.assertEqual((self.dest / 'zimgit-water_en_2025-03.zim').read_bytes(), b'water' * 1000)
        self.assertFalse((self.dest / 'zimgit-water_en_2025-03.zim.part').exists())
        entry = json.loads((self.dest / 'manifest.json').read_text())['files']['zimgit-water_en_2025-03.zim']
        self.assertEqual(entry['prefix'], 'zimgit-water_en')
        self.assertEqual(content.plan(self.kiwix(fake), [SOURCE], self.dest)[0]['state'], 'installed')

    def test_fetch_resumes_partial_download(self):
        body = b'0123456789' * 100
        (self.dest / 'zimgit-water_en_2025-03.zim.part').write_bytes(body[:400])
        fake = FakeKiwix({'zimgit-water_en_2025-03.zim': body})
        content.fetch(self.kiwix(fake), [SOURCE], self.dest, self.out)
        self.assertEqual(fake.ranges, ['bytes=400-'])
        self.assertEqual((self.dest / 'zimgit-water_en_2025-03.zim').read_bytes(), body)

    def test_complete_part_is_checked_without_downloading(self):
        (self.dest / 'zimgit-water_en_2025-03.zim.part').write_bytes(b'done')
        fake = FakeKiwix({'zimgit-water_en_2025-03.zim': b'done'})
        content.fetch(self.kiwix(fake), [SOURCE], self.dest, self.out)
        self.assertEqual(fake.ranges, [])
        self.assertTrue((self.dest / 'zimgit-water_en_2025-03.zim').exists())

    def test_corrupt_download_is_removed_and_reported(self):
        fake = FakeKiwix({'zimgit-water_en_2025-03.zim': b'good'})
        kiwix = self.kiwix(fake)
        row = content.plan(kiwix, [SOURCE], self.dest)[0]
        fake.files['zimgit-water_en_2025-03.zim'] = b'evil'
        with self.assertRaises(content.ContentError):
            content.download(kiwix, row, self.dest, self.out)
        self.assertEqual(list(self.dest.iterdir()), [])

    def test_failed_update_does_not_offer_old_copy_for_deletion(self):
        fake = FakeKiwix({'zimgit-water_en_2024-01.zim': b'old'})
        content.fetch(self.kiwix(fake), [SOURCE], self.dest, self.out)
        fake.files['zimgit-water_en_2025-03.zim'] = b'new'
        kiwix = self.kiwix(fake)
        kiwix.latest(SOURCE)  # Publish the real hash, then corrupt the body in transit.
        fake.files['zimgit-water_en_2025-03.zim'] = b'bad'
        kiwix.metalink = lambda url: (3, hashlib.sha256(b'new').hexdigest())
        with self.assertRaises(content.ContentError):
            content.fetch(kiwix, [SOURCE], self.dest, self.out)
        self.assertNotIn('Superseded', self.out.getvalue())

    def test_dropped_connection_keeps_part_and_reports(self):
        body = b'0123456789' * 10
        fake = FakeKiwix({'zimgit-water_en_2025-03.zim': body})
        for error in (ConnectionResetError(104, 'reset'), TimeoutError(),
                      http.client.IncompleteRead(b'')):
            fake.break_with = error
            self.out = io.StringIO()
            code = content.main(['fetch', '0', '--dest', str(self.dest), '--only', 'zimgit-water_en'],
                                opener=fake, out=self.out)
            self.assertEqual(code, 1)
            self.assertIn('partial file was kept', self.out.getvalue())
        self.assertTrue((self.dest / 'zimgit-water_en_2025-03.zim.part').exists())
        fake.break_with = None
        content.fetch(self.kiwix(fake), [SOURCE], self.dest, self.out)
        self.assertEqual((self.dest / 'zimgit-water_en_2025-03.zim').read_bytes(), body)

    def test_short_response_keeps_part_for_resume(self):
        fake = FakeKiwix({'zimgit-water_en_2025-03.zim': b'0123456789'})
        fake.break_with = 'short'
        with self.assertRaises(content.ContentError):
            content.fetch(self.kiwix(fake), [SOURCE], self.dest, self.out)
        self.assertEqual((self.dest / 'zimgit-water_en_2025-03.zim.part').read_bytes(), b'012')

    def test_verify_without_manifest_is_not_an_all_clear(self):
        code = content.main(['verify', '--dest', str(self.dest)], out=self.out)
        self.assertEqual(code, 1)
        self.assertIn('No manifest.json', self.out.getvalue())

    def test_refuses_when_disk_would_be_too_full(self):
        fake = FakeKiwix({'zimgit-water_en_2025-03.zim': b'x' * 100})
        with mock.patch.object(content.shutil, 'disk_usage', return_value=Usage(1000, 850, 150)):
            with self.assertRaises(content.ContentError) as caught:
                content.fetch(self.kiwix(fake), [SOURCE], self.dest, self.out)
        self.assertIn('only', str(caught.exception))
        self.assertEqual(fake.ranges, [])

    def test_newer_release_downloads_beside_old_and_lists_it(self):
        fake = FakeKiwix({'zimgit-water_en_2024-01.zim': b'old'})
        content.fetch(self.kiwix(fake), [SOURCE], self.dest, self.out)
        fake.files['zimgit-water_en_2025-03.zim'] = b'new'
        kiwix = self.kiwix(fake)
        self.assertEqual(content.plan(kiwix, [SOURCE], self.dest)[0]['state'], 'newer')
        content.fetch(kiwix, [SOURCE], self.dest, self.out)
        self.assertTrue((self.dest / 'zimgit-water_en_2024-01.zim').exists())
        self.assertIn('Superseded', self.out.getvalue())

    def test_verify_reports_changed_and_missing_files(self):
        fake = FakeKiwix({'zimgit-water_en_2025-03.zim': b'water'})
        content.fetch(self.kiwix(fake), [SOURCE], self.dest, self.out)
        self.assertEqual(content.verify(self.dest, self.out), [])
        (self.dest / 'zimgit-water_en_2025-03.zim').write_bytes(b'rot!!')
        self.assertEqual(content.verify(self.dest, self.out), ['zimgit-water_en_2025-03.zim'])
        (self.dest / 'zimgit-water_en_2025-03.zim').unlink()
        self.assertEqual(content.verify(self.dest, self.out), ['zimgit-water_en_2025-03.zim'])
        self.assertIn('MISSING', self.out.getvalue())

    def test_unreachable_server_is_a_message_not_a_crash(self):
        def refuse(request, timeout=None):
            raise URLError('offline')
        opener = mock.Mock(open=refuse)
        code = content.main(['plan', '0', '--dest', str(self.dest)], opener=opener, out=self.out)
        self.assertEqual(code, 1)
        self.assertIn('Could not reach', self.out.getvalue())

    def test_missing_dest_says_so(self):
        code = content.main(['verify', '--dest', str(self.dest / 'nope')], out=self.out)
        self.assertEqual(code, 2)
        self.assertIn('Is the SSD mounted', self.out.getvalue())

    def test_shipped_tiers_are_consistent(self):
        config = content.load_sources()
        prefixes = [s['prefix'] for s in config['sources']]
        self.assertEqual(len(prefixes), len(set(prefixes)))
        for tier in range(5):
            wikipedia = [p for p in prefixes if p.startswith('wikipedia_')
                         and p in [s['prefix'] for s in content.tier_sources(config, tier)]]
            self.assertLessEqual(len(wikipedia), 1, tier)
        with self.assertRaises(content.ContentError):
            content.tier_sources(config, 0, ['gutenberg_en_all'])


if __name__ == '__main__':
    unittest.main()
