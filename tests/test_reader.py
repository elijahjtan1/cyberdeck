"""READER app: search, results, paging and honest errors, with a fake backend."""
import io
import json
import sys
import unittest
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import panel_app as app  # noqa: E402
from panel_tools import reader  # noqa: E402
from panel_tools.ask import keyboard_rects, field_rect  # noqa: E402

CLOCK = datetime(2026, 10, 12, 14, 20)
BASE = 'http://127.0.0.1:8090'
PARAGRAPHS = [f'Paragraph {i}: amateur radio operators use the shortwave bands to talk across '
              f'continents, often with simple wire antennas and low power.' for i in range(30)]


def centre(rect):
    return (rect[0]+rect[2])/2, (rect[1]+rect[3])/2


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class FakeBackend:
    """Answers like cyberdeck-core: search, article and local library text."""
    def __init__(self, results=None, article=None, fail=None):
        self.results = results if results is not None else [
            {'title': 'Amateur radio', 'url': 'http://127.0.0.1:8080/content/wiki/A/Amateur_radio',
             'snippet': '', 'source': 'wikipedia'},
            {'title': 'Antenna notes', 'url': '/api/library/notes/antenna.txt', 'snippet': '', 'source': 'local'}]
        self.article = article if article is not None else {'paragraphs': PARAGRAPHS, 'truncated': False}
        self.fail, self.calls = fail, []

    def open(self, url, timeout=None):
        self.calls.append(url)
        if self.fail == 'down':
            raise URLError('connection refused')
        parts = urlsplit(url)
        if self.fail is not None:
            body = io.BytesIO(json.dumps({'error': self.fail[1]}).encode()) if self.fail[1] else io.BytesIO(b'')
            raise HTTPError(url, self.fail[0], 'error', {}, body)
        if parts.path == '/api/search':
            return Response(json.dumps({'results': self.results, 'warnings': []}).encode())
        if parts.path == '/api/article':
            self.asked = parse_qs(parts.query)['url'][0]
            return Response(json.dumps(self.article).encode())
        if parts.path.startswith('/api/library/'):
            return Response(b'First local paragraph.\n\nSecond local paragraph.\n')
        raise HTTPError(url, 404, 'not found', {}, io.BytesIO(b''))


def make(backend=None, base=BASE):
    return reader.ReaderApp(app, base, opener=backend or FakeBackend())


def wait(view):
    if view.thread is not None:
        view.thread.join(5)


def type_and_search(view, text):
    keys = keyboard_rects(app.W, app.H)
    for letter in text:
        rect = next(r for r, k in keys if k == ('space' if letter == ' ' else letter))
        view.tap(*centre(rect), 0)
    view.tap(*centre(next(r for r, k in keys if k == 'ask')), 0)
    wait(view)


