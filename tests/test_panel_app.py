"""Hardware-free tests. Run: python3 -m unittest discover -s tests -v"""
import json
import builtins
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from collections import deque
from datetime import datetime

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import panel_app as app  # noqa: E402


def scene(size=(320, 240)):
    image = Image.new('RGB', size, '#8ab4c0')
    d = ImageDraw.Draw(image)
    w, h = size
    d.rectangle((0, h*.6, w, h), fill='#3f7a62')
    d.polygon([(w*.1, h*.6), (w*.3, h*.2), (w*.5, h*.6)], fill='#355566')
    return image


class OptionsTest(unittest.TestCase):
    def test_new_options_default_and_validate(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'options.json'
            path.write_text(json.dumps({'photo_size': 'preview', 'snap_timer': 10,
                                        'alerts': 'bogus', 'pixel_level': 70}))
            options = app.load_options(path)
            self.assertEqual(options['photo_size'], 'preview')
            self.assertEqual(options['snap_timer'], 10)
            self.assertEqual(options['alerts'], 'on')
            self.assertEqual(options['pixel_level'], 70)
            path.write_text(json.dumps({'snap_timer': '3'}))
            self.assertEqual(app.load_options(path)['snap_timer'], 0)


class AlertTest(unittest.TestCase):
    def test_quiet_system_has_no_alerts(self):
        rows = {'power': 'No undervoltage', 'limits': 'None active', 'temp_c': 52.0,
                'disk_free_gib': 18.0, 'disk_used_percent': 40}
        self.assertEqual(app.health_alerts(rows), [])

    def test_unknown_readings_raise_nothing(self):
        self.assertEqual(app.health_alerts({'power': 'Unknown', 'limits': 'Unknown',
                                            'temp_c': None}), [])

    def test_levels_and_order(self):
        rows = {'power': 'LOW VOLTAGE', 'limits': 'Throttled, Capped', 'temp_c': 76.4,
                'disk_free_gib': .1, 'disk_used_percent': 99}
        alerts = app.health_alerts(rows)
        self.assertEqual([a[0] for a in alerts], ['critical', 'critical', 'warning', 'warning'])
        texts = [a[1] for a in alerts]
        self.assertIn('LOW VOLTAGE', texts)
        self.assertIn('THROTTLED', texts)
        self.assertIn('WARM 76 C', texts)
        self.assertTrue(any(t.startswith('STORAGE') for t in texts))

    def test_hot_and_low_storage(self):
        alerts = app.health_alerts({'temp_c': 81, 'disk_free_gib': .8, 'disk_used_percent': 50})
        self.assertEqual(alerts, [('critical', 'HOT 81 C'), ('warning', 'LOW STORAGE 0.8 GB')])

    def test_recent_dip_expires(self):
        rows = {'power': 'No undervoltage', 'undervolt_seen_at': 100.0}
        self.assertEqual(app.health_alerts(rows, now=150), [('warning', 'VOLTAGE DIPPED')])
        self.assertEqual(app.health_alerts(rows, now=500), [])

    def test_sticky_bit_only_counts_when_it_changes_during_run(self):
        poller = app.HealthPoller()
        self.assertIsNone(poller.note_throttle(0x50000, now=1))  # Old dip from before launch.
        self.assertIsNone(poller.note_throttle(0x50000, now=2))
        poller = app.HealthPoller()
        poller.note_throttle(0, now=1)
        self.assertEqual(poller.note_throttle(0x10000, now=5), 5)
        self.assertEqual(poller.note_throttle(0x1, now=9), 9)
        self.assertIsNone(app.decode_throttle('garbage')['flags'])
        self.assertEqual(app.decode_throttle('throttled=0x50005')['flags'], 0x50005)


class FramebufferLookupTest(unittest.TestCase):
    def test_finds_panel_when_renumbered(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name, label in (('fb0', 'vc4drmfb'), ('fb1', 'fb_ili9341')):
                (root / name).mkdir()
                (root / name / 'name').write_text(label + '\n')
            self.assertEqual(app.find_panel_framebuffer(root).name, 'fb1')
            (root / 'fb1' / 'name').write_text('other')
            with self.assertRaises(RuntimeError):
                app.find_panel_framebuffer(root)


class PhotoLibraryTest(unittest.TestCase):
    def test_save_list_preview_delete(self):
        with tempfile.TemporaryDirectory() as folder:
            library = app.PhotoLibrary(Path(folder))
            self.assertIsNone(library.latest())
            first = library.save(scene((1640, 1232)))
            time.sleep(.01)
            second = library.save(scene())
            self.assertEqual(library.items, [first, second])
            self.assertEqual(library.latest(), second)
            picture, size = library.preview(first, (320, 240))
            self.assertEqual(size, (1640, 1232))
            self.assertLessEqual(picture.width, 320)
            self.assertLessEqual(picture.height, 240)
            self.assertIs(library.preview(first, (320, 240))[0], picture)  # Cached.
            self.assertRegex(app.photo_label(first), r'^\d{4}/\d\d/\d\d \d\d:\d\d:\d\d$')
            self.assertFalse(list(Path(folder).glob('.*.tmp')))
            library.delete(first)
            self.assertEqual(library.items, [second])
            self.assertEqual(library.preview(first, (320, 240)), (None, None))
            with self.assertRaises(OSError):
                library.delete(Path('/etc/hostname'))

    def test_ignores_non_photos_and_bad_files(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / 'notes.txt').write_text('x')
            broken = Path(folder) / 'photo-20261009-120000-000000.jpg'
            broken.write_bytes(b'not a jpeg')
            library = app.PhotoLibrary(Path(folder))
            self.assertEqual(library.items, [broken])
            self.assertEqual(library.preview(broken, (60, 45)), (None, None))

    def test_refuses_when_storage_nearly_full(self):
        with tempfile.TemporaryDirectory() as folder:
            library = app.PhotoLibrary(Path(folder))
            with mock.patch.object(app, 'MIN_FREE_BYTES', 2**62):
                with self.assertRaisesRegex(OSError, 'storage'):
                    library.save(scene())
            self.assertEqual(library.refresh(), [])


class LayoutTest(unittest.TestCase):
    rows = {'cpu': 20, 'temp': '48.0 C', 'ram': '1.1/3.7 GiB', 'ram_percent': 30,
            'disk_free': '18 GiB', 'disk_used_percent': 40, 'uptime': '1h 2m'}

    def test_every_page_renders(self):
        photo = {'image': scene((640, 480)), 'size': (3280, 2464), 'index': 1, 'count': 3,
                 'label': '2026/10/09 19:12:33'}
        cam = {'thumb': scene((60, 45)), 'countdown': 3, 'flash': False,
               'capturing': None, 'saved': '3280x2464'}
        alerts = [('critical', 'LOW VOLTAGE'), ('warning', 'WARM 77 C')]
        capture = app.AudioCapture(which=lambda name: None)
        extra = {'notes': {'snake': 'Best score: 4'}, 'capture': capture,
                 'audio': {'backend': 'PipeWire', 'volume': 45, 'muted': True,
                           'player': 'Playing', 'title': 'Artist - Song'}}
        for page in ('camera', 'photos', 'photofull', 'photodelete', 'status', 'health',
                     'options', 'savers', 'saverstyle', 'apps', 'audio', 'calconfirm',
                     'colours', 'exit', 'shutdown', 'stopped', 'poweroff'):
            for banner in ((), alerts):
                image = app.render(page, scene(), False, 20, self.rows, 'msg', (), 0,
                                   alerts=banner, cam=cam, photo=photo, service=True,
                                   extra=extra)
                self.assertEqual(image.size, (320, 240), page)
        empty = app.render('photos', None, False, 20, {}, '', photo={'count': 0})
        self.assertEqual(empty.size, (320, 240))

    def test_banner_is_drawn_in_alert_colour(self):
        image = app.render('camera', scene(), False, 20, self.rows, '',
                           alerts=[('critical', 'LOW VOLTAGE')])
        self.assertEqual(image.getpixel((160, 2)), Image.new('RGB', (1, 1), app.DANGER).getpixel((0, 0)))
        quiet = app.render('camera', scene(), False, 20, self.rows, '')
        self.assertNotEqual(quiet.getpixel((160, 2)), image.getpixel((160, 2)))
        saver = app.screensaver_frame('radar', 1, alerts=[('warning', 'HOT')])
        self.assertEqual(saver.getpixel((160, 2)), Image.new('RGB', (1, 1), app.WARN).getpixel((0, 0)))

    def test_labels_fit_their_buttons(self):
        d = ImageDraw.Draw(Image.new('RGB', (1, 1)))
        for label, width in zip(app.TABS, app.TAB_WIDTHS):
            self.assertLess(d.textlength(label, font=app.TAB_FONT), width-8, label)
        for label in ('TIMER 10s', 'TIMER OFF', 'RESUME', '20 FPS', '< OLDER', 'NEWER >'):
            self.assertLess(d.textlength(label, font=app.SMALL), 72, label)

    def test_hotspots_follow_what_was_drawn(self):
        self.assertEqual(app.TAB_EDGES[-1], 320)
        app.render('camera', scene(), False, 20, self.rows, '')
        self.assertEqual([app.hit(x, 222) for x in (10, 100, 136, 180, 240, 300)],
                         ['tab:camera', 'tab:photos', 'tab:apps', 'tab:status',
                          'tab:options', 'tab:exit'])
        self.assertEqual([app.hit(x, 190) for x in (40, 120, 200, 280)],
                         ['cam:snap', 'cam:timer', 'cam:pause', 'cam:fps'])
        self.assertIsNone(app.hit(160, 90))  # Preview area: nothing to press.
        app.render('options', None, False, 20, self.rows, '')
        rects = app.grid_rects(10)
        self.assertEqual(app.hit(*rects[8][:2]), 'opt:8')
        self.assertLess(rects[-1][3], 204)  # Ten option buttons clear the tab bar.
        app.render('exit', None, False, 20, self.rows, '')
        self.assertEqual([app.hit(x, 150) for x in (50, 160, 260)],
                         ['exit:cancel', 'exit:close', 'exit:shutdown'])
        self.assertEqual(app.HOTSPOTS_PAGE[0], 'exit')


class RotationTest(unittest.TestCase):
    rows = LayoutTest.rows

    def tearDown(self):
        app.set_rotation(0)

    def test_coordinates_round_trip_and_match_the_turned_image(self):
        for degrees in (0, 90, 180, 270):
            app.set_rotation(degrees)
            self.assertEqual((app.W, app.H), (240, 320) if degrees in (90, 270) else (320, 240))
            logical = Image.new('RGB', (app.W, app.H), 'black')
            for point in ((0, 0), (17, 5), (app.W-1, app.H-1), (app.W-30, 40)):
                logical.putpixel(point, (255, 255, 255))
                panel = app.physical_image(logical)
                self.assertEqual(panel.size, (320, 240))
                self.assertEqual(panel.getpixel(app.to_physical(*point)), (255, 255, 255), (degrees, point))
                self.assertEqual(app.to_logical(*app.to_physical(*point)), point)
                logical.putpixel(point, (0, 0, 0))

    def test_ninety_clockwise_puts_the_top_on_the_right(self):
        app.set_rotation(90)
        self.assertEqual(app.to_physical(0, 0), (319, 0))
        self.assertEqual(app.to_physical(0, app.H-1), (0, 0))
        with self.assertRaises(ValueError):
            app.physical_image(Image.new('RGB', (320, 240)))  # Wrong canvas for 90 degrees.
        with self.assertRaises(ValueError):
            app.set_rotation(45)

    def test_portrait_pages_fit_and_hotspots_stay_on_canvas(self):
        app.set_rotation(90)
        capture = app.AudioCapture(which=lambda name: None)
        extra = {'notes': {}, 'capture': capture, 'audio': {'volume': 40, 'player': 'Paused'}}
        photo = {'image': scene((640, 480)), 'size': (3280, 2464), 'index': 1, 'count': 3,
                 'label': '2026/10/09 19:12:33'}
        cam = {'thumb': scene((60, 45)), 'countdown': 3}
        d = ImageDraw.Draw(Image.new('RGB', (1, 1)))
        for page in ('camera', 'photos', 'photodelete', 'status', 'health', 'options', 'savers',
                     'saverstyle', 'apps', 'audio', 'calconfirm', 'colours', 'exit', 'shutdown',
                     'stopped', 'poweroff', 'photofull'):
            image = app.render(page, scene(), False, 20, self.rows, 'A long status message here',
                               photo=photo, cam=cam, extra=extra,
                               alerts=[('warning', 'WARM 77 C')])
            self.assertEqual(image.size, (240, 320), page)
            for (x0, y0, x1, y1), action in app.HOTSPOTS:
                self.assertTrue(0 <= x0 < x1 < 240 and 0 <= y0 < y1 < 320, (page, action))
        # Tabs sit in two rows of three under the content, all reachable.
        app.render('status', None, False, 20, self.rows, '')
        tabs = [a for _, a in app.HOTSPOTS if a.startswith('tab:')]
        self.assertEqual(len(tabs), 6)
        self.assertEqual(app.hit(40, 275), 'tab:camera')
        self.assertEqual(app.hit(200, 305), 'tab:exit')
        self.assertEqual(app.hit(*app.grid_rects(10)[0][:2]), None)  # Status has no grid.
        for label in ('Pixel level: 100%', 'Calibrate touch...', 'Screen: 90\u00b0 CW'):
            self.assertLess(d.textlength(label, font=app.SMALL), 240)

    def test_portrait_savers_and_apps(self):
        app.set_rotation(90)
        options = {**app.DEFAULT_OPTIONS, 'bunny_count': 6}
        saver = app.BunnySaver(options, lambda n: options.update(bunny_count=n) or 'Saved.', seed=1)
        for t in range(30):
            image, _ = saver.render(t*.1, datetime.now(), ())
        self.assertEqual(image.size, (240, 320))
        self.assertTrue(saver.tap(220, 310, 4))  # + is bottom right in portrait.
        self.assertEqual(options['bunny_count'], 12)
        for style in ('radar', 'stars'):
            self.assertEqual(app.screensaver_frame(style, 3).size, (240, 320))
        self.assertEqual(app.ClockSaver(options).render(0, datetime(2026, 1, 1, 23, 59, 59), ())[0].size,
                         (240, 320))
        snake = app.SnakeApp(seed=2)
        self.assertEqual((snake.game.cols, snake.game.rows), (19, 20))
        box, start, quit_ = snake.dialog_rects()
        self.assertTrue(0 <= box[0] and box[2] < 240)
        snake.tap((start[0]+start[2])/2, (start[1]+start[3])/2, 0)
        self.assertTrue(snake.running)
        self.assertEqual(snake.render(0, datetime.now(), ()).size, (240, 320))
        app.set_rotation(0)
        snake.render(0, datetime.now(), ())  # Rotating back starts a fresh landscape board.
        self.assertEqual((snake.game.cols, snake.game.rows, snake.state), (26, 14, 'ready'))


class SnakeTest(unittest.TestCase):
    def test_moves_eats_grows_and_dies_on_wall(self):
        game = app.SnakeGame(cols=8, rows=4, seed=1)
        head = game.body[0]
        game.food = (head[0]+1, head[1])
        game.step()
        self.assertEqual((game.score, len(game.body)), (1, 4))
        self.assertFalse(game.turn((-1, 0)))  # No reversing into yourself.
        self.assertTrue(game.turn((0, -1)))
        for _ in range(5):
            game.step()
        self.assertFalse(game.alive)

    def test_self_collision_and_tail_follow(self):
        game = app.SnakeGame(cols=10, rows=10, seed=2)
        game.body = deque([(5, 5), (4, 5), (4, 6), (5, 6)])  # Head next to tail.
        game.direction, game.food = (1, 0), (0, 0)
        game.turn((0, 1))
        game.step()  # Moving into the cell the tail leaves is allowed.
        self.assertTrue(game.alive)
        game.body = deque([(5, 5), (4, 5), (4, 6), (5, 6), (6, 6)])
        game.direction, game.pending = (1, 0), []
        game.turn((0, 1))
        game.step()
        self.assertFalse(game.alive)

    def test_app_taps_steer_toward_finger_and_record_best(self):
        saved = []
        snake = app.SnakeApp(best=0, save_best=saved.append, seed=3)
        self.assertEqual(snake.tap(100, 170, 0), None)  # START
        self.assertTrue(snake.running)
        hx, hy = app.cell_rect(snake.game.body[0])[:2]
        snake.tap(hx, hy-60, 0.1)  # Well above the head: turn up.
        self.assertEqual(snake.game.pending, [(0, -1)])
        snake.game.food = None
        snake.game.score = 7
        snake.update(100)  # Long enough to hit a wall.
        self.assertEqual(snake.state, 'over')
        self.assertEqual(saved, [7])
        self.assertEqual(snake.tap(220, 170, 101), 'exit')
        self.assertEqual(snake.render(101, datetime.now(), ()).size, (320, 240))


class SaverTest(unittest.TestCase):
    def options(self, **changes):
        return {**app.DEFAULT_OPTIONS, **changes}

    def test_bunnies_hop_on_tap_and_count_controls(self):
        options = self.options(bunny_count=3)
        def set_count(n):
            options['bunny_count'] = n
            return 'Saved.'
        saver = app.BunnySaver(options, set_count, seed=4)
        image, delay = saver.render(10, datetime.now(), ())
        self.assertEqual(image.size, (320, 240))
        bunny = saver.bunnies[0]
        left, top, right, bottom = saver.bunny_box(bunny, 10)
        self.assertTrue(saver.tap((left+right)/2, (top+bottom)/2, 10))
        self.assertEqual(bunny.hop_height, 34)
        self.assertTrue(saver.tap(300, 226, 10))   # +
        self.assertEqual(options['bunny_count'], 6)
        saver.render(10.1, datetime.now(), ())
        self.assertEqual(len(saver.bunnies), 6)
        self.assertTrue(saver.tap(20, 226, 11))    # -
        self.assertEqual(options['bunny_count'], 3)
        self.assertFalse(saver.tap(160, 60, 12))   # Sky: wakes the panel.
        for t in range(50):  # Bunnies keep wandering and stay near the screen.
            saver.render(12 + t*.1, datetime.now(), ())
        self.assertTrue(all(-40 <= b.x <= 360 for b in saver.bunnies))

    def test_custom_sprite_sheet(self):
        with tempfile.TemporaryDirectory() as folder:
            sprites = Path(folder) / 'sprites'
            sprites.mkdir()
            Image.new('RGBA', (18, 60), (255, 255, 255, 255)).save(sprites / 'bunny.png')
            (sprites / 'bunny.json').write_text(json.dumps({'frames': 5, 'scale': 2}))
            frames = app.load_bunny_frames(folder)
            self.assertEqual([f.size for f in frames], [(36, 24)]*5)
            (sprites / 'bunny.json').write_text('{"frames": 0}')
            self.assertEqual(len(app.load_bunny_frames(folder)), 2)  # Falls back.

    def test_clock_formats(self):
        wall = datetime(2026, 10, 9, 21, 5, 30)
        image, delay = app.ClockSaver(self.options(clock_format='12h')).render(0, wall, ())
        self.assertEqual(image.size, (320, 240))
        self.assertLessEqual(delay, 1.02)

    def test_slideshow_advances_and_fades(self):
        with tempfile.TemporaryDirectory() as folder:
            photos, album = Path(folder) / 'photos', Path(folder) / 'album'
            photos.mkdir()
            album.mkdir()
            scene().save(photos / 'photo-20261009-100000-000000.jpg')
            Image.new('RGB', (200, 100), 'red').save(album / 'a.png')
            options = self.options(slide_seconds=5)
            show = app.Slideshow(options, photos, album, app.PhotoLibrary(photos))
            self.assertEqual(len(show.items), 2)
            show.frame(0)
            self.assertEqual(show.index, 0)
            image, delay = show.frame(5.1)
            self.assertEqual(show.index, 1)
            self.assertLess(delay, .2)  # Crossfading.
            options['album_source'] = 'album'
            self.assertEqual(len(show.refresh()), 1)
            options['album_source'] = 'camera'
            self.assertEqual(len(show.refresh()), 1)

    def test_empty_album_and_gifs_show_help(self):
        with tempfile.TemporaryDirectory() as folder:
            empty = Path(folder)
            show = app.Slideshow(self.options(), empty, empty, app.PhotoLibrary(empty))
            self.assertEqual(show.frame(0)[0].size, (320, 240))
            self.assertEqual(app.GifPlayer(empty).frame(0)[0].size, (320, 240))

    def test_gif_player_timing_loops_and_skips_bad_files(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            frames = [Image.new('RGB', (64, 48), c) for c in ('red', 'lime', 'blue')]
            frames[0].save(folder / 'b.gif', save_all=True, append_images=frames[1:],
                           duration=100, loop=0)
            (folder / 'a.gif').write_bytes(b'broken')
            player = app.GifPlayer(folder)
            image, delay = player.frame(0)
            self.assertEqual(player.files[player.index].name, 'b.gif')
            self.assertAlmostEqual(delay, .1, places=2)
            self.assertEqual(image.getpixel((160, 120)), (255, 0, 0))
            player.frame(.11)
            self.assertEqual(player.current.getpixel((160, 120))[1], 255)
            for t in (.22, .33):
                player.frame(t)
            self.assertEqual(player.loops, 1)
            saver = app.GifSaver(player)
            self.assertEqual(saver.render(.5, datetime.now(), [('warning', 'HOT')])[0].size, (320, 240))
            saver.close()


class AudioTest(unittest.TestCase):
    def test_parsers(self):
        self.assertEqual(app.parse_wpctl('Volume: 0.45'), (45, False))
        self.assertEqual(app.parse_wpctl('Volume: 0.30 [MUTED]'), (30, True))
        self.assertEqual(app.parse_wpctl(None), (None, False))
        out = "Simple mixer control 'Master',0\n  Front Left: Playback 40 [62%] [off]\n"
        self.assertEqual(app.parse_amixer(out), (62, True))
        self.assertEqual(app.parse_amixer('nothing'), (None, False))

    def test_control_backends_and_actions(self):
        calls = []
        def run(args):
            calls.append(args)
            if args[:2] == ['wpctl', 'get-volume']:
                return 'Volume: 0.50'
            if args[:2] == ['playerctl', 'status']:
                return 'Playing'
            if args[:2] == ['playerctl', 'metadata']:
                return 'Band - Tune'
            return ''
        control = app.AudioControl(run=run, which=lambda name: '/usr/bin/' + name)
        control.backend = control.detect()
        self.assertEqual(control.backend, ('wpctl', None))
        control.act('up')
        control.act('next')
        control.refresh()
        self.assertIn(['wpctl', 'set-volume', '-l', '1.0', '@DEFAULT_AUDIO_SINK@', '5%+'], calls)
        self.assertIn(['playerctl', 'next'], calls)
        self.assertEqual(control.state['volume'], 50)
        self.assertEqual(control.state['title'], 'Band - Tune')

        amixer = app.AudioControl(run=lambda a: "Simple mixer control 'PCM',0" if a[1:] == ['scontrols'] else '',
                                  which=lambda name: '/usr/bin/amixer' if name == 'amixer' else None)
        self.assertEqual(amixer.detect(), ('amixer', 'PCM'))
        nothing = app.AudioControl(run=lambda a: None, which=lambda name: None)
        nothing.backend = nothing.detect()
        nothing.refresh()
        self.assertEqual(nothing.state['backend'], 'No volume control found')

    def test_spectrum_peaks_at_tone(self):
        capture = app.AudioCapture(which=lambda name: None)
        t = np.arange(app.VIS_CHUNK) / app.VIS_RATE
        capture.feed((np.sin(2*np.pi*1000*t) * 16000).astype(np.int16))
        band = int(np.searchsorted(app.VIS_EDGES, 1000)) - 1
        self.assertEqual(int(np.argmax(capture.bands)), band)
        self.assertGreater(capture.bands[band], .8)
        capture.feed(np.zeros(app.VIS_CHUNK))
        self.assertLess(capture.bands[band], capture.bands[band]/.8 + 1e-9)  # Decays.

    def test_capture_reports_missing_tools(self):
        capture = app.AudioCapture(which=lambda name: None)
        capture.start('output')
        capture.thread.join(2)
        self.assertIn('parec', capture.status)
        capture.stop()

    def test_capture_reads_pcm_stream(self):
        import io
        tone = (np.sin(np.arange(app.VIS_CHUNK*4)*.3) * 12000).astype('<i2').tobytes()
        class FakeProcess:
            def __init__(self, *args, **kwargs):
                self.stdout = io.BytesIO(tone)
            def kill(self):
                pass
            def wait(self, timeout=None):
                return 0
        capture = app.AudioCapture(which=lambda name: '/usr/bin/' + name, popen=FakeProcess)
        capture.start('mic')
        capture.thread.join(2)
        self.assertEqual(capture.status, 'arecord stopped')
        self.assertGreater(capture.level, .1)
        capture.stop()


# ---- End-to-end: the real main() loop with fake hardware ----------------------

class FakeScreen:
    frames = []

    def __init__(self):
        self.pixel_level = 100

    def show(self, image):
        app.physical_image(image)  # Same size/rotation check as the real framebuffer.
        FakeScreen.frames.append(image)

    def close(self):
        pass


class FakeTouch:
    """Delivers scripted (seconds_after_start, x, y) taps given in on-screen (logical)
    coordinates, turned into panel coordinates the way the real touchscreen reports them."""
    script = []
    timeout = 20

    def __init__(self, path=None):
        self.signature = ['fake', [0, 320], [0, 240]]
        self.start = time.monotonic()
        self.pending = list(FakeTouch.script)

    def poll(self):
        if time.monotonic()-self.start > FakeTouch.timeout:
            raise RuntimeError('app did not exit')  # Watchdog for a stuck test.
        if self.pending and time.monotonic()-self.start >= self.pending[0][0]:
            _, x, y = self.pending.pop(0)
            return [np.array(app.to_physical(x, y), dtype=float)]
        return []

    def drain_until_idle(self, *args, **kwargs):
        pass

    def close(self):
        pass


class FakeCamera:
    instances = []

    def __init__(self, fps):
        self.fps, self.full_captures, self.closed = fps, 0, False
        FakeCamera.instances.append(self)

    def frame(self):
        return scene()

    def set_fps(self, fps):
        self.fps = fps

    def capture_full(self):
        self.full_captures += 1
        return scene((1640, 1232))

    def recover(self):
        return True

    def close(self):
        self.closed = True


class MainLoopTest(unittest.TestCase):
    def run_app(self, script, options=None):
        folder = tempfile.mkdtemp()
        data = Path(folder)
        (data / 'photos').mkdir()
        (data / 'touch.json').write_text(json.dumps(
            {'device': ['fake', [0, 320], [0, 240]], 'matrix': [[1, 0], [0, 1], [0, 0]]}))
        (data / 'options.json').write_text(json.dumps({'rotation': 0, **(options or {})}))
        FakeTouch.script, FakeScreen.frames, FakeCamera.instances = script, [], []
        poweroff = mock.Mock()
        real_open = builtins.open
        def local_lock(path, *args, **kwargs):
            if path == '/run/lock/cyberdeck-camera-monitor.lock':
                path = data / 'test.lock'
            return real_open(path, *args, **kwargs)
        fake_stats = mock.Mock()
        fake_stats.history = []
        fake_stats.read.return_value = {}
        with mock.patch.object(app, 'Framebuffer', FakeScreen), \
                mock.patch.dict(sys.modules, {'fcntl': mock.Mock(LOCK_EX=2, LOCK_NB=4)}), \
                mock.patch('builtins.open', side_effect=local_lock), \
                mock.patch.object(app, 'Stats', return_value=fake_stats), \
                mock.patch.object(app, 'user_owns'), \
                mock.patch('panel_tools.updates.Updates.git', return_value='test'), \
                mock.patch.object(app, 'Touch', FakeTouch), \
                mock.patch.object(app, 'Camera', FakeCamera), \
                mock.patch.object(app, 'request_poweroff', poweroff), \
                mock.patch.object(app, 'command_output', return_value=''), \
                mock.patch.object(sys, 'argv', ['panel_app.py', '--data', str(data)]):
            try:
                app.main()
            finally:
                app.set_rotation(0)
        return data, poweroff

    # Coordinates: camera row y=190, tabs y=222, exit/shutdown buttons y=150/160.
    def test_navigation_stays_responsive_during_full_capture(self):
        import threading
        started, finished = threading.Event(), threading.Event()
        navigated_while_busy = []
        original_capture, original_render = FakeCamera.capture_full, app.render
        def slow_capture(camera):
            started.set()
            time.sleep(1.2)
            result = original_capture(camera)
            finished.set()
            return result
        def observed_render(page, *args, **kwargs):
            if page == 'photos' and started.is_set() and not finished.is_set():
                navigated_while_busy.append(True)
            return original_render(page, *args, **kwargs)
        with mock.patch.object(FakeCamera, 'capture_full', slow_capture), \
                mock.patch.object(app, 'render', side_effect=observed_render):
            data, _ = self.run_app([(.5, 40, 190), (.9, 96, 222),
                                    (2.4, 290, 222), (3.1, 160, 150)])
        self.assertTrue(navigated_while_busy)
        self.assertEqual(len(list((data/'photos').glob('*.jpg'))), 1)

    def test_tools_timelapse_runs_after_leaving_its_page(self):
        data, _ = self.run_app([(.5, 136, 222), (1.1, 80, 164), # APPS > TOOLS
                               (1.7, 240, 68), (2.3, 80, 142), # TIMELAPSE > START
                               (2.9, 240, 222), # return to CAMERA
                               (3.6, 290, 222), (4.3, 160, 150)])
        self.assertGreaterEqual(FakeCamera.instances[0].full_captures, 1)
        self.assertGreaterEqual(len(list((data/'photos').glob('*.jpg'))), 1)

    def test_snap_review_delete_then_shutdown(self):
        script = [(.5, 40, 190),     # SNAP -> full-resolution capture
                  (1.2, 96, 222),    # PHOTOS tab
                  (1.9, 280, 190),   # DELETE
                  (2.6, 240, 185),   # confirm DELETE
                  (3.3, 290, 222),   # EXIT tab
                  (4.0, 260, 150),   # SHUT DOWN
                  (4.8, 240, 160)]   # confirm SHUT DOWN
        data, poweroff = self.run_app(script)
        self.assertEqual(FakeCamera.instances[0].full_captures, 1)
        self.assertEqual(list((data / 'photos').iterdir()), [])  # Saved, then deleted.
        poweroff.assert_called_once()
        self.assertTrue(FakeCamera.instances[0].closed)
        last = FakeScreen.frames[-1]
        self.assertEqual(last.getpixel((5, 230)), Image.new('RGB', (1, 1), app.BG).getpixel((0, 0)))

    def test_self_timer_and_preview_size(self):
        script = [(.5, 120, 190),    # TIMER: off -> 3 s
                  (1.2, 40, 190),    # SNAP starts countdown
                  (5.0, 290, 222),   # EXIT tab (after the timed capture)
                  (5.7, 160, 150)]   # CLOSE APP
        data, poweroff = self.run_app(script, options={'photo_size': 'preview'})
        photos = list((data / 'photos').iterdir())
        self.assertEqual(len(photos), 1)
        with Image.open(photos[0]) as saved:
            self.assertEqual(saved.size, (320, 240))
        self.assertEqual(FakeCamera.instances[0].full_captures, 0)
        self.assertEqual(json.loads((data / 'options.json').read_text())['snap_timer'], 3)
        poweroff.assert_not_called()

    def test_apps_snake_and_bunny_saver(self):
        # Coordinates: APPS tab x=136; GAMES tile (240, 70); menu SNAKE (80, 62);
        # dialog START (100, 170) / QUIT (220, 170); menu BACK (237, 194).
        script = [(.5, 136, 222),    # APPS tab
                  (1.1, 240, 70),    # GAMES
                  (1.7, 80, 62),     # SNAKE in the Games menu (top left)
                  (2.3, 100, 170),   # START
                  (2.9, 280, 12),    # PAUSE (header)
                  (3.5, 220, 170),   # QUIT -> back to the Games menu
                  (4.1, 237, 194),   # BACK -> Apps (bottom right)
                  (4.7, 240, 222),   # OPTIONS tab
                  (5.3, 240, 47),    # Screensavers...
                  (5.9, 80, 47),     # Style...
                  (6.5, 80, 87),     # BUNNIES in the style picker
                  (7.1, 240, 167),   # PREVIEW
                  (7.7, 300, 226),   # Bunny saver: + (stays in saver)
                  (8.3, 160, 60),    # Tap sky: wake
                  (8.9, 290, 222), (9.5, 160, 150)]
        data, _ = self.run_app(script)
        saved = json.loads((data / 'options.json').read_text())
        self.assertEqual(saved['saver_style'], 'bunnies')
        self.assertEqual(saved['bunny_count'], 12)
        for folder in ('album', 'gifs', 'sprites'):
            self.assertTrue((data / folder).is_dir())

    def test_portrait_snap_and_rotate_back_from_options(self):
        # Portrait coordinates (240x320): camera row y=243, tab rows y=274 and y=304.
        script = [(.5, 30, 243),      # SNAP
                  (1.2, 120, 274),    # PHOTOS tab (top row, middle)
                  (1.8, 200, 243),    # DELETE
                  (2.4, 180, 210),    # confirm DELETE (dialog shifted down in portrait)
                  (3.0, 120, 304),    # OPTIONS tab (bottom row, middle)
                  (3.6, 60, 216),     # Screen: 90 CW -> Landscape
                  (4.4, 300, 222),    # EXIT tab, now in landscape
                  (5.0, 160, 150)]    # CLOSE APP
        data, _ = self.run_app(script, options={'rotation': 90})
        self.assertEqual(FakeCamera.instances[0].full_captures, 1)
        self.assertEqual(list((data / 'photos').iterdir()), [])
        self.assertEqual(json.loads((data / 'options.json').read_text())['rotation'], 0)
        sizes = [f.size for f in FakeScreen.frames]
        self.assertIn((240, 320), sizes)
        self.assertEqual(sizes[-1], (320, 240))

    def test_leaving_camera_cancels_timer(self):
        script = [(.5, 120, 190), (.9, 120, 190),   # TIMER 3 s -> 10 s
                  (1.4, 40, 190),                   # SNAP starts 10 s countdown
                  (2.0, 160, 222),                  # STATUS tab cancels it
                  (2.7, 290, 222), (3.4, 160, 150)]
        data, _ = self.run_app(script)
        self.assertEqual(list((data / 'photos').iterdir()), [])


class ServiceSetupTest(unittest.TestCase):
    def setUp(self):
        import setup_panel_service as service
        self.service = service

    def test_install_and_undo(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            app_path = root / 'home/eli/panel_app.py'
            app_path.parent.mkdir(parents=True)
            app_path.write_text('# app')
            calls = []
            unit = self.service.install(app_path, 1000, 1000, root=root, run=calls.append)
            text = unit.read_text()
            self.assertIn(f'ExecStart=/usr/bin/python3 {app_path}', text)
            self.assertIn(f'--data {self.service.default_data(1000)}', text)
            self.assertIn('Restart=on-failure', text)
            self.assertIn('Environment=SUDO_UID=1000 SUDO_GID=1000', text)
            self.assertEqual(calls[-1], ['systemctl', 'enable', 'cyberdeck-panel.service'])
            self.service.install(app_path, 1000, 1000, root=root, run=calls.append)  # Idempotent.
            self.service.undo(root=root, run=calls.append)
            self.assertFalse(unit.exists())
            self.assertIn(['systemctl', 'disable', '--now', 'cyberdeck-panel.service'], calls)

    def test_refuses_foreign_unit_and_odd_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            app_path = root / 'panel_app.py'
            app_path.write_text('# app')
            unit = self.service.unit_path(root)
            unit.parent.mkdir(parents=True)
            unit.write_text('[Service]\nExecStart=/bin/true\n')
            with self.assertRaises(RuntimeError):
                self.service.install(app_path, 1000, 1000, root=root, run=lambda c: None)
            with self.assertRaises(RuntimeError):
                self.service.undo(root=root, run=lambda c: None)
            self.assertIn('/bin/true', unit.read_text())
            spaced = root / 'my app.py'
            spaced.write_text('x')
            with self.assertRaises(RuntimeError):
                self.service.unit_text(spaced, 1000, 1000)


if __name__ == '__main__':
    unittest.main()
