"""Power/performance features. Hardware-free: sysfs and /dev/fb0 are faked with files."""
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image, ImageChops

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import panel_app as app  # noqa: E402
import setup_backlight  # noqa: E402
from panel_tools.backlight import PwmBacklight, NoBacklight, duty_fraction, load_backlight  # noqa: E402
from panel_tools.runtime import CameraWorker  # noqa: E402


def fake_pwm(root, channel=0, exported=False):
    chip = root / 'pwmchip0'
    chip.mkdir(parents=True)
    (chip / 'export').write_text('')
    pwm = chip / f'pwm{channel}'
    if exported:
        pwm.mkdir()
    else:
        # Simulate udev creating the channel when export is written.
        pwm.mkdir()
    for name in ('period', 'duty_cycle', 'enable'):
        (pwm / name).write_text('0')
    return pwm


class BacklightTest(unittest.TestCase):
    def test_levels_map_to_duty_and_skip_repeats(self):
        with tempfile.TemporaryDirectory() as folder:
            pwm = fake_pwm(Path(folder))
            light = PwmBacklight(root=folder, frequency=2000)
            self.assertEqual((pwm / 'period').read_text(), '500000')
            self.assertEqual((pwm / 'enable').read_text(), '1')
            self.assertEqual((pwm / 'duty_cycle').read_text(), '500000')  # Starts fully lit.
            light.set(100)
            self.assertEqual((pwm / 'duty_cycle').read_text(), '500000')
            light.set(0)
            self.assertEqual((pwm / 'duty_cycle').read_text(), '0')
            light.set(70)
            self.assertEqual(int((pwm / 'duty_cycle').read_text()), round(500000*.7**2))
            (pwm / 'duty_cycle').write_text('sentinel')
            light.set(70)  # Same level: sysfs untouched.
            self.assertEqual((pwm / 'duty_cycle').read_text(), 'sentinel')
            light.close()
            self.assertEqual((pwm / 'duty_cycle').read_text(), '500000')

    def test_inverted_driver(self):
        with tempfile.TemporaryDirectory() as folder:
            pwm = fake_pwm(Path(folder))
            light = PwmBacklight(root=folder, invert=True)
            self.assertEqual((pwm / 'duty_cycle').read_text(), '0')  # Lit = held low.
            light.set(0)
            self.assertEqual((pwm / 'duty_cycle').read_text(), '500000')  # Off = held high.
            light.set(100)
            self.assertEqual((pwm / 'duty_cycle').read_text(), '0')

    def test_waits_for_the_pwm_driver_at_boot(self):
        with tempfile.TemporaryDirectory() as folder:
            root, naps = Path(folder), []

            def sleep(seconds):  # The driver loads while the app is waiting.
                naps.append(seconds)
                if len(naps) == 3:
                    fake_pwm(root)
            light = PwmBacklight(root=root, wait=10, sleep=sleep)
            self.assertEqual(len(naps), 3)
            self.assertEqual((root / 'pwmchip0/pwm0/duty_cycle').read_text(), str(light.period))
            with self.assertRaises(RuntimeError):
                PwmBacklight(root=root / 'none', wait=1, sleep=lambda s: None)

    def test_already_lit_channel_is_not_blanked(self):
        with tempfile.TemporaryDirectory() as folder:
            pwm = fake_pwm(Path(folder))
            writes = []
            real = PwmBacklight._write

            def spy(self, name, value):
                writes.append((name, value))
                real(self, name, value)
            (pwm / 'period').write_text('500000')
            (pwm / 'duty_cycle').write_text('500000')  # Lit by the boot service.
            with mock.patch.object(PwmBacklight, '_write', spy):
                light = PwmBacklight(root=folder)
                light.set(100)
            self.assertNotIn(('duty_cycle', 0), writes)
            self.assertEqual(writes.count(('duty_cycle', 500000)), 1)

    def test_perceptual_curve_keeps_low_levels_visible(self):
        self.assertEqual(duty_fraction(0), 0)
        self.assertEqual(duty_fraction(100), 1)
        self.assertGreaterEqual(duty_fraction(1), .02)
        self.assertLess(duty_fraction(40), .4)

    def test_load_is_opt_in_and_falls_back(self):
        with tempfile.TemporaryDirectory() as folder:
            data, sysfs = Path(folder) / 'data', Path(folder) / 'sys'
            data.mkdir()
            self.assertIsInstance(load_backlight(data, sysfs), NoBacklight)
            (data / 'backlight.json').write_text(json.dumps({'chip': 0, 'channel': 0}))
            messages = []
            self.assertIsInstance(load_backlight(data, sysfs, messages.append, wait=0), NoBacklight)
            self.assertIn('pixel dimming', messages[0])  # No pwmchip yet: not rebooted.
            fake_pwm(sysfs)
            self.assertTrue(load_backlight(data, sysfs).available)

    def test_brightness_uses_backlight_or_pixels(self):
        screen = mock.Mock(pixel_level=100)
        pwm = mock.Mock(available=True)
        app.Brightness(screen, pwm).apply(40)
        pwm.set.assert_called_with(40)
        self.assertEqual(screen.pixel_level, 100)  # Full colour; the LEDs do the dimming.
        app.Brightness(screen, NoBacklight()).apply(40)
        self.assertEqual(screen.pixel_level, 40)


