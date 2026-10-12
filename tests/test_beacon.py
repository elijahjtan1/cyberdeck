"""SOS & LIGHT: Morse timing, flash rate and the panel app's touch flow."""
import sys
import unittest
from unittest import mock
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import panel_app as app  # noqa: E402
from panel_tools import beacon  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402


def centre(rect):
    return (rect[0]+rect[2])/2, (rect[1]+rect[3])/2


CLOCK = datetime(2026, 10, 11, 12, 0)


class MorseTimingTest(unittest.TestCase):
    def test_sos_has_standard_units(self):
        steps = beacon.schedule('SOS', unit=1)
        # S: 1 on, 1 off, 1 on, 1 off, 1 on; letter gap 3; O: three 3-unit dashes; then S.
        expected = ([(True, 1), (False, 1)]*2 + [(True, 1), (False, 3)]
                    + [(True, 3), (False, 1)]*2 + [(True, 3), (False, 3)]
                    + [(True, 1), (False, 1)]*2 + [(True, 1), (False, 7)])
        self.assertEqual(steps, expected)
        self.assertEqual(sum(s for _, s in steps), 34)

    def test_words_and_unknown_characters(self):
        self.assertEqual(beacon.schedule('e e', unit=1), [(True, 1), (False, 7)]*2)
        self.assertEqual(beacon.schedule('?!', unit=1), [(False, 7)])

    def test_state_loops_and_counts_down(self):
        steps = beacon.schedule('SOS', unit=1)
        self.assertEqual(beacon.state_at(steps, 0), (True, 1))
        self.assertEqual(beacon.state_at(steps, 1.5), (False, .5))
        self.assertEqual(beacon.state_at(steps, 34 + 8), (True, 3))  # First dash, next pass.
        self.assertEqual(beacon.state_at([], 5), (False, 1.))

    def test_never_flashes_faster_than_two_a_second(self):
        steps = beacon.schedule('SOS')
        for (lit, on), (_, off) in zip(steps, steps[1:]):
            if lit:
                self.assertGreaterEqual(on + off, .5)


class SignalAppTest(unittest.TestCase):
    def tearDown(self):
        app.set_rotation(0)
        app.BACKLIGHT_PWM = False

    def test_menu_buttons_fit_and_start_each_mode(self):
        for rotation in (0, 90):
            app.set_rotation(rotation)
            signal = app.SignalApp()
            image = signal.render(0, CLOCK, ())
            self.assertEqual(image.size, (app.W, app.H))
            self.assertIsNone(signal.level)  # Menu keeps the user's brightness.
            for rect in signal.rects():
                self.assertTrue(0 <= rect[0] < rect[2] <= app.W and 24 < rect[1] < rect[3] <= app.H,
                                (rotation, rect))
            sos, torch, leave = (centre(r) for r in signal.rects())
            self.assertIsNone(signal.tap(*sos, 10))
            self.assertEqual(signal.mode, 'sos')
            self.assertIsNone(signal.tap(5, 5, 11))  # Any tap stops the light.
            self.assertIsNone(signal.mode)
            signal.tap(*torch, 12)
            self.assertEqual(signal.mode, 'torch')
            signal.tap(*leave, 13)
            self.assertIsNone(signal.mode)
            self.assertEqual(signal.tap(*leave, 14), 'exit')

    def test_beacon_frames_follow_the_schedule(self):
        signal = app.SignalApp()
        signal.tap(*centre(signal.rects()[0]), 100)
        lit = signal.render(100.1, CLOCK, ())
        self.assertEqual(lit.getpixel((app.W//2, app.H//2)), (255, 255, 255))
        self.assertEqual(signal.level, 100)
        self.assertAlmostEqual(signal.delay, beacon.UNIT - .1)
        dark = signal.render(100 + beacon.UNIT + .05, CLOCK, ())
        self.assertEqual(dark.getpixel((5, 5)), (0, 0, 0))
        self.assertEqual(signal.level, 0)

    def test_torch_is_full_white(self):
        signal = app.SignalApp()
        signal.tap(*centre(signal.rects()[1]), 0)
        image = signal.render(50, CLOCK, ())
        self.assertEqual(image.getextrema(), ((255, 255),)*3)
        self.assertEqual(signal.level, 100)

    def test_alerts_stay_readable_while_flashing(self):
        signal = app.SignalApp()
        signal.tap(*centre(signal.rects()[0]), 0)
        alerts = [('warning', 'TIMER DONE - tap to dismiss')]
        dark = signal.render(beacon.UNIT + .05, CLOCK, alerts)  # Between flashes.
        self.assertEqual(dark.getpixel((2, 2)), (0xf0, 0xb3, 0x47))  # Warning banner.
        self.assertEqual(dark.getpixel((5, app.H-5)), (0, 0, 0))     # Still dark below it.
        self.assertEqual(signal.level, 100)

    def test_light_level_hook_restores_the_users_level(self):
        options = {**app.DEFAULT_OPTIONS, 'pixel_level': 70}
        signal = app.SignalApp()
        self.assertEqual(app.app_light_level(signal, options), 70)  # Menu.
        self.assertEqual(app.app_light_level(app.VisualApp(None, None), options), 70)
        signal.tap(*centre(signal.rects()[0]), 0)
        signal.render(beacon.UNIT + .05, CLOCK, ())
        pwm = mock.Mock(available=True)
        app.Brightness(mock.Mock(pixel_level=100), pwm).apply(app.app_light_level(signal, options))
        pwm.set.assert_called_with(0)  # Dark phase: LEDs really off.
        signal.tap(5, 5, 1)
        signal.render(2, CLOCK, ())
        self.assertEqual(app.app_light_level(signal, options), 70)

    def test_apps_tile_text_stays_inside_tiles(self):
        notes = {'audio': 'Volume, media, visualizer', 'games': '9 games: arcade and puzzles',
                 'album': '12 pictures | slideshow', 'gifs': 'Copy GIFs to cyberdeck-data/gifs',
                 'tools': 'Timers, network, GNSS and more', 'quick': 'Camera, sound, sleep',
                 'sos': 'SOS beacon and torch', 'planner': 'Next: Fri 19:30 Check the antenna mast'}
        d = ImageDraw.Draw(Image.new('RGB', (8, 8)))
        for rotation in (0, 90):
            app.set_rotation(rotation)
            rects = app.grid_rects(len(app.APP_TILES), 30, app.content_bottom()+4, max_pitch=200)
            for rect, (label, key) in zip(rects, app.APP_TILES):
                lines = app.tile_text(d, rect, label, notes[key])
                self.assertTrue(lines, key)
                self.assertNotIn('...', lines[0][0], (rotation, key))  # Titles stay whole.
                for line, y, selected, _ in lines:
                    box = d.textbbox(((rect[0]+rect[2])//2, y), line, font=selected, anchor='mm')
                    self.assertTrue(rect[0] <= box[0] and box[2] <= rect[2]
                                    and rect[1] <= box[1] and box[3] <= rect[3],
                                    (rotation, key, line, box, rect))

    def test_listed_in_apps_and_never_covered_by_a_saver(self):
        self.assertIn(('SOS & LIGHT', 'sos'), app.APP_TILES)
        options = {**app.DEFAULT_OPTIONS, 'saver_style': 'radar', 'saver_delay': 1}
        self.assertFalse(app.saver_due(options, 10**6, 'sos'))


if __name__ == '__main__':
    unittest.main()
