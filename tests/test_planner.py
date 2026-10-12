"""PLANNER app: saved entries, the month grid and the add/delete flow, without a Pi."""
import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import panel_app as app  # noqa: E402
from panel_tools import planner  # noqa: E402
from panel_tools.ask import keyboard_rects  # noqa: E402

NOW = datetime(2026, 10, 12, 14, 20, tzinfo=timezone.utc)


def centre(rect):
    return (rect[0]+rect[2])/2, (rect[1]+rect[3])/2


class PlannerStoreTest(unittest.TestCase):
    def test_entries_are_sorted_saved_and_reloaded(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'planner.json'
            store = planner.Planner(path)
            day = date(2026, 10, 12)
            self.assertTrue(store.add(day, '  Dentist  ', '14:00'))
            self.assertTrue(store.add(day, 'Pack bag'))
            self.assertTrue(store.add(day, 'Breakfast', '08:30'))
            self.assertFalse(store.add(day, '   '))
            self.assertEqual([e['text'] for e in store.entries(day)], ['Pack bag', 'Breakfast', 'Dentist'])
            again = planner.Planner(path)
            self.assertEqual(again.entries(day), store.entries(day))
            again.delete(day, 0)
            again.delete(day, 9)  # Out of range: ignored.
            self.assertEqual(len(planner.Planner(path).entries(day)), 2)

    def test_bad_files_and_entries_are_dropped(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'planner.json'
            for bad in ('not json', '[]', '{"days": 3}'):
                path.write_text(bad)
                self.assertEqual(planner.Planner(path).days, {})
            path.write_text(json.dumps({'days': {
                'yesterday': [{'text': 'x'}],
                '2026-10-12': [{'text': 'ok', 'time': '25:99'}, 'nope', {'time': '09:00'},
                               {'text': 'y'*200, 'time': '7:05'}]}}))
            entries = planner.Planner(path).entries(date(2026, 10, 12))
            self.assertEqual(entries, [{'time': '', 'text': 'ok'}, {'time': '07:05', 'text': 'y'*60}])

    def test_next_entry_skips_past_times(self):
        with tempfile.TemporaryDirectory() as folder:
            store = planner.Planner(Path(folder)/'planner.json')
            store.add(date(2026, 10, 12), 'Earlier', '09:00')
            store.add(date(2026, 10, 12), 'All day thing')
            self.assertIsNone(store.next_entry(NOW))
            store.add(date(2026, 10, 14), 'Market', '10:00')
            self.assertEqual(store.next_entry(NOW), (date(2026, 10, 14), {'time': '10:00', 'text': 'Market'}))
            store.add(date(2026, 10, 12), 'Call home', '18:30')
            self.assertEqual(store.next_entry(NOW)[1]['text'], 'Call home')

    def test_calendar_helpers(self):
        weeks = planner.month_cells(2026, 10)  # 1 October 2026 is a Thursday.
        self.assertEqual(weeks[0][:4], [None, None, None, date(2026, 10, 1)])
        self.assertEqual(sum(day is not None for week in weeks for day in week), 31)
        self.assertEqual(planner.shift_month(date(2026, 12, 1), 1), date(2027, 1, 1))
        self.assertEqual(planner.shift_month(date(2026, 1, 1), -1), date(2025, 12, 1))
        self.assertEqual(planner.step_time('23:30', 60), '00:30')
        self.assertEqual(planner.step_time('00:00', -15), '23:45')


class PlannerAppTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.sync = 'Synchronized'
        self.ui = planner.PlannerApp(app, Path(self.folder.name), lambda: self.sync)

    def tearDown(self):
        self.folder.cleanup()
        app.set_rotation(0)

    def draw(self):
        return self.ui.render(0, NOW, ())

    def tap(self, rect):
        return self.ui.tap(*centre(rect), 0)

    def type_text(self, text):
        for char in text:
            keys = keyboard_rects(app.W, app.H, False)
            self.tap(next(rect for rect, key in keys if key == ('space' if char == ' ' else char)))

    def test_add_and_delete_an_entry_in_both_orientations(self):
        for degrees in (0, 90):
            app.set_rotation(degrees)
            ui = self.ui
            self.draw()
            today = next(rect for rect, day in ui.day_rects(NOW.date().replace(day=1)) if day == NOW.date())
            self.tap(today)
            self.assertEqual((ui.view, ui.day), ('day', NOW.date()))
            self.draw()
            self.tap(planner.bottom_buttons(app.W, app.H)[3])  # ADD
            self.assertEqual(ui.view, 'type')
            self.type_text('call home')
            self.draw()
            keys = keyboard_rects(app.W, app.H, False)
            self.tap(next(rect for rect, key in keys if key == 'ask'))  # NEXT
            self.assertEqual((ui.view, ui.time), ('time', '15:00'))  # Next whole hour today.
            self.draw()
            self.tap(ui.time_rects()[2])  # +15m
            self.tap(planner.bottom_buttons(app.W, app.H)[3])  # SAVE
            self.assertEqual(ui.planner.entries(NOW.date()), [{'time': '15:15', 'text': 'call home'}])
            self.assertEqual(ui.note(NOW), 'Next: 15:15 call home')
            self.draw()
            self.tap(ui.entry_rects(1)[0])
            self.assertEqual(ui.selected, 0)
            self.draw()
            self.tap(planner.bottom_buttons(app.W, app.H)[2])  # DELETE
            self.assertEqual(ui.planner.entries(NOW.date()), [])
            self.tap(planner.bottom_buttons(app.W, app.H)[0])  # MONTH
            self.assertEqual(ui.view, 'month')
            self.assertEqual(self.tap(planner.bottom_buttons(app.W, app.H)[3]), 'exit')

    def test_all_day_entry_and_month_paging(self):
        ui = self.ui
        self.draw()
        self.tap(planner.bottom_buttons(app.W, app.H)[2])  # >
        self.assertEqual(ui.shown, date(2026, 11, 1))
        self.tap(planner.bottom_buttons(app.W, app.H)[1])  # TODAY
        self.assertEqual((ui.view, ui.day, ui.shown), ('day', NOW.date(), None))
        self.tap(planner.bottom_buttons(app.W, app.H)[2])  # Next day
        self.assertEqual(ui.day, date(2026, 10, 13))
        self.tap(planner.bottom_buttons(app.W, app.H)[3])
        self.type_text('hike')
        self.tap(next(rect for rect, key in keyboard_rects(app.W, app.H, False) if key == 'ask'))
        self.assertEqual(ui.time, '09:00')  # Not today: a morning default.
        self.tap(planner.bottom_buttons(app.W, app.H)[1])  # ALL DAY
        self.tap(planner.bottom_buttons(app.W, app.H)[3])
        self.assertEqual(ui.planner.entries(date(2026, 10, 13)), [{'time': '', 'text': 'hike'}])

    def test_every_view_draws_in_every_rotation_with_and_without_sync(self):
        ui = self.ui
        ui.planner.add(NOW.date(), 'A long entry that will not fit on one line of the panel', '10:00')
        for degrees in (0, 90, 180, 270):
            app.set_rotation(degrees)
            for sync in ('Synchronized', 'Not synchronized', 'Unknown'):
                self.sync = sync
                for view in ('month', 'day', 'type', 'time'):
                    ui.view, ui.day, ui.text, ui.time = view, NOW.date(), 'x', '10:00'
                    self.assertEqual(self.draw().size, (app.W, app.H))
            for rect, _ in ui.day_rects(date(2026, 10, 1)):
                self.assertTrue(0 <= rect[0] < rect[2] <= app.W and 0 <= rect[1] < rect[3] <= app.H-36)

    def test_clock_warning(self):
        self.assertIsNone(self.ui.clock_note())
        self.sync = 'Not synchronized'
        self.assertEqual(self.ui.clock_note(), 'Clock not synced: check today')
        self.sync = 'Unknown'
        self.assertEqual(self.ui.clock_note(), 'Clock sync unknown')


if __name__ == '__main__':
    unittest.main()