class FramebufferTest(unittest.TestCase):
    def make(self, folder):
        base = Path(folder) / 'fb0'
        base.mkdir()
        (base / 'virtual_size').write_text('320,240\n')
        (base / 'bits_per_pixel').write_text('16\n')
        (base / 'stride').write_text('640\n')
        (base / 'blank').write_text('0')
        device = Path(folder) / 'dev'
        device.write_bytes(bytes(640*240))
        return app.Framebuffer(base, device), device, base

    def test_sends_only_changed_rows_and_skips_identical_frames(self):
        with tempfile.TemporaryDirectory() as folder:
            fb, device, base = self.make(folder)
            try:
                image = Image.new('RGB', (320, 240), 'navy')
                fb.show(image)
                self.assertEqual(fb.rows_sent, 240)
                fb.show(image.copy())
                self.assertEqual(fb.rows_sent, 240)  # Identical: nothing written.
                changed = image.copy()
                changed.paste('white', (10, 100, 50, 110))
                fb.show(changed)
                self.assertEqual(fb.rows_sent, 250)  # Just rows 100-109.
                expected = app.rgb565(changed)
                fb.map.flush() if fb.map is not None else None
                self.assertEqual(device.read_bytes(), expected)
                self.assertTrue(fb.set_blank(True))
                self.assertEqual((base / 'blank').read_text(), '1')
                fb.set_blank(False)
                fb.show(changed)
                self.assertEqual(fb.rows_sent, 490)  # Full frame after unblanking.
            finally:
                fb.close()

    def test_periodic_full_refresh(self):
        with tempfile.TemporaryDirectory() as folder:
            fb, _, _ = self.make(folder)
            try:
                image = Image.new('RGB', (320, 240), 'black')
                fb.show(image)
                fb.refreshed -= fb.FULL_REFRESH + 1
                fb.show(image)
                self.assertEqual(fb.rows_sent, 480)
            finally:
                fb.close()

    def test_portrait_frames_are_turned_before_diffing(self):
        with tempfile.TemporaryDirectory() as folder:
            fb, device, _ = self.make(folder)
            try:
                app.set_rotation(90)
                logical = Image.new('RGB', (240, 320), 'black')
                logical.putpixel((0, 0), (255, 255, 255))
                fb.show(logical)
                if fb.map is not None:
                    fb.map.flush()
                pixels = np.frombuffer(device.read_bytes(), '<u2').reshape(240, 320)
                self.assertEqual(int(pixels[0, 319]), 0xFFFF)  # Top-left -> panel top-right.
            finally:
                app.set_rotation(0)
                fb.close()


