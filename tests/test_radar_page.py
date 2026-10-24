"""RADAR page of the main app: states, tracking and polling, with a fake backend."""
import io
import json
import sys
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main_app  # noqa: E402
from panel_tools import radar_page  # noqa: E402
from panel_tools.radar_page import RadarPage  # noqa: E402

WALL = datetime(2026, 10, 24, 6, 30)
LIVE = {'configured': True, 'source': 'serial', 'simulated': False, 'stale': False, 'age_s': 0.1,
        'range_m': 6.0, 'fov_deg': 120, 'error': None,
        'targets': [{'x_mm': -800, 'y_mm': 2500, 'speed_cms': -40, 'moving': True},
                    {'x_mm': 1500, 'y_mm': 4200, 'speed_cms': 0, 'moving': False}]}


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class FakeBackend:
    def __init__(self, answer):
        self.answer, self.urls = answer, []

    def open(self, url, timeout=None):
        self.urls.append(url)
        if isinstance(self.answer, Exception):
            raise self.answer
        body = self.answer if isinstance(self.answer, bytes) else json.dumps(self.answer).encode()
        return Response(body)


class Clock:
    def __init__(self):
        self.t = 100.

    def __call__(self):
        return self.t


def has_colour(image, colour):
    return bool((np.asarray(image) == colour).all(axis=2).any())


def page(answer, clock=None, base='http://127.0.0.1:8765'):
    view = RadarPage(main_app, base, opener=FakeBackend(answer), clock=clock or Clock())
    view.keep_polling = lambda: None  # Tests call fetch() themselves.
    return view


class HelpersTest(unittest.TestCase):
    def test_heartbeat_has_two_beats_a_second(self):
        self.assertAlmostEqual(radar_page.beat(0), 1.)
        self.assertGreater(radar_page.beat(.25), .6)            # The softer second beat.
        self.assertLess(radar_page.beat(.18), radar_page.beat(.25))
        self.assertLess(radar_page.beat(.7), .05)                # Quiet between beats.
        self.assertAlmostEqual(radar_page.beat(3.25), radar_page.beat(.25))

    def test_wording(self):
        self.assertEqual(radar_page.bearing(-17.6), '18° L')
        self.assertEqual(radar_page.bearing(0.2), 'ahead')
        self.assertEqual(radar_page.side_text(-40), 'approaching')
        self.assertEqual(radar_page.side_text(25), 'moving away')
        self.assertEqual(radar_page.side_text(0), 'still')


class StatesTest(unittest.TestCase):
    def test_every_state_is_named_honestly_and_draws(self):
        old = HTTPError('u', 404, 'Not Found', {}, io.BytesIO(b''))
        cases = [
            (URLError('refused'), 'offline', 'backend not running'),
            (old, 'old', 'no radar support'),
            (HTTPError('u', 500, 'Boom', {}, io.BytesIO(b'')), 'offline', 'error 500'),
            (b'not json', 'offline', 'backend not running'),
            ({'configured': False, 'targets': [], 'simulated': False, 'error': None}, 'unset', 'No radar'),
            ({**LIVE, 'targets': [], 'stale': True, 'age_s': 4.2}, 'stale', 'No data for 4 s'),
            ({**LIVE, 'targets': [], 'age_s': None}, 'waiting', 'Waiting'),
            ({**LIVE, 'targets': []}, 'quiet', 'No movement'),
            (LIVE, 'live', ''),
        ]
        for answer, state, words in cases:
            view = page(answer)
            view.fetch()
            got = view.status()
            self.assertEqual(got[0], state, answer)
            self.assertIn(words, got[1])
            self.assertEqual(view.render(0., WALL, []).size, (1024, 600))
            self.assertEqual(view.delay, radar_page.FRAME_S if state in ('live', 'quiet') else .5)
        view = page(LIVE, base=None)
        view.fetch()
        self.assertEqual(view.status()[0], 'off')
        self.assertEqual(view.opener.urls, [])

    def test_stale_or_lost_data_clears_the_dots(self):
        view = page(LIVE)
        view.fetch()
        self.assertEqual(len(view.blips), 2)
        view.opener.answer = {**LIVE, 'targets': [], 'stale': True, 'age_s': 3}
        view.fetch()
        self.assertEqual(view.blips, [])  # NO SIGNAL never leaves old contacts on screen.
        view.opener.answer = LIVE
        view.fetch()
        view.opener.answer = URLError('gone')
        view.fetch()
        self.assertEqual(view.blips, [])

    def test_simulated_is_labelled_on_screen(self):
        view = page({**LIVE, 'simulated': True, 'label': 'SIMULATED'})
        view.fetch()
        image = view.render(0., WALL, [])
        # The label is drawn large in orange at the top right.
        corner = image.crop((700, main_app.HEADER + 40, 1024, main_app.HEADER + 90))
        self.assertTrue(has_colour(corner, (242, 155, 66)))
        plain = page(LIVE)
        plain.fetch()
        corner = plain.render(0., WALL, []).crop((700, main_app.HEADER + 40, 1024, main_app.HEADER + 90))
        self.assertFalse(has_colour(corner, (242, 155, 66)))


