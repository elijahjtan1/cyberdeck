import json
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main_app  # noqa: E402
from panel_tools.main_pages import button_rects  # noqa: E402
from panel_tools.planner_page import PlannerPage  # noqa: E402
from tests.test_ask_page import centre  # noqa: E402

SIZE = (1024, 600)
CLOCK = datetime(2026, 11, 12, 18, 50)  # November 2026 runs over six weeks.


class PlannerPageTest(unittest.TestCase):
    def setUp(self):
        self.data = Path(tempfile.mkdtemp())

    def page(self, size=SIZE, sync='Synchronized'):
        page = PlannerPage(main_app, self.data, lambda: sync, size=size)
        page.render(0, CLOCK, [])  # Taps use the wall time of the last draw, as on screen.
        return page

    def test_type_an_entry_and_delete_it(self):
        page = self.page()
        days = page.day_rects(date(2026, 11, 1))
        page.tap(*centre(days[13][0]), 0)  # The 14th.
        self.assertEqual((page.view, page.day), ('day', date(2026, 11, 14)))
        for key in 'Radar':
            page.key(key, 0)  # Typing on a day starts a new entry.
        page.key('enter', 0)
        self.assertEqual((page.view, page.time), ('time', '09:00'))
        page.key('up', 0)
        page.key('enter', 0)
        saved = json.loads((self.data / 'planner.json').read_text())
        self.assertEqual(saved['days']['2026-11-14'], [{'time': '09:15', 'text': 'Radar'}])
        self.assertEqual(page.render(0, CLOCK, []).size, SIZE)
        page.tap(*centre(page.entry_rects(1)[0]), 0)
        self.assertEqual(page.selected, 0)
        page.tap(*centre(button_rects(SIZE)[2]), 0)  # DELETE.
        self.assertEqual(page.planner.entries(date(2026, 11, 14)), [])

    def test_buttons_and_escape(self):
        page = self.page()
        page.tap(*centre(button_rects(SIZE)[2]), 0)  # MONTH >.
        self.assertEqual(page.shown, date(2026, 12, 1))
        page.tap(*centre(button_rects(SIZE)[1]), 0)  # TODAY.
        self.assertEqual((page.view, page.day), ('day', CLOCK.date()))
        page.tap(*centre(button_rects(SIZE)[3]), 0)  # ADD.
        self.assertEqual(page.view, 'type')
        page.tap(*centre(button_rects(SIZE)[0]), 0)  # BACK.
        self.assertEqual(page.key('esc', 0), None)  # Day: back to the month...
        self.assertEqual(page.key('esc', 0), 'exit')  # ...then home.
        self.assertEqual(page.tap(*centre(button_rects(SIZE)[3]), 0), 'exit')  # HOME.

    def test_time_buttons_step_the_time(self):
        page = self.page()
        page.open_day(date(2026, 11, 20))
        page.key('x', 0)
        page.key('enter', 0)
        page.tap(*centre(page.time_rects()[3]), 0)  # +1h.
        self.assertEqual(page.time, '10:00')

    def test_layout_fits_above_the_buttons(self):
        for size in (SIZE, (800, 480)):
            page = self.page(size)
            top = button_rects(size)[0][1]
            rects = [r for r, _ in page.day_rects(date(2026, 11, 1))]
            rects += page.entry_rects(30) + page.scroll_rects(30) + page.time_rects()
            for r in rects:
                self.assertTrue(0 <= r[0] < r[2] <= size[0] and main_app.HEADER < r[1] < r[3] < top, (size, r))

    def test_unsynced_clock_is_shown(self):
        page = self.page(sync='Not synchronized')
        self.assertEqual(page.clock_note(), 'Clock not synced: check today')
        page.render(0, CLOCK, [('warning', 'HOT')])  # Draws with an alert in the header.

    def test_main_app_opens_it_here(self):
        tile = next(t for t in main_app.TILES if t[1] == 'planner')
        self.assertEqual(tile[3], 'here')


if __name__ == '__main__':
    unittest.main()