class CameraIdleTest(unittest.TestCase):
    def test_sensor_stops_when_unwatched_and_restarts_for_preview_and_photos(self):
        events = []
        class Driver:
            def __init__(self, fps):
                events.append('open')
            def frame(self):
                time.sleep(.005)
                return 'frame'
            def pause(self):
                events.append('pause')
            def resume(self):
                events.append('resume')
            def close(self):
                events.append('close')
        worker = CameraWorker(Driver, 20, idle_stop=.1)
        try:
            deadline = time.monotonic() + 2
            while worker.sequence < 3 and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertGreaterEqual(worker.sequence, 3)
            worker.preview = False
            while worker.streaming and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertFalse(worker.streaming)
            self.assertEqual(events[-1], 'pause')
            seen = worker.sequence
            time.sleep(.2)
            self.assertEqual(worker.sequence, seen)  # No frames pulled while stopped.
            done = threading.Event()
            self.assertTrue(worker.snapshot(lambda driver: done.set() or 'saved'))
            self.assertEqual(worker.results.get(timeout=2), ('saved', None))
            self.assertIn('resume', events)  # Woken for the photo.
            worker.preview = True
            while worker.sequence == seen and time.monotonic() < deadline + 2:
                time.sleep(.01)
            self.assertGreater(worker.sequence, seen)
        finally:
            worker.close()


class CameraCacheTest(unittest.TestCase):
    def test_cached_frame_matches_full_render(self):
        frame = Image.fromarray(np.random.default_rng(1).integers(0, 255, (240, 320, 3), np.uint8))
        wall = app.datetime(2026, 10, 10, 12, 0, 0)
        try:
            for rotation in (0, 90):
                app.set_rotation(rotation)
                app.CAMERA_CHROME.clear()
                cam = {'thumb': Image.new('RGB', (60, 45), 'red'), 'countdown': 2}
                app.render('camera', frame, False, 20, {}, 'Live', wall_time=wall, cam=cam)
                cached = app.render('camera', frame, False, 20, {}, 'Live', wall_time=wall, cam=cam)
                hotspots = list(app.HOTSPOTS)
                full = app.render_page('camera', frame, False, 20, {}, 'Live', wall_time=wall, cam=cam)
                self.assertIsNone(ImageChops.difference(cached, full).getbbox())
                self.assertEqual(hotspots, app.HOTSPOTS)
                # A changed message rebuilds the cached layer.
                other = app.render('camera', frame, False, 20, {}, 'Saved', wall_time=wall, cam=cam)
                self.assertIsNotNone(ImageChops.difference(other, cached).getbbox())
        finally:
            app.set_rotation(0)

    def test_camera_stream_is_landscape_in_portrait(self):
        app.set_rotation(90)
        try:
            with mock.patch.object(app.CameraDriver, '__init__', return_value=None) as init:
                app.Camera(20)
            init.assert_called_with(20, preview_size=(320, 240))
        finally:
            app.set_rotation(0)


