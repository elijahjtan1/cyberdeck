"""STATUS -> HISTORY page and the panel's link to cyberdeck-core."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
import panel_app as app  # noqa: E402


def centre(rect):
    return (rect[0]+rect[2])/2, (rect[1]+rect[3])/2


def history(seconds, now=10_000.):
    samples = [[now - seconds + i*seconds/20, 10. + i, 50. + i, 30., int(i == 5), 0]
               for i in range(20)]
    events = [{'kind': 'undervoltage', 'detail': '', 'start': now-100, 'end': now-95},
              {'kind': 'ai', 'detail': 'q', 'start': now-300, 'end': now-200}]
    return {'now': now, 'seconds': seconds, 'samples': samples, 'events': events}


ROWS = {'temp': '48.2 C', 'disk_free': '18 GiB', 'cpu': 12, 'ram_percent': 30}


class HistoryPageTest(unittest.TestCase):
    def tearDown(self):
        app.set_rotation(0)

    def ink(self, image, box):
        return len(set(image.crop(box).getdata()))

    def test_states_and_range_buttons(self):
        for rotation in (0, 90):
            app.set_rotation(rotation)
            bottom = app.content_bottom()
            offline = app.render('history', None, False, 20, {**ROWS, 'backend_online': False}, '')
            loading = app.render('history', None, False, 20,
                                 {**ROWS, 'backend_online': True, 'history_range': 3600}, '')
            stale = app.render('history', None, False, 20, {
                **ROWS, 'backend_online': True, 'history_range': 86400, 'history': history(3600)}, '')
            body = (0, 52, app.W, bottom)  # Below the range buttons.
            self.assertEqual(loading.crop(body).tobytes(), stale.crop(body).tobytes())  # Still loading.
            shown = app.render('history', None, False, 20, {
                **ROWS, 'backend_online': True, 'history_range': 3600, 'history': history(3600)}, '')
            self.assertNotEqual(shown.tobytes(), loading.tobytes())
            self.assertNotEqual(offline.tobytes(), loading.tobytes())
            self.assertGreater(self.ink(shown, (28, 66, app.W-10, bottom-30)), 4)  # Lines drawn.
            for (seconds, _), rect in zip(app.HISTORY_RANGES, app.row_rects(4, 28, 50)):
                self.assertEqual(app.hit(*centre(rect)), f'hist:{seconds}')

    def test_status_page_has_history_and_details(self):
        for rotation in (0, 90):
            app.set_rotation(rotation)
            app.render('status', None, False, 20, ROWS, '')
            actions = [a for _, a in app.HOTSPOTS]
            self.assertIn('status:history', actions)
            self.assertIn('status:details', actions)
            rects = {a: r for r, a in app.HOTSPOTS}
            a, b = rects['status:history'], rects['status:details']
            self.assertTrue(a[2] < b[0] and 0 <= a[0] and b[2] <= app.W, rotation)

    def test_ai_badge_in_header(self):
        quiet = app.render('status', None, False, 20, ROWS, '')
        busy = app.render('status', None, False, 20, {**ROWS, 'busy': ['ai']}, '')
        self.assertNotEqual(quiet.crop((150, 0, 320, 24)).tobytes(), busy.crop((150, 0, 320, 24)).tobytes())
        self.assertEqual(quiet.crop((0, 26, 320, 240)).tobytes(), busy.crop((0, 26, 320, 240)).tobytes())


class FakeBackend:
    instances = []

    def __init__(self, *args):
        self.args, self.online, self.history, self.busy = args, True, None, []
        self.base = 'http://127.0.0.1:8765'
        self.wanted, self.marks, self.closed = [], [], False
        FakeBackend.instances.append(self)

    def start(self):
        pass

    def close(self):
        self.closed = True

    def mark(self, kind, detail=''):
        self.marks.append((kind, detail))

    def want_history(self, seconds):
        self.wanted.append(seconds)
        if seconds:
            self.history = history(seconds)


class HistoryLoopTest(unittest.TestCase):
    def test_status_history_range_and_back(self):
        import test_panel_app as t
        FakeBackend.instances = []
        script = [(.5, 184, 222),  # STATUS tab
                  (1.1, 161, 188),  # HISTORY
                  (1.7, 200, 39),  # 24H
                  (2.9, 300, 222), (3.5, 160, 150)]  # EXIT -> close
        t.FakeTouch.script, t.FakeScreen.frames = script, []
        pages = []
        real_render = app.render

        def spy(page, *a, **k):
            if not pages or pages[-1] != page:
                pages.append(page)
            return real_render(page, *a, **k)
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder)
            (data / 'photos').mkdir()
            (data / 'touch.json').write_text(json.dumps(
                {'device': ['fake', [0, 320], [0, 240]], 'matrix': [[1, 0], [0, 1], [0, 0]]}))
            (data / 'options.json').write_text(json.dumps({'rotation': 0}))
            with mock.patch.object(app, 'Framebuffer', t.FakeScreen), \
                    mock.patch.object(app, 'Touch', t.FakeTouch), \
                    mock.patch.object(app, 'Camera', t.FakeCamera), \
                    mock.patch.object(app, 'Backend', FakeBackend), \
                    mock.patch.object(app, 'render', spy), \
                    mock.patch.object(app, 'boot_service_enabled', return_value=False), \
                    mock.patch.object(app, 'command_output', return_value=''), \
                    mock.patch.object(sys, 'argv', ['panel_app.py', '--data', str(data)]):
                try:
                    app.main()
                finally:
                    app.set_rotation(0)
        backend = FakeBackend.instances[0]
        self.assertIn('history', pages)
        self.assertEqual(pages[pages.index('history')-1], 'status')
        self.assertIn(86400, backend.wanted)
        self.assertEqual(backend.wanted[-1], None)  # Stops fetching once the page closes.
        self.assertTrue(backend.closed)

    def test_backend_off_switch(self):
        import test_panel_app as t
        FakeBackend.instances = []
        t.FakeTouch.script, t.FakeScreen.frames = [(.5, 300, 222), (1.1, 160, 150)], []
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder)
            (data / 'photos').mkdir()
            (data / 'touch.json').write_text(json.dumps(
                {'device': ['fake', [0, 320], [0, 240]], 'matrix': [[1, 0], [0, 1], [0, 0]]}))
            (data / 'options.json').write_text(json.dumps({'rotation': 0}))
            with mock.patch.object(app, 'Framebuffer', t.FakeScreen), \
                    mock.patch.object(app, 'Touch', t.FakeTouch), \
                    mock.patch.object(app, 'Camera', t.FakeCamera), \
                    mock.patch.object(app, 'Backend', FakeBackend), \
                    mock.patch.object(app, 'boot_service_enabled', return_value=False), \
                    mock.patch.object(app, 'command_output', return_value=''), \
                    mock.patch.object(sys, 'argv', ['panel_app.py', '--data', str(data),
                                                    '--backend', 'off']):
                try:
                    app.main()
                finally:
                    app.set_rotation(0)
        self.assertEqual(FakeBackend.instances, [])


if __name__ == '__main__':
    unittest.main()