class TrackingTest(unittest.TestCase):
    def test_contacts_glide_fade_and_go(self):
        clock = Clock()
        view = page({**LIVE, 'targets': LIVE['targets'][:1]}, clock)
        view.fetch()
        clock.t += .2  # The contact moves 30 cm: still one dot.
        view.opener.answer = {**LIVE, 'targets': [{**LIVE['targets'][0], 'x_mm': -500}]}
        view.fetch()
        self.assertEqual([b[0] for b in view.blips], [-500])
        clock.t += .2  # It disappears: the dot stays to fade, then goes.
        view.opener.answer = {**LIVE, 'targets': []}
        view.fetch()
        self.assertEqual(len(view.blips), 1)
        clock.t += radar_page.FRESH_S + radar_page.FADE_S
        view.fetch()
        self.assertEqual(view.blips, [])

    def test_contact_is_drawn_where_the_radar_puts_it(self):
        view = page({**LIVE, 'targets': [{'x_mm': 0, 'y_mm': 3000, 'speed_cms': 0, 'moving': False}]})
        view.fetch()
        image = view.render(0.5, WALL, [])
        (cx, cy), radius = view.geometry()
        red, green, blue = image.getpixel((cx, round(cy - radius / 2)))  # 3 m of 6 m, straight ahead.
        self.assertGreater(red, green + 40)


class InputTest(unittest.TestCase):
    def test_home_button_and_keys_leave(self):
        view = page(LIVE)
        r = view.home_rect()
        self.assertEqual(view.tap((r[0]+r[2])/2, (r[1]+r[3])/2, 0), 'exit')
        self.assertIsNone(view.tap(512, 300, 0))
        self.assertEqual(view.key('esc', 0), 'exit')
        self.assertIsNone(view.key('a', 0))


class PollingTest(unittest.TestCase):
    def test_polls_only_while_drawn(self):
        backend = FakeBackend(LIVE)
        view = RadarPage(main_app, 'http://127.0.0.1:8765', opener=backend)
        with mock.patch.object(radar_page, 'IDLE_S', .3), mock.patch.object(radar_page, 'POLL_S', .05):
            view.render(time.monotonic(), WALL, [])
            view.thread.join(2)
            self.assertFalse(view.thread.is_alive())  # Stopped once nothing drew it.
            calls = len(backend.urls)
            self.assertGreaterEqual(calls, 2)
            time.sleep(.2)
            self.assertEqual(len(backend.urls), calls)
        self.assertTrue(backend.urls[0].endswith('/api/radar'))
        self.assertEqual(view.status()[0], 'live')


if __name__ == '__main__':
    unittest.main()