class SetupBacklightTest(unittest.TestCase):
    CONFIG = ('[all]\ndtparam=spi=on\n'
              'dtoverlay=fbtft,spi0-0,ili9341,dc_pin=24,reset_pin=25,rotate=270,bgr\n'
              'dtoverlay=ads7846,cs=1,penirq=17,speed=1000000\n')

    def test_add_is_idempotent_and_undo_restores(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / 'boot/firmware/config.txt'
            config.parent.mkdir(parents=True)
            config.write_text(self.CONFIG)
            self.assertEqual(setup_backlight.config_path(Path(folder)), config)
            self.assertTrue(setup_backlight.add_overlay(config, 18))
            self.assertIn('dtoverlay=pwm,pin=18,func=2', config.read_text())
            self.assertFalse(setup_backlight.add_overlay(config, 18))
            with self.assertRaises(RuntimeError):
                setup_backlight.add_overlay(config, 12)  # Different pin: undo first.
            self.assertTrue(setup_backlight.remove_overlay(config))
            self.assertEqual(config.read_text(), self.CONFIG)
            self.assertTrue((config.parent / 'config.txt.before-backlight').exists())

    def test_refuses_pins_in_use_and_foreign_pwm(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / 'config.txt'
            config.write_text(self.CONFIG)
            self.assertEqual(setup_backlight.pins_in_use(self.CONFIG), {24, 25, 17})
            config.write_text(self.CONFIG.replace('dc_pin=24', 'dc_pin=18'))
            with self.assertRaises(RuntimeError):
                setup_backlight.add_overlay(config, 18)
            self.assertTrue(setup_backlight.add_overlay(config, 18, force=True))
            config.write_text(self.CONFIG + 'dtoverlay=pwm-2chan\n')
            with self.assertRaises(RuntimeError):
                setup_backlight.add_overlay(config, 18)

    def test_settings_channel_follows_pin(self):
        with tempfile.TemporaryDirectory() as folder:
            path = setup_backlight.write_settings(Path(folder), 13, 1000, True)
            self.assertEqual(json.loads(path.read_text())['channel'], 1)
            path = setup_backlight.write_settings(Path(folder), 18, 2000, False)
            self.assertEqual(json.loads(path.read_text())['channel'], 0)


class BlackSaverPowerOffTest(unittest.TestCase):
    """Real main loop: Black saver switches the PWM backlight off and blanks the panel;
    the wake tap brings both back, and exiting leaves the backlight on."""
    def test_black_saver_turns_off_and_wakes(self):
        sys.path.insert(0, str(ROOT / 'tests'))
        import test_panel_app as t
        with tempfile.TemporaryDirectory() as folder:
            sysfs = Path(folder) / 'sys'
            pwm = fake_pwm(sysfs)
            levels, blanks = [], []
            real = app.load_backlight

            def load(data):
                (Path(data) / 'backlight.json').write_text('{}')
                light = real(data, sysfs)
                original = light.set
                light.set = lambda level: (levels.append(level), original(level))
                return light

            original_show = t.FakeScreen.show
            t.FakeScreen.set_blank = lambda self, blank: blanks.append(blank) or True
            try:
                # Options tab, Screensavers..., PREVIEW (Black), wake tap, Exit tab, Close app.
                # Off means the loop sleeps up to 1 s, so the taps after waking are spaced out.
                script = [(.5, 240, 222), (1.0, 240, 43), (1.6, 240, 167), (2.6, 160, 120),
                          (4.2, 300, 222), (4.9, 160, 150)]
                with mock.patch.object(app, 'load_backlight', load):
                    t.MainLoopTest.run_app(self, script, options={'saver_style': 'black',
                                                                  'pixel_level': 70})
            finally:
                del t.FakeScreen.set_blank
                t.FakeScreen.show = original_show
            self.assertIn(0, levels)                        # LEDs off during Black.
            self.assertEqual(blanks[:2], [True, False])     # Blanked, then woken.
            after = levels[levels.index(0)+1:]
            self.assertIn(70, after)                        # Brightness restored on wake.
            self.assertEqual(levels[-1], 100)               # Left lit after exit.
            self.assertEqual((pwm / 'duty_cycle').read_text(), str(round(1e9/2000)))


class BootLightTest(unittest.TestCase):
    def test_unit_lights_panel_before_the_app(self):
        text = setup_backlight.unit_text(Path('/home/eli/cyberdeck-repo/setup_backlight.py'),
                                         Path('/home/eli/cyberdeck-data'))
        self.assertIn('ExecStart=/usr/bin/python3 /home/eli/cyberdeck-repo/setup_backlight.py '
                      '--on --data /home/eli/cyberdeck-data', text)
        self.assertIn('Before=cyberdeck-panel.service', text)
        self.assertIn('Type=oneshot', text)
        import setup_panel_service
        self.assertIn('cyberdeck-backlight.service', setup_panel_service.unit_text(
            Path('/home/eli/cyberdeck-repo/panel_app.py'), 1000, 1000))

    def test_install_and_remove(self):
        with tempfile.TemporaryDirectory() as folder:
            root, commands = Path(folder), []
            path = setup_backlight.install_unit(Path('/r/setup_backlight.py'), Path('/d'),
                                                root=root, run=commands.append)
            self.assertIn(['systemctl', 'enable', setup_backlight.UNIT], commands)
            setup_backlight.install_unit(Path('/r/setup_backlight.py'), Path('/d'),
                                         root=root, run=commands.append)  # Re-run is fine.
            self.assertTrue(setup_backlight.remove_unit(root=root, run=commands.append))
            self.assertFalse(path.exists())
            self.assertFalse(setup_backlight.remove_unit(root=root, run=commands.append))
            path.write_text('someone else\n')
            with self.assertRaises(RuntimeError):
                setup_backlight.install_unit(Path('/r/x.py'), Path('/d'), root=root, run=commands.append)
            self.assertFalse(setup_backlight.remove_unit(root=root, run=commands.append))
            self.assertTrue(path.exists())  # Not ours: left alone.


if __name__ == '__main__':
    unittest.main()
