import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main_app  # noqa: E402
from panel_tools.main_pages import button_rects  # noqa: E402
from panel_tools.tools_page import MENU, TIMER_ALERT, ToolsPage  # noqa: E402
from tests.test_ask_page import centre  # noqa: E402

SIZE = (1024, 600)
CLOCK = datetime(2026, 11, 12, 20, 5)
NET = 'Inter-|   Receive\n face |bytes\n  wlan0: {} 0 0 0 0 0 0 0 {} 0 0 0 0 0 0 0\n'


class ToolsPageTest(unittest.TestCase):
    def setUp(self):
        self.data = Path(tempfile.mkdtemp())
        self.rows = {'ip': 'wlan0 192.168.1.42', 'wifi': 'HomeNet -52 dBm', 'sync': 'Synchronized'}

    def page(self, size=SIZE):
        return ToolsPage(main_app, self.data, lambda: self.rows, size=size)

    def test_menu_opens_pages_and_the_quick_panel(self):
        page = self.page()
        for index, (label, target, key, note) in enumerate(MENU[:3]):
            self.assertIsNone(page.tap(*centre(page.menu_rects()[index]), 0))
            self.assertEqual(page.page, target)
            self.assertEqual(page.render(0, CLOCK, []).size, SIZE)
            self.assertIsNone(page.key('esc', 0))  # Back to the menu...
            self.assertEqual(page.page, 'menu')
        self.assertEqual(page.key('4', 0), 'quick:tools')
        self.assertEqual(page.tap(*centre(page.menu_rects()[3]), 0), 'quick:tools')
        self.assertEqual(page.tap(*centre(button_rects(SIZE)[3]), 0), 'exit')  # HOME.
        self.assertEqual(page.key('esc', 0), 'exit')  # ...then home.

    def test_countdown_ends_on_any_page_and_is_dismissed(self):
        page = self.page()
        page.key('1', 0)
        page.key('l', 0)  # 5 minutes -> 10.
        self.assertEqual(page.timer_seconds, 600)
        page.tap(*centre(button_rects(SIZE, 5)[3]), 100)  # START TIMER.
        self.assertEqual((page.clocks.deadline, page.delay), (700, .2))
        page.key('esc', 100)
        page.tick(699)
        self.assertEqual(page.alerts(), [])
        page.tick(700)
        self.assertEqual(page.alerts(), [TIMER_ALERT])
        page.key('1', 701)
        self.assertEqual(page.timer_labels()[3], 'DISMISS')
        page.render(701, CLOCK, page.alerts())
        page.key('t', 701)
        self.assertEqual((page.alerts(), page.clocks.deadline), ([], None))  # Dismissed, not restarted.

    def test_stopwatch_and_timer_buttons(self):
        page = self.page()
        page.key('1', 0)
        page.key('s', 10)
        self.assertEqual(page.clocks.stopwatch(25), 15)
        page.key('space', 25)  # Pause.
        self.assertEqual((page.clocks.stopwatch(99), page.delay), (15, 1.))
        page.key('r', 99)
        self.assertEqual(page.clocks.stopwatch(99), 0)
        page.key('t', 0)
        page.key('t', 5)  # STOP TIMER.
        self.assertIsNone(page.clocks.deadline)
        page.tap(*centre(button_rects(SIZE, 5)[4]), 5)  # BACK.
        self.assertEqual(page.page, 'menu')

    def test_network_samples_once_a_second_with_the_signal(self):
        page = self.page()
        texts = iter((NET.format(1000, 500), NET.format(3048, 1524)))
        sample = page.network.sample
        page.network.sample = lambda now, signal=None: sample(now, signal, next(texts))
        page.tick(10)
        page.tick(10.5)  # Too soon: no second sample.
        page.tick(11)
        self.assertEqual(list(page.network.samples), [(0., 0., -52.), (2048., 1024., -52.)])
        page.key('2', 11)
        page.render(11, CLOCK, [])

    def test_sky_says_when_the_position_or_clock_is_missing(self):
        from PIL import ImageDraw
        seen = []
        text = ImageDraw.ImageDraw.text
        ImageDraw.ImageDraw.text = lambda d, xy, value, *a, **k: (seen.append(value), text(d, xy, value, *a, **k))[1]
        try:
            page = self.page()
            page.key('3', 0)
            self.rows['sync'] = 'Not synchronized'
            page.render(0, CLOCK, [])
            self.assertIn('Clock not synced: check the date', seen)
            self.assertIn('Sun times need a position:', seen)
            (self.data / 'location.json').write_text(json.dumps({'lat': 44.65, 'lon': -63.57}))
            del seen[:]
            page.render(0, CLOCK, [])
            self.assertTrue(any(value.startswith('Sunrise ') for value in seen), seen)
        finally:
            ImageDraw.ImageDraw.text = text

    def test_layout_fits_the_screen(self):
        for size in (SIZE, (800, 480)):
            page = self.page(size)
            top = button_rects(size)[0][1]
            for r in page.menu_rects():
                self.assertTrue(0 <= r[0] < r[2] <= size[0] and main_app.HEADER < r[1] < r[3] < top, (size, r))
            from PIL import Image, ImageDraw
            d = ImageDraw.Draw(Image.new('RGB', (1, 1)))
            page.clocks.deadline = 1
            for rect, label in zip(button_rects(size, 5), page.timer_labels()):
                self.assertLess(d.textlength(label, font=main_app.SMALL_BOLD), rect[2] - rect[0] - 8, (size, label))

    def test_main_app_opens_it_here(self):
        tile = next(t for t in main_app.TILES if t[1] == 'tools')
        self.assertEqual(tile[3], 'here')


if __name__ == '__main__':
    unittest.main()
