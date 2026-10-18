import sys
import unittest
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main_app  # noqa: E402
from panel_tools import main_pages  # noqa: E402
from panel_tools.main_pages import AskPage, WikiPage, button_rects, text_rect, wrap  # noqa: E402
from tests.test_ask_page import DONE, PASSAGES, centre, scripted  # noqa: E402
from tests.test_reader import BASE, FakeBackend, PARAGRAPHS  # noqa: E402

SIZE = (1024, 600)
CLOCK = datetime(2026, 10, 9, 19, 24)


class WrapTest(unittest.TestCase):
    def test_lines_fit_and_long_words_are_cut(self):
        d = ImageDraw.Draw(Image.new('RGB', (1, 1)))
        lines = wrap(d, 'short words ' * 40 + 'x' * 300, 500, main_app.READ)
        self.assertTrue(all(d.textlength(line, font=main_app.READ) <= 500 for line in lines))
        self.assertEqual(''.join(lines).count('x'), 300)


class AskPageTest(unittest.TestCase):
    def test_streams_the_answer_with_its_notes_and_goes_home(self):
        events, calls = scripted(PASSAGES, dict(DONE, incomplete=True))
        page = AskPage(main_app, 'http://127.0.0.1:8765', size=SIZE, events=events)
        for key in 'Why?':
            page.key(key, 0)
        page.key('enter', 0)
        page.thread.join(5)
        self.assertEqual(calls[0][0], 'Why?')
        first, lines, visible, rect = page.layout()
        self.assertIn("PARTIAL: the sources don't contain the full answer.", lines)
        self.assertTrue(rect[2] <= SIZE[0] and visible >= 10)
        self.assertEqual(page.render(0, CLOCK, []).size, SIZE)
        self.assertEqual(page.tap(*centre(button_rects(SIZE)[3]), 0), 'exit')  # HOME.
        page.tap(*centre(button_rects(SIZE)[0]), 0)  # EDIT.
        self.assertEqual((page.view, page.question), ('type', 'Why?'))

    def test_tapping_an_example_asks_it(self):
        events, calls = scripted(PASSAGES, DONE)
        page = AskPage(main_app, 'http://127.0.0.1:8765', size=SIZE, events=events)
        page.render(0, CLOCK, [])
        wanted = page.examples()[1]
        page.tap(*centre(page.example_rects[1]), 0)
        page.thread.join(5)
        self.assertEqual(calls[0][0], wanted)
        self.assertEqual(page.key('esc', 0), None)  # Answer view: Esc edits first...
        self.assertEqual(page.key('esc', 0), 'exit')  # ...then goes home.

    def test_backend_off_says_so(self):
        page = AskPage(main_app, None, size=SIZE)
        page.ask('Anything?')
        self.assertIn('--backend off', ' '.join(page.layout()[1]))


class WikiPageTest(unittest.TestCase):
    def test_search_open_and_turn_pages(self):
        backend = FakeBackend(article={'paragraphs': PARAGRAPHS * 20, 'truncated': True})
        page = WikiPage(main_app, BASE, opener=backend, size=SIZE)
        for key in 'radio':
            page.key(key, 0)
        page.key('enter', 0)
        page.thread.join(5)
        self.assertEqual(page.view, 'results')
        page.render(0, CLOCK, [])
        self.assertEqual(len(page.result_rects), 2)
        page.key('1', 0)
        page.thread.join(5)
        self.assertEqual((page.view, page.title), ('article', 'Amateur radio'))
        lines = page.lines()
        d = ImageDraw.Draw(Image.new('RGB', (1, 1)))
        width = text_rect(SIZE)[2] - text_rect(SIZE)[0]
        self.assertTrue(all(d.textlength(line, font=main_app.READ) <= width for line in lines))
        self.assertIn('first part only', lines[-1])
        self.assertGreater(page.pages(), 1)
        page.tap(SIZE[0] - 50, 300, 0)  # Right of the text: next page.
        self.assertEqual(page.page, 1)
        page.tap(50, 300, 0)
        self.assertEqual(page.page, 0)
        self.assertEqual(page.render(0, CLOCK, []).size, SIZE)
        page.tap(*centre(button_rects(SIZE)[0]), 0)  # RESULTS.
        page.tap(*centre(page.result_rects[1]), 0)
        page.thread.join(5)
        self.assertEqual(page.title, 'Antenna notes')
        self.assertEqual(page.tap(*centre(button_rects(SIZE)[3]), 0), 'exit')

    def test_backend_down_is_reported(self):
        page = WikiPage(main_app, BASE, opener=FakeBackend(fail='down'), size=SIZE)
        page.search('radio')
        page.thread.join(5)
        self.assertIn('not running', page.error)
        page.render(0, CLOCK, [])

    def test_results_fit_above_the_buttons(self):
        page = WikiPage(main_app, BASE, opener=FakeBackend(results=[
            {'title': f'Article {n}', 'url': f'http://127.0.0.1:8080/content/wiki/A/{n}'} for n in range(9)]),
            size=SIZE)
        page.search('article')
        page.thread.join(5)
        page.render(0, CLOCK, [])
        self.assertEqual(len(page.result_rects), main_pages.RESULTS_PER_PAGE)
        self.assertLess(page.result_rects[-1][3], button_rects(SIZE)[0][1])
        self.assertTrue(page.more_results())


if __name__ == '__main__':
    unittest.main()
