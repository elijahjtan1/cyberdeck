#!/usr/bin/env python3
"""320x240 RGB565 camera + system monitor for Eli's ILI9341 Pi.

Run: sudo python3 panel_app.py
Optional: --calibrate, --fps 15, --touch /dev/input/eventN, --status-only, --data DIR
Apps: audio + visualizer, Snake, photo album, GIF player. See docs/APPS-AND-SAVERS.md.
No desktop preview or console switching. Hardware imports happen only at startup.
Boot service: sudo python3 setup_panel_service.py (see docs/CAMERA-PHOTOS-POWER.md).
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
import threading
import re
import math
from datetime import datetime
from collections import deque

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps

W, H = 320, 240
BG, PANEL, TEXT, MUTED, ACCENT = '#101923', '#253747', '#eef5fa', '#a9bbc9', '#44d4ad'
DANGER, WARN = '#d8443c', '#f0b347'
SERVICE = 'cyberdeck-panel'
SAVER_STYLES = ('radar', 'stars', 'bunnies', 'clock', 'album', 'gif', 'black', 'off')
SAVER_NAMES = {'radar': 'Radar', 'stars': 'Starfield', 'bunnies': 'Bunnies', 'clock': 'Clock',
               'album': 'Photo album', 'gif': 'GIFs', 'black': 'Black', 'off': 'Off'}
DEFAULT_OPTIONS = {'pixel_level': 100, 'history_seconds': 60,
                   'saver_style': 'off', 'saver_delay': 60,
                   'saver_speed': 'normal', 'saver_level': 70,
                   'photo_size': 'full', 'snap_timer': 0, 'alerts': 'on',
                   'bunny_count': 6, 'clock_format': '24h', 'slide_seconds': 10,
                   'album_source': 'both', 'album_order': 'order', 'vis_source': 'output'}
OPTION_VALUES = {'pixel_level': (100, 70, 40), 'history_seconds': (30, 60, 120),
                 'saver_style': SAVER_STYLES,
                 'saver_delay': (30, 60, 300), 'saver_speed': ('slow', 'normal', 'fast'),
                 'saver_level': (100, 70, 40), 'photo_size': ('full', 'preview'),
                 'snap_timer': (0, 3, 10), 'alerts': ('on', 'off'),
                 'bunny_count': (1, 3, 6, 12, 24), 'clock_format': ('24h', '12h'),
                 'slide_seconds': (5, 10, 30, 60), 'album_source': ('both', 'camera', 'album'),
                 'album_order': ('order', 'shuffle'), 'vis_source': ('output', 'mic', 'off')}
PHOTO_TYPES = ('.jpg', '.jpeg', '.png')
MIN_FREE_BYTES = 200 * 2**20  # Refuse new photos rather than filling the SD card.


def load_options(path):
    options = DEFAULT_OPTIONS.copy()
    try:
        saved = json.loads(path.read_text())
        if isinstance(saved, dict):
            # Carry forward the old idle-blank choice without enabling a new animation.
            if 'saver_style' not in saved and type(saved.get('idle_blank')) is int:
                if saved['idle_blank'] in (30, 60, 300):
                    options['saver_style'] = 'black'
                    options['saver_delay'] = saved['idle_blank']
            for key, values in OPTION_VALUES.items():
                if type(saved.get(key)) is type(values[0]) and saved[key] in values:
                    options[key] = saved[key]
    except (OSError, ValueError):
        pass
    return options


def save_options(path, options):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(options))
    user_owns(temporary)
    temporary.replace(path)


def command_output(args):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=.6,
                              check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ''


def decode_throttle(output):
    match = re.fullmatch(r'throttled=(0x[0-9a-fA-F]+)', output)
    if not match:
        return {'power': 'Unknown', 'limits': 'Unknown', 'prior_alerts': 'Unknown',
                'flags': None}
    flags = int(match[1], 16)
    active = []
    if flags & 4:
        active.append('Throttled')
    if flags & 2:
        active.append('Capped')
    if flags & 8:
        active.append('Temp limit')
    return {'power': 'LOW VOLTAGE' if flags & 1 else 'No undervoltage',
            'limits': ', '.join(active) or 'None active',
            'prior_alerts': 'Yes, since boot' if flags & 0xf0000 else 'None since boot',
            'flags': flags}


def health_alerts(rows, now=None):
    """Return (level, text) pairs for the banner, most serious first."""
    now = time.monotonic() if now is None else now
    alerts = []
    if rows.get('power') == 'LOW VOLTAGE':
        alerts.append(('critical', 'LOW VOLTAGE'))
    elif rows.get('undervolt_seen_at') is not None and now-rows['undervolt_seen_at'] < 300:
        # The sticky firmware bit flipped since launch: a dip happened between polls.
        alerts.append(('warning', 'VOLTAGE DIPPED'))
    temp = rows.get('temp_c')
    if temp is not None:
        if temp >= 80:
            alerts.append(('critical', f'HOT {temp:.0f} C'))
        elif temp >= 75:
            alerts.append(('warning', f'WARM {temp:.0f} C'))
    limits = rows.get('limits')
    if limits not in (None, 'Unknown', 'None active'):
        alerts.append(('warning', 'THROTTLED' if 'Throttled' in limits else
                       'CPU CAPPED' if 'Capped' in limits else 'TEMP LIMIT'))
    free = rows.get('disk_free_gib')
    if free is not None:
        if free < .25:
            alerts.append(('critical', f'STORAGE {free*1024:.0f} MB'))
        elif free < 1 or rows.get('disk_used_percent', 0) >= 95:
            alerts.append(('warning', f'LOW STORAGE {free:.1f} GB'))
    alerts.sort(key=lambda alert: alert[0] != 'critical')
    return alerts


class HealthPoller:
    """Bounded slow commands run away from the camera/touch rendering loop."""
    def __init__(self):
        self.snapshot = {'ip': 'Checking...', 'wifi': 'Checking...', 'sync': 'Unknown',
                         'undervolt_seen_at': None, **decode_throttle('')}
        self.previous_flags = None
        self.undervolt_seen_at = None
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def note_throttle(self, flags, now=None):
        if flags is not None:
            new_sticky = (self.previous_flags is not None and flags & 0x10000
                          and not self.previous_flags & 0x10000)
            if flags & 1 or new_sticky:
                self.undervolt_seen_at = time.monotonic() if now is None else now
            self.previous_flags = flags
        return self.undervolt_seen_at

    def start(self):
        self.thread.start()

    def run(self):
        while not self.stop_event.is_set():
            values = {'ip': 'No IPv4 address', 'wifi': 'Not connected / unavailable',
                      'sync': 'Unknown', **decode_throttle('')}
            raw = command_output(['ip', '-j', '-4', 'addr', 'show', 'scope', 'global'])
            try:
                addresses = [(item['ifname'], addr['local']) for item in json.loads(raw)
                             for addr in item.get('addr_info', []) if addr.get('family') == 'inet']
                if addresses:
                    values['ip'] = f'{addresses[0][0]} {addresses[0][1]}'
            except (ValueError, KeyError, TypeError):
                values['ip'] = 'Unavailable'
            wireless = [p.name for p in Path('/sys/class/net').glob('*') if (p/'wireless').exists()]
            for name in wireless:
                link = command_output(['iw', 'dev', name, 'link'])
                match = re.search(r'signal:\s*(-?\d+(?:\.\d+)?)\s*dBm', link)
                if match:
                    values['wifi'] = f'{name}: {match[1]} dBm'
                    break
            synced = command_output(['timedatectl', 'show', '-p', 'NTPSynchronized', '--value'])
            values['sync'] = {'yes': 'Synchronized', 'no': 'Not synchronized'}.get(synced, 'Unknown')
            values.update(decode_throttle(command_output(['vcgencmd', 'get_throttled'])))
            values['undervolt_seen_at'] = self.note_throttle(values['flags'])
            self.snapshot = values
            self.stop_event.wait(10)

    def close(self):
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=3)


def font(size, bold=False):
    names = (('DejaVuSans-Bold.ttf', 'LiberationSans-Bold.ttf') if bold else
             ('DejaVuSans.ttf', 'LiberationSans-Regular.ttf'))
    for path in ('/usr/share/fonts/truetype/dejavu/' + names[0],
                 '/usr/share/fonts/truetype/liberation2/' + names[1]):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default()


FONT, SMALL, HUGE = font(15), font(12), font(46)


def rgb565(image):
    a = np.asarray(image.convert('RGB'), dtype=np.uint16)
    return (((a[:, :, 0] >> 3) << 11) |
            ((a[:, :, 1] >> 2) << 5) | (a[:, :, 2] >> 3)).astype('<u2').tobytes()


def user_owns(path):
    if hasattr(os, 'chown') and 'SUDO_UID' in os.environ:
        os.chown(path, int(os.environ['SUDO_UID']), int(os.environ['SUDO_GID']))


def find_panel_framebuffer(root=Path('/sys/class/graphics')):
    """fb0 normally, but boot order can renumber framebuffers when HDMI is attached."""
    for base in sorted(root.glob('fb[0-9]*'), key=lambda p: int(p.name[2:])):
        try:
            if 'ili9341' in (base / 'name').read_text().lower():
                return base
        except OSError:
            continue
    raise RuntimeError('ILI9341 framebuffer not found. Stopping.')


class Framebuffer:
    def __init__(self):
        base = find_panel_framebuffer()
        if (base / 'virtual_size').read_text().strip() != '320,240':
            raise RuntimeError('Expected a 320x240 framebuffer. Keep rotate=270.')
        if int((base / 'bits_per_pixel').read_text()) != 16:
            raise RuntimeError('Expected 16-bit RGB565 framebuffer.')
        self.stride = int((base / 'stride').read_text())
        if self.stride < W * 2:
            raise RuntimeError('Unexpected framebuffer stride.')
        self.file = open(Path('/dev') / base.name, 'r+b', buffering=0)
        self.pixel_level = 100

    def show(self, image):
        level = getattr(self, 'pixel_level', 100)
        if level != 100:
            image = image.point([round(v*level/100) for v in range(256)]*3)
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


def camera_image(array):
    """BGR888 picamera2 arrays are already in R,G,B byte order."""
    return Image.fromarray(np.ascontiguousarray(array[:, :, :3]))


class Camera:
    """Low-resolution live preview plus an on-demand full-sensor still mode."""
    def __init__(self, fps):
        from picamera2 import Picamera2
        self.fps = fps
        self.cam = Picamera2()
        try:
            self.preview_config = self.cam.create_video_configuration(
                main={'size': (W, H), 'format': 'BGR888'},
                controls={'FrameRate': fps}, buffer_count=4)
            # One full-size buffer keeps camera memory use modest on the Pi 4.
            self.still_config = self.cam.create_still_configuration(
                main={'format': 'BGR888'}, buffer_count=1)
            self.cam.configure(self.preview_config)
            self.cam.start(show_preview=False)
        except Exception:
            self.cam.close()
            raise

    def frame(self):
        return camera_image(self.cam.capture_array('main'))

    def set_fps(self, fps):
        self.fps = fps
        self.cam.set_controls({'FrameRate': fps})

    def capture_full(self):
        """Blocks for roughly a second while the sensor switches mode and back."""
        array = self.cam.switch_mode_and_capture_array(self.still_config, 'main')
        self.cam.set_controls({'FrameRate': self.fps})
        return camera_image(array)

    def recover(self):
        """Put the preview back after a failed still capture. False if unusable."""
        try:
            try:
                self.cam.stop()
            except Exception:
                pass
            self.cam.configure(self.preview_config)
            self.cam.start(show_preview=False)
            self.cam.set_controls({'FrameRate': self.fps})
            return True
        except Exception as exc:
            print('Camera could not restart:', exc, flush=True)
            return False

    def close(self):
        self.cam.close()


def photo_label(path):
    match = re.match(r'photo-(\d{4})(\d\d)(\d\d)-(\d\d)(\d\d)(\d\d)', path.name)
    if match:
        y, mo, d, h, mi, s = match.groups()
        return f'{y}/{mo}/{d} {h}:{mi}:{s}'
    return path.name


class PhotoLibrary:
    """Saved snapshots, newest last, with small cached previews for review."""
    def __init__(self, folder):
        self.folder = Path(folder)
        self.items = []
        self.cache = {}
        self.refresh()

    def refresh(self):
        try:
            self.items = sorted(p for p in self.folder.iterdir()
                                if p.suffix.lower() in PHOTO_TYPES and p.is_file()
                                and not p.name.startswith('.'))
        except OSError:
            self.items = []
        return self.items

    def latest(self):
        return self.items[-1] if self.items else None

    def preview(self, path, size):
        """Return (image fitted to size, original size) or (None, None)."""
        if path is None:
            return None, None
        try:
            stamp = path.stat().st_mtime_ns
        except OSError:
            return None, None
        key = (path, size)
        cached = self.cache.get(key)
        if cached and cached[0] == stamp:
            return cached[1], cached[2]
        try:
            with Image.open(path) as source:
                original = source.size
                source.draft('RGB', size)  # JPEG DCT scaling: fast even for 12 MP files.
                picture = ImageOps.contain(source.convert('RGB'), size)
        except (OSError, ValueError, Image.DecompressionBombError) as exc:
            print('Cannot open photo:', path, exc, flush=True)
            return None, None
        if len(self.cache) > 24:
            self.cache.clear()
        self.cache[key] = (stamp, picture, original)
        return picture, original

    def save(self, image):
        if shutil.disk_usage(self.folder).free < MIN_FREE_BYTES:
            raise OSError('storage nearly full')
        name = datetime.now().strftime('photo-%Y%m%d-%H%M%S-%f.jpg')
        final = self.folder / name
        temporary = self.folder / ('.' + name + '.tmp')
        try:
            image.save(temporary, 'JPEG', quality=92)
            user_owns(temporary)
            temporary.replace(final)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        self.refresh()
        return final

    def delete(self, path):
        if path.parent != self.folder:
            raise OSError('not in the photo folder')
        path.unlink()
        self.cache = {k: v for k, v in self.cache.items() if k[0] != path}
        self.refresh()


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

    def drain_until_idle(self, quiet=.3, timeout=5):
        """Keep the grab; consume stale events and require a quiet, released finger."""
        deadline = time.monotonic() + timeout
        idle_since = time.monotonic()
        while time.monotonic() < deadline:
            pending = bool(select.select([self.device], [], [], 0)[0])
            taps = self.poll()
            now = time.monotonic()
            if pending or taps or self.down:
                idle_since = now
            elif now-idle_since >= quiet:
                self.samples = []
                self.released = False
                return
            time.sleep(.01)
        raise RuntimeError('Lift your finger from the screen and try calibration again.')


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
            touch.drain_until_idle()
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
            matrix = fit_calibration(raw, targets)
            touch.drain_until_idle()
            return matrix
        except ValueError as exc:
            print(exc, flush=True)


class Stats:
    def __init__(self):
        self.previous = None
        self.health = HealthPoller()
        self.history = deque(maxlen=180)

    def start(self):
        self.health.start()

    def close(self):
        self.health.close()

    def read(self):
        cpu = [int(v) for v in Path('/proc/stat').read_text().splitlines()[0].split()[1:9]]
        total, idle = sum(cpu), cpu[3] + cpu[4]
        usage = None
        if self.previous:
            dt, di = total-self.previous[0], idle-self.previous[1]
            if dt > 0:
                usage = max(0, min(100, 100*(1-di/dt)))
        self.previous = (total, idle)
        mem = {}
        for line in Path('/proc/meminfo').read_text().splitlines():
            parts = line.split()
            mem[parts[0].rstrip(':')] = int(parts[1])
        used = (mem['MemTotal'] - mem['MemAvailable']) / 1048576
        allmem = mem['MemTotal'] / 1048576
        memory_percent = 100*used/allmem
        disk = shutil.disk_usage('/')
        seconds = int(float(Path('/proc/uptime').read_text().split()[0]))
        try:
            temp_c = int(Path('/sys/class/thermal/thermal_zone0/temp').read_text())/1000
            temp = f'{temp_c:.1f} C'
        except (OSError, ValueError):
            temp_c, temp = None, 'Unavailable'
        try:
            freq = f"{int(Path('/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq').read_text())/1000:.0f} MHz"
        except (OSError, ValueError):
            freq = 'Unknown'
        now = time.monotonic()
        self.history.append((now, usage, memory_percent))
        while self.history and self.history[0][0] < now-120:
            self.history.popleft()
        return {'cpu': usage, 'temp': temp, 'ram_percent': memory_percent,
                'ram': f'{used:.1f}/{allmem:.1f} GiB',
                'disk_free': f'{disk.free/2**30:.1f} GiB', 'disk_free_gib': disk.free/2**30,
                'temp_c': temp_c,
                'disk_used_percent': 100*disk.used/disk.total,
                'uptime': f'{seconds//3600}h {(seconds//60)%60}m', 'freq': freq,
                **self.health.snapshot}


def button(d, rect, label, active=False, fill=None, font_=None):
    d.rounded_rectangle(rect, radius=5, fill=fill or (ACCENT if active else PANEL))
    d.text(((rect[0]+rect[2])//2, (rect[1]+rect[3])//2), label,
           font=font_ or SMALL, fill=BG if active else TEXT, anchor='mm')


def draw_banner(d, alerts, clock):
    """Health alert bar across the header; tapping it opens Status details."""
    level = alerts[0][0]
    fill, ink = (DANGER, TEXT) if level == 'critical' else (WARN, BG)
    d.rectangle((0, 0, W-1, 23), fill=fill)
    clock_width = d.textlength(clock, font=SMALL)
    summary = '! ' + '  |  '.join(text for _, text in alerts)
    d.text((6, 5), fit_text(d, summary, W-24-clock_width, SMALL), font=SMALL, fill=ink)
    d.text((314, 5), clock, font=SMALL, fill=ink, anchor='ra')


def graph(d, rect, history, channel, now, colour, seconds=60):
    """Time-based x axis and fixed 0..100% y axis; gaps stay gaps."""
    x0, y0, x1, y1 = rect
    d.rectangle(rect, fill='#15232f')
    for fraction in (0, .5, 1):
        y = round(y1-fraction*(y1-y0))
        d.line((x0, y, x1, y), fill=PANEL)
    d.text((2, y0-2), '100', font=TINY, fill=MUTED)
    d.text((14, y1-8), '0', font=TINY, fill=MUTED)
    previous = None
    for sample in history:
        t, value = sample[0], sample[channel]
        if value is None or t < now-seconds or t > now:
            previous = None
            continue
        point = (round(x0+(t-(now-seconds))/seconds*(x1-x0)),
                 round(y1-max(0, min(100, value))/100*(y1-y0)))
        if previous and t-previous[0] <= 2.5:
            d.line((*previous[1], *point), fill=colour, width=2)
        else:
            d.ellipse((point[0]-1,point[1]-1,point[0]+1,point[1]+1),fill=colour)
        previous = (t, point)


TINY = font(10)

STAR_SEEDS = np.random.default_rng(9341).uniform(-1, 1, (48, 3))


def screensaver_frame(style, elapsed, speed='normal', wall_time=None, alerts=()):
    """Pure decorative animation. No sensors, network, or hardware dependencies."""
    image = Image.new('RGB', (W, H), 'black')
    if style in ('black', 'off'):
        return image
    wall_time = wall_time or datetime.now().astimezone()
    d = ImageDraw.Draw(image)
    factor = {'slow': .55, 'normal': 1., 'fast': 1.7}.get(speed, 1.)
    t = max(0, elapsed)*factor
    if style == 'radar':
        cx, cy, radius = 160, 120, 86
        angle = (t*math.tau/12) % math.tau
        d.ellipse((cx-radius,cy-radius,cx+radius,cy+radius),fill='#03140e')
        # Draw the fading tail first, then put scope markings over it.
        for i in range(30):
            a0 = angle-1.05+i*.035
            a1 = a0+.045
            brightness = (i+1)/30
            colour = (0, round(18+67*brightness**2), round(9+27*brightness**2))
            d.polygon([(cx,cy),(cx+radius*math.cos(a0),cy+radius*math.sin(a0)),
                       (cx+radius*math.cos(a1),cy+radius*math.sin(a1))],fill=colour)
        for r in (22,43,65,86):
            d.ellipse((cx-r,cy-r,cx+r,cy+r),outline='#155a3a')
        d.line((cx-radius,cy,cx+radius,cy),fill='#103a28')
        d.line((cx,cy-radius,cx,cy+radius),fill='#103a28')
        for degrees in range(0,360,30):
            a = math.radians(degrees)
            d.line((cx+(radius-4)*math.cos(a),cy+(radius-4)*math.sin(a),
                    cx+radius*math.cos(a),cy+radius*math.sin(a)),fill='#448a60')
        for a,r in ((.3,54),(1.4,72),(2.6,39),(3.7,62),(5.1,48)):
            age = (angle-a) % math.tau
            glow = math.exp(-age*1.45)
            if glow > .025:
                x,y = cx+r*math.cos(a),cy+r*math.sin(a)
                d.ellipse((x-4,y-4,x+4,y+4),outline=(0,round(80*glow),round(35*glow)))
                d.ellipse((x-1,y-1,x+1,y+1),fill=(round(90*glow),round(245*glow),round(130*glow)))
        d.line((cx,cy,cx+radius*math.cos(angle),cy+radius*math.sin(angle)),fill='#74ffc1',width=2)
        d.ellipse((cx-2,cy-2,cx+2,cy+2),fill='#caffd9')
        label, colour = 'AMBIENT RADAR', '#66c998'
    elif style == 'stars':
        for sx,sy,phase in STAR_SEEDS:
            z = 1-((phase/2+.5+t*.1)%1)
            if z < .05:
                continue
            x,y = 160+sx*95/z,120+sy*70/z
            if not (0 <= x < W and 27 <= y < 213):
                continue
            brightness = round(80+175*(1-z))
            tailx,taily = 160+sx*95/(z+.025),120+sy*70/(z+.025)
            d.line((tailx,taily,x,y),fill=(round(brightness*.6),round(brightness*.8),brightness),width=1)
            d.point((round(x),round(y)),fill=(brightness,brightness,brightness))
        label, colour = 'DEEP SPACE', '#85bded'
    else:
        raise ValueError(f'Unknown screensaver: {style}')
    d.rectangle((0,0,319,25),fill='black')
    d.rectangle((0,214,319,239),fill='black')
    if alerts:
        draw_banner(d, alerts, wall_time.strftime('%H:%M'))
    else:
        d.text((8,7),label,font=SMALL,fill=colour)
        d.text((312,7),wall_time.strftime('%H:%M'),font=SMALL,fill=colour,anchor='ra')
    d.text((160,226),'SCREENSAVER / TAP TO WAKE',font=TINY,fill=colour,anchor='mm')
    return image


# ---- Fun: screensavers with state, photo album, GIFs, snake and audio ---------------

BIG, BOLD = font(60, bold=True), font(20, bold=True)

# Original pixel art in the spirit of the Terraria bunny (not Re-Logic's sprite).
# Drop your own sheet in cyberdeck-data/sprites/bunny.png to replace it.
BUNNY_PALETTE = {'o': (46, 40, 56), 'w': (240, 240, 244), 'g': (178, 178, 192),
                 'p': (232, 150, 170), 'e': (20, 16, 24), 'n': (232, 120, 150)}
BUNNY_SIT = ["..........o.o...", ".........owowo..", ".........opopo..", ".........opopo..",
             ".........owowo..", "........owwwwwo.", "........owwwewo.", "...oooooowwwwwno",
             "..owwwwwwwwwwwo.", ".owgwwwwwwwwwo..", ".owgwwwwwwwwwo..", "..ogggwwwgwwgo..",
             "...oooooooooo..."]
BUNNY_HOP = [".........o.o....", "........owowo...", "........opopo...", "........opopo...",
             "........owowo...", ".......owwwwwo..", ".......owwwewo..", "..oooooowwwwwno.",
             ".owwwwwwwwwwwo..", "owgwwwwwwwwwwo..", ".ogggwwwwwwgo...", "..ogo.....ogo...",
             "..oo.......oo..."]


def pixel_sprite(rows, scale=2):
    image = Image.new('RGBA', (len(rows[0]), len(rows)), (0, 0, 0, 0))
    for y, row in enumerate(rows):
        for x, key in enumerate(row):
            if key in BUNNY_PALETTE:
                image.putpixel((x, y), BUNNY_PALETTE[key] + (255,))
    return image.resize((image.width*scale, image.height*scale), Image.NEAREST)


def load_bunny_frames(data=None):
    """Right-facing RGBA frames. Frame 0 = resting; the rest animate while hopping.

    Custom sheet: <data>/sprites/bunny.png, optional bunny.json with
    {"frames": 1, "layout": "vertical" | "horizontal", "scale": 2, "faces": "right" | "left"}.
    """
    default = [pixel_sprite(BUNNY_SIT), pixel_sprite(BUNNY_HOP)]
    path = Path(data) / 'sprites' / 'bunny.png' if data else None
    if path is None or not path.is_file():
        return default
    try:
        meta = path.with_suffix('.json')
        meta = json.loads(meta.read_text()) if meta.is_file() else {}
        count = int(meta.get('frames', 1))
        if not 1 <= count <= 64:
            raise ValueError(f'frames must be 1-64, not {count}')
        scale = max(1, min(4, int(meta.get('scale', 2))))
        with Image.open(path) as sheet:
            sheet = sheet.convert('RGBA')
        vertical = meta.get('layout', 'vertical') == 'vertical'
        fw, fh = (sheet.width, sheet.height//count) if vertical else (sheet.width//count, sheet.height)
        if fw < 2 or fh < 2 or fw*scale > 120 or fh*scale > 120:
            raise ValueError(f'unexpected sprite size {sheet.size} for {count} frame(s)')
        frames = []
        for i in range(count):
            box = (0, i*fh, fw, (i+1)*fh) if vertical else (i*fw, 0, (i+1)*fw, fh)
            frame = sheet.crop(box).resize((fw*scale, fh*scale), Image.NEAREST)
            if meta.get('faces') == 'left':
                frame = ImageOps.mirror(frame)
            frames.append(frame)
        return frames
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        print('Custom bunny sprite ignored:', exc, flush=True)
        return default


def letterbox(picture, size=(W, H)):
    canvas = Image.new('RGB', size, 'black')
    if picture is not None:
        fitted = ImageOps.contain(picture, size)
        canvas.paste(fitted, ((size[0]-fitted.width)//2, (size[1]-fitted.height)//2))
    return canvas


def message_screen(title, lines=(), colour=MUTED):
    image = Image.new('RGB', (W, H), 'black')
    d = ImageDraw.Draw(image)
    d.text((160, 96), title, font=FONT, fill=TEXT, anchor='mm')
    for i, line in enumerate(lines):
        d.text((160, 122+i*18), fit_text(d, line, 300, SMALL), font=SMALL, fill=colour, anchor='mm')
    return image


def saver_header(d, label, colour, wall_time, alerts):
    d.rectangle((0, 0, W-1, 25), fill='black')
    if alerts:
        draw_banner(d, alerts, wall_time.strftime('%H:%M'))
    else:
        d.text((8, 7), label, font=SMALL, fill=colour)
        d.text((312, 7), wall_time.strftime('%H:%M'), font=SMALL, fill=colour, anchor='ra')


class Saver:
    """Screensaver interface: render() -> (image, seconds until next draw)."""
    style = 'black'

    def render(self, now, wall_time, alerts):
        return screensaver_frame('black', 0), float('inf')

    def tap(self, x, y, now):
        return False  # False wakes the panel.

    def close(self):
        pass


class ClassicSaver(Saver):
    def __init__(self, style, options, started):
        self.style, self.options, self.started = style, options, started

    def render(self, now, wall_time, alerts):
        image = screensaver_frame(self.style, now-self.started, self.options['saver_speed'],
                                  wall_time, alerts=alerts)
        # Black is written once; animations are capped at eight frames/sec.
        return image, float('inf') if self.style == 'black' else .125


class Bunny:
    def __init__(self, rng, entering=None):
        self.rng = rng
        self.direction = rng.choice((-1, 1))
        self.ground = rng.uniform(170, 208)
        self.x = rng.uniform(10, W-10) if entering is None else (-16 if entering > 0 else W+16)
        if entering is not None:
            self.direction = entering
        self.hop_start, self.hop_length, self.hop_height = None, .45, 10
        self.rest_until = 0
        self.heart_until = 0

    def jump(self, now, big=False):
        self.hop_start = now
        self.hop_length = .8 if big else self.rng.uniform(.38, .5)
        self.hop_height = 34 if big else self.rng.uniform(6, 13)
        if big:
            self.heart_until = now + 1.2

    def update(self, now, dt, speed):
        if self.hop_start is not None:
            self.x += self.direction*speed*dt
            if now-self.hop_start >= self.hop_length:
                self.hop_start = None
                self.rest_until = now + self.rng.uniform(.15, 1.4)
                if self.rng.random() < .08:
                    self.direction *= -1
        elif now >= self.rest_until:
            self.jump(now)
        # Wrap just past the edge so nearly every bunny is visible at any moment.
        if self.x < -18:
            self.x, self.ground = W+16, self.rng.uniform(170, 208)
        elif self.x > W+18:
            self.x, self.ground = -16, self.rng.uniform(170, 208)

    def lift(self, now):
        if self.hop_start is None:
            return 0
        phase = min(1, (now-self.hop_start)/self.hop_length)
        return 4*self.hop_height*phase*(1-phase)


def bunny_background():
    image = Image.new('RGB', (W, H))
    d = ImageDraw.Draw(image)
    for y in range(H):
        t = y/150
        d.line((0, y, W, y), fill=(round(92+90*t), round(150+75*t), round(235+15*t)))
    for cx, cy, r in ((40, 172, 70), (150, 182, 90), (270, 168, 80)):
        d.ellipse((cx-r, cy-r*.55, cx+r, cy+r*.55), fill='#6fae5c')
    for cx, cy in ((60, 52), (205, 38), (290, 70)):
        for dx, r in ((-14, 10), (0, 14), (14, 10)):
            d.ellipse((cx+dx-r, cy-r*.7, cx+dx+r, cy+r*.7), fill='#f4f8ff')
    d.rectangle((0, 160, W, H), fill='#4f9a3e')
    d.rectangle((0, 160, W, 163), fill='#7cc95a')
    for x in range(0, W, 7):
        d.line((x, 160, x+2, 156), fill='#7cc95a')
    d.rectangle((0, 214, W, H), fill='#7a5233')
    return image


class BunnySaver(Saver):
    """Interactive: tap a bunny to make it jump; - and + change how many there are."""
    style = 'bunnies'

    def __init__(self, options, set_count, frames=None, seed=None):
        import random
        self.options, self.set_count = options, set_count
        self.rng = random.Random(seed)
        self.frames = frames or load_bunny_frames()
        self.mirrored = [ImageOps.mirror(f) for f in self.frames]
        self.background = bunny_background()
        self.bunnies = [Bunny(self.rng) for _ in range(options['bunny_count'])]
        self.last = None
        self.message, self.message_until = '', 0

    def sync_count(self):
        target = self.options['bunny_count']
        while len(self.bunnies) < target:
            self.bunnies.append(Bunny(self.rng, entering=self.rng.choice((-1, 1))))
        del self.bunnies[target:]

    def bunny_box(self, bunny, now):
        frame = self.frames[0]
        top = bunny.ground - frame.height - bunny.lift(now)
        return (bunny.x-frame.width/2, top, bunny.x+frame.width/2, bunny.ground)

    def tap(self, x, y, now):
        if y >= 214 and (x <= 54 or x >= 266):
            values = OPTION_VALUES['bunny_count']
            index = values.index(self.options['bunny_count']) + (1 if x >= 266 else -1)
            if 0 <= index < len(values):
                self.message = self.set_count(values[index])
                self.message_until = now + 1.5
                self.sync_count()
            return True
        hit = []
        for bunny in self.bunnies:
            left, top, right, bottom = self.bunny_box(bunny, now)
            if left-6 <= x <= right+6 and top-6 <= y <= bottom+4:
                hit.append(bunny)
        if hit:
            for bunny in hit:
                bunny.jump(now, big=True)
            return True
        return False

    def render(self, now, wall_time, alerts):
        dt = 0 if self.last is None else min(.25, now-self.last)
        self.last = now
        self.sync_count()
        speed = {'slow': .6, 'normal': 1., 'fast': 1.7}[self.options['saver_speed']] * 40
        image = self.background.copy()
        d = ImageDraw.Draw(image)
        for bunny in sorted(self.bunnies, key=lambda b: b.ground):
            bunny.update(now, dt, speed)
            frames = self.frames if bunny.direction > 0 else self.mirrored
            if bunny.hop_start is None or len(frames) == 1:
                frame = frames[0]
            elif len(frames) == 2:
                frame = frames[1]
            else:
                frame = frames[1 + int((now-bunny.hop_start)*10) % (len(frames)-1)]
            lift = bunny.lift(now)
            shadow = max(4, frame.width//2 - lift/3)
            d.ellipse((bunny.x-shadow, bunny.ground-2, bunny.x+shadow, bunny.ground+2), fill='#3d7a30')
            image.paste(frame, (round(bunny.x-frame.width/2), round(bunny.ground-frame.height-lift)), frame)
            if now < bunny.heart_until:
                hx, hy = bunny.x, bunny.ground-frame.height-lift-8-(1.2-(bunny.heart_until-now))*12
                d.text((hx, hy), '♥', font=FONT, fill='#ff5a7a', anchor='mm')
        saver_header(d, 'BUNNIES', '#e8f4ff', wall_time, alerts)
        for x0, label in ((4, '−'), (270, '+')):
            d.rounded_rectangle((x0, 217, x0+46, 237), radius=5, fill='#5a3a22')
            d.text((x0+23, 226), label, font=FONT, fill=TEXT, anchor='mm')
        count = self.options['bunny_count']
        text = self.message if now < self.message_until else (
            f"{count} bunn{'y' if count == 1 else 'ies'} | tap one!")
        d.text((160, 227), text, font=TINY, fill='#f2e6d8', anchor='mm')
        return image, .1


class ClockSaver(Saver):
    style = 'clock'

    def __init__(self, options):
        self.options = options

    def render(self, now, wall_time, alerts):
        image = Image.new('RGB', (W, H), 'black')
        d = ImageDraw.Draw(image)
        twelve = self.options['clock_format'] == '12h'
        hours = wall_time.strftime('%I' if twelve else '%H').lstrip('0' if twelve else '') or '12'
        main = f"{hours}:{wall_time.strftime('%M')}"
        # Drift a little each minute so the same pixels are not lit all day.
        minute = wall_time.hour*60 + wall_time.minute
        cx, cy = 160 + 22*math.sin(minute*.7), 112 + 16*math.cos(minute*1.3)
        d.text((cx, cy), main, font=BIG, fill=ACCENT, anchor='mm')
        width = d.textlength(main, font=BIG)
        suffix = wall_time.strftime('%p') if twelve else wall_time.strftime('%S')
        d.text((cx+width/2+6, cy+20), suffix, font=SMALL, fill=MUTED, anchor='ls')
        d.text((cx, cy+46), wall_time.strftime('%A %d %B %Y'), font=SMALL, fill=TEXT, anchor='mm')
        # Seconds progress under the date: a faint track with a filled part.
        d.rectangle((cx-width/2, cy+62, cx+width/2, cy+63), fill='#132029')
        d.rectangle((cx-width/2, cy+62, cx-width/2+round(width*wall_time.second/59), cy+63),
                    fill='#2f9e80')
        if alerts:
            draw_banner(d, alerts, wall_time.strftime('%H:%M'))
        return image, 1.02 - wall_time.microsecond/1e6


def album_folders(options, photos, album):
    source = options['album_source']
    return [f for f, use in ((photos, source != 'album'), (album, source != 'camera')) if use]


class Slideshow:
    """Photos from the camera folder and/or cyberdeck-data/album, with a short crossfade."""
    def __init__(self, options, photos, album, library, seed=None):
        import random
        self.options, self.photos, self.album, self.library = options, photos, album, library
        self.rng = random.Random(seed)
        self.items, self.index, self.paused = [], 0, False
        self.shown_at = self.previous = None
        self.refresh()

    def refresh(self):
        items = []
        for folder in album_folders(self.options, self.photos, self.album):
            items += PhotoLibrary(folder).items
        if self.options['album_order'] == 'shuffle':
            self.rng.shuffle(items)
        self.items, self.index, self.shown_at, self.previous = items, 0, None, None
        return items

    def picture(self, index):
        if not self.items:
            return None
        return letterbox(self.library.preview(self.items[index % len(self.items)], (W, H))[0])

    def move(self, step, now):
        if self.items:
            self.previous = (self.picture(self.index), now)
            self.index = (self.index + step) % len(self.items)
            self.shown_at = now

    def frame(self, now):
        if not self.items:
            return message_screen('No pictures for the album',
                                  ['Take photos with SNAP, or copy images to',
                                   '~/cyberdeck-data/album/']), 1.
        if self.shown_at is None:
            self.shown_at = now
        interval = self.options['slide_seconds']
        if not self.paused and now-self.shown_at >= interval:
            self.move(1, now)
        current = self.picture(self.index)
        if self.previous is not None:
            fade = (now-self.previous[1])/.5
            if fade < 1 and self.previous[0] is not None:
                return Image.blend(self.previous[0], current, fade), .08
            self.previous = None
        return current, min(1., max(.05, interval-(now-self.shown_at)))

    def label(self):
        if not self.items:
            return ''
        path = self.items[self.index % len(self.items)]
        return f'{self.index % len(self.items) + 1}/{len(self.items)}  {photo_label(path)}'


class AlbumSaver(Saver):
    style = 'album'

    def __init__(self, slideshow):
        self.show = slideshow
        slideshow.paused = False

    def render(self, now, wall_time, alerts):
        image, delay = self.show.frame(now)
        if alerts:
            image = image.copy()
            draw_banner(ImageDraw.Draw(image), alerts, wall_time.strftime('%H:%M'))
        return image, delay


class GifPlayer:
    """Streams frames from cyberdeck-data/gifs/*.gif, one file open at a time."""
    def __init__(self, folder):
        self.folder = Path(folder)
        self.files, self.index, self.source = [], 0, None
        self.current = self.due = None
        self.frame_no = self.loops = 0
        self.opened_at = 0
        self.paused = False
        self.refresh()

    def refresh(self):
        try:
            self.files = sorted(p for p in self.folder.iterdir()
                                if p.suffix.lower() == '.gif' and p.is_file())
        except OSError:
            self.files = []
        self.close()
        self.index = 0
        return self.files

    def close(self):
        if self.source is not None:
            self.source.close()
        self.source = self.current = None

    def open(self, index, now):
        self.close()
        for attempt in range(len(self.files)):
            self.index = (index + attempt) % len(self.files)
            try:
                self.source = Image.open(self.files[self.index])
                self.frame_no, self.loops, self.opened_at = 0, 0, now
                self.show_frame(now)
                return True
            except (OSError, ValueError, EOFError) as exc:
                print('Cannot play GIF:', self.files[self.index], exc, flush=True)
                self.close()
        return False

    def show_frame(self, now):
        self.source.seek(self.frame_no)
        self.current = letterbox(self.source.convert('RGB'))
        self.due = now + max(40, self.source.info.get('duration') or 100)/1000

    def step(self, now):
        try:
            self.frame_no += 1
            self.show_frame(now)
        except EOFError:
            self.frame_no, self.loops = 0, self.loops + 1
            self.show_frame(now)

    def move(self, step, now):
        if self.files:
            self.open(self.index + step, now)

    def frame(self, now):
        if not self.files:
            return message_screen('No GIFs yet', ['Copy .gif files to', '~/cyberdeck-data/gifs/']), 1.
        if self.source is None and not self.open(self.index, now):
            return message_screen('GIFs could not be opened', ['See the terminal or journal.']), 1.
        if not self.paused and now >= self.due:
            try:
                self.step(now)
            except (OSError, ValueError) as exc:
                print('GIF playback stopped:', exc, flush=True)
                self.open(self.index + 1, now)
        return self.current, max(.01, self.due-now) if not self.paused else .25

    def label(self):
        if not self.files or self.source is None:
            return ''
        frames = getattr(self.source, 'n_frames', 1)
        return f'{self.index+1}/{len(self.files)}  {self.files[self.index].name}  ({frames} frames)'


class GifSaver(Saver):
    style = 'gif'

    def __init__(self, player):
        self.player = player
        player.paused = False

    def render(self, now, wall_time, alerts):
        player = self.player
        if player.source is not None and player.loops >= 1 and now-player.opened_at >= 20:
            player.move(1, now)  # Each GIF plays for at least 20 s and one full loop.
        image, delay = player.frame(now)
        if alerts:
            image = image.copy()
            draw_banner(ImageDraw.Draw(image), alerts, wall_time.strftime('%H:%M'))
        return image, delay

    def close(self):
        self.player.close()


class OverlayApp:
    """Full-screen viewer with tap-to-show controls (album and GIF player)."""
    def __init__(self):
        self.overlay_until, self.delay = 0, .25

    def bottom_labels(self):
        return ('< PREV', 'RESUME' if self.content.paused else 'PAUSE', 'NEXT >', 'EXIT')

    def top_labels(self):
        return ()

    def draw_overlay(self, image, now):
        d = ImageDraw.Draw(image)
        d.rectangle((0, 0, W-1, 21), fill=BG)
        d.text((6, 4), fit_text(d, self.content.label(), 308, TINY), font=TINY, fill=TEXT)
        for i, label in enumerate(self.top_labels()):
            button(d, (i*106+3, 26, i*106+103, 52), label)
        for i, label in enumerate(self.bottom_labels()):
            button(d, (i*80+3, 208, i*80+77, 236), label)

    def tap(self, x, y, now):
        if now >= self.overlay_until:
            self.overlay_until = now + 5
            return None
        self.overlay_until = now + 5
        if y >= 204:
            action = row_button(x)
            if action == 3:
                return 'exit'
            if action == 1:
                self.content.paused = not self.content.paused
            else:
                self.content.move(-1 if action == 0 else 1, now)
        elif self.top_labels() and 26 <= y <= 52:
            self.top_action(min(2, int(x)//106))
        else:
            self.overlay_until = 0
        return None

    def top_action(self, index):
        pass

    def render(self, now, wall_time, alerts):
        image, self.delay = self.content.frame(now)
        if now < self.overlay_until:
            image = image.copy()
            self.draw_overlay(image, now)
            self.delay = min(self.delay, max(.05, self.overlay_until-now))
        return image


class AlbumApp(OverlayApp):
    def __init__(self, slideshow, cycle_option):
        super().__init__()
        self.content, self.cycle_option = slideshow, cycle_option

    def top_labels(self):
        options = self.content.options
        return (f"Every {options['slide_seconds']}s",
                {'both': 'All pictures', 'camera': 'Camera only', 'album': 'Album folder'}[options['album_source']],
                'Shuffled' if options['album_order'] == 'shuffle' else 'In order')

    def top_action(self, index):
        key = ('slide_seconds', 'album_source', 'album_order')[index]
        self.cycle_option(key)
        if key != 'slide_seconds':
            self.content.refresh()


class GifApp(OverlayApp):
    def __init__(self, player):
        super().__init__()
        self.content = player


SNAKE_COLS, SNAKE_ROWS, SNAKE_CELL, SNAKE_ORIGIN = 26, 14, 12, (4, 26)


class SnakeGame:
    def __init__(self, cols=SNAKE_COLS, rows=SNAKE_ROWS, seed=None):
        import random
        self.cols, self.rows = cols, rows
        self.rng = random.Random(seed)
        self.reset()

    def reset(self):
        cx, cy = self.cols//2, self.rows//2
        self.body = deque([(cx, cy), (cx-1, cy), (cx-2, cy)])
        self.direction, self.pending = (1, 0), []
        self.score, self.alive, self.won = 0, True, False
        self.food = self.place_food()

    def place_food(self):
        taken = set(self.body)
        free = [(x, y) for x in range(self.cols) for y in range(self.rows) if (x, y) not in taken]
        return self.rng.choice(free) if free else None

    def turn(self, direction):
        last = self.pending[-1] if self.pending else self.direction
        if direction in (last, (-last[0], -last[1])) or len(self.pending) >= 2:
            return False
        self.pending.append(direction)
        return True

    def steps_per_second(self):
        return min(12, 5 + self.score*.25)

    def step(self):
        if not self.alive:
            return
        if self.pending:
            self.direction = self.pending.pop(0)
        hx, hy = self.body[0]
        head = (hx+self.direction[0], hy+self.direction[1])
        growing = head == self.food
        blocked = set(self.body) if growing else set(list(self.body)[:-1])
        if not (0 <= head[0] < self.cols and 0 <= head[1] < self.rows) or head in blocked:
            self.alive = False
            return
        self.body.appendleft(head)
        if growing:
            self.score += 1
            self.food = self.place_food()
            if self.food is None:
                self.alive, self.won = False, True
        else:
            self.body.pop()


def cell_rect(cell, inset=1):
    x0 = SNAKE_ORIGIN[0] + cell[0]*SNAKE_CELL
    y0 = SNAKE_ORIGIN[1] + cell[1]*SNAKE_CELL
    return (x0+inset, y0+inset, x0+SNAKE_CELL-1-inset, y0+SNAKE_CELL-1-inset)


class SnakeApp:
    """Tap where you want the snake to go; it turns toward the tap."""
    def __init__(self, best=0, save_best=lambda best: None, seed=None):
        self.game = SnakeGame(seed=seed)
        self.best, self.save_best = best, save_best
        self.state, self.next_step, self.delay = 'ready', 0, .25

    @property
    def running(self):
        return self.state == 'running'

    def start(self, now):
        if not self.game.alive:
            self.game.reset()
        self.state, self.next_step = 'running', now + .4

    def tap(self, x, y, now):
        if self.state == 'running':
            if y < 24 and x >= 240:
                self.state = 'paused'
                return None
            hx, hy = self.game.body[0]
            cx = SNAKE_ORIGIN[0] + (hx+.5)*SNAKE_CELL
            cy = SNAKE_ORIGIN[1] + (hy+.5)*SNAKE_CELL
            dx, dy = x-cx, y-cy
            axes = [(1 if dx > 0 else -1, 0), (0, 1 if dy > 0 else -1)]
            if abs(dy) > abs(dx):
                axes.reverse()
            for direction, size in zip(axes, sorted((abs(dx), abs(dy)), reverse=True)):
                if size >= SNAKE_CELL/2 and self.game.turn(direction):
                    break
            return None
        # Ready / paused / game over: two big buttons.
        if 150 <= y <= 196:
            if x < 160:
                if self.state == 'paused':
                    self.state, self.next_step = 'running', now + .4
                else:
                    self.start(now)
            else:
                if self.state == 'paused':
                    self.state = 'ready'
                    self.game.reset()
                return 'exit'
        return None

    def update(self, now):
        while self.state == 'running' and now >= self.next_step:
            self.game.step()
            self.next_step += 1/self.game.steps_per_second()
            if not self.game.alive:
                self.state = 'over'
                if self.game.score > self.best:
                    self.best = self.game.score
                    self.save_best(self.best)

    def render(self, now, wall_time, alerts):
        self.update(now)
        image = Image.new('RGB', (W, H), '#0b1218')
        d = ImageDraw.Draw(image)
        d.text((8, 4), f'SNAKE  {self.game.score}', font=FONT, fill=ACCENT)
        d.text((150, 7), f'BEST {self.best}', font=SMALL, fill=MUTED)
        if self.state == 'running':
            button(d, (244, 2, 316, 22), 'PAUSE')
        else:
            d.text((312, 7), wall_time.strftime('%H:%M'), font=SMALL, fill=TEXT, anchor='ra')
        x0, y0 = SNAKE_ORIGIN
        d.rectangle((x0-1, y0-1, x0+SNAKE_COLS*SNAKE_CELL, y0+SNAKE_ROWS*SNAKE_CELL), outline=PANEL)
        if self.game.food:
            d.ellipse(cell_rect(self.game.food, 2), fill='#ff6b5e')
        for i, cell in enumerate(self.game.body):
            d.rectangle(cell_rect(cell), fill=ACCENT if i == 0 else '#2f9e80')
        d.text((160, 214), 'Tap where the snake should go. Avoid walls and your tail.',
               font=TINY, fill=MUTED, anchor='mm')
        d.text((160, 228), 'PAUSE (top right) to stop.', font=TINY, fill=MUTED, anchor='mm')
        if self.state != 'running':
            title, left, right = {
                'ready': ('SNAKE', 'START', 'QUIT'),
                'paused': ('PAUSED', 'RESUME', 'QUIT'),
                'over': ('YOU WIN!' if self.game.won else 'GAME OVER', 'PLAY AGAIN', 'QUIT')}[self.state]
            d.rounded_rectangle((30, 56, 290, 204), radius=8, fill=BG, outline=ACCENT, width=2)
            d.text((160, 84), title, font=BOLD, fill=TEXT, anchor='mm')
            d.text((160, 115), f'Score {self.game.score}   Best {self.best}', font=SMALL, fill=MUTED, anchor='mm')
            if self.state == 'ready':
                d.text((160, 133), 'Tap ahead of the head to turn.', font=TINY, fill=MUTED, anchor='mm')
            button(d, (40, 150, 155, 196), left, active=True)
            button(d, (165, 150, 280, 196), right)
        self.delay = max(.01, self.next_step-now) if self.state == 'running' else .25
        return image


def desktop_user():
    uid = int(os.environ.get('SUDO_UID', os.getuid()))
    gid = int(os.environ.get('SUDO_GID', os.getgid()))
    return uid, gid


def user_process_kwargs():
    """Run audio tools as the desktop user so they reach that user's PipeWire/D-Bus."""
    uid, gid = desktop_user()
    env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'LANG': 'C.UTF-8',
           'XDG_RUNTIME_DIR': f'/run/user/{uid}',
           'DBUS_SESSION_BUS_ADDRESS': f'unix:path=/run/user/{uid}/bus'}
    kwargs = {'env': env}
    try:
        import pwd
        entry = pwd.getpwuid(uid)
        env['HOME'] = entry.pw_dir
        groups = os.getgrouplist(entry.pw_name, gid)
    except (ImportError, KeyError, OSError):
        groups = None
    if os.getuid() == 0 and uid != 0:
        kwargs.update(user=uid, group=gid)
        if groups is not None:
            kwargs['extra_groups'] = groups
    return kwargs


def user_command(args, timeout=2.):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=True,
                              **user_process_kwargs()).stdout.strip()
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def parse_wpctl(output):
    match = re.search(r'Volume:\s*([0-9.]+)', output or '')
    if not match:
        return None, False
    return round(float(match[1])*100), '[MUTED]' in output


def parse_amixer(output):
    levels = re.findall(r'\[(\d+)%\]', output or '')
    states = re.findall(r'\[(on|off)\]', output or '')
    if not levels:
        return None, False
    return int(levels[0]), bool(states) and all(s == 'off' for s in states)


class AudioControl:
    """Volume (PipeWire wpctl, else ALSA amixer) and media keys (playerctl), off-thread."""
    SINK = '@DEFAULT_AUDIO_SINK@'

    def __init__(self, run=user_command, which=shutil.which):
        self.run, self.which = run, which
        self.state = {'backend': 'Checking...', 'volume': None, 'muted': False,
                      'player': None, 'title': ''}
        self.backend = None
        self.active = False
        self.actions = deque(maxlen=8)
        self.wake = threading.Event()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.loop, daemon=True)

    def start(self):
        self.thread.start()

    def close(self):
        self.stop_event.set()
        self.wake.set()
        if self.thread.is_alive():
            self.thread.join(timeout=3)

    def request(self, action):
        self.actions.append(action)
        self.wake.set()

    def detect(self):
        if self.which('wpctl') and parse_wpctl(self.run(['wpctl', 'get-volume', self.SINK]))[0] is not None:
            return ('wpctl', None)
        if self.which('amixer'):
            names = re.findall(r"'([^']+)'", self.run(['amixer', 'scontrols']) or '')
            for preferred in ('Master', 'PCM', 'Headphone', 'Speaker', 'HDMI'):
                if preferred in names:
                    return ('amixer', preferred)
            if names:
                return ('amixer', names[0])
        return (None, None)

    def act(self, action):
        kind, control = self.backend
        player = self.which('playerctl')
        commands = {
            'wpctl': {'up': ['wpctl', 'set-volume', '-l', '1.0', self.SINK, '5%+'],
                      'down': ['wpctl', 'set-volume', self.SINK, '5%-'],
                      'mute': ['wpctl', 'set-mute', self.SINK, 'toggle']},
            'amixer': {'up': ['amixer', '-M', 'sset', control or '', '5%+'],
                       'down': ['amixer', '-M', 'sset', control or '', '5%-'],
                       'mute': ['amixer', 'sset', control or '', 'toggle']},
        }.get(kind, {})
        media = {'play': 'play-pause', 'next': 'next', 'prev': 'previous'}
        if action in commands:
            self.run(commands[action])
        elif action in media and player:
            self.run(['playerctl', media[action]])

    def refresh(self):
        kind, control = self.backend
        if kind == 'wpctl':
            volume, muted = parse_wpctl(self.run(['wpctl', 'get-volume', self.SINK]))
            backend = 'PipeWire'
        elif kind == 'amixer':
            volume, muted = parse_amixer(self.run(['amixer', '-M', 'sget', control]))
            backend = f'ALSA {control}'
        else:
            volume, muted, backend = None, False, 'No volume control found'
        player = title = None
        if self.which('playerctl'):
            player = self.run(['playerctl', 'status']) or 'No player'
            if player in ('Playing', 'Paused'):
                title = self.run(['playerctl', 'metadata', '--format', '{{artist}} - {{title}}'])
                title = (title or '').strip(' -')
        self.state = {'backend': backend, 'volume': volume, 'muted': muted,
                      'player': player, 'title': title or ''}

    def loop(self):
        while not self.stop_event.is_set():
            self.wake.wait(2 if self.active else 30)
            self.wake.clear()
            if self.stop_event.is_set():
                break
            if not self.active and not self.actions:
                continue
            if self.backend is None:
                self.backend = self.detect()
            while self.actions:
                self.act(self.actions.popleft())
            self.refresh()


VIS_BANDS, VIS_RATE, VIS_CHUNK = 24, 22050, 1024
CAPTURE_COMMANDS = {
    'output': (['parec', '--raw', '--format=s16le', f'--rate={VIS_RATE}', '--channels=1',
                '--latency-msec=40', '-d', '@DEFAULT_MONITOR@'],
               ['pw-record', '-P', '{ stream.capture.sink=true }', '--rate', str(VIS_RATE),
                '--channels', '1', '--format', 's16', '-']),
    'mic': (['arecord', '-q', '-t', 'raw', '-f', 'S16_LE', '-r', str(VIS_RATE), '-c', '1'],
            ['pw-record', '--rate', str(VIS_RATE), '--channels', '1', '--format', 's16', '-']),
}
VIS_EDGES = np.logspace(np.log10(60), np.log10(10000), VIS_BANDS+1)
VIS_FREQS = np.fft.rfftfreq(VIS_CHUNK, 1/VIS_RATE)
VIS_WINDOW = np.hanning(VIS_CHUNK)


class AudioCapture:
    """Reads mono PCM from a capture tool and keeps smoothed spectrum bands (0..1)."""
    def __init__(self, which=shutil.which, popen=subprocess.Popen):
        self.which, self.popen = which, popen
        self.bands = np.zeros(VIS_BANDS)
        self.wave = np.zeros(128)
        self.level = 0.
        self.source, self.status = None, 'Off'
        self.process = self.thread = None
        self.stop_event = threading.Event()

    def feed(self, samples):
        x = np.asarray(samples, dtype=float)[-VIS_CHUNK:] / 32768
        if len(x) < VIS_CHUNK:
            x = np.pad(x, (VIS_CHUNK-len(x), 0))
        self.level = float(np.sqrt(np.mean(x*x)))
        magnitude = np.abs(np.fft.rfft(x*VIS_WINDOW)) / (VIS_CHUNK/4)
        bands = np.zeros(VIS_BANDS)
        for i in range(VIS_BANDS):
            mask = (VIS_FREQS >= VIS_EDGES[i]) & (VIS_FREQS < VIS_EDGES[i+1])
            peak = magnitude[mask].max() if mask.any() else 0
            bands[i] = np.clip((20*np.log10(peak+1e-9) + 60) / 60, 0, 1)
        self.bands = np.maximum(bands, self.bands*.8)  # Fast attack, slow fall.
        self.wave = x[::VIS_CHUNK//128][:128]

    def ensure(self, source):
        if source == 'off':
            self.stop()
            self.status = 'Visualizer off'
        elif source != self.source:
            self.start(source)

    def start(self, source):
        self.stop()
        self.source, self.status = source, 'Starting...'
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, args=(source, self.stop_event), daemon=True)
        self.thread.start()

    def run(self, source, stop_event):
        for command in CAPTURE_COMMANDS[source]:
            if stop_event.is_set():
                return
            if not self.which(command[0]):
                continue
            try:
                process = self.popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     **user_process_kwargs())
            except (OSError, ValueError) as exc:
                print('Audio capture failed:', command[0], exc, flush=True)
                continue
            self.process = process
            if stop_event.is_set():  # stop() ran while the tool was launching.
                process.kill()
                process.wait()
                return
            self.status = f'Listening via {command[0]}'
            got = False
            while not stop_event.is_set():
                data = process.stdout.read(VIS_CHUNK*2)
                if len(data) < 2:
                    break
                got = True
                self.feed(np.frombuffer(data[:len(data)//2*2], dtype='<i2'))
            if stop_event.is_set():
                return
            process.kill()
            process.wait()
            if got:
                self.status = f'{command[0]} stopped'
                return
        self.bands[:] = 0
        self.status = ('No output capture (needs parec or pw-record)' if source == 'output'
                       else 'No microphone found')

    def stop(self):
        self.stop_event.set()
        process, self.process = self.process, None
        if process is not None:
            try:
                process.kill()
                process.wait(timeout=2)
            except (OSError, subprocess.SubprocessError):
                pass
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=2)
        self.thread, self.source = None, None
        self.bands = np.zeros(VIS_BANDS)
        self.level = 0.


def draw_bars(d, rect, bands, colour_top=ACCENT, colour_low='#2e6fb0'):
    x0, y0, x1, y1 = rect
    width = (x1-x0) / len(bands)
    for i, value in enumerate(bands):
        height = max(1, round(value*(y1-y0)))
        left = round(x0+i*width+1)
        right = round(x0+(i+1)*width-2)
        d.rectangle((left, y1-height, right, y1), fill=colour_low)
        cap = max(1, height//3)
        d.rectangle((left, y1-height, right, y1-height+cap), fill=colour_top)


class VisualApp:
    """Full-screen visualizer; tap anywhere to return to the Audio page."""
    def __init__(self, capture, audio):
        self.capture, self.audio, self.delay = capture, audio, 1/15

    def tap(self, x, y, now):
        return 'exit'

    def render(self, now, wall_time, alerts):
        image = Image.new('RGB', (W, H), 'black')
        d = ImageDraw.Draw(image)
        draw_bars(d, (4, 28, 316, 190), self.capture.bands)
        wave = self.capture.wave
        points = [(4+i*312/(len(wave)-1), 214+v*40) for i, v in enumerate(wave)]
        d.line(points, fill='#67bfff', width=1)
        saver_header(d, fit_text(d, self.audio.state.get('title') or self.capture.status, 240, SMALL),
                     MUTED, wall_time, alerts)
        return image


def app_tile_at(x, y):
    for i in range(4):
        col, row = i % 2, i // 2
        if 8+col*156 <= x <= 155+col*156 and 30+row*86 <= y <= 110+row*86:
            return i
    return None


APP_TILES = (('AUDIO', 'audio'), ('SNAKE', 'snake'), ('PHOTO ALBUM', 'album'), ('GIF PLAYER', 'gifs'))
AUDIO_ROWS = ((150, 175), (179, 204))
AUDIO_BUTTONS = (('down', 'mute', 'up', 'source'), ('prev', 'play', 'next', 'full'))


def saver_due(options, idle_seconds, page):
    return (options['saver_style'] != 'off' and idle_seconds >= options['saver_delay']
            and page not in ('exit','shutdown','calconfirm','savers','saverstyle','colours',
                             'photodelete','album','gifs','visual'))


def grid_at(x, y, count=8):
    """Two-column, 40 px-pitch button grid shared by Options and Screensavers."""
    for i in range(count):
        col,row = i%2,i//2
        if 8+col*156 <= x <= 155+col*156 and 30+row*40 <= y <= 64+row*40:
            return i
    return None


saver_control_at = option_at = grid_at
TABS = ('CAMERA', 'PHOTOS', 'APPS', 'STATUS', 'OPTIONS', 'EXIT')
TAB_WIDTHS = (58, 56, 44, 52, 62, 48)  # Sized to their labels; sums to 320.
TAB_EDGES = tuple(sum(TAB_WIDTHS[:i]) for i in range(len(TABS)+1))
TAB_PARENT = {'health': 'status', 'savers': 'options', 'saverstyle': 'options',
              'calconfirm': 'options', 'photodelete': 'photos', 'shutdown': 'exit',
              'audio': 'apps'}
TAB_FONT = font(11)
EXIT_BUTTONS = ((12, 103), (114, 205), (216, 307))  # CANCEL, CLOSE APP, SHUT DOWN


def row_button(x):
    """Index of the four 80 px buttons on the Camera and Photos pages."""
    return min(3, max(0, int(x)//80))


def tab_at(x):
    return next((i for i in range(len(TABS)) if x < TAB_EDGES[i+1]), len(TABS)-1)


def timer_label(seconds):
    return f'TIMER {seconds}s' if seconds else 'TIMER OFF'


def render(page, frame, paused, fps, rows, message, history=(), now=None,
           options=None, wall_time=None, alerts=(), cam=None, photo=None,
           service=False, extra=None):
    """cam: camera overlays. photo: review state. extra: apps/audio page data."""
    extra = extra or {}
    options = options or DEFAULT_OPTIONS
    wall_time = wall_time or datetime.now().astimezone()
    cam, photo = cam or {}, photo or {}
    image = Image.new('RGB', (W, H), BG)
    d = ImageDraw.Draw(image)
    if page == 'photofull':
        if photo.get('image') is not None:
            full = ImageOps.contain(photo['image'], (W, H))
            image.paste(full, ((W-full.width)//2, (H-full.height)//2))
        d.text((6, 4), photo.get('label', ''), font=TINY, fill=TEXT,
               stroke_width=2, stroke_fill='black')
        d.text((314, 234), 'tap to return', font=TINY, fill=TEXT, anchor='rs',
               stroke_width=2, stroke_fill='black')
        return image
    title = {'calconfirm':'TOUCH SETUP', 'savers':'SAVERS', 'saverstyle':'SAVER STYLE',
             'photodelete':'PHOTOS',
             'shutdown':'POWER', 'poweroff':'POWER'}.get(page, page.upper())
    if alerts and page not in ('stopped', 'poweroff'):
        draw_banner(d, alerts, wall_time.strftime('%H:%M'))
    else:
        d.text((8, 4), 'PANEL / ' + title, font=FONT, fill=ACCENT)
        d.text((312, 6), wall_time.strftime('%H:%M:%S'), font=SMALL, fill=TEXT, anchor='ra')
    if page == 'stopped':
        d.text((160, 85), 'Panel app is not running', font=FONT, fill=TEXT, anchor='mm')
        if message:
            d.text((160, 108), fit_text(d, message, 300, SMALL), font=SMALL, fill=WARN, anchor='mm')
        if service:
            d.text((160, 135), 'Restart it from a terminal, or reboot:', font=SMALL, fill=MUTED, anchor='mm')
            d.text((160, 158), f'sudo systemctl start {SERVICE}', font=SMALL, fill=ACCENT, anchor='mm')
        else:
            d.text((160, 135), 'Restart from your VNC terminal.', font=SMALL, fill=MUTED, anchor='mm')
            d.text((160, 158), 'sudo python3 ~/panel_app.py', font=SMALL, fill=ACCENT, anchor='mm')
        return image
    if page == 'poweroff':
        d.text((160, 70), 'Shutting down...', font=FONT, fill=TEXT, anchor='mm')
        d.text((160, 105), 'Wait until the green ACT LED stops', font=SMALL, fill=TEXT, anchor='mm')
        d.text((160, 123), 'flashing, then switch off the power.', font=SMALL, fill=TEXT, anchor='mm')
        d.text((160, 160), 'This screen stays lit after shutdown;', font=TINY, fill=MUTED, anchor='mm')
        d.text((160, 174), 'its backlight is wired straight to 3.3 V.', font=TINY, fill=MUTED, anchor='mm')
        return image
    if page == 'camera':
        box = None
        if frame is not None:
            preview = ImageOps.contain(frame, (W, 132))
            box = ((W-preview.width)//2, 25+(132-preview.height)//2)
            image.paste(preview, box)
            box = (*box, box[0]+preview.width-1, box[1]+preview.height-1)
            if cam.get('flash'):
                d.rectangle(box, fill='white')
        else:
            d.text((160, 85), 'Camera is not available', font=FONT, fill=MUTED, anchor='mm')
        # Left gutter: last snapshot (tap to review). Right gutter: capture settings.
        d.text((36, 33), 'LAST', font=TINY, fill=MUTED, anchor='ma')
        thumb = cam.get('thumb')
        if thumb is not None:
            image.paste(thumb, (36-thumb.width//2, 46+(48-thumb.height)//2))
            d.rectangle((36-thumb.width//2-1, 45+(48-thumb.height)//2,
                         36+(thumb.width+1)//2, 46+(48+thumb.height)//2), outline=MUTED)
            d.text((36, 100), 'tap to view', font=TINY, fill=MUTED, anchor='ma')
        else:
            d.rectangle((6, 46, 66, 93), outline=PANEL)
            d.text((36, 70), 'none', font=TINY, fill=MUTED, anchor='mm')
        size_label = 'FULL' if options['photo_size'] == 'full' else '320x240'
        for y, label, value in ((33, 'SIZE', size_label),
                                (75, 'TIMER', f"{options['snap_timer']}s" if options['snap_timer'] else 'OFF')):
            d.text((284, y), label, font=TINY, fill=MUTED, anchor='ma')
            d.text((284, y+14), value, font=SMALL, fill=TEXT, anchor='ma')
        centre = ((box[0]+box[2])//2, (box[1]+box[3])//2) if box else (160, 91)
        if cam.get('countdown'):
            r = 34
            d.ellipse((centre[0]-r, centre[1]-r, centre[0]+r, centre[1]+r), fill=BG,
                      outline=ACCENT, width=3)
            d.text(centre, str(cam['countdown']), font=HUGE, fill=TEXT, anchor='mm')
        if cam.get('capturing'):
            d.rounded_rectangle((centre[0]-80, centre[1]-20, centre[0]+80, centre[1]+20),
                                radius=6, fill=BG, outline=ACCENT, width=2)
            d.text(centre, cam['capturing'], font=FONT, fill=TEXT, anchor='mm')
        if cam.get('saved'):
            d.rounded_rectangle((centre[0]-82, centre[1]-24, centre[0]+82, centre[1]+24),
                                radius=6, fill=ACCENT)
            d.text((centre[0], centre[1]-8), 'PHOTO SAVED', font=FONT, fill=BG, anchor='mm')
            d.text((centre[0], centre[1]+12), cam['saved'], font=SMALL, fill=BG, anchor='mm')
        d.text((8, 158), fit_text(d, message, 304, SMALL), font=SMALL, fill=TEXT)
        labels = ('CANCEL' if cam.get('countdown') else 'SNAP', timer_label(options['snap_timer']),
                  'RESUME' if paused else 'PAUSE', f'{fps:g} FPS')
        for i, label in enumerate(labels):
            button(d, (i*80+3, 176, i*80+77, 203), label, active=(i == 0 and bool(cam.get('countdown'))))
    elif page in ('photos', 'photodelete'):
        picture, count = photo.get('image'), photo.get('count', 0)
        if page == 'photodelete':
            if picture is not None:
                small = ImageOps.contain(picture, (120, 90))
                image.paste(small, (100+(120-small.width)//2, 28))
            d.text((160, 132), 'Delete this photo?', font=FONT, fill=TEXT, anchor='mm')
            d.text((160, 151), photo.get('label', ''), font=SMALL, fill=MUTED, anchor='mm')
            button(d, (12, 164, 151, 202), 'CANCEL', active=True)
            button(d, (168, 164, 307, 202), 'DELETE', fill=DANGER)
        elif not count:
            d.text((160, 80), 'No photos yet', font=FONT, fill=TEXT, anchor='mm')
            d.text((160, 106), 'Use SNAP on the Camera page.', font=SMALL, fill=MUTED, anchor='mm')
            d.text((160, 130), fit_text(d, message, 300, SMALL), font=SMALL, fill=TEXT, anchor='mm')
        else:
            if picture is not None:
                shown = ImageOps.contain(picture, (W, 132))
                image.paste(shown, ((W-shown.width)//2, 25+(132-shown.height)//2))
            else:
                d.text((160, 91), 'Cannot open this photo', font=FONT, fill=MUTED, anchor='mm')
            original = photo.get('size')
            info = f"{photo['index']+1}/{count}  {photo.get('label', '')}"
            if original:
                info += f'  {original[0]}x{original[1]}'
            d.text((8, 158), fit_text(d, message or info, 304, SMALL), font=SMALL, fill=TEXT)
            for i, label in enumerate(('< OLDER', 'NEWER >', 'FULL', 'DELETE')):
                enabled = not ((i == 0 and photo['index'] == 0) or
                               (i == 1 and photo['index'] == count-1))
                button(d, (i*80+3, 176, i*80+77, 203), label if enabled else '',
                       fill=None if enabled else '#18252f')
    elif page == 'status':
        now = time.monotonic() if now is None else now
        cpu = '--' if rows.get('cpu') is None else f"{rows['cpu']:.0f}%"
        d.text((8, 25), f'CPU  {cpu}', font=SMALL, fill=ACCENT)
        d.text((308, 25), rows.get('temp', '--'), font=SMALL, fill=TEXT, anchor='ra')
        graph(d, (28, 43, 310, 75), history, 1, now, ACCENT, options['history_seconds'])
        d.text((8, 82), 'RAM', font=SMALL, fill='#67bfff')
        d.text((308, 82), f"{rows.get('ram', '--')}   {rows.get('ram_percent', 0):.0f}%",
               font=SMALL, fill=TEXT, anchor='ra')
        graph(d, (28, 100, 310, 132), history, 2, now, '#67bfff', options['history_seconds'])
        d.text((8, 140), 'Disk free  ' + rows.get('disk_free', '--'), font=SMALL, fill=TEXT)
        d.rounded_rectangle((235, 145, 310, 152), radius=3, fill=PANEL)
        used_width = round(.75*max(0, min(100, rows.get('disk_used_percent',0))))
        if used_width:
            d.rectangle((235,145,235+used_width,152),fill='#67bfff')
        d.text((8, 157), 'Up ' + rows.get('uptime', '--'), font=SMALL, fill=TEXT)
        d.text((308, 158), 'bar = disk used', font=TINY, fill=MUTED, anchor='ra')
        d.text((8, 176), wall_time.strftime('%Y/%m/%d'), font=SMALL, fill=TEXT)
        button(d, (213, 174, 310, 202), 'DETAILS')
        d.text((8, 194), f"{options['history_seconds']}s history | 0-100%", font=TINY, fill=MUTED)
    elif page == 'health':
        details = [('Date', wall_time.strftime('%Y/%m/%d')),
                   ('Timezone', wall_time.strftime('%Z')),
                   ('Clock sync', rows.get('sync','Unknown')),
                   ('Wi-Fi', rows.get('wifi','Unknown')),
                   ('Power', rows.get('power','Unknown')),
                   ('Clock limits', rows.get('limits','Unknown')),
                   ('Past alerts', rows.get('prior_alerts','Unknown')),
                   ('CPU clock', rows.get('freq','Unknown')),
                   ('Network', rows.get('ip','Unknown'))]
        for i, (label, value) in enumerate(details):
            y = 28+i*19
            d.text((8,y),label,font=TINY,fill=MUTED)
            text = fit_text(d, value, 222, SMALL)
            colour = WARN if value in ('LOW VOLTAGE','Not synchronized') else TEXT
            d.text((88,y),text,font=SMALL,fill=colour)
    elif page == 'options':
        labels = [f"Pixel level: {options['pixel_level']}%", 'Screensavers...',
                  'Preview saver', 'Test colours', 'Calibrate touch...',
                  f"Graph: {options['history_seconds']}s",
                  'Photo size: ' + ('Full' if options['photo_size'] == 'full' else '320x240'),
                  'Alert banner: ' + options['alerts'].title()]
        for i, label in enumerate(labels):
            col, row = i%2, i//2
            button(d, (8+col*156, 30+row*40, 155+col*156, 64+row*40), label)
        d.text((8,190),fit_text(d,message or 'Saved automatically. Dimming leaves backlight on.',304,TINY),
               font=TINY,fill=TEXT if message else MUTED)
    elif page == 'savers':
        delay = options['saver_delay']
        labels = ['Style: ' + SAVER_NAMES[options['saver_style']] + '...',
                  f"After: {str(delay)+'s' if delay < 60 else str(delay//60)+' min'}",
                  'Speed: '+options['saver_speed'].title(),
                  f"Pixel level: {options['saver_level']}%",
                  f"Bunnies: {options['bunny_count']}",
                  f"Slides: every {options['slide_seconds']}s",
                  f"Clock: {options['clock_format']}", 'PREVIEW']
        for i,label in enumerate(labels):
            col,row = i%2,i//2
            button(d,(8+col*156,30+row*40,155+col*156,64+row*40),label,i == 7)
        d.text((8,190),fit_text(d,message or 'Tap once to wake. Bunnies: tap one to make it jump.',304,TINY),font=TINY,fill=MUTED)
    elif page == 'saverstyle':
        for i, style in enumerate(SAVER_STYLES):
            col,row = i%2,i//2
            button(d,(8+col*156,30+row*40,155+col*156,64+row*40),SAVER_NAMES[style].upper(),
                   options['saver_style'] == style)
        d.text((8,190),fit_text(d,'Pictures: camera + cyberdeck-data/album. GIFs: cyberdeck-data/gifs',304,TINY),
               font=TINY,fill=MUTED)
    elif page == 'apps':
        notes = extra.get('notes', {})
        for i, (label, key) in enumerate(APP_TILES):
            col, row = i % 2, i // 2
            rect = (8+col*156, 30+row*86, 155+col*156, 110+row*86)
            d.rounded_rectangle(rect, radius=8, fill=PANEL)
            d.text(((rect[0]+rect[2])//2, rect[1]+32), label, font=FONT, fill=TEXT, anchor='mm')
            d.text(((rect[0]+rect[2])//2, rect[1]+56), fit_text(d, notes.get(key, ''), 140, TINY),
                   font=TINY, fill=MUTED, anchor='mm')
    elif page == 'audio':
        state, capture = extra.get('audio', {}), extra.get('capture')
        d.rectangle((8, 27, 312, 104), fill='#0b1218')
        if capture is not None and capture.source:
            draw_bars(d, (10, 30, 310, 103), capture.bands)
        status = capture.status if capture is not None else 'Visualizer off'
        source = {'output': 'Visualizer: what is playing', 'mic': 'Visualizer: microphone',
                  'off': 'Visualizer off'}[options['vis_source']]
        d.text((10, 107), fit_text(d, f'{source} | {status}', 300, TINY), font=TINY, fill=MUTED)
        if state.get('player') in ('Playing', 'Paused'):
            playing = f"{state['player']}: {state.get('title') or 'unknown track'}"
        elif state.get('player'):
            playing = 'No media player running'
        else:
            playing = 'Media keys need playerctl (see docs)'
        d.text((10, 120), fit_text(d, playing, 300, SMALL), font=SMALL, fill=TEXT)
        volume = state.get('volume')
        d.text((10, 136), 'VOL', font=TINY, fill=MUTED)
        d.rounded_rectangle((40, 137, 250, 145), radius=3, fill=PANEL)
        if volume is not None:
            d.rectangle((40, 137, 40+round(2.1*max(0, min(100, volume))), 145),
                        fill=MUTED if state.get('muted') else ACCENT)
        label = (state.get('backend', '') if volume is None else
                 f"{volume}%" + (' MUTED' if state.get('muted') else ''))
        d.text((310, 135), fit_text(d, label, 56, TINY), font=TINY,
               fill=WARN if state.get('muted') else TEXT, anchor='ra')
        labels = (('VOL -', 'MUTE', 'VOL +', 'SOURCE'),
                  ('|< PREV', 'PAUSE' if state.get('player') == 'Playing' else 'PLAY', 'NEXT >|', 'FULL'))
        for (y0, y1), row in zip(AUDIO_ROWS, labels):
            for i, text in enumerate(row):
                button(d, (i*80+3, y0, i*80+77, y1), text,
                       active=(text == 'MUTE' and state.get('muted')))
    elif page == 'calconfirm':
        d.text((160,57),'Recalibrate the touchscreen?',font=FONT,fill=TEXT,anchor='mm')
        d.text((160,83),'Only needed if taps miss their targets.',font=SMALL,fill=MUTED,anchor='mm')
        button(d,(12,121,151,174),'CANCEL',active=True)
        button(d,(168,121,307,174),'START')
    elif page == 'colours':
        colours = ['white','yellow','cyan','lime','magenta','red','blue','black']
        for i, colour in enumerate(colours):
            d.rectangle((i*40,30,i*40+39,104),fill=colour)
            d.text((i*40+20,110),('W','Y','C','G','M','R','B','K')[i],font=SMALL,fill=TEXT,anchor='ma')
        for x in range(320):
            v = round(x*255/319)
            d.line((x,133,x,168),fill=(v,v,v))
        d.text((160,187),'Full intensity | Tap to return',font=SMALL,fill=TEXT,anchor='mm')
        return image
    elif page == 'shutdown':
        d.text((160, 52), 'Shut down the Raspberry Pi?', font=FONT, fill=TEXT, anchor='mm')
        d.text((160, 78), 'Save your work in VNC first.', font=SMALL, fill=MUTED, anchor='mm')
        d.text((160, 96), 'Power can be removed once the green LED', font=SMALL, fill=MUTED, anchor='mm')
        d.text((160, 112), 'stops flashing.', font=SMALL, fill=MUTED, anchor='mm')
        button(d, (12, 132, 151, 185), 'CANCEL', active=True)
        button(d, (168, 132, 307, 185), 'SHUT DOWN', fill=DANGER)
    else:
        d.text((160, 52), 'Close the app or shut down?', font=FONT, fill=TEXT, anchor='mm')
        d.text((160, 80), 'Close releases the camera and touch.', font=SMALL, fill=MUTED, anchor='mm')
        d.text((160, 98), 'Shut down asks once more first.', font=SMALL, fill=MUTED, anchor='mm')
        for (x0, x1), label in zip(EXIT_BUTTONS, ('CANCEL', 'CLOSE APP', 'SHUT DOWN')):
            button(d, (x0, 121, x1, 174), label, active=(label == 'CANCEL'),
                   fill=DANGER if label == 'SHUT DOWN' else None)
    for i, label in enumerate(TABS):
        active = TAB_PARENT.get(page, page) == label.lower()
        button(d, (TAB_EDGES[i]+2, 207, TAB_EDGES[i+1]-3, 238), label, active, font_=TAB_FONT)
    return image


def fit_text(draw, value, width, selected_font):
    value = str(value)
    if draw.textlength(value, font=selected_font) <= width:
        return value
    while value and draw.textlength(value+'...', font=selected_font) > width:
        value = value[:-1]
    return value+'...'


def recalibrate_safely(screen, touch, previous, save):
    """A timeout or save failure must not exit the running app or release touch."""
    try:
        result = calibrate(screen, touch)
        save(result)
        return result, 'Touch calibration saved'
    except (RuntimeError, OSError, ValueError) as exc:
        print('Calibration not changed:', exc, flush=True)
        return previous, 'Calibration unchanged; see terminal'


def start_camera(fps, attempts=3):
    """Retry briefly: at boot the camera stack can come up after this service."""
    for attempt in range(attempts):
        try:
            return Camera(fps)
        except Exception as exc:
            print(f'Camera unavailable (attempt {attempt+1}/{attempts}):', exc, flush=True)
            if attempt < attempts-1:
                time.sleep(2)
    return None


def request_poweroff():
    subprocess.run(['systemctl', 'poweroff'], check=True, timeout=20,
                   capture_output=True, text=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fps', type=int, choices=(10, 15, 20), default=20)
    parser.add_argument('--calibrate', action='store_true')
    parser.add_argument('--touch', help='Override touchscreen event device')
    parser.add_argument('--status-only', action='store_true')
    parser.add_argument('--data', help='Data folder (default: cyberdeck-data beside this script)')
    args = parser.parse_args()
    # Prevent two copies from competing for the camera and touchscreen.
    import fcntl
    lock = open('/run/lock/cyberdeck-camera-monitor.lock', 'a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise RuntimeError('Cyberdeck is already running. Stop the other copy first '
                           f'(boot service: sudo systemctl stop {SERVICE}).')
    data = Path(args.data) if args.data else Path(__file__).resolve().parent / 'cyberdeck-data'
    photos = data / 'photos'
    photos.mkdir(parents=True, exist_ok=True)
    user_owns(data)
    user_owns(photos)
    calibration_file = data / 'touch.json'
    options_file = data / 'options.json'
    options = load_options(options_file)
    library = PhotoLibrary(photos)
    album_dir, gif_dir = data / 'album', data / 'gifs'
    for folder in (album_dir, gif_dir, data / 'sprites'):
        folder.mkdir(exist_ok=True)
        user_owns(folder)
    games_file = data / 'games.json'
    service = 'INVOCATION_ID' in os.environ  # Set by systemd for the boot service.
    screen = touch = camera = stats = audio = capture = None
    players = []
    shutdown_requested = False
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
        def save_calibration(result):
            temporary = calibration_file.with_suffix('.tmp')
            temporary.write_text(json.dumps({'device': touch.signature, 'matrix': result.tolist()}))
            user_owns(temporary)
            temporary.replace(calibration_file)
        if matrix is None:
            matrix = calibrate(screen, touch)
            save_calibration(matrix)
        ready_message = 'Live | SNAP saves a photo'
        message = ready_message
        if not args.status_only:
            camera = start_camera(args.fps)
            if camera is None:
                message = 'Camera unavailable; see terminal'
        page = 'camera' if camera else 'status'
        frame, paused, fps = None, False, args.fps
        stats, rows, alerts = Stats(), {}, []
        stats.start()
        next_stats, next_draw = 0, 0
        running = True
        exit_return_page = page
        ignore_touch_until = 0
        active_saver = None
        next_saver_draw = 0
        last_touch = time.monotonic()
        option_message = photo_message = ''
        photo_index = len(library.items)-1
        countdown_end = None
        flash_until = saved_until = 0
        saved_text = ''

        def set_option(key, value):
            previous = options[key]
            options[key] = value
            try:
                save_options(options_file, options)
                return 'Saved.'
            except OSError as exc:
                options[key] = previous
                print('Options save failed:',exc,flush=True)
                return 'Save failed; option unchanged.'

        def cycle_option(key):
            values = OPTION_VALUES[key]
            return set_option(key, values[(values.index(options[key])+1) % len(values)])

        def save_best(best):
            try:
                temporary = games_file.with_suffix('.tmp')
                temporary.write_text(json.dumps({'snake_best': best}))
                user_owns(temporary)
                temporary.replace(games_file)
            except OSError as exc:
                print('Could not save high score:', exc, flush=True)

        try:
            best = int(json.loads(games_file.read_text()).get('snake_best', 0))
        except (OSError, ValueError, TypeError, AttributeError):
            best = 0
        audio, capture = AudioControl(), AudioCapture()
        audio.start()
        album_show = Slideshow(options, photos, album_dir, library)
        gif_player, saver_gifs = GifPlayer(gif_dir), GifPlayer(gif_dir)
        players.extend((gif_player, saver_gifs))
        bunny_frames = load_bunny_frames(data)
        apps = {'snake': SnakeApp(best, save_best), 'album': AlbumApp(album_show, cycle_option),
                'gifs': GifApp(gif_player), 'visual': VisualApp(capture, audio)}

        def make_saver(style, now):
            style = style if style != 'off' else 'radar'  # Preview with Off shows Radar.
            if style == 'bunnies':
                return BunnySaver(options, lambda n: set_option('bunny_count', n), bunny_frames)
            if style == 'clock':
                return ClockSaver(options)
            if style == 'album':
                return AlbumSaver(Slideshow(options, photos, album_dir, library))
            if style == 'gif':
                saver_gifs.refresh()
                return GifSaver(saver_gifs)
            return ClassicSaver(style, options, now)

        def open_app(name, now):
            if name == 'album':
                album_show.refresh()
                album_show.paused = False
            elif name == 'gifs':
                gif_player.refresh()
                gif_player.paused = False
            if name in apps and hasattr(apps[name], 'overlay_until'):
                apps[name].overlay_until = now + 4  # Show the controls once on entry.
            return name

        def app_notes():
            count = sum(len(PhotoLibrary(f).items) for f in album_folders(options, photos, album_dir))
            try:
                gifs = sum(1 for p in gif_dir.iterdir() if p.suffix.lower() == '.gif')
            except OSError:
                gifs = 0
            return {'audio': 'Volume, media, visualizer',
                    'snake': f"Best score: {apps['snake'].best}",
                    'album': f"{count} picture{'s' if count != 1 else ''} | slideshow",
                    'gifs': f"{gifs} GIF{'s' if gifs != 1 else ''} | tap to play" if gifs else
                            'Copy GIFs to cyberdeck-data/gifs'}

        def current_photo():
            nonlocal photo_index
            if not library.items:
                return None
            photo_index = max(0, min(photo_index, len(library.items)-1))
            return library.items[photo_index]

        def photo_state():
            path = current_photo()
            if path is None:
                return {'count': 0}
            picture, size = library.preview(path, (W, H))
            return {'image': picture, 'size': size, 'index': photo_index,
                    'count': len(library.items), 'label': photo_label(path)}

        def draw(now, capturing=None):
            """Draw the current page; returns seconds until it next needs drawing."""
            if page in apps:
                screen.pixel_level = options['pixel_level']
                screen.show(apps[page].render(now, datetime.now().astimezone(), alerts))
                return apps[page].delay
            extra = None
            if page == 'apps':
                extra = {'notes': app_notes()}
            elif page == 'audio':
                extra = {'audio': audio.state, 'capture': capture}
            if page == 'camera':
                thumb = library.preview(library.latest(), (60, 45))[0]
                countdown = (max(1, math.ceil(countdown_end-now))
                             if countdown_end is not None else None)
                cam = {'thumb': thumb, 'countdown': countdown, 'capturing': capturing,
                       'flash': now < flash_until,
                       'saved': saved_text if now < saved_until and not capturing else ''}
            else:
                cam = None
            photo = photo_state() if page in ('photos', 'photofull', 'photodelete') else None
            text = {'options': option_message, 'savers': option_message,
                    'photos': photo_message}.get(page, message)
            screen.pixel_level = 100 if page == 'colours' else options['pixel_level']
            screen.show(render(page, frame, paused, fps, rows, text, stats.history, now,
                               options, alerts=alerts, cam=cam, photo=photo, extra=extra))
            return 1/15 if page == 'audio' else 2. if page == 'apps' else .25

        def take_photo():
            """Full-resolution still (or the preview frame), then a visible confirmation."""
            nonlocal camera, message, flash_until, saved_until, saved_text, photo_index
            note = ''
            image = None
            if camera is not None and not paused and options['photo_size'] == 'full':
                draw(time.monotonic(), capturing='Capturing...')
                try:
                    image = camera.capture_full()
                except Exception as exc:
                    print('Full-resolution capture failed:', exc, flush=True)
                    if not camera.recover():
                        try:
                            camera.close()
                        except Exception:
                            pass
                        camera = None
                        message = 'Camera stopped; see terminal'
                        return
                    note = ' (full-res failed)'
            if image is None:
                if frame is None:
                    message = 'No camera frame to save'
                    return
                image = frame.copy()
            if image.width > W:
                draw(time.monotonic(), capturing='Saving...')
            try:
                path = library.save(image)
            except OSError as exc:
                print('Could not save photo:', exc, flush=True)
                message = 'Save failed: ' + ('storage full' if 'storage' in str(exc)
                                             else 'see terminal')
                return
            print('Saved:', path, f'{image.width}x{image.height}', flush=True)
            now = time.monotonic()
            flash_until, saved_until = now + .15, now + 1.8
            saved_text = f'{image.width}x{image.height}{note}'
            message = f'Saved {path.name[6:21]}  {image.width}x{image.height}{note}'
            photo_index = len(library.items)-1

        print('Ready. Tap buttons or press Ctrl+C to exit. Photos:', photos, flush=True)
        while running:
            now = time.monotonic()
            for raw in touch.poll():
                if now < ignore_touch_until:
                    continue
                last_touch = now
                x, y = np.array([*raw, 1.0]) @ matrix
                if active_saver is not None:
                    # Interactive savers (bunnies) may use a tap; otherwise it only wakes
                    # the panel and is consumed so it cannot activate the hidden UI.
                    if 0 <= x < W and 0 <= y < H and active_saver.tap(x, y, now):
                        next_saver_draw = 0
                        ignore_touch_until = now + .12
                        break
                    active_saver.close()
                    active_saver = None
                    next_draw = 0
                    ignore_touch_until = now + .3
                    break
                if page in ('colours', 'photofull'):
                    page = 'options' if page == 'colours' else 'photos'
                    next_draw = 0
                    ignore_touch_until = now + .3
                    break
                if not (0 <= x < W and 0 <= y < H):
                    continue
                if page in apps:
                    if apps[page].tap(x, y, now) == 'exit':
                        if page == 'gifs':
                            gif_player.close()
                        page = 'audio' if page == 'visual' else 'apps'
                        ignore_touch_until = now + .3
                    else:
                        ignore_touch_until = now + .12
                    next_draw = 0
                    break
                if (alerts and y < 24 and
                        page not in ('calconfirm', 'exit', 'shutdown', 'photodelete')):
                    page = 'health'
                elif page == 'calconfirm' and 121 <= y <= 174:
                    if 12 <= x <= 151:
                        page = 'options'
                    elif 168 <= x <= 307:
                        screen.pixel_level = 100
                        matrix, option_message = recalibrate_safely(screen, touch, matrix, save_calibration)
                        page = 'options'
                        last_touch = time.monotonic()
                        ignore_touch_until = last_touch + .5
                elif page == 'savers' and saver_control_at(x,y) is not None:
                    action = saver_control_at(x,y)
                    if action == 0:
                        page = 'saverstyle'
                    elif action < 7:
                        option_message = cycle_option({1:'saver_delay',2:'saver_speed',3:'saver_level',
                                                       4:'bunny_count',5:'slide_seconds',
                                                       6:'clock_format'}[action])
                    else:
                        active_saver, next_saver_draw = make_saver(options['saver_style'], now), 0
                    ignore_touch_until = now + .3
                elif page == 'saverstyle' and saver_control_at(x,y) is not None:
                    option_message = set_option('saver_style', SAVER_STYLES[saver_control_at(x,y)])
                    page = 'savers'
                    ignore_touch_until = now + .3
                elif page == 'apps' and app_tile_at(x, y) is not None:
                    page = open_app(APP_TILES[app_tile_at(x, y)][1], now)
                    ignore_touch_until = now + .3
                elif page == 'audio' and any(y0 <= y <= y1 for y0, y1 in AUDIO_ROWS):
                    row = 0 if y <= AUDIO_ROWS[0][1] else 1
                    action = AUDIO_BUTTONS[row][row_button(x)]
                    if action == 'source':
                        cycle_option('vis_source')
                    elif action == 'full':
                        page = 'visual'
                        ignore_touch_until = now + .3
                    else:
                        audio.request(action)
                elif page == 'options' and option_at(x, y) is not None:
                    action = option_at(x, y)
                    key = {0:'pixel_level', 5:'history_seconds', 6:'photo_size', 7:'alerts'}.get(action)
                    if key:
                        option_message = cycle_option(key)
                    elif action == 1:
                        page = 'savers'
                        option_message = ''
                    elif action == 2:
                        active_saver, next_saver_draw = make_saver(options['saver_style'], now), 0
                    elif action == 3:
                        page = 'colours'
                    elif action == 4:
                        page = 'calconfirm'
                    ignore_touch_until = now + .3
                elif page == 'status' and 213 <= x <= 310 and 174 <= y <= 202:
                    page = 'health'
                elif page == 'exit' and 121 <= y <= 174:
                    choice = next((i for i, (x0, x1) in enumerate(EXIT_BUTTONS) if x0 <= x <= x1), None)
                    if choice == 0:
                        page = exit_return_page
                    elif choice == 1:
                        running = False
                    elif choice == 2:
                        page = 'shutdown'
                        ignore_touch_until = now + .5
                elif page == 'shutdown' and 132 <= y <= 185:
                    if 12 <= x <= 151:
                        page = exit_return_page
                    elif 168 <= x <= 307:
                        shutdown_requested = True
                        running = False
                elif page == 'photodelete' and 164 <= y <= 202:
                    if 12 <= x <= 151:
                        page = 'photos'
                    elif 168 <= x <= 307:
                        path = current_photo()
                        try:
                            if path is not None:
                                library.delete(path)
                                print('Deleted:', path, flush=True)
                                photo_message = 'Deleted ' + photo_label(path)
                        except OSError as exc:
                            print('Could not delete photo:', exc, flush=True)
                            photo_message = 'Delete failed; see terminal'
                        page = 'photos'
                    ignore_touch_until = now + .3
                elif y >= 207:
                    tab = TABS[tab_at(x)].lower()
                    if tab == 'exit':
                        if page not in ('exit', 'shutdown'):
                            exit_return_page = page
                        page = 'exit'
                        ignore_touch_until = time.monotonic() + .35
                    else:
                        if tab == 'photos':
                            library.refresh()
                            photo_index = len(library.items)-1
                            photo_message = ''
                        elif tab == 'options':
                            option_message = ''
                            ignore_touch_until = now + .3
                        page = tab
                elif page == 'camera' and 176 <= y <= 203:
                    action = row_button(x)
                    if action == 0:
                        if countdown_end is not None:
                            countdown_end = None
                            message = 'Timer cancelled'
                        elif options['snap_timer']:
                            countdown_end = now + options['snap_timer']
                            message = 'Self-timer running | CANCEL stops it'
                        else:
                            take_photo()
                            touch.poll()  # Taps made while capturing are discarded.
                            last_touch = time.monotonic()
                            ignore_touch_until = last_touch + .3
                    elif action == 1:
                        cycle_option('snap_timer')
                        message = ('Self-timer: ' + (f"{options['snap_timer']} s" if options['snap_timer']
                                                     else 'off'))
                    elif action == 2 and camera:
                        paused = not paused
                        message = 'Preview paused | SNAP saves this frame' if paused else ready_message
                    elif action == 3 and camera:
                        fps = {10: 15, 15: 20, 20: 10}[fps]
                        camera.set_fps(fps)
                        message = f'Camera target: {fps} FPS'
                elif page == 'camera' and x < 70 and 25 <= y <= 112 and library.items:
                    library.refresh()
                    photo_index = len(library.items)-1
                    photo_message = ''
                    page = 'photos'
                elif page == 'photos' and library.items:
                    if 176 <= y <= 203:
                        action = row_button(x)
                        if action == 0:
                            photo_index = max(0, photo_index-1)
                        elif action == 1:
                            photo_index = min(len(library.items)-1, photo_index+1)
                        elif action == 2:
                            page = 'photofull'
                        else:
                            page = 'photodelete'
                            ignore_touch_until = now + .35
                        photo_message = ''
                    elif 25 <= y <= 157:
                        page = 'photofull'
                next_draw = 0
                break  # Avoid a second queued tap triggering a newly opened screen.
            if not running:
                break
            now = time.monotonic()
            if now >= next_stats:
                rows = stats.read()
                alerts = health_alerts(rows, now) if options['alerts'] == 'on' else []
                next_stats = now + 1
            if countdown_end is not None:
                if page != 'camera':
                    countdown_end = None
                    message = 'Timer cancelled'
                elif now >= countdown_end:
                    countdown_end = None
                    take_photo()
                    touch.poll()
                    last_touch = time.monotonic()
                    ignore_touch_until = last_touch + .3
                    next_draw = 0
                    continue
            # Visualizer audio and volume polling run only while their pages are open.
            listening = page in ('audio', 'visual') and active_saver is None
            audio.active = listening
            if listening:
                capture.ensure(options['vis_source'])
            elif capture.source is not None:
                capture.stop()
            if (active_saver is None and countdown_end is None
                    and not (page == 'snake' and apps['snake'].running)
                    and saver_due(options, now-last_touch, page)):
                active_saver, next_saver_draw = make_saver(options['saver_style'], now), 0
            if active_saver is not None:
                if now >= next_saver_draw:
                    screen.pixel_level = min(options['pixel_level'],options['saver_level'])
                    image, delay = active_saver.render(now, datetime.now().astimezone(), alerts)
                    screen.show(image)
                    next_saver_draw = now + delay
                time.sleep(min(.02, max(0, next_saver_draw-time.monotonic())))
                continue
            live = page == 'camera' and camera is not None and not paused
            if live:
                frame = camera.frame()
            if live or now >= next_draw:
                delay = draw(now)
                next_draw = now + (1/fps if live else delay)
            if not live:
                time.sleep(min(.02, max(0, next_draw-time.monotonic())))
    except KeyboardInterrupt:
        print('\nStopped.', flush=True)
        shutdown_requested = False
    finally:
        if stats is not None:
            stats.close()
        for helper in (audio, capture):
            if helper is not None:
                try:
                    helper.close() if helper is audio else helper.stop()
                except Exception:
                    pass
        for player in players:
            player.close()
        if screen is not None:
            try:
                screen.pixel_level = 100
                screen.show(render('poweroff' if shutdown_requested else 'stopped',
                                   None, False, 0, {}, '', service=service))
            except Exception:
                pass  # Display failure must never prevent camera/input cleanup.
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
    if shutdown_requested:
        # systemd stops this process during shutdown; let SIGTERM end it quietly.
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        print('Shutting down the Pi.', flush=True)
        try:
            request_poweroff()
        except (OSError, subprocess.SubprocessError) as exc:
            detail = getattr(exc, 'stderr', '') or exc
            try:
                failed = Framebuffer()
                failed.show(render('stopped', None, False, 0, {}, 'Shutdown failed; see terminal',
                                   service=service))
                failed.close()
            except Exception:
                pass
            raise RuntimeError(f'Shutdown failed: {detail}')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        raise SystemExit(f'Cyberdeck stopped: {exc}')
