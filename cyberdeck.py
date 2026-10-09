#!/usr/bin/env python3
"""320x240 RGB565 camera + system monitor for Eli's ILI9341 Pi.

Run: sudo python3 cyberdeck.py
Optional: --calibrate, --fps 15, --touch /dev/input/eventN, --status-only
No desktop preview or console switching. Hardware imports happen only at startup.
"""
import argparse
import json
import os
from pathlib import Path
import select
import shutil
import signal
import subprocess
import time
from datetime import datetime

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps

W, H = 320, 240
BG, PANEL, TEXT, MUTED, ACCENT = '#101923', '#253747', '#eef5fa', '#a9bbc9', '#44d4ad'


def font(size):
    for path in ('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
                 '/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf'):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default()


FONT, SMALL = font(15), font(12)


def rgb565(image):
    a = np.asarray(image.convert('RGB'), dtype=np.uint16)
    return (((a[:, :, 0] >> 3) << 11) |
            ((a[:, :, 1] >> 2) << 5) | (a[:, :, 2] >> 3)).astype('<u2').tobytes()


def user_owns(path):
    if hasattr(os, 'chown') and 'SUDO_UID' in os.environ:
        os.chown(path, int(os.environ['SUDO_UID']), int(os.environ['SUDO_GID']))


class Framebuffer:
    def __init__(self):
        base = Path('/sys/class/graphics/fb0')
        if 'ili9341' not in (base / 'name').read_text().lower():
            raise RuntimeError('/dev/fb0 is not the ILI9341 screen. Stopping.')
        if (base / 'virtual_size').read_text().strip() != '320,240':
            raise RuntimeError('Expected a 320x240 framebuffer. Keep rotate=270.')
        if int((base / 'bits_per_pixel').read_text()) != 16:
            raise RuntimeError('Expected 16-bit RGB565 framebuffer.')
        self.stride = int((base / 'stride').read_text())
        if self.stride < W * 2:
            raise RuntimeError('Unexpected framebuffer stride.')
        self.file = open('/dev/fb0', 'r+b', buffering=0)

    def show(self, image):
        data = rgb565(image)
        if self.stride != W * 2:
            rows = np.frombuffer(data, dtype=np.uint8).reshape(H, W * 2)
            padded = np.zeros((H, self.stride), dtype=np.uint8)
            padded[:, :W * 2] = rows
            data = padded.tobytes()
        self.file.seek(0)
        view = memoryview(data)
        while view:
            n = self.file.write(view)
            if not n:
                raise OSError('Framebuffer write stopped.')
            view = view[n:]

    def close(self):
        self.file.close()


