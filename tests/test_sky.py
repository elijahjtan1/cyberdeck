"""Sun and moon page: offline sunrise/sunset and moon phase, checked against published times."""
import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import panel_app as app  # noqa: E402
from panel_tools import sky  # noqa: E402
from panel_tools.ui import ToolsUI  # noqa: E402

LONDON = (51.5074, -0.1278)
UTC = timezone.utc


def hhmm(moment):
    return moment.hour*60 + moment.minute


class SunTest(unittest.TestCase):
    def assertNear(self, moment, expected, minutes=3):
        self.assertLessEqual(abs(hhmm(moment) - (expected[0]*60 + expected[1])), minutes, moment)

    def test_london_matches_published_times(self):
        # Published UTC times for London (timeanddate.com / HM Nautical Almanac Office).
        for day, rise, set_ in ((date(2026, 6, 21), (3, 43), (20, 21)),
                                (date(2026, 12, 21), (8, 4), (15, 53)),
                                (date(2026, 3, 20), (6, 3), (18, 13))):
            sunrise, sunset = sky.sun_events(day, *LONDON)
            self.assertNear(sunrise, rise)
            self.assertNear(sunset, set_)
        dawn, dusk = sky.sun_events(date(2026, 6, 21), *LONDON, altitude=sky.CIVIL_ALTITUDE)
        self.assertNear(dawn, (2, 56))
        self.assertNear(dusk, (21, 8))

    def test_southern_hemisphere_rise_falls_on_the_local_date(self):
        sydney = timezone(timedelta(hours=10))
        sunrise, sunset = sky.sun_events(date(2026, 6, 21), -33.87, 151.21)
        # Published: 07:00 and 16:54 AEST.
        self.assertEqual(sunrise.astimezone(sydney).date(), date(2026, 6, 21))
        self.assertNear(sunrise.astimezone(sydney), (7, 0))
        self.assertNear(sunset.astimezone(sydney), (16, 54))

    def test_polar_day_and_night(self):
        self.assertEqual(sky.sun_events(date(2026, 6, 21), 78.2, 15.6), ('up', 'up'))
        self.assertEqual(sky.sun_events(date(2026, 12, 21), 78.2, 15.6), ('down', 'down'))


class MoonTest(unittest.TestCase):
    def test_known_full_and_new_moons(self):
        for moment in (datetime(2026, 1, 3, 10, 3, tzinfo=UTC), datetime(2026, 3, 3, 11, 38, tzinfo=UTC)):
            _, lit, name, _, _ = sky.moon_phase(moment)
            self.assertGreater(lit, .97)
            self.assertEqual(name, 'Full moon')
        _, lit, name, _, _ = sky.moon_phase(datetime(2026, 2, 17, 12, 1, tzinfo=UTC))
        self.assertLess(lit, .03)
        self.assertEqual(name, 'New moon')

    def test_quarter_is_half_lit_and_waxing(self):
        # First quarter, 2026-01-26 04:47 UTC.
        info = sky.sky_lines(datetime(2026, 1, 26, 4, 47, tzinfo=UTC), None)
        self.assertAlmostEqual(info['lit'], .5, delta=.1)
        self.assertTrue(info['waxing'])
        self.assertIn('Full moon in about', info['next'])


class LocationTest(unittest.TestCase):
    def test_only_valid_positions_are_used(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'location.json'
            self.assertIsNone(sky.load_location(path))
            for bad in ('not json', '[]', '{"lat": 95, "lon": 0}', '{"lat": "x", "lon": 1}',
                        '{"lat": NaN, "lon": 0}', '{"lon": 3}'):
                path.write_text(bad)
                self.assertIsNone(sky.load_location(path), bad)
            path.write_text(json.dumps({'lat': -33.9, 'lon': 151.2}))
            self.assertEqual(sky.load_location(path), (-33.9, 151.2))
        self.assertEqual(sky.position_label(-33.87, 151.21), '33.9S 151.2E')

    def test_without_a_location_no_sun_times_are_invented(self):
        info = sky.sky_lines(datetime(2026, 10, 10, 12, tzinfo=UTC), None)
        self.assertEqual(info['sun'], [])
        self.assertEqual(info['where'], 'No saved location')

    def test_lines_use_the_wall_clock_zone(self):
        summer = timezone(timedelta(hours=1))
        info = sky.sky_lines(datetime(2026, 6, 21, 12, tzinfo=summer), LONDON)
        self.assertEqual(info['where'], 'Saved location 51.5N 0.1W')
        self.assertTrue(info['sun'][0].startswith('Sunrise 04:4'), info['sun'])
        self.assertIn('Daylight 16h 3', info['sun'][2])
        polar = sky.sky_lines(datetime(2026, 6, 21, 12, tzinfo=UTC), (78.2, 15.6))
        self.assertEqual(polar['sun'][0], 'Sun up all day')
        self.assertEqual(polar['sun'][2], 'No sunrise or sunset today')
        self.assertFalse(polar['south'])
        self.assertTrue(sky.sky_lines(datetime(2026, 6, 21, 12, tzinfo=UTC), (-33.9, 151.2))['south'])


class ClockTest(unittest.TestCase):
    def test_unsynced_clock_is_flagged(self):
        moment = datetime(2026, 10, 10, 12, tzinfo=UTC)
        self.assertIsNone(sky.sky_lines(moment, LONDON, 'Synchronized')['clock'])
        self.assertEqual(sky.sky_lines(moment, LONDON, 'Not synchronized')['clock'],
                         'Clock not synced: check the date')
        self.assertEqual(sky.sky_lines(moment, None)['clock'], 'Clock sync unknown: check the date')


class SkyPageTest(unittest.TestCase):
    def tearDown(self):
        app.set_rotation(0)

    def test_moon_disc_lights_the_requested_side(self):
        from PIL import Image, ImageDraw
        for right in (True, False):
            image = Image.new('RGB', (40, 40))
            ToolsUI.draw_moon(None, ImageDraw.Draw(image), (20, 20), 15, .5, right)
            lit_x, dark_x = (28, 12) if right else (12, 28)
            self.assertEqual(image.getpixel((lit_x, 20)), (0xe8, 0xe4, 0xd0))
            self.assertEqual(image.getpixel((dark_x, 20)), (0x26, 0x32, 0x3c))

    def test_page_renders_with_and_without_location_in_both_orientations(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory); (data/'photos').mkdir()
            audio = mock.Mock(); audio.state = {}
            ui = ToolsUI(app, data, data, app.PhotoLibrary(data/'photos'), dict(app.DEFAULT_OPTIONS),
                         audio, {})
            try:
                ui.menu = 1
                ui.render(0, datetime(2026, 10, 10, 12, 30), ())
                sky_button = next(rect for rect, action in ui.rects if action == 'go:sky')
                ui.tap((sky_button[0]+sky_button[2])/2, (sky_button[1]+sky_button[3])/2, 0)
                self.assertEqual(ui.page, 'sky')
                for degrees, sync in ((0, 'Synchronized'), (90, 'Synchronized'),
                                      (0, 'Not synchronized'), (90, 'Unknown')):
                    app.set_rotation(degrees)
                    ui.rows = {'sync': sync}
                    (data/'location.json').unlink(missing_ok=True)
                    self.assertEqual(ui.render(0, datetime(2026, 10, 10, 12, 30), ()).size, (app.W, app.H))
                    (data/'location.json').write_text('{"lat": 51.5, "lon": -0.13}')
                    self.assertEqual(ui.render(0, datetime(2026, 10, 10, 12, 30), ()).size, (app.W, app.H))
            finally:
                ui.close()


if __name__ == '__main__':
    unittest.main()
