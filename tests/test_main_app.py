import builtins
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main_app  # noqa: E402
import panel_app  # noqa: E402


def make_screen(folder, width, height, depth=16, name='vc4drmfb'):
    base = Path(folder) / 'fb0'
    base.mkdir()
    (base / 'name').write_text(name + '\n')
    (base / 'virtual_size').write_text(f'{width},{height}\n')
    (base / 'bits_per_pixel').write_text(f'{depth}\n')
    (base / 'stride').write_text(f'{width*depth//8}\n')
    device = Path(folder) / 'dev'
    device.write_bytes(bytes(width*depth//8*height))
    return base, device


class LayoutTest(unittest.TestCase):
    def test_tiles_fit_and_do_not_overlap(self):
        for size in ((1024, 600), (800, 480)):
            rects = main_app.tile_rects(*size)
            self.assertEqual(len(rects), len(main_app.TILES))
            for i, a in enumerate(rects):
                self.assertTrue(0 <= a[0] < a[2] <= size[0] and main_app.HEADER < a[1] < a[3] < size[1])
                for b in rects[i+1:]:
                    self.assertTrue(a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])
            self.assertEqual(main_app.tile_at(size, *rects[7][:2]), 7)
            self.assertIsNone(main_app.tile_at(size, 2, 2))

    def test_pages_render_at_screen_size(self):
        state = {'stats': {'temp': '48.0 C', 'cpu': 12.0, 'ram': '1.2/3.7 GiB'}, 'backend': False,
                 'alerts': [('critical', 'LOW VOLTAGE')], 'selected': 2, 'busy': ['ai']}
        for page in ('home', 'status'):
            image = main_app.render(page, (1024, 600), state)
            self.assertEqual(image.size, (1024, 600))
        self.assertEqual(main_app.render('home', (800, 480), {}).size, (800, 480))

    def test_arrows_walk_the_grid(self):
        self.assertEqual(main_app.move(None, 'right'), 0)  # First press shows the highlight.
        self.assertEqual(main_app.move(0, 'right'), 1)
        self.assertEqual(main_app.move(1, 'down'), 6)
        self.assertEqual(main_app.move(6, 'down'), 6)  # Stays on the grid.
        self.assertEqual(main_app.move(0, 'left'), 0)

    def test_quick_panel_handoff_opens_the_page_and_comes_back(self):
        command = main_app.quick_panel_command('reader', 'hdmi', '/data')
        self.assertTrue(command[1].endswith('panel_app.py'))
        self.assertEqual(command[2:], ['--display', 'hdmi', '--main-app', '--open', 'reader', '--data', '/data'])
        self.assertNotIn('--open', main_app.quick_panel_command('home', 'hdmi'))
        for label, key, colour, where in main_app.TILES:
            self.assertIn(where, ('here', 'home') + panel_app.OPEN_PAGES, label)

    def test_quick_panel_exit_offers_the_main_app(self):
        rows = {}
        try:
            panel_app.MAIN_APP[0] = True
            panel_app.render('exit', None, False, 20, rows, '')
            self.assertEqual(panel_app.hit(160, 150), 'exit:close')  # Labelled MAIN APP.
        finally:
            panel_app.MAIN_APP[0] = False


class ScreenTest(unittest.TestCase):
    def test_draws_at_full_size_and_maps_panel_taps(self):
        with tempfile.TemporaryDirectory() as folder:
            screen = main_app.Screen(*make_screen(folder, 1024, 600))
            try:
                self.assertEqual((screen.size, screen.left, screen.top), ((1024, 600), 0, 0))
                # Calibration is in the quick panel's 2.5x page, centred with 112 px bars.
                self.assertEqual(screen.from_panel(0, 0), (112, 0))
                self.assertEqual(screen.from_panel(320, 240), (912, 600))
                image = Image.new('RGB', (1024, 600), (0, 0, 0))
                screen.show(image)
                self.assertEqual(screen.rows_sent, 600)
                image.paste((255, 255, 255), (1000, 300, 1024, 302))
                screen.show(image)
                self.assertEqual(screen.rows_sent, 602)  # Only the two changed rows.
                if screen.map is not None:
                    screen.map.flush()
                pixels = np.frombuffer((Path(folder) / 'dev').read_bytes(), np.uint16).reshape(600, 1024)
                self.assertEqual(int(pixels[300, 1023]), 0xFFFF)
                self.assertEqual(int(pixels[302, 1023]), 0)
            finally:
                screen.close()

    def test_refuses_the_small_panel(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(RuntimeError):
                main_app.Screen(*make_screen(folder, 320, 240, name='fb_ili9341'))



class FakeScreen:
    def __init__(self, display='hdmi'):
        self.size, self.frames = (1024, 600), 0

    def show(self, image):
        self.frames += 1

    def close(self):
        pass


class FakeKeys:
    """Scripted (seconds, key) presses; records which ones were read."""
    script, read = [], []

    def __init__(self, skip=()):
        self.start, self.devices, self.pending = time.monotonic(), [], list(FakeKeys.script)

    def poll(self):
        if time.monotonic() - self.start > 10:
            raise RuntimeError('main app did not exit')
        keys = []
        while self.pending and time.monotonic() - self.start >= self.pending[0][0]:
            keys.append(self.pending.pop(0)[1])
        FakeKeys.read += keys
        return keys

    def close(self):
        pass


class MainLoopTest(unittest.TestCase):
    def run_main(self, keys, env):
        FakeKeys.script, FakeKeys.read = keys, []
        real_open = builtins.open
        with tempfile.TemporaryDirectory() as folder:
            def local_lock(path, *args, **kwargs):
                if path == '/run/lock/cyberdeck-camera-monitor.lock':
                    path = Path(folder) / 'test.lock'
                return real_open(path, *args, **kwargs)
            stats = mock.Mock()
            stats.read.return_value = {}
            with mock.patch.object(main_app, 'Screen', FakeScreen), \
                    mock.patch.object(main_app, 'Keyboard', FakeKeys), \
                    mock.patch.object(panel_app, 'Touch', side_effect=RuntimeError('no touch')), \
                    mock.patch.object(panel_app, 'Stats', return_value=stats), \
                    mock.patch('builtins.open', side_effect=local_lock), \
                    mock.patch.dict(os.environ, env), \
                    mock.patch.object(sys, 'argv', ['main_app.py', '--backend', 'off', '--data', folder]):
                if 'INVOCATION_ID' not in env:
                    os.environ.pop('INVOCATION_ID', None)
                main_app.main()
        return FakeKeys.read

    def test_ctrl_c_closes_a_terminal_run_but_not_the_boot_service(self):
        keys = [(.1, 'quit'), (2.5, '0')]  # Ctrl+C, then the CLOSE tile (home waits 1 s a loop).
        self.assertEqual(self.run_main(keys, {}), ['quit'])
        self.assertEqual(self.run_main(keys, {'INVOCATION_ID': 'test'}), ['quit', '0'])


if __name__ == '__main__':
    unittest.main()
