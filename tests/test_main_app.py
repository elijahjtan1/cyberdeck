import sys
import tempfile
import unittest
from pathlib import Path

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
            empty = rects[main_app.COLS - 1][0] + 5, rects[-1][1] + 5  # The 12th cell has no tile.
            self.assertIsNone(main_app.tile_at(size, *empty))

    def test_tile_labels_fit_their_tiles(self):
        from PIL import Image, ImageDraw
        d = ImageDraw.Draw(Image.new('RGB', (1, 1)))
        for size in ((1024, 600), (800, 480)):
            for rect, (label, *_) in zip(main_app.tile_rects(*size), main_app.TILES):
                self.assertTrue(any(d.textlength(label, font=f) < rect[2]-rect[0]-20 for f in main_app.TILE_FONTS),
                                (size, label))

    def test_radar_is_the_eleventh_tile(self):
        self.assertEqual(len(main_app.TILE_KEYS), len(main_app.TILES))
        self.assertEqual(main_app.TILES[main_app.TILE_KEYS.index('R')][1], 'radar')
        self.assertEqual(main_app.TILE_KEYS[:10], '1234567890')

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
        self.assertEqual(main_app.move(1, 'down'), 5)
        self.assertEqual(main_app.move(9, 'down'), 9)  # Stays on the grid.
        self.assertEqual(main_app.move(7, 'down'), 7)  # The empty 12th cell is skipped.
        self.assertEqual(main_app.move(10, 'right'), 10)
        self.assertEqual(main_app.move(6, 'down'), 10)
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


if __name__ == '__main__':
    unittest.main()