class Touch:
    def __init__(self, path=None):
        import evdev
        self.e = evdev.ecodes
        self.device = None
        for candidate in ([path] if path else evdev.list_devices()):
            d = evdev.InputDevice(candidate)
            caps = d.capabilities(absinfo=False)
            axes = caps.get(self.e.EV_ABS, [])
            keys = caps.get(self.e.EV_KEY, [])
            match = path or any(n in d.name.lower() for n in ('ads7846', 'xpt2046'))
            if match and self.e.ABS_X in axes and self.e.ABS_Y in axes and self.e.BTN_TOUCH in keys:
                self.device = d
                break
            d.close()
        if self.device is None:
            raise RuntimeError('Touchscreen not found. Check sudo evtest first.')
        try:
            self.device.grab()
        except Exception:
            self.device.close()
            raise RuntimeError('Touchscreen is busy. Stop evtest or another copy of this app.')
        self.x = self.device.absinfo(self.e.ABS_X).value
        self.y = self.device.absinfo(self.e.ABS_Y).value
        self.down = False
        self.released = False
        self.samples = []
        self.dropped = False
        self.signature = [self.device.name,
                          list(self.device.absinfo(self.e.ABS_X))[1:3],
                          list(self.device.absinfo(self.e.ABS_Y))[1:3]]
        print('Touch:', self.device.path, self.device.name, flush=True)

    def poll(self):
        """Return one median coordinate per completed tap, on release."""
        taps = []
        if not select.select([self.device], [], [], 0)[0]:
            return taps
        try:
            events = list(self.device.read())
        except BlockingIOError:
            return taps
        e = self.e
        for event in events:
            if event.type == e.EV_SYN and event.code == e.SYN_DROPPED:
                self.dropped = True
                self.samples = []
                self.down = self.released = False
                continue
            if self.dropped:
                if event.type == e.EV_SYN and event.code == e.SYN_REPORT:
                    self.x = self.device.absinfo(e.ABS_X).value
                    self.y = self.device.absinfo(e.ABS_Y).value
                    self.dropped = False
                continue
            if event.type == e.EV_ABS:
                if event.code == e.ABS_X:
                    self.x = event.value
                elif event.code == e.ABS_Y:
                    self.y = event.value
            elif event.type == e.EV_KEY and event.code == e.BTN_TOUCH:
                if event.value == 1:
                    self.samples = []
                    self.down = True
                elif event.value == 0:
                    self.released = self.down
                    self.down = False
            elif event.type == e.EV_SYN and event.code == e.SYN_REPORT:
                if self.down:
                    self.samples.append((self.x, self.y))
                    self.samples = self.samples[-30:]
                if self.released:
                    if self.samples:
                        taps.append(np.median(self.samples, axis=0))
                    self.samples = []
                    self.released = False
        return taps

    def close(self):
        try:
            self.device.ungrab()
        finally:
            self.device.close()


def fit_calibration(raw, targets):
    a = np.column_stack([np.asarray(raw, dtype=float), np.ones(len(raw))])
    if np.linalg.matrix_rank(a) < 3:
        raise ValueError('Calibration points overlap; try again.')
    matrix = np.linalg.lstsq(a, np.asarray(targets), rcond=None)[0]
    error = np.max(np.linalg.norm(a @ matrix - targets, axis=1))
    if error > 15:
        raise ValueError('Calibration taps were inconsistent; try again.')
    return matrix


def calibrate(screen, touch):
    targets = [(26, 26), (293, 26), (293, 213), (26, 213), (160, 120)]
    while True:
        raw = []
        for number, (x, y) in enumerate(targets, 1):
            touch.poll()
            img = Image.new('RGB', (W, H), BG)
            d = ImageDraw.Draw(img)
            d.text((65, 70), 'TOUCH CALIBRATION', font=FONT, fill=TEXT)
            d.text((66, 92), f'Tap + and release ({number}/5)', font=SMALL, fill=MUTED)
            d.line((x-12, y, x+12, y), fill=ACCENT, width=3)
            d.line((x, y-12, x, y+12), fill=ACCENT, width=3)
            screen.show(img)
            deadline = time.monotonic() + 60
            while True:
                taps = touch.poll()
                if taps:
                    raw.append(taps[0])
                    break
                if time.monotonic() > deadline:
                    raise RuntimeError('Calibration timed out. Check touch with evtest.')
                time.sleep(.01)
        try:
            return fit_calibration(raw, targets)
        except ValueError as exc:
            print(exc, flush=True)


