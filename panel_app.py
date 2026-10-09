#!/usr/bin/env python3
"""320x240 RGB565 camera + system monitor for Eli's ILI9341 Pi.

Run: sudo python3 panel_app.py
Optional: --calibrate, --fps 15, --touch /dev/input/eventN, --status-only, --data DIR
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
DEFAULT_OPTIONS = {'pixel_level': 100, 'history_seconds': 60,
                   'saver_style': 'off', 'saver_delay': 60,
                   'saver_speed': 'normal', 'saver_level': 70,
                   'photo_size': 'full', 'snap_timer': 0, 'alerts': 'on'}
OPTION_VALUES = {'pixel_level': (100, 70, 40), 'history_seconds': (30, 60, 120),
                 'saver_style': ('radar', 'stars', 'black', 'off'),
                 'saver_delay': (30, 60, 300), 'saver_speed': ('slow', 'normal', 'fast'),
                 'saver_level': (100, 70, 40), 'photo_size': ('full', 'preview'),
                 'snap_timer': (0, 3, 10), 'alerts': ('on', 'off')}
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


def font(size):
    for path in ('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
                 '/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf'):
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


def button(d, rect, label, active=False, fill=None):
    d.rounded_rectangle(rect, radius=5, fill=fill or (ACCENT if active else PANEL))
    d.text(((rect[0]+rect[2])//2, (rect[1]+rect[3])//2), label,
           font=SMALL, fill=BG if active else TEXT, anchor='mm')


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


def saver_due(options, idle_seconds, page):
    return (options['saver_style'] != 'off' and idle_seconds >= options['saver_delay']
            and page not in ('exit','shutdown','calconfirm','savers','colours',
                             'photodelete'))


def grid_at(x, y, count=8):
    """Two-column, 40 px-pitch button grid shared by Options and Screensavers."""
    for i in range(count):
        col,row = i%2,i//2
        if 8+col*156 <= x <= 155+col*156 and 30+row*40 <= y <= 64+row*40:
            return i
    return None


saver_control_at = option_at = grid_at
TABS = ('CAMERA', 'PHOTOS', 'STATUS', 'OPTIONS', 'EXIT')
TAB_PARENT = {'health': 'status', 'savers': 'options', 'calconfirm': 'options',
              'photodelete': 'photos', 'shutdown': 'exit'}
EXIT_BUTTONS = ((12, 103), (114, 205), (216, 307))  # CANCEL, CLOSE APP, SHUT DOWN


def row_button(x):
    """Index of the four 80 px buttons on the Camera and Photos pages."""
    return min(3, max(0, int(x)//80))


def tab_at(x):
    return min(len(TABS)-1, max(0, int(x)//64))


def timer_label(seconds):
    return f'TIMER {seconds}s' if seconds else 'TIMER OFF'


def render(page, frame, paused, fps, rows, message, history=(), now=None,
           options=None, wall_time=None, alerts=(), cam=None, photo=None,
           service=False):
    """cam: countdown/flash/capturing/saved/thumb overlays. photo: review state."""
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
    title = {'calconfirm':'TOUCH SETUP', 'savers':'SAVERS', 'photodelete':'PHOTOS',
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
        labels = ['RADAR','STARFIELD','BLACK','OFF',
                  f"After: {str(delay)+'s' if delay < 60 else str(delay//60)+' min'}",
                  'Speed: '+options['saver_speed'].title(),
                  f"Pixel level: {options['saver_level']}%", 'PREVIEW']
        styles = ('radar','stars','black','off')
        for i,label in enumerate(labels):
            col,row = i%2,i//2
            active = i<4 and options['saver_style']==styles[i]
            button(d,(8+col*156,30+row*40,155+col*156,64+row*40),label,active)
        d.text((8,190),fit_text(d,message or 'Decorative animation. Tap once to wake.',304,TINY),font=TINY,fill=MUTED)
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
        button(d, (i*64+2, 207, i*64+61, 238), label, active)
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
    service = 'INVOCATION_ID' in os.environ  # Set by systemd for the boot service.
    screen = touch = camera = stats = None
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
        saver_started = next_saver_draw = 0
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
                               options, alerts=alerts, cam=cam, photo=photo))

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
                if active_saver is not None:
                    # Consume the wake-up tap: it must not activate the hidden UI.
                    active_saver = None
                    next_draw = 0
                    ignore_touch_until = now + .3
                    break
                if page in ('colours', 'photofull'):
                    page = 'options' if page == 'colours' else 'photos'
                    next_draw = 0
                    ignore_touch_until = now + .3
                    break
                x, y = np.array([*raw, 1.0]) @ matrix
                if not (0 <= x < W and 0 <= y < H):
                    continue
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
                    if action < 4:
                        option_message = set_option('saver_style', ('radar','stars','black','off')[action])
                    elif action < 7:
                        option_message = cycle_option({4:'saver_delay',5:'saver_speed',6:'saver_level'}[action])
                    else:
                        active_saver = options['saver_style'] if options['saver_style'] != 'off' else 'radar'
                        saver_started, next_saver_draw = now, 0
                    ignore_touch_until = now + .3
                elif page == 'options' and option_at(x, y) is not None:
                    action = option_at(x, y)
                    key = {0:'pixel_level', 5:'history_seconds', 6:'photo_size', 7:'alerts'}.get(action)
                    if key:
                        option_message = cycle_option(key)
                    elif action == 1:
                        page = 'savers'
                        option_message = ''
                    elif action == 2:
                        active_saver = options['saver_style'] if options['saver_style'] != 'off' else 'radar'
                        saver_started, next_saver_draw = now, 0
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
            if (active_saver is None and countdown_end is None
                    and saver_due(options, now-last_touch, page)):
                active_saver = options['saver_style']
                saver_started, next_saver_draw = now, 0
            if active_saver is not None:
                if now >= next_saver_draw:
                    screen.pixel_level = min(options['pixel_level'],options['saver_level'])
                    screen.show(screensaver_frame(active_saver,now-saver_started,options['saver_speed'],
                                                  alerts=alerts))
                    # Black is written once; animations are capped at eight frames/sec.
                    next_saver_draw = float('inf') if active_saver == 'black' else time.monotonic()+.125
                time.sleep(.02)
                continue
            live = page == 'camera' and camera is not None and not paused
            if live:
                frame = camera.frame()
            if live or now >= next_draw:
                draw(now)
                next_draw = now + (1/fps if live else .25)
            if not live:
                time.sleep(.02)
    except KeyboardInterrupt:
        print('\nStopped.', flush=True)
        shutdown_requested = False
    finally:
        if stats is not None:
            stats.close()
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