class ReaderTest(unittest.TestCase):
    def tearDown(self):
        app.set_rotation(0)

    def test_search_open_and_page_through_in_both_rotations(self):
        for rotation in (0, 90):
            app.set_rotation(rotation)
            backend = FakeBackend()
            view = make(backend)
            view.render(0, CLOCK, ())
            type_and_search(view, 'radio')
            self.assertEqual(view.query, 'radio')
            self.assertEqual(view.view, 'results')
            self.assertEqual(len(view.results), 2)
            self.assertEqual(view.render(0, CLOCK, ()).size, (app.W, app.H))
            rects = reader.result_rects(app.W, app.H, 2)
            for rect in rects:
                self.assertTrue(0 <= rect[0] < rect[2] <= app.W and rect[3] < app.H-31, (rotation, rect))
            view.tap(*centre(rects[0]), 0)
            wait(view)
            self.assertEqual(view.view, 'article')
            self.assertEqual(backend.asked, backend.results[0]['url'])
            self.assertEqual(view.note(), 'Reading: Amateur radio')
            pages = view.pages()
            self.assertGreater(pages, 3)
            buttons = reader.bottom_buttons(app.W, app.H)
            view.render(0, CLOCK, ())
            view.tap(*centre(buttons[2]), 0)
            self.assertEqual(view.page, 1)
            view.tap(app.W//2, app.H*3//4, 0)  # Tapping the text turns the page too.
            self.assertEqual(view.page, 2)
            view.tap(*centre(buttons[1]), 0)
            self.assertEqual(view.page, 1)
            for _ in range(pages + 3):
                view.tap(*centre(buttons[2]), 0)
            self.assertEqual(view.page, pages-1)
            image = view.render(0, CLOCK, ())
            self.assertEqual(image.size, (app.W, app.H))
            # Every wrapped line fits the screen width.
            d = app.ImageDraw.Draw(image)
            rect = reader.text_rect(app.W, app.H)
            for line in view.lines():
                self.assertLessEqual(d.textlength(line, font=app.SMALL), rect[2]-rect[0], line)
            view.tap(*centre(buttons[0]), 0)
            self.assertEqual(view.view, 'results')
            view.tap(*centre(buttons[0]), 0)
            self.assertEqual(view.view, 'type')
            view.render(0, CLOCK, ())
            view.tap(*centre(field_rect(app.W, app.H)), 0)  # Back to the open article.
            self.assertEqual(view.view, 'article')
            self.assertEqual(view.tap(*centre(buttons[3]), 0), 'exit')

    def test_local_library_text_is_split_into_paragraphs(self):
        backend = FakeBackend()
        view = make(backend)
        type_and_search(view, 'antenna')
        view.open(view.results[1])
        wait(view)
        self.assertEqual(backend.calls[-1], BASE + '/api/library/notes/antenna.txt')
        self.assertEqual(view.paragraphs, ['First local paragraph.', 'Second local paragraph.'])
        self.assertEqual(view.lines(), ['First local paragraph.', '', 'Second local paragraph.'])

    def test_truncated_articles_say_so(self):
        view = make(FakeBackend(article={'paragraphs': ['Short.'], 'truncated': True}))
        view.open({'title': 'Long one', 'url': 'http://127.0.0.1:8080/content/x'})
        wait(view)
        lines = view.lines()
        self.assertEqual(lines[:2], ['Short.', ''])
        self.assertEqual(' '.join(lines[view.note_from:]), reader.CUT_NOTE)

    def test_results_page_when_there_are_many(self):
        rows = [{'title': f'Result {i}', 'url': f'/api/library/{i}.txt'} for i in range(8)]
        for rotation in (0, 90):
            app.set_rotation(rotation)
            view = make(FakeBackend(results=rows))
            type_and_search(view, 'r')
            per = len(reader.result_rects(app.W, app.H, 8))
            self.assertLess(per, 8)
            buttons = reader.bottom_buttons(app.W, app.H)
            view.tap(*centre(buttons[2]), 0)
            self.assertEqual(view.shown_results()[0]['title'], f'Result {per}')
            for _ in range(5):
                view.tap(*centre(buttons[2]), 0)
            self.assertFalse(view.more_results())
            last = view.result_page
            self.assertEqual(last, (8-1) // per)  # Stops on the last page.
            view.render(0, CLOCK, ())
            view.tap(*centre(buttons[1]), 0)
            self.assertEqual(view.result_page, last-1)

    def test_errors_are_honest_and_never_raise(self):
        cases = [
            (FakeBackend(fail='down'), 'not running'),
            (FakeBackend(fail=(502, 'Kiwix is not answering.')), 'Kiwix is not answering.'),
            (FakeBackend(fail=(404, '')), 'no reader support'),
            (FakeBackend(results=[]), 'Nothing in the offline library matches "zzz".'),
        ]
        for backend, expected in cases:
            view = make(backend)
            type_and_search(view, 'zzz')
            self.assertIn(expected, view.error)
            self.assertEqual(view.results, [])
            view.render(0, CLOCK, ())
        view = make(FakeBackend(fail=(404, 'Kiwix has no such article.')))
        view.open({'title': 'Gone', 'url': 'http://127.0.0.1:8080/content/gone'})
        wait(view)
        self.assertEqual((view.view, view.error), ('article', 'Kiwix has no such article.'))
        self.assertEqual(view.lines(), [])
        view.render(0, CLOCK, ())
        view = make(FakeBackend(article={'paragraphs': []}))
        view.open({'title': 'Empty', 'url': 'http://127.0.0.1:8080/content/empty'})
        wait(view)
        self.assertIn('no readable text', view.error)

    def test_backend_off_needs_no_network(self):
        backend = FakeBackend()
        view = make(backend, base=None)
        self.assertEqual(view.note(), 'Backend off')
        type_and_search(view, 'radio')
        self.assertIn('--backend off', view.error)
        self.assertEqual(backend.calls, [])
        view.render(0, CLOCK, ())

    def test_listed_in_apps_and_not_covered_by_a_saver(self):
        self.assertIn(('READER', 'reader'), [(label, key) for label, key, _ in app.HOME_TILES])
        options = {**app.DEFAULT_OPTIONS, 'saver_style': 'radar', 'saver_delay': 1}
        self.assertFalse(app.saver_due(options, 10**6, 'reader'))


if __name__ == '__main__':
    unittest.main()