class Stats:
    def __init__(self):
        self.previous = None
        self.last_network = -100
        self.ip = 'No IPv4 address'

    def read(self):
        cpu = [int(v) for v in Path('/proc/stat').read_text().splitlines()[0].split()[1:9]]
        total, idle = sum(cpu), cpu[3] + cpu[4]
        usage = '--'
        if self.previous:
            dt, di = total-self.previous[0], idle-self.previous[1]
            if dt > 0:
                usage = f'{max(0, min(100, 100*(1-di/dt))):.0f}%'
        self.previous = (total, idle)
        mem = {}
        for line in Path('/proc/meminfo').read_text().splitlines():
            parts = line.split()
            mem[parts[0].rstrip(':')] = int(parts[1])
        used = (mem['MemTotal'] - mem['MemAvailable']) / 1048576
        allmem = mem['MemTotal'] / 1048576
        disk = shutil.disk_usage('/')
        seconds = int(float(Path('/proc/uptime').read_text().split()[0]))
        try:
            temp = f"{int(Path('/sys/class/thermal/thermal_zone0/temp').read_text())/1000:.1f} C"
        except (OSError, ValueError):
            temp = 'Unavailable'
        if time.monotonic()-self.last_network > 10:
            self.last_network = time.monotonic()
            try:
                result = subprocess.run(['ip', '-j', '-4', 'addr', 'show', 'scope', 'global'],
                                        capture_output=True, text=True, timeout=.5, check=True)
                addresses = [(item['ifname'], addr['local']) for item in json.loads(result.stdout)
                             for addr in item.get('addr_info', []) if addr.get('family') == 'inet']
                self.ip = f'{addresses[0][0]} {addresses[0][1]}' if addresses else 'No IPv4 address'
            except (OSError, ValueError, subprocess.SubprocessError, KeyError):
                self.ip = 'Unavailable'
        return [('CPU', f'{usage}    {temp}'),
                ('RAM', f'{used:.1f} / {allmem:.1f} GiB'),
                ('Disk free', f'{disk.free/2**30:.1f} GiB'),
                ('Uptime', f'{seconds//3600}h {(seconds//60)%60}m'),
                ('Network', self.ip)]


