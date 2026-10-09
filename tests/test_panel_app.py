"""Hardware-free tests. Run: python3 -m unittest discover -s tests -v"""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

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
        for page in ('camera', 'photos', 'photofull', 'photodelete', 'status', 'health',
                     'options', 'savers', 'calconfirm', 'colours', 'exit', 'shutdown',
                     'stopped', 'poweroff'):
            for banner in ((), alerts):
                image = app.render(page, scene(), False, 20, self.rows, 'msg', (), 0,
                                   alerts=banner, cam=cam, photo=photo, service=True)
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
        for label in app.TABS:
            self.assertLess(d.textlength(label, font=app.SMALL), 58, label)
        for label in ('TIMER 10s', 'TIMER OFF', 'RESUME', '20 FPS', '< OLDER', 'NEWER >'):
            self.assertLess(d.textlength(label, font=app.SMALL), 72, label)

    def test_hit_helpers(self):
        self.assertEqual([app.tab_at(x) for x in (0, 63, 64, 200, 319)], [0, 0, 1, 3, 4])
        self.assertEqual([app.row_button(x) for x in (0, 79, 80, 319)], [0, 0, 1, 3])
        self.assertEqual(app.grid_at(20, 160), 6)
        self.assertEqual(app.grid_at(300, 170), 7)
        self.assertIsNone(app.grid_at(160, 100))


# ---- End-to-end: the real main() loop with fake hardware ----------------------

class FakeScreen:
    frames = []

    def __init__(self):
        self.pixel_level = 100

    def show(self, image):
        FakeScreen.frames.append(image)

    def close(self):
        pass


class FakeTouch:
    """Delivers scripted (seconds_after_start, x, y) taps in screen coordinates."""
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
            return [np.array([x, y], dtype=float)]
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
        if options:
            (data / 'options.json').write_text(json.dumps(options))
        FakeTouch.script, FakeScreen.frames, FakeCamera.instances = script, [], []
        poweroff = mock.Mock()
        with mock.patch.object(app, 'Framebuffer', FakeScreen), \
                mock.patch.object(app, 'Touch', FakeTouch), \
                mock.patch.object(app, 'Camera', FakeCamera), \
                mock.patch.object(app, 'request_poweroff', poweroff), \
                mock.patch.object(app, 'command_output', return_value=''), \
                mock.patch.object(sys, 'argv', ['panel_app.py', '--data', str(data)]):
            app.main()
        return data, poweroff

    # Coordinates: camera row y=190, tabs y=222, exit/shutdown buttons y=150/160.
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