def button(d, rect, label, active=False):
    d.rounded_rectangle(rect, radius=5, fill=ACCENT if active else PANEL)
    d.text(((rect[0]+rect[2])//2, (rect[1]+rect[3])//2), label,
           font=SMALL, fill=BG if active else TEXT, anchor='mm')


def render(page, frame, paused, fps, rows, message):
    image = Image.new('RGB', (W, H), BG)
    d = ImageDraw.Draw(image)
    d.text((8, 4), 'CYBERDECK / ' + page.upper(), font=FONT, fill=ACCENT)
    if page == 'camera':
        if frame is not None:
            preview = ImageOps.contain(frame, (W, 132))
            image.paste(preview, ((W-preview.width)//2, 25+(132-preview.height)//2))
        else:
            d.text((40, 75), 'Camera is not available', font=FONT, fill=MUTED)
        d.text((8, 158), message[:45], font=SMALL, fill=TEXT)
        for i, label in enumerate(('SNAP', 'RESUME' if paused else 'PAUSE', f'{fps:g} FPS')):
            button(d, (i*106+3, 176, i*106+103, 203), label)
    else:
        for i, (label, value) in enumerate(rows):
            y = 32+i*28
            d.text((8, y), label, font=SMALL, fill=MUTED)
            d.text((90, y), value, font=SMALL, fill=TEXT)
        d.text((8, 178), 'Battery: no sensor configured', font=SMALL, fill=MUTED)
    for i, label in enumerate(('CAMERA', 'STATUS', 'CAL', 'EXIT')):
        button(d, (i*80+2, 207, i*80+77, 238), label, page == label.lower())
    return image


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fps', type=int, choices=(10, 15, 20), default=20)
    parser.add_argument('--calibrate', action='store_true')
    parser.add_argument('--touch', help='Override touchscreen event device')
    parser.add_argument('--status-only', action='store_true')
    args = parser.parse_args()
    # Prevent two copies from competing for the camera and touchscreen.
    import fcntl
    lock = open('/run/lock/cyberdeck-camera-monitor.lock', 'a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise RuntimeError('Cyberdeck is already running. Stop the other copy first.')
    data = Path(__file__).resolve().parent / 'cyberdeck-data'
    photos = data / 'photos'
    photos.mkdir(parents=True, exist_ok=True)
    user_owns(data)
    user_owns(photos)
    calibration_file = data / 'touch.json'
    screen = touch = camera = None
    def terminate(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, terminate)
    try:
        screen = Framebuffer()
        touch = Touch(args.touch)
        matrix = None
        if not args.calibrate:
            try:
                saved = json.loads(calibration_file.read_text())
                if saved['device'] == touch.signature:
                    candidate = np.asarray(saved['matrix'], dtype=float)
                    if candidate.shape == (3, 2) and np.isfinite(candidate).all():
                        matrix = candidate
            except (OSError, ValueError, KeyError, TypeError):
                pass
        def new_calibration():
            result = calibrate(screen, touch)
            temporary = calibration_file.with_suffix('.tmp')
            temporary.write_text(json.dumps({'device': touch.signature, 'matrix': result.tolist()}))
            user_owns(temporary)
            temporary.replace(calibration_file)
            return result
        if matrix is None:
            matrix = new_calibration()
        message = 'Live | SNAP saves 320x240 JPG'
        if not args.status_only:
            try:
                from picamera2 import Picamera2
                camera = Picamera2()
                camera.configure(camera.create_video_configuration(
                    main={'size': (W, H), 'format': 'BGR888'},
                    controls={'FrameRate': args.fps}, buffer_count=4))
                camera.start(show_preview=False)
            except Exception as exc:
                print('Camera unavailable:', exc, flush=True)
                if camera is not None:
                    camera.close()
                camera = None
                message = 'Camera unavailable; see terminal'
        page = 'camera' if camera else 'status'
        frame, paused, fps = None, False, args.fps
        stats, rows = Stats(), []
        next_stats, next_draw = 0, 0
        running = True
        print('Ready. Tap buttons or press Ctrl+C to exit. Photos:', photos, flush=True)
        while running:
            now = time.monotonic()
            for raw in touch.poll():
                x, y = np.array([*raw, 1.0]) @ matrix
                if not (0 <= x < W and 0 <= y < H):
                    continue
                if y >= 207:
                    tab = int(x)//80
                    if tab == 0:
                        page = 'camera'
                    elif tab == 1:
                        page = 'status'
                    elif tab == 2:
                        matrix = new_calibration()
                    else:
                        running = False
                elif page == 'camera' and 176 <= y <= 203:
                    action = min(2, int(x)//106)
                    if action == 0 and frame is not None:
                        name = datetime.now().strftime('photo-%Y%m%d-%H%M%S-%f.jpg')
                        try:
                            frame.save(photos / name, quality=95)
                            user_owns(photos / name)
                            message = 'Saved ' + name[6:21]
                            print('Saved:', photos / name, flush=True)
                        except OSError as exc:
                            message = 'Save failed; see terminal'
                            print('Could not save photo:', exc, flush=True)
                    elif action == 1 and camera:
                        paused = not paused
                        message = 'Preview paused' if paused else 'Live | SNAP saves 320x240 JPG'
                    elif action == 2 and camera:
                        fps = {10: 15, 15: 20, 20: 10}[fps]
                        camera.set_controls({'FrameRate': fps})
                        message = f'Camera target: {fps} FPS'
                next_draw = 0
                break  # Avoid a second queued tap triggering a newly opened screen.
            if not running:
                break
            if now >= next_stats:
                rows = stats.read()
                next_stats = now + 1
            live = page == 'camera' and camera is not None and not paused
            if live:
                frame = Image.fromarray(camera.capture_array('main'), 'RGB')
            if live or now >= next_draw:
                screen.show(render(page, frame, paused, fps, rows, message))
                next_draw = now + (1/fps if live else .25)
            if not live:
                time.sleep(.02)
    except KeyboardInterrupt:
        print('\nStopped.', flush=True)
    finally:
        # Always release touch, even if camera cleanup itself fails.
        try:
            if camera is not None:
                camera.close()
        finally:
            try:
                if touch is not None:
                    touch.close()
            finally:
                if screen is not None:
                    screen.close()
                lock.close()


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        raise SystemExit(f'Cyberdeck stopped: {exc}')
