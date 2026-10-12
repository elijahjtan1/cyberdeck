#!/usr/bin/env python3
"""320x240 RGB565 camera + system monitor for Eli's ILI9341 Pi.

Run: sudo python3 panel_app.py
Optional: --calibrate, --fps 15, --touch /dev/input/eventN, --status-only, --data DIR,
          --rotation 0|90|180|270 (this run only; normally OPTIONS -> Screen)
Apps: audio + visualizer, Snake, photo album, GIF player. See docs/APPS-AND-SAVERS.md.
No desktop preview or console switching. Hardware imports happen only at startup.
Boot service: sudo python3 setup_panel_service.py (see docs/CAMERA-PHOTOS-POWER.md).
"""
import argparse
import sys
import queue
from panel_tools.runtime import CameraWorker, Job, atomic_json, data_home
from panel_tools.backend import Backend
from core.sysinfo import (SystemSampler, command_output, decode_throttle, health_alerts,
                          undervoltage_seen, wireless_signal)
from panel_tools.camera import CameraDriver
from panel_tools.backlight import load_backlight
from panel_tools.games import (TetrisGame, PongGame, BrickGame, FlappyGame, InvadersGame, MinesGame,
                               Klondike, SudokuGame, PIECES, PIECE_COLOURS, SUITS, rank_name)
from panel_tools.ui import ToolsUI
from panel_tools.ask import AskApp
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

PW, PH = 320, 240  # Physical panel: the framebuffer is always 320x240 landscape.
W, H = PW, PH      # Logical canvas every screen is drawn on; 240x320 when rotated 90/270.
ROTATION = 0       # Degrees clockwise the whole UI is turned on the panel.
BG, PANEL, TEXT, MUTED, ACCENT = '#101923', '#253747', '#eef5fa', '#a9bbc9', '#44d4ad'
DANGER, WARN = '#d8443c', '#f0b347'
# Named colours for everything that isn't one-off artwork (sprites, cards, birds), so a
# palette change such as night mode (roadmap 7.10) edits these lines, not a hundred literals.
RED, ORANGE, YELLOW, GREEN = '#f0605a', '#f29b42', '#f2d24b', '#5fd36b'
CYAN, BLUE, INDIGO, PURPLE = '#4fd8e8', '#67bfff', '#4f86f0', '#b07cf0'
SCREEN, DEEP = '#0b1218', '#070d12'  # Full-screen app and game backgrounds
INSET, SHADE, GRID = '#15232f', '#18252f', '#2a3a46'  # Graph wells, disabled fills, grid lines
SERVICE = 'cyberdeck-panel'
SAVER_STYLES = ('radar', 'stars', 'bunnies', 'clock', 'album', 'gif', 'black', 'off')
SAVER_NAMES = {'radar': 'Radar', 'stars': 'Starfield', 'bunnies': 'Bunnies', 'clock': 'Clock',
               'album': 'Photo album', 'gif': 'GIFs', 'black': 'Black', 'off': 'Off'}
DEFAULT_OPTIONS = {'pixel_level': 100, 'history_seconds': 60,
                   'saver_style': 'off', 'saver_delay': 60,
                   'saver_speed': 'normal', 'saver_level': 70,
                   'photo_size': 'full', 'snap_timer': 0, 'alerts': 'on',
                   'bunny_count': 6, 'clock_format': '24h', 'slide_seconds': 10,
                   'album_source': 'both', 'album_order': 'order', 'vis_source': 'output',
                   'start_page': 'home',
                   'rotation': 90, 'camera_turn': 0}
OPTION_VALUES = {'pixel_level': (100, 70, 40), 'history_seconds': (30, 60, 120),
                 'saver_style': SAVER_STYLES,
                 'saver_delay': (30, 60, 300), 'saver_speed': ('slow', 'normal', 'fast'),
                 'saver_level': (100, 70, 40), 'photo_size': ('full', 'preview'),
                 'snap_timer': (0, 3, 10), 'alerts': ('on', 'off'),
                 'bunny_count': (1, 3, 6, 12, 24), 'clock_format': ('24h', '12h'),
                 'slide_seconds': (5, 10, 30, 60), 'album_source': ('both', 'camera', 'album'),
                 'album_order': ('order', 'shuffle'), 'vis_source': ('output', 'mic', 'off'),
                 'start_page': ('home', 'camera'),
                 'rotation': (90, 0, 270, 180), 'camera_turn': (0, 90, 180, 270)}
PHOTO_TYPES = ('.jpg', '.jpeg', '.png')
MIN_FREE_BYTES = 200 * 2**20  # Refuse new photos rather than filling the SD card.


ROTATION_NAMES = {0: 'Landscape', 90: '90\u00b0 CW', 180: '180\u00b0', 270: '90\u00b0 CCW'}
TRANSPOSE = {90: Image.Transpose.ROTATE_270, 180: Image.Transpose.ROTATE_180,
             270: Image.Transpose.ROTATE_90}  # PIL names count counter-clockwise.


def set_rotation(degrees):
    """Turn the whole UI clockwise on the panel. Layouts read W/H at draw time."""
    global ROTATION, W, H
    if degrees not in (0, 90, 180, 270):
        raise ValueError(f'rotation must be 0, 90, 180 or 270, not {degrees}')
    ROTATION = degrees
    W, H = (PH, PW) if degrees in (90, 270) else (PW, PH)


def to_logical(px, py):
    """Panel (landscape) coordinates -> coordinates on the rotated canvas."""
    if ROTATION == 90:
        return py, PW-1-px
    if ROTATION == 180:
        return PW-1-px, PH-1-py
    if ROTATION == 270:
        return PH-1-py, px
    return px, py


def to_physical(lx, ly):
    if ROTATION == 90:
        return PW-1-ly, lx
    if ROTATION == 180:
        return PW-1-lx, PH-1-ly
    if ROTATION == 270:
        return ly, PH-1-lx
    return lx, ly


def physical_image(image):
    """The logical canvas turned to fit the 320x240 panel."""
    if ROTATION:
        image = image.transpose(TRANSPOSE[ROTATION])
    if image.size != (PW, PH):
        raise ValueError(f'screen image is {image.size}, expected {(W, H)} before rotation')
    return image


def turn_image(image, degrees):
    """Rotate a camera picture clockwise (for a camera mounted at an angle)."""
    return image.transpose(TRANSPOSE[degrees]) if degrees else image


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
    atomic_json(path, options, user_owns)


class HealthPoller:
    """Bounded slow commands run away from the camera/touch rendering loop."""
    def __init__(self):
        self.snapshot = {'ip': 'Checking...', 'wifi': 'Checking...', 'sync': 'Unknown',
                         'undervolt_seen_at': None, **decode_throttle('')}
        self.previous_flags = None
        self.undervolt_seen_at = None
        self.sync, self.sync_due = 'Unknown', 0
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def note_throttle(self, flags, now=None):
        if flags is not None:
            if undervoltage_seen(self.previous_flags, flags):
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
            values['wifi'] = wireless_signal(run=command_output) or values['wifi']
            # Clock sync changes rarely; one process spawn a minute is plenty.
            if self.sync_due <= 0:
                synced = command_output(['timedatectl', 'show', '-p', 'NTPSynchronized', '--value'])
                self.sync = {'yes': 'Synchronized', 'no': 'Not synchronized'}.get(synced, 'Unknown')
                self.sync_due = 6
            self.sync_due -= 1
            values['sync'] = self.sync
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


BACKLIGHT_PWM = False  # True once a PWM backlight is configured: levels mean brightness.


class Brightness:
    """Single place that turns a 0-100 level into light. With a PWM backlight the LEDs
    are dimmed and pixels stay at full colour (less power, better contrast); without one
    the old pixel dimming is used and the 3.3 V backlight stays on."""
    def __init__(self, screen, backlight):
        self.screen, self.backlight = screen, backlight

    def apply(self, level):
        if self.backlight.available:
            self.screen.pixel_level = 100
            self.backlight.set(level)
        else:
            self.screen.pixel_level = level


class Framebuffer:
    """Writes only what changed. fbtft's deferred I/O sends just the touched rows over SPI
    when the framebuffer is memory-mapped, so a static page costs no SPI traffic at all
    and a ticking clock costs a strip of rows instead of the whole 153 KB frame."""
    FULL_REFRESH = 10.  # Seconds; rewrite everything now and then in case anything else drew.

    def __init__(self, base=None, device=None):
        base = base or find_panel_framebuffer()
        if (base / 'virtual_size').read_text().strip() != '320,240':
            raise RuntimeError('Expected a 320x240 framebuffer. Keep rotate=270.')
        if int((base / 'bits_per_pixel').read_text()) != 16:
            raise RuntimeError('Expected 16-bit RGB565 framebuffer.')
        self.stride = int((base / 'stride').read_text())
        if self.stride < PW * 2:
            raise RuntimeError('Unexpected framebuffer stride.')
        self.base = base
        self.file = open(device or Path('/dev') / base.name, 'r+b', buffering=0)
        self.map = None
        try:
            import mmap
            self.map = mmap.mmap(self.file.fileno(), self.stride * PH, mmap.MAP_SHARED,
                                 mmap.PROT_READ | mmap.PROT_WRITE)
        except (ImportError, OSError, ValueError) as exc:
            print('Framebuffer mmap unavailable, using full writes:', exc, flush=True)
        self.last = None
        self.refreshed = 0.
        self.pixel_level = 100
        self.rows_sent = 0  # Diagnostic/test counter.

    def show(self, image):
        image = physical_image(image)
        level = getattr(self, 'pixel_level', 100)
        if level != 100:
            image = image.point([round(v*level/100) for v in range(256)]*3)
        data = np.frombuffer(rgb565(image), dtype=np.uint8).reshape(PH, PW * 2)
        if self.stride != PW * 2:
            padded = np.zeros((PH, self.stride), dtype=np.uint8)
            padded[:, :PW * 2] = data
            data = padded
        now = time.monotonic()
        first, end = 0, PH
        if self.last is not None and now - self.refreshed < self.FULL_REFRESH:
            changed = np.flatnonzero((data != self.last).any(axis=1))
            if not len(changed):
                return  # Identical frame: nothing to send.
            first, end = int(changed[0]), int(changed[-1]) + 1
        else:
            self.refreshed = now
        chunk = data[first:end].tobytes()
        if self.map is not None:
            self.map[first*self.stride:end*self.stride] = chunk
        else:
            self.file.seek(first*self.stride)
            view = memoryview(chunk)
            while view:
                n = self.file.write(view)
                if not n:
                    raise OSError('Framebuffer write stopped.')
                view = view[n:]
        self.last = data
        self.rows_sent += end - first

    def set_blank(self, blank):
        """Ask the driver to switch the panel off/on (best effort; not every fbtft
        driver implements it). The next frame is always sent in full after unblanking."""
        self.last = None
        try:
            (self.base / 'blank').write_text('1' if blank else '0')
            return True
        except OSError:
            return False

    def close(self):
        if self.map is not None:
            self.map.close()
        self.file.close()


class Camera(CameraDriver):
    """The preview stream is always 4:3 in the sensor's own orientation (320x240).
    Screen rotation and OPTIONS -> Camera turn are applied to the picture afterwards;
    asking the sensor for a portrait-shaped stream would crop or stretch it."""
    def __init__(self, fps):
        super().__init__(fps, preview_size=(PW, PH))


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

    def current(self):
        """Raw position of a finger that is down right now (for dragging), else None."""
        return np.array([self.x, self.y], dtype=float) if self.down else None

    def wait(self, timeout):
        """Sleep until a touch event arrives or the timeout passes (no busy polling)."""
        select.select([self.device], [], [], max(0, timeout))

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
    """Crosses are drawn upright for the current rotation; the fit is always to panel
    coordinates, so a saved calibration keeps working when the rotation changes."""
    targets = [(26, 26), (W-27, 26), (W-27, H-27), (26, H-27), (W//2, H//2)]
    panel_targets = [to_physical(x, y) for x, y in targets]
    while True:
        raw = []
        for number, (x, y) in enumerate(targets, 1):
            touch.drain_until_idle()
            img = Image.new('RGB', (W, H), BG)
            d = ImageDraw.Draw(img)
            d.text((W//2, H//2-44), 'TOUCH CALIBRATION', font=FONT, fill=TEXT, anchor='mm')
            d.text((W//2, H//2-22), f'Tap + and release ({number}/5)', font=SMALL, fill=MUTED,
                   anchor='mm')
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
            matrix = fit_calibration(raw, panel_targets)
            touch.drain_until_idle()
            return matrix
        except ValueError as exc:
            print(exc, flush=True)


class Stats:
    def __init__(self):
        self.sampler = SystemSampler()
        self.health = HealthPoller()
        self.history = deque(maxlen=180)

    def start(self):
        self.health.start()

    def close(self):
        self.health.close()

    def read(self):
        v = self.sampler.read()
        now = time.monotonic()
        self.history.append((now, v['cpu'], v['mem_percent']))
        while self.history and self.history[0][0] < now-120:
            self.history.popleft()
        seconds = v['uptime_s']
        return {'cpu': v['cpu'], 'ram_percent': v['mem_percent'], 'temp_c': v['temp_c'],
                'temp': 'Unavailable' if v['temp_c'] is None else f"{v['temp_c']:.1f} C",
                'ram': f"{v['mem_used_gib']:.1f}/{v['mem_total_gib']:.1f} GiB",
                'disk_free': f"{v['disk_free_gib']:.1f} GiB", 'disk_free_gib': v['disk_free_gib'],
                'disk_used_percent': v['disk_used_percent'],
                'uptime': f'{seconds//3600}h {(seconds//60)%60}m',
                'freq': 'Unknown' if v['freq_mhz'] is None else f"{v['freq_mhz']:.0f} MHz",
                **self.health.snapshot}


HOTSPOTS = []  # (rect, action) for the screen drawn last by render(); see hit().
HOTSPOTS_PAGE = [None]  # Which page those belong to, so a stale list is never used.


def hotspot(rect, action):
    if action is not None:
        HOTSPOTS.append((tuple(rect), action))


def hit(x, y, slop=2):
    """Action of the button under a tap, using exactly the rectangles last drawn."""
    for (x0, y0, x1, y1), action in reversed(HOTSPOTS):
        if x0-slop <= x <= x1+slop and y0-slop <= y <= y1+slop:
            return action
    return None


def button(d, rect, label, active=False, fill=None, font_=None, action=None):
    """Rounded button; the label shrinks, then wraps or truncates, to fit narrow portrait buttons."""
    d.rounded_rectangle(rect, radius=5, fill=fill or (ACCENT if active else PANEL))
    chosen, room = font_ or SMALL, rect[2]-rect[0]-8
    if d.textlength(label, font=chosen) > room:
        chosen = TINY
    lines = [label]
    if d.textlength(label, font=chosen) > room and rect[3]-rect[1] >= 24:
        lines = wrap_text(d, label, room, chosen)  # 'TIMER OFF' reads better than 'TIMER ...'.
    for i, line in enumerate(lines):
        d.text(((rect[0]+rect[2])//2, (rect[1]+rect[3])//2+11*i-11*(len(lines)-1)//2),
               fit_text(d, line, room, chosen), font=chosen, fill=BG if active else TEXT, anchor='mm')
    hotspot(rect, action)


def centred(d, y, text, chosen=None, fill=TEXT, x=None):
    """Centred line that steps down a font size, then truncates, if the canvas is narrow."""
    chosen = chosen or SMALL
    room = W-12
    if d.textlength(text, font=chosen) > room and chosen is not TINY:
        chosen = SMALL if chosen is FONT else TINY
    d.text((W//2 if x is None else x, y), fit_text(d, text, room, chosen), font=chosen,
           fill=fill, anchor='mm')


def portrait():
    return H > W


def index_at(rects, x, y, slop=2):
    for i, (x0, y0, x1, y1) in enumerate(rects):
        if x0-slop <= x <= x1+slop and y0-slop <= y <= y1+slop:
            return i
    return None


def content_bottom():
    """Lowest y available to a page above the tab bar."""
    return H-62 if portrait() else 204


def row_rects(count, y0, y1, margin=3, gap=6):
    """Evenly spaced buttons across the canvas (80 px pitch on 320 px, as before)."""
    width = (W - 2*margin - (count-1)*gap) / count
    return [(round(margin+i*(width+gap)), y0, round(margin+i*(width+gap)+width), y1)
            for i in range(count)]


def grid_rects(count, top=30, bottom=None, max_pitch=None):
    """Two-column button grid that fills the content area (Options, Screensavers...)."""
    bottom = content_bottom() if bottom is None else bottom
    rows = (count+1)//2
    pitch = min(max_pitch or (48 if portrait() else 40), (bottom-top-14)//rows)
    width = (W-24)//2
    return [(8+(i % 2)*(width+8), top+(i//2)*pitch, 8+(i % 2)*(width+8)+width-1,
             top+(i//2)*pitch+pitch-6) for i in range(count)]


def tab_rects():
    if portrait():  # Two rows of three under the content area.
        return [((i % 3)*80+2, H-60+(i//3)*30, (i % 3)*80+77, H-60+(i//3)*30+27)
                for i in range(len(TABS))]
    return [(TAB_EDGES[i]+2, 207, TAB_EDGES[i+1]-3, 238) for i in range(len(TABS))]


def draw_banner(d, alerts, clock):
    """Health alert bar across the header; tapping it opens Status details."""
    level = alerts[0][0]
    fill, ink = (DANGER, TEXT) if level == 'critical' else (WARN, BG)
    d.rectangle((0, 0, W-1, 23), fill=fill)
    clock_width = d.textlength(clock, font=SMALL)
    summary = '! ' + '  |  '.join(text for _, text in alerts)
    d.text((6, 5), fit_text(d, summary, W-24-clock_width, SMALL), font=SMALL, fill=ink)
    d.text((W-6, 5), clock, font=SMALL, fill=ink, anchor='ra')


def graph(d, rect, history, channel, now, colour, seconds=60):
    """Time-based x axis and fixed 0..100% y axis; gaps stay gaps."""
    x0, y0, x1, y1 = rect
    d.rectangle(rect, fill=INSET)
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
        cx, cy, radius = W//2, H//2, 86
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
            spread_x, spread_y = W*.3, H*.29
            x,y = W/2+sx*spread_x/z,H/2+sy*spread_y/z
            if not (0 <= x < W and 27 <= y < H-27):
                continue
            brightness = round(80+175*(1-z))
            tailx,taily = W/2+sx*spread_x/(z+.025),H/2+sy*spread_y/(z+.025)
            d.line((tailx,taily,x,y),fill=(round(brightness*.6),round(brightness*.8),brightness),width=1)
            d.point((round(x),round(y)),fill=(brightness,brightness,brightness))
        label, colour = 'DEEP SPACE', '#85bded'
    else:
        raise ValueError(f'Unknown screensaver: {style}')
    d.rectangle((0,0,W-1,25),fill='black')
    d.rectangle((0,H-26,W-1,H-1),fill='black')
    if alerts:
        draw_banner(d, alerts, wall_time.strftime('%H:%M'))
    else:
        d.text((8,7),label,font=SMALL,fill=colour)
        d.text((W-8,7),wall_time.strftime('%H:%M'),font=SMALL,fill=colour,anchor='ra')
    d.text((W//2,H-14),'SCREENSAVER / TAP TO WAKE',font=TINY,fill=colour,anchor='mm')
    return image


# ---- Fun: screensavers with state, photo album, GIFs, snake and audio ---------------

BIG, BOLD, BIG_PORTRAIT = font(60, bold=True), font(20, bold=True), font(50, bold=True)
HOME_CLOCK, HOME_LABEL, HOME_LABEL_SMALL = font(34, bold=True), font(14, bold=True), font(12, bold=True)

# Original pixel art in the spirit of the Terraria bunny (not Re-Logic's sprite).
# Drop your own sheet in cyberdeck-data/sprites/bunny.png to replace it.
# Original pixel art in the spirit of the Terraria bunny (not Re-Logic's sprite).
# Keys: o outline, w fur, g shading, m markings (fur colour unless the coat has patches),
# p inner ear, e eye, n nose. Drop your own sheet in cyberdeck-data/sprites/bunny.png
# to replace them all.
BUNNY_BASE = {'o': (46, 40, 56), 'p': (232, 150, 170), 'e': (20, 16, 24), 'n': (232, 120, 150)}
BUNNY_COATS = {
    'white': {'w': (240, 240, 244), 'g': (178, 178, 192)},
    'grey': {'w': (150, 150, 160), 'g': (110, 110, 122)},
    'chestnut': {'w': (166, 112, 70), 'g': (122, 80, 48)},
    'black': {'w': (58, 54, 64), 'g': (34, 32, 40), 'o': (16, 14, 20), 'e': (210, 210, 220)},
    'fawn': {'w': (238, 214, 170), 'g': (205, 170, 120)},
    'orange': {'w': (232, 152, 74), 'g': (190, 112, 48)},
    'dutch': {'w': (240, 240, 244), 'g': (178, 178, 192), 'm': (58, 54, 64)},
    'spotted': {'w': (240, 240, 244), 'g': (178, 178, 192), 'm': (166, 112, 70)},
    'castor': {'w': (150, 92, 58), 'g': (98, 60, 38)},
    'blue': {'w': (120, 128, 150), 'g': (86, 92, 112)},
    'lilac': {'w': (196, 176, 186), 'g': (156, 136, 148)},
    'sable': {'w': (110, 82, 64), 'g': (70, 50, 40)},
    'harlequin': {'w': (232, 152, 74), 'g': (190, 112, 48), 'm': (40, 36, 44)},
    'opal': {'w': (176, 168, 150), 'g': (128, 130, 140)},
    'cinnamon': {'w': (184, 120, 82), 'g': (136, 92, 70)},
}
# Compact, rounded sitting pose (a shorter, taller body than the first version) + hop pose.
BUNNY_SIT = ["...........o.o..", "..........owowo.", "..........opopo.", "..........opopo.",
             "..........owowo.", ".........owwwwo.", "......oooowwwewo", ".....owwwwwwwwno",
             "....owmwwwwwwwo.", "...owmmwwwwwwo..", "..owmmmwwwwwwo..", "..owgmmwwwwwwo..",
             "...oggwwwwgwo...", "....ooooooooo..."]
BUNNY_HOP = ["..........o.o...", ".........owowo..", ".........opopo..", ".........opopo..",
             ".........owowo..", "........owwwwo..", "....ooooowwwewo.", "...owmwwwwwwwno.",
             "..owmmmwwwwwwo..", ".owgmmwwwwwwo...", "..oggwwwwwwgo...", "...ogo...ogo....",
             "...oo....oo.....", "................"]
# Lop: ears hang down beside a rounder head.
LOP_SIT = ["................", "................", "................", "................",
           "..........ooo...", "........ooowwwo.", "......oogggowwwo", ".....owogggowewo",
           "....owmogggowwwn", "...owmmogggowwo.", "..owmmmwoggowo..", "..owgmmwwoooo...",
           "...oggwwwwgwo...", "....ooooooooo..."]
LOP_HOP = ["................", "................", "................", "................",
           ".........ooo....", ".......ooowwwo..", "....oooogggowwwo", "...owmmogggowewo",
           "..owmmmogggowwwn", ".owgmmwwoggowwo.", "..oggwwwwoooo...", "...ogo...ogo....",
           "...oo....oo.....", "................"]
# Lionhead: a shaggy mane ('f', a lighter shade of the coat) around the neck and head.
LION_SIT = ["................", "...........o.o..", "..........owowo.", "......o.o.opopo.",
            ".....ofoffowwwo.", "....offgffwwwwwo", "...ofgfffwwwwewo", "...offgffwwwwwno",
            "..owffffgfwwwwo.", "..owmfgffffwwo..", ".owmmmwwfwwwwo..", ".owgmmwwwwwwwo..",
            "..oggwwwwwgwo...", "...ooooooooo...."]
LION_HOP = ["................", "..........o.o...", ".........owowo..", ".....o.o.opopo..",
            "....ofoffowwwo..", "...offgffwwwwwo.", "..ofgfffwwwwewo.", ".owffgffwwwwwno.",
            "owgmffffwwwwo...", ".oggwfwwwwwgo...", "..ogo....ogo....", "..oo.....oo.....",
            "................", "................"]
# Rex: shorter, broader ears over the same compact plush body.
REX_SIT = ["................", "................", "...........oo.o.", "..........owwowo",
           "..........opwopo", "..........owwowo", ".........owwwwo.", "......oooowwwewo",
           ".....owwwwwwwwno", "....owmwwwwwwwo.", "...owmmwwwwwwo..", "..owgmmwwwwwwo..",
           "...oggwwwwgwo...", "....ooooooooo..."]
REX_HOP = ["................", ".........oo.o...", "........owwowo..", "........opwopo..",
           "........owwowo..", "........owwwwo.."] + BUNNY_HOP[6:]
BUNNY_SHAPES = {'standard': (BUNNY_SIT, BUNNY_HOP), 'lop': (LOP_SIT, LOP_HOP),
                'lionhead': (LION_SIT, LION_HOP), 'rex': (REX_SIT, REX_HOP)}


def eating_pose(rows):
    """Head lowered one row to nibble: the head columns shift down, the feet stay put."""
    out = [list(r) for r in rows]
    last = len(rows) - 3
    for r in range(last, 0, -1):
        for c in range(9, len(rows[0])):
            out[r][c] = rows[r-1][c]
    for c in range(9, len(rows[0])):
        out[0][c] = '.'
    return [''.join(r) for r in out]


def sleeping_pose(rows):
    """Eyes closed (a dark line)."""
    return [r.replace('e', 'o') for r in rows]


def grooming_pose(rows, raised=False):
    """A front paw lifted to the face: under the chin, or (raised) rubbing over the nose with
    the eyes shut. The paw gets its own outline so it shows against the fur."""
    grid = [list(r) for r in rows]
    ny = next(i for i, r in enumerate(rows) if 'n' in r)
    nx = rows[ny].index('n')
    py = ny if raised else ny + 1
    paw = {(py, nx-1), (py, nx), (py+1, nx-1), (py+1, nx)}
    for y in range(py-1, py+3):
        for x in range(nx-2, nx+2):
            inside = 0 <= y < len(grid) and 0 <= x < len(grid[0])
            if inside and (y, x) not in paw and (y, x) != (ny, nx) and grid[y][x] in '.wgmf':
                grid[y][x] = 'o'
    for y, x in paw:
        grid[y][x] = 'w'
    if raised:
        grid = [['o' if k == 'e' else k for k in r] for r in grid]
    return [''.join(r) for r in grid]

# Breeds: (shape, scale, weight, coats). Flemish giants are drawn 1.5x and come in the
# solid colours the breed is known for.
BUNNY_BREEDS = {
    'standard': ('standard', 2, 34, tuple(BUNNY_COATS)),
    'lop': ('lop', 2, 22, tuple(BUNNY_COATS)),
    'lionhead': ('lionhead', 2, 16, tuple(BUNNY_COATS)),
    'rex': ('rex', 2, 14, ('castor', 'black', 'blue', 'lilac', 'sable', 'opal', 'white',
                           'chestnut', 'cinnamon')),
    'giant': ('standard', 3, 14, ('grey', 'chestnut', 'black', 'fawn', 'white', 'blue', 'sable')),
}
BUNNY_PALETTE = {**BUNNY_BASE, **BUNNY_COATS['white']}  # The classic white bunny.


def pixel_sprite(rows, scale=2, coat='white'):
    colours = {**BUNNY_BASE, **BUNNY_COATS[coat]}
    colours.setdefault('m', colours['w'])
    colours.setdefault('f', tuple(round(v + (255-v)*.35) for v in colours['w']))  # Mane.
    image = Image.new('RGBA', (len(rows[0]), len(rows)), (0, 0, 0, 0))
    for y, row in enumerate(rows):
        for x, key in enumerate(row):
            if key in colours:
                image.putpixel((x, y), colours[key] + (255,))
    return image.resize((image.width*scale, image.height*scale), Image.NEAREST)


def bunny_look(shape='standard', coat='white', scale=2):
    """[sit, hop] frames for one breed/coat."""
    poses = bunny_poses(shape, coat, scale)
    return [poses['sit'], poses['hop'][0]]


def bunny_poses(shape='standard', coat='white', scale=2):
    """{'sit', 'hop' (list), 'eat', 'sleep', 'groom', 'groom2'} frames for one breed/coat,
    trimmed of shared empty rows on top so the tap target hugs the visible bunny."""
    sit, hop = BUNNY_SHAPES[shape]
    templates = {'sit': sit, 'hop': hop, 'eat': eating_pose(sit), 'sleep': sleeping_pose(sit),
                 'groom': grooming_pose(sit), 'groom2': grooming_pose(sit, raised=True)}
    frames = {name: pixel_sprite(rows, scale, coat) for name, rows in templates.items()}
    top = min(f.getbbox()[1] for f in frames.values())
    frames = {name: f.crop((0, top, f.width, f.height)) for name, f in frames.items()}
    frames['hop'] = [frames['hop']]
    return frames


def mirrored_poses(poses):
    return {name: [ImageOps.mirror(f) for f in value] if isinstance(value, list)
            else ImageOps.mirror(value) for name, value in poses.items()}


def random_breed(rng):
    names = list(BUNNY_BREEDS)
    breed = rng.choices(names, weights=[BUNNY_BREEDS[n][2] for n in names])[0]
    shape, scale, _, coats = BUNNY_BREEDS[breed]
    return breed, (shape, rng.choice(coats), scale)


def custom_bunny_frames(data):
    """Right-facing frames from <data>/sprites/bunny.png, or None (missing or unusable) to use
    the built-in breeds. Optional bunny.json beside it:
    {"frames": 1, "layout": "vertical" | "horizontal", "scale": 2, "faces": "right" | "left"}.
    Frame 0 = resting; the rest animate while hopping.
    """
    path = Path(data) / 'sprites' / 'bunny.png' if data else None
    if path is None or not path.is_file():
        return None
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
        return None


def load_bunny_frames(data=None):
    """The custom sprite if there is a usable one, else the classic white bunny."""
    return custom_bunny_frames(data) or bunny_look()


def letterbox(picture, size=None):
    size = size or (W, H)
    canvas = Image.new('RGB', size, 'black')
    if picture is not None:
        fitted = ImageOps.contain(picture, size)
        canvas.paste(fitted, ((size[0]-fitted.width)//2, (size[1]-fitted.height)//2))
    return canvas


def message_screen(title, lines=(), colour=MUTED):
    image = Image.new('RGB', (W, H), 'black')
    d = ImageDraw.Draw(image)
    centred(d, H//2-24, title, FONT)
    for i, line in enumerate(lines):
        centred(d, H//2+2+i*18, line, SMALL, colour)
    return image


def saver_header(d, label, colour, wall_time, alerts):
    d.rectangle((0, 0, W-1, 25), fill='black')
    if alerts:
        draw_banner(d, alerts, wall_time.strftime('%H:%M'))
    else:
        d.text((8, 7), label, font=SMALL, fill=colour)
        d.text((W-8, 7), wall_time.strftime('%H:%M'), font=SMALL, fill=colour, anchor='ra')


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


def bunny_lane(rng):
    """Ground line for a bunny: somewhere on the grass between the hills and the dirt."""
    return rng.uniform(H-70, H-32)


class Bunny:
    def __init__(self, rng, entering=None, breed=None, look=None, half=16):
        self.rng = rng
        self.breed, self.look = breed, look  # look = (shape, coat, scale); None = custom sprite.
        self.half = half  # Half the sprite width, for wrapping at the screen edges.
        self.direction = rng.choice((-1, 1))
        self.ground = bunny_lane(rng)
        self.x = rng.uniform(10, W-10) if entering is None else (-half if entering > 0 else W+half)
        if entering is not None:
            self.direction = entering
        self.hop_start, self.hop_length, self.hop_height = None, .45, 10
        self.rest_until = 0
        self.heart_until = 0
        self.activity, self.activity_start, self.activity_until = None, 0., 0.
        self.food = None  # 'carrot' or 'hay' while eating.

    ACTIVITIES = (  # (name, chance each time a rest ends, duration range in seconds)
        ('eat', .10, (4., 8.)), ('sleep', .05, (7., 15.)), ('poop', .04, (.8, .8)),
        ('groom', .07, (3., 6.)), ('binky', .05, (.9, .9)))

    def jump(self, now, big=False):
        self.activity = self.food = None  # A tap wakes a sleeper / interrupts a snack.
        self.hop_start = now
        heavy = self.breed == 'giant'  # Giants: slower, bigger hops.
        self.hop_length = .8 if big else self.rng.uniform(.38, .5) * (1.35 if heavy else 1)
        self.hop_height = (34 if big else self.rng.uniform(6, 13)) * (1.3 if heavy else 1)
        if big:
            self.heart_until = now + 1.2

    def start_activity(self, now):
        """Maybe eat, sleep, poop, groom or binky instead of the next hop."""
        roll = self.rng.random()
        for name, chance, (shortest, longest) in self.ACTIVITIES:
            if roll < chance:
                self.activity, self.activity_start = name, now
                self.activity_until = now + self.rng.uniform(shortest, longest)
                self.food = self.rng.choice(('carrot', 'hay')) if name == 'eat' else None
                return name
            roll -= chance
        return None

    def update(self, now, dt, speed):
        """Move/animate; returns 'poop' once when droppings should appear."""
        if self.activity is not None:
            if now >= self.activity_until:
                if self.activity == 'binky' and self.rng.random() < .5:
                    self.direction *= -1  # Lands facing the other way half the time.
                self.activity = self.food = None
                self.rest_until = now + self.rng.uniform(.3, 1.)
            elif self.activity == 'binky':  # A short leap forward while twisting.
                self.x += self.direction*speed*dt*.8
                self.wrap()
            return None
        if self.hop_start is not None:
            self.x += self.direction*speed*dt*(.75 if self.breed == 'giant' else 1)
            if now-self.hop_start >= self.hop_length:
                self.hop_start = None
                self.rest_until = now + self.rng.uniform(.15, 1.4)
                if self.rng.random() < .08:
                    self.direction *= -1
        elif now >= self.rest_until:
            started = self.start_activity(now)
            if started:
                return 'poop' if started == 'poop' else None
            self.jump(now)
        self.wrap()
        return None

    def wrap(self):
        """Wrap just past the edge so nearly every bunny is visible at any moment."""
        if self.x < -self.half-2:
            self.x, self.ground = W+self.half, bunny_lane(self.rng)
        elif self.x > W+self.half+2:
            self.x, self.ground = -self.half, bunny_lane(self.rng)

    def binky_phase(self, now):
        """0..1 through a binky, or None when not doing one."""
        if self.activity != 'binky':
            return None
        span = max(.1, self.activity_until - self.activity_start)
        return max(0., min(1., (now - self.activity_start) / span))

    def lift(self, now):
        phase = self.binky_phase(now)
        if phase is not None:  # A binky leaps well above a normal hop.
            return 4*28*(1.2 if self.breed == 'giant' else 1)*phase*(1-phase)
        if self.hop_start is None:
            return 0
        phase = min(1, (now-self.hop_start)/self.hop_length)
        return 4*self.hop_height*phase*(1-phase)


def bunny_background():
    image = Image.new('RGB', (W, H))
    d = ImageDraw.Draw(image)
    grass = H-80
    sx = W/320  # Scenery positions were designed on the 320 px wide landscape canvas.
    for y in range(H):
        t = min(1, y/(grass-10))
        d.line((0, y, W, y), fill=(round(92+90*t), round(150+75*t), round(235+15*t)))
    for cx, cy, r in ((40, 12, 70), (150, 22, 90), (270, 8, 80)):
        d.ellipse((cx*sx-r, grass+cy-r*.55, cx*sx+r, grass+cy+r*.55), fill='#6fae5c')
    clouds = ((60, 52), (205, 38), (290, 70)) + (((90, 130), (200, 165)) if portrait() else ())
    for cx, cy in clouds:
        for dx, r in ((-14, 10), (0, 14), (14, 10)):
            d.ellipse((cx*sx+dx-r, cy-r*.7, cx*sx+dx+r, cy+r*.7), fill='#f4f8ff')
    d.rectangle((0, grass, W, H), fill='#4f9a3e')
    d.rectangle((0, grass, W, grass+3), fill='#7cc95a')
    for x in range(0, W, 7):
        d.line((x, grass, x+2, grass-4), fill='#7cc95a')
    d.rectangle((0, H-26, W, H), fill='#7a5233')
    return image


class BunnySaver(Saver):
    """Interactive: tap a bunny to make it jump; - and + change how many there are."""
    style = 'bunnies'

    def __init__(self, options, set_count, frames=None, seed=None):
        """frames: a custom sprite used for every bunny; None = random breeds and coats."""
        import random
        self.options, self.set_count = options, set_count
        self.rng = random.Random(seed)
        self.custom = frames
        self.looks = {}  # (shape, coat, scale) -> (right-facing, left-facing) pose dicts.
        self.pellets = []  # [x, y, born]: droppings fade away after a while.
        self.background = bunny_background()
        self.bunnies = [self.new_bunny() for _ in range(options['bunny_count'])]
        self.last = None
        self.message, self.message_until = '', 0

    def poses_for(self, look):
        if look not in self.looks:
            if look is None:  # Custom sprite: frame 0 rests (eats, sleeps, grooms), the rest hop.
                frames = self.custom
                right = {'sit': frames[0], 'hop': frames[1:] or frames[:1], 'eat': frames[0],
                         'sleep': frames[0], 'groom': frames[0], 'groom2': frames[0]}
            else:
                right = bunny_poses(*look)
            self.looks[look] = (right, mirrored_poses(right))
        return self.looks[look]

    def frames_for(self, look):
        """Plain right-facing frames: the custom sprite as given, else [sit, hop]."""
        if look is None:
            return list(self.custom), None
        right = self.poses_for(look)[0]
        return [right['sit'], *right['hop']], None

    def new_bunny(self, entering=None):
        breed, look = (None, None) if self.custom else random_breed(self.rng)
        half = self.poses_for(look)[0]['sit'].width // 2
        return Bunny(self.rng, entering, breed, look, half)

    def sync_count(self):
        target = self.options['bunny_count']
        while len(self.bunnies) < target:
            self.bunnies.append(self.new_bunny(entering=self.rng.choice((-1, 1))))
        del self.bunnies[target:]

    def bunny_box(self, bunny, now):
        frame = self.poses_for(bunny.look)[0]['sit']
        top = bunny.ground - frame.height - bunny.lift(now)
        return (bunny.x-frame.width/2, top, bunny.x+frame.width/2, bunny.ground)

    def tap(self, x, y, now):
        if y >= H-26 and (x <= 54 or x >= W-54):
            values = OPTION_VALUES['bunny_count']
            index = values.index(self.options['bunny_count']) + (1 if x >= W-54 else -1)
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

    def draw_food(self, d, bunny, now):
        """A carrot or hay tuft in front of the nose, in the bunnies' chunky pixel scale,
        shrinking as it is eaten."""
        px = bunny.look[2] if bunny.look else 2  # Screen pixels per sprite pixel.
        span = max(.1, bunny.activity_until - bunny.activity_start)
        left = 1 - min(1, (now - bunny.activity_start) / span)
        facing = bunny.direction
        near = bunny.x + facing*(bunny.half + px)  # Just in front of the nose.
        base = bunny.ground - px

        def block(col, row, colour):  # One sprite pixel, `col` counted away from the bunny.
            x = near + facing*col*px
            x0, x1 = sorted((x, x + facing*px))
            d.rectangle((x0, base - (row+1)*px, x1 - 1, base - row*px - 1), fill=colour)

        if bunny.food == 'carrot':
            length = max(1, round(6*left))  # Eaten from the tip, which faces the mouth.
            for col in range(length):
                block(col, 0, '#e8781e')
                if col >= 1:
                    block(col, 1, '#f39a3c')
            for row, colour in ((1, '#3fa34d'), (2, '#5fc35a'), (0, '#3fa34d')):
                block(length, row, colour)  # Leafy top on the far end.
            block(length+1, 2, '#3fa34d')
        else:
            blades = max(1, round(5*left))
            for col in range(blades):
                height = 2 + (col % 2)
                for row in range(height):
                    block(col, row, '#e3cf74' if (row+col) % 2 else '#c9ad4f')

    def render(self, now, wall_time, alerts):
        dt = 0 if self.last is None else min(.25, now-self.last)
        self.last = now
        self.sync_count()
        speed = {'slow': .6, 'normal': 1., 'fast': 1.7}[self.options['saver_speed']] * 40
        image = self.background.copy()
        d = ImageDraw.Draw(image)
        self.pellets = [p for p in self.pellets if now - p[2] < 45][-40:]
        for x, y, born in self.pellets:  # Under the bunnies; fade into the grass.
            age = (now - born) / 45
            colour = tuple(round(a + (b-a)*age) for a, b in ((70, 79), (46, 154), (30, 62)))
            d.ellipse((x-2, y-2, x+1, y+1), fill=colour)
        for bunny in sorted(self.bunnies, key=lambda b: b.ground):
            if bunny.update(now, dt, speed) == 'poop':
                behind = bunny.x - bunny.direction*(bunny.half-3)
                for _ in range(self.rng.randint(1, 3)):
                    self.pellets.append([behind + self.rng.uniform(-3, 3),
                                         bunny.ground - self.rng.uniform(0, 2), now])
            right, left = self.poses_for(bunny.look)
            poses = right if bunny.direction > 0 else left
            binky = bunny.binky_phase(now)
            if binky is not None:
                # Twist mid-air: the body tilts up, flicks round (mirrored) at the top, then
                # tilts down for the landing.
                twisted = .35 <= binky < .65
                frame = ((left if bunny.direction > 0 else right) if twisted else poses)['hop'][0]
                frame = frame.rotate(22*math.cos(binky*math.pi)*bunny.direction, Image.NEAREST,
                                     expand=True)
            elif bunny.hop_start is not None:
                hops = poses['hop']
                frame = hops[int((now-bunny.hop_start)*10) % len(hops)]
            elif bunny.activity == 'eat':
                frame = poses['eat'] if int(now*4) % 2 else poses['sit']  # Nibbling.
            elif bunny.activity == 'sleep':
                frame = poses['sleep']
            elif bunny.activity == 'groom':  # Paw strokes over the face.
                frame = poses['groom2'] if int(now*5) % 2 else poses['groom']
            else:
                frame = poses['sit']
            lift = bunny.lift(now)
            shadow = max(4, frame.width//2 - lift/3)
            d.ellipse((bunny.x-shadow, bunny.ground-2, bunny.x+shadow, bunny.ground+2), fill='#3d7a30')
            image.paste(frame, (round(bunny.x-frame.width/2), round(bunny.ground-frame.height-lift)), frame)
            if bunny.activity == 'eat' and bunny.food:
                self.draw_food(d, bunny, now)
            elif bunny.activity == 'sleep':
                for k in range(3):  # Little z's drifting up from the head.
                    phase = (now*.5 + k/3) % 1
                    zx = bunny.x + bunny.direction*(bunny.half*.4 + phase*8)
                    zy = bunny.ground - frame.height - 4 - phase*16
                    d.text((zx, zy), 'z', font=TINY if phase < .5 else SMALL, fill='#eef6ff', anchor='mm')
            elif binky is not None and .15 < binky < .85:
                top = bunny.ground - frame.height - lift
                for k in range(4):  # Sparkles of joy around the leap.
                    angle = k*math.pi/2 + now*5
                    sx = bunny.x + math.cos(angle)*(frame.width*.6 + 3)
                    sy = top + frame.height/2 + math.sin(angle)*(frame.height*.6 + 3)
                    d.line((sx-2, sy, sx+2, sy), fill='#fff6a8')
                    d.line((sx, sy-2, sx, sy+2), fill='#fff6a8')
            if now < bunny.heart_until:
                hx, hy = bunny.x, bunny.ground-frame.height-lift-8-(1.2-(bunny.heart_until-now))*12
                d.text((hx, hy), '♥', font=FONT, fill='#ff5a7a', anchor='mm')
        saver_header(d, 'BUNNIES', '#e8f4ff', wall_time, alerts)
        for x0, label in ((4, '\u2212'), (W-50, '+')):
            d.rounded_rectangle((x0, H-23, x0+46, H-3), radius=5, fill='#5a3a22')
            d.text((x0+23, H-14), label, font=FONT, fill=TEXT, anchor='mm')
        count = self.options['bunny_count']
        text = self.message if now < self.message_until else (
            f"{count} bunn{'y' if count == 1 else 'ies'} | tap one!")
        d.text((W//2, H-13), fit_text(d, text, W-120, TINY), font=TINY, fill='#f2e6d8', anchor='mm')
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
        big = BIG_PORTRAIT if portrait() else BIG
        width = d.textlength(main, font=big)
        sway = max(0, min(22, (W-width-56)/2))  # Keep the seconds/AM-PM on screen.
        cx, cy = W/2 + sway*math.sin(minute*.7), H/2 - 8 + 16*math.cos(minute*1.3)
        d.text((cx, cy), main, font=big, fill=ACCENT, anchor='mm')
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

    @staticmethod
    def top_rects():
        return row_rects(3, 26, 52)

    @staticmethod
    def bottom_rects():
        return row_rects(4, H-32, H-4)

    def draw_overlay(self, image, now):
        d = ImageDraw.Draw(image)
        d.rectangle((0, 0, W-1, 21), fill=BG)
        d.text((6, 4), fit_text(d, self.content.label(), W-12, TINY), font=TINY, fill=TEXT)
        for rect, label in zip(self.top_rects(), self.top_labels()):
            button(d, rect, label)
        for rect, label in zip(self.bottom_rects(), self.bottom_labels()):
            button(d, rect, label)

    def tap(self, x, y, now):
        if now >= self.overlay_until:
            self.overlay_until = now + 5
            return None
        self.overlay_until = now + 5
        bottom = index_at(self.bottom_rects(), x, y, slop=4)
        top = index_at(self.top_rects(), x, y, slop=4) if self.top_labels() else None
        if bottom == 3:
            return 'exit'
        if bottom == 1:
            self.content.paused = not self.content.paused
        elif bottom is not None:
            self.content.move(-1 if bottom == 0 else 1, now)
        elif top is not None:
            self.top_action(top)
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


SNAKE_CELL = 12


def snake_geometry():
    """(cols, rows, origin) of the board: 26x14 in landscape, 19x20 in portrait."""
    cols, rows = (W-8)//SNAKE_CELL, (H-72)//SNAKE_CELL
    return cols, rows, ((W-cols*SNAKE_CELL)//2, 26)


class SnakeGame:
    def __init__(self, cols=None, rows=None, seed=None):
        import random
        default_cols, default_rows, _ = snake_geometry()
        self.cols, self.rows = cols or default_cols, rows or default_rows
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
    origin = snake_geometry()[2]
    x0 = origin[0] + cell[0]*SNAKE_CELL
    y0 = origin[1] + cell[1]*SNAKE_CELL
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

    def fit_board(self):
        """After a Screen rotation the board shape changes: start a fresh game on it."""
        cols, rows, _ = snake_geometry()
        if (self.game.cols, self.game.rows) != (cols, rows):
            self.game.cols, self.game.rows = cols, rows
            self.game.reset()
            if self.state in ('running', 'paused'):
                self.state = 'ready'

    @staticmethod
    def dialog_rects():
        cx, top = W//2, (H-240)//2
        half = min(130, W//2-10)
        box = (cx-half, 56+top, cx+half, 204+top)
        return box, (box[0]+10, 150+top, cx-5, 196+top), (cx+5, 150+top, box[2]-10, 196+top)

    def tap(self, x, y, now):
        self.fit_board()
        if self.state == 'running':
            if y < 24 and x >= W-80:
                self.state = 'paused'
                return None
            hx, hy = self.game.body[0]
            origin = snake_geometry()[2]
            cx = origin[0] + (hx+.5)*SNAKE_CELL
            cy = origin[1] + (hy+.5)*SNAKE_CELL
            dx, dy = x-cx, y-cy
            axes = [(1 if dx > 0 else -1, 0), (0, 1 if dy > 0 else -1)]
            if abs(dy) > abs(dx):
                axes.reverse()
            for direction, size in zip(axes, sorted((abs(dx), abs(dy)), reverse=True)):
                if size >= SNAKE_CELL/2 and self.game.turn(direction):
                    break
            return None
        # Ready / paused / game over: two big buttons.
        choice = index_at(self.dialog_rects()[1:], x, y, slop=4)
        if choice is not None:
            if choice == 0:
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
        self.fit_board()
        self.update(now)
        image = Image.new('RGB', (W, H), SCREEN)
        d = ImageDraw.Draw(image)
        score = f'SNAKE  {self.game.score}'
        d.text((8, 4), score, font=FONT, fill=ACCENT)
        d.text((max(150 if not portrait() else 0, 20+d.textlength(score, font=FONT)), 7),
               f'BEST {self.best}', font=SMALL, fill=MUTED)
        if self.state == 'running':
            button(d, (W-76, 2, W-4, 22), 'PAUSE')
        else:
            d.text((W-8, 7), wall_time.strftime('%H:%M'), font=SMALL, fill=TEXT, anchor='ra')
        cols, rows, (x0, y0) = snake_geometry()
        d.rectangle((x0-1, y0-1, x0+cols*SNAKE_CELL, y0+rows*SNAKE_CELL), outline=PANEL)
        if self.game.food:
            d.ellipse(cell_rect(self.game.food, 2), fill='#ff6b5e')
        for i, cell in enumerate(self.game.body):
            d.rectangle(cell_rect(cell), fill=ACCENT if i == 0 else '#2f9e80')
        centred(d, H-26, 'Tap where the snake should go. Avoid walls and your tail.', TINY, MUTED)
        centred(d, H-12, 'PAUSE (top right) to stop.', TINY, MUTED)
        if self.state != 'running':
            title, left, right = {
                'ready': ('SNAKE', 'START', 'QUIT'),
                'paused': ('PAUSED', 'RESUME', 'QUIT'),
                'over': ('YOU WIN!' if self.game.won else 'GAME OVER', 'PLAY AGAIN', 'QUIT')}[self.state]
            box, left_rect, right_rect = self.dialog_rects()
            d.rounded_rectangle(box, radius=8, fill=BG, outline=ACCENT, width=2)
            d.text((W//2, box[1]+28), title, font=BOLD, fill=TEXT, anchor='mm')
            d.text((W//2, box[1]+59), f'Score {self.game.score}   Best {self.best}', font=SMALL,
                   fill=MUTED, anchor='mm')
            if self.state == 'ready':
                d.text((W//2, box[1]+77), 'Tap ahead of the head to turn.', font=TINY, fill=MUTED,
                       anchor='mm')
            button(d, left_rect, left, active=True)
            button(d, right_rect, right)
        self.delay = max(.01, self.next_step-now) if self.state == 'running' else .25
        return image


def draw_game_dialog(d, title, subtitle, left, right, hint=''):
    """Start / pause / game-over box shared by the games (same geometry as Snake's)."""
    box, left_rect, right_rect = SnakeApp.dialog_rects()
    d.rounded_rectangle(box, radius=8, fill=BG, outline=ACCENT, width=2)
    d.text((W//2, box[1]+28), title, font=BOLD, fill=TEXT, anchor='mm')
    d.text((W//2, box[1]+59), subtitle, font=SMALL, fill=MUTED, anchor='mm')
    if hint:
        d.text((W//2, box[1]+77), fit_text(d, hint, box[2]-box[0]-12, TINY), font=TINY,
               fill=MUTED, anchor='mm')
    button(d, left_rect, left, active=True)
    button(d, right_rect, right)


def dialog_choice(x, y):
    """0 = left button (start/resume/again), 1 = right (quit), None = elsewhere."""
    return index_at(SnakeApp.dialog_rects()[1:], x, y, slop=4)


def game_header(d, title, wall_time, running):
    d.text((8, 4), title, font=FONT, fill=ACCENT)
    if running:
        button(d, (W-76, 2, W-4, 22), 'PAUSE')
    else:
        d.text((W-8, 7), wall_time.strftime('%H:%M'), font=SMALL, fill=TEXT, anchor='ra')


def pause_tapped(x, y):
    return y < 24 and x >= W-80


def tetris_layout():
    """Board placement, side panel and control buttons for the current rotation."""
    if portrait():
        cell, origin = 11, (8, 28)
        panel = (8 + 10*cell + 8, 28, W-6, 28 + 20*cell)
        rows = ((H-66, H-37), (H-33, H-4))
        labels = (('< LEFT', 'left'), ('ROTATE', 'rotate'), ('RIGHT >', 'right'),
                  ('DOWN', 'down'), ('DROP', 'drop'), ('PAUSE', 'pause'))
        rects = row_rects(3, *rows[0]) + row_rects(3, *rows[1])
    else:
        cell, origin = 10, (8, 30)
        panel = (8 + 10*cell + 10, 26, W-6, 104)
        left, right = panel[0], W-6
        middle = (left+right)//2
        rects = []
        for top in (108, 152, 196):
            rects += [(left, top, middle-3, top+38), (middle+3, top, right, top+38)]
        labels = (('< LEFT', 'left'), ('RIGHT >', 'right'), ('ROTATE', 'rotate'),
                  ('DOWN', 'down'), ('DROP', 'drop'), ('PAUSE', 'pause'))
    board = (origin[0], origin[1], origin[0]+10*cell, origin[1]+20*cell)
    return cell, origin, board, panel, [(rect, action, label) for rect, (label, action)
                                        in zip(rects, labels)]


class TetrisApp:
    """Buttons for move/rotate/drop; tapping the board also rotates. Holding LEFT, RIGHT or
    DOWN repeats, so a piece can be slid across without many taps."""
    REPEATABLE = ('left', 'right', 'down')

    def __init__(self, best=0, save_best=lambda best: None, seed=None):
        self.game = TetrisGame(seed=seed)
        self.best, self.save_best = best, save_best
        self.state, self.next_fall, self.delay = 'ready', 0., .25
        self.held = None
        self.repeated = False
        self.next_repeat = 0.

    @property
    def running(self):
        return self.state == 'running'

    def act(self, action, now):
        game = self.game
        if action == 'left':
            game.move(-1)
        elif action == 'right':
            game.move(1)
        elif action == 'rotate':
            game.rotate()
        elif action == 'down':
            if game.step(soft=True):
                self.next_fall = now + game.gravity()
        elif action == 'drop':
            game.hard_drop()
            self.next_fall = now + game.gravity()
        elif action == 'pause':
            self.state = 'paused'
        self.check_over()

    def check_over(self):
        if not self.game.alive and self.state == 'running':
            self.state = 'over'
            if self.game.score > self.best:
                self.best = self.game.score
                self.save_best(self.best)

    def action_at(self, x, y):
        _, _, board, _, buttons = tetris_layout()
        for rect, action, _ in buttons:
            if index_at([rect], x, y, slop=3) is not None:
                return action
        if index_at([board], x, y) is not None:
            return 'rotate'
        return None

    def tap(self, x, y, now):
        if self.state == 'running':
            if self.repeated:  # The release after an auto-repeat is not another move.
                self.repeated = False
                return None
            action = 'pause' if pause_tapped(x, y) else self.action_at(x, y)
            if action:
                self.act(action, now)
            return None
        choice = dialog_choice(x, y)
        if choice == 0:
            if self.state != 'paused':
                self.game.reset()
            self.state, self.next_fall = 'running', now + self.game.gravity()
        elif choice == 1:
            if self.state == 'paused':
                self.state = 'ready'
            return 'exit'
        return None

    def hold(self, point, now):
        """Called every loop pass with the finger position (None when lifted)."""
        action = self.action_at(*point) if point is not None and self.running else None
        if action not in self.REPEATABLE:
            self.held = None
            return
        if action != self.held:
            self.held, self.repeated, self.next_repeat = action, False, now + .28
        elif now >= self.next_repeat:
            self.act(action, now)
            self.repeated, self.next_repeat = True, now + .07

    def update(self, now):
        if self.state != 'running':
            return
        if now - self.next_fall > 1:
            self.next_fall = now  # Resumed after a long gap: no burst of catch-up moves.
        while self.state == 'running' and now >= self.next_fall:
            self.game.step()
            self.next_fall += self.game.gravity()
            self.check_over()

    def render(self, now, wall_time, alerts):
        self.update(now)
        game = self.game
        cell, (x0, y0), board, panel, buttons = tetris_layout()
        image = Image.new('RGB', (W, H), SCREEN)
        d = ImageDraw.Draw(image)
        game_header(d, 'TETRIS', wall_time, False)
        d.rectangle((board[0]-1, board[1]-1, board[2], board[3]), outline=PANEL, fill=DEEP)

        def block(cx, cy, colour, outline_only=False, ox=x0, oy=y0, size=cell):
            rect = (ox+cx*size, oy+cy*size, ox+cx*size+size-2, oy+cy*size+size-2)
            if outline_only:
                d.rectangle(rect, outline=colour)
            else:
                d.rectangle(rect, fill=colour)

        for row, cells in enumerate(game.board):
            for col, kind in enumerate(cells):
                if kind:
                    block(col, row, PIECE_COLOURS[kind])
        if game.alive and self.state != 'ready':
            ghost = game.ghost_y()
            for cx, cy in game.cells(y=ghost):
                if cy >= 0:
                    block(cx, cy, '#3a4a58', outline_only=True)
            for cx, cy in game.cells():
                if cy >= 0:
                    block(cx, cy, PIECE_COLOURS[game.kind])
        # Side panel: next piece and numbers.
        px, py = panel[0], panel[1]
        d.text((px, py), 'NEXT', font=TINY, fill=MUTED)
        preview = 9 if portrait() else 10
        for cx, cy in PIECES[game.next_kind][0]:
            block(cx, cy, PIECE_COLOURS[game.next_kind], ox=px+2, oy=py+14, size=preview)
        stats = (('SCORE', game.score), ('LINES', game.lines), ('LEVEL', game.level),
                 ('BEST', self.best))
        if portrait():
            for i, (label, value) in enumerate(stats):
                y = py + 58 + i*38
                d.text((px, y), label, font=TINY, fill=MUTED)
                d.text((px, y+12), str(value), font=FONT, fill=TEXT)
        else:
            sx = px + 56
            for i, (label, value) in enumerate(stats):
                d.text((sx, py+2+i*19), label, font=TINY, fill=MUTED)
                d.text((W-8, py+i*19), str(value), font=SMALL, fill=TEXT, anchor='ra')
        for rect, action, label in buttons:
            button(d, rect, label, active=(action == 'drop'))
        if self.state != 'running':
            title, left, right = {
                'ready': ('TETRIS', 'START', 'QUIT'),
                'paused': ('PAUSED', 'RESUME', 'QUIT'),
                'over': ('GAME OVER', 'PLAY AGAIN', 'QUIT')}[self.state]
            draw_game_dialog(d, title, f'Score {game.score}   Best {self.best}', left, right,
                             'Buttons move; tap the board to rotate.' if self.state == 'ready' else '')
            self.delay = .25
        else:
            self.delay = max(.01, min(self.next_fall-now, .07 if self.held else .25))
        return image


def pong_court():
    """(rect, width, length): `width` runs across the court, `length` between paddles.
    Landscape: your paddle on the left. Portrait: your paddle at the bottom."""
    top = 26
    rect = (0, top, W-1, H-1)
    if portrait():
        return rect, W, H-top
    return rect, H-top, W


def pong_to_screen(across, along):
    (x0, y0, x1, y1), _, _ = pong_court()
    if portrait():
        return x0+across, y1-along
    return x0+along, y0+across


class PongApp:
    """Drag a finger anywhere on the court to move your paddle. First to 7 wins."""
    def __init__(self, wins=0, save_wins=lambda wins: None, seed=None):
        _, width, length = pong_court()
        self.game = PongGame(width, length, seed=seed)
        self.wins, self.save_wins = wins, save_wins
        self.state, self.delay, self.last = 'ready', .25, None
        self.flash, self.flash_until = '', 0.

    @property
    def running(self):
        return self.state == 'running'

    def tap(self, x, y, now):
        if self.state == 'running':
            if pause_tapped(x, y):
                self.state = 'paused'
            return None
        choice = dialog_choice(x, y)
        if choice == 0:
            if self.state != 'paused':
                _, width, length = pong_court()
                self.game = PongGame(width, length)
            self.state, self.last = 'running', now
        elif choice == 1:
            if self.state == 'paused':
                self.state = 'ready'
            return 'exit'
        return None

    def hold(self, point, now):
        if point is None or not self.running or point[1] < 26:
            return
        (x0, y0, _, _), _, _ = pong_court()
        self.game.aim(point[0]-x0 if portrait() else point[1]-y0)

    def update(self, now):
        _, width, length = pong_court()
        self.game.resize(width, length)  # Rotation changed: re-serve on the new court.
        if self.state != 'running':
            self.last = None
            return
        dt = 0 if self.last is None else now - self.last
        self.last = now
        point = self.game.update(dt)
        if point:
            self.flash, self.flash_until = ('POINT!' if point == 'player' else 'CPU SCORES'), now + 1
        if self.game.over:
            self.state = 'over'
            if self.game.player_score > self.game.cpu_score:
                self.wins += 1
                self.save_wins(self.wins)

    def render(self, now, wall_time, alerts):
        self.update(now)
        game = self.game
        rect, width, length = pong_court()
        image = Image.new('RGB', (W, H), 'black')
        d = ImageDraw.Draw(image)
        game_header(d, f'PONG  {game.player_score} : {game.cpu_score}', wall_time, self.running)
        d.line((0, rect[1]-1, W-1, rect[1]-1), fill=PANEL)
        for i in range(0, int(width), 12):  # Dashed centre line across the court.
            a = pong_to_screen(i, length/2)
            b = pong_to_screen(min(width, i+6), length/2)
            d.line((a, b), fill=GRID, width=2)
        for score, along in ((game.player_score, length*.25), (game.cpu_score, length*.75)):
            d.text(pong_to_screen(width/2, along), str(score), font=BIG_PORTRAIT, fill='#16242e',
                   anchor='mm')
        for centre, along, colour in ((game.player, 0, ACCENT), (game.cpu, length-game.THICK, TEXT)):
            a = pong_to_screen(centre-game.PADDLE/2, along)
            b = pong_to_screen(centre+game.PADDLE/2, along+game.THICK)
            d.rectangle((min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1])),
                        fill=colour)
        bx, by = pong_to_screen(*game.ball)
        r = game.BALL/2
        d.rectangle((bx-r, by-r, bx+r, by+r), fill='white')
        if now < self.flash_until and self.running:
            centred(d, (rect[1]+rect[3])//2, self.flash, FONT, WARN)
        if self.state != 'running':
            won = game.player_score > game.cpu_score
            title, left, right = {
                'ready': ('PONG', 'START', 'QUIT'),
                'paused': ('PAUSED', 'RESUME', 'QUIT'),
                'over': ('YOU WIN!' if won else 'CPU WINS', 'PLAY AGAIN', 'QUIT')}[self.state]
            where = 'bottom' if portrait() else 'left'
            draw_game_dialog(d, title, f'You {game.player_score} : {game.cpu_score} CPU   Wins {self.wins}',
                             left, right,
                             f'Drag: your paddle is {where}. First to {game.TARGET}.'
                             if self.state == 'ready' else '')
        self.delay = 1/30 if self.running else .25
        return image


def court_rect():
    """Play area under the 26 px header, used by Brick breaker and Flappy."""
    return (0, 26, W-1, H-1), W, H-26


class BrickApp:
    """Brick breaker: drag to move the paddle, tap to launch. 3 lives, levels speed up."""
    COLOURS = (RED, ORANGE, YELLOW, GREEN, CYAN, INDIGO)

    def __init__(self, best=0, save_best=lambda best: None, seed=None):
        _, width, height = court_rect()
        self.game = BrickGame(width, height, seed=seed)
        self.best, self.save_best = best, save_best
        self.state, self.delay, self.last = 'ready', .25, None
        self.flash, self.flash_until = '', 0.

    @property
    def running(self):
        return self.state == 'running'

    def tap(self, x, y, now):
        if self.state == 'running':
            if pause_tapped(x, y):
                self.state = 'paused'
            elif y >= 26:
                self.game.launch()
            return None
        choice = dialog_choice(x, y)
        if choice == 0:
            if self.state != 'paused':
                _, width, height = court_rect()
                self.game = BrickGame(width, height)
            self.state, self.last = 'running', now
        elif choice == 1:
            if self.state == 'paused':
                self.state = 'ready'
            return 'exit'
        return None

    def hold(self, point, now):
        if point is not None and self.running and point[1] >= 26:
            self.game.aim(point[0])

    def update(self, now):
        _, width, height = court_rect()
        self.game.resize(width, height)
        if self.state != 'running':
            self.last = None
            return
        dt = 0 if self.last is None else now - self.last
        self.last = now
        event = self.game.update(dt)
        if event == 'cleared':
            self.flash, self.flash_until = f'LEVEL {self.game.level}', now + 1.5
        elif event == 'lost' and not self.game.over:
            self.flash, self.flash_until = f'{self.game.lives} LEFT - TAP TO LAUNCH', now + 1.5
        if self.game.over:
            self.state = 'over'
            if self.game.score > self.best:
                self.best = self.game.score
                self.save_best(self.best)

    def render(self, now, wall_time, alerts):
        self.update(now)
        game = self.game
        (cx0, cy0, _, _), _, _ = court_rect()
        image = Image.new('RGB', (W, H), DEEP)
        d = ImageDraw.Draw(image)
        game_header(d, f'BRICKS  {game.score}', wall_time, self.running)
        d.text((W-84 if self.running else W-56, 7), '\u2665'*game.lives, font=SMALL,
               fill='#ff5a7a', anchor='ra')
        for row, col in game.bricks:
            x0, y0, x1, y1 = game.brick_rect(row, col)
            d.rectangle((cx0+x0, cy0+y0, cx0+x1, cy0+y1), fill=self.COLOURS[row])
        py = cy0 + game.paddle_y()
        d.rectangle((game.paddle-game.PADDLE/2, py, game.paddle+game.PADDLE/2, py+game.THICK),
                    fill=ACCENT)
        bx, by, r = game.ball[0], cy0+game.ball[1], game.BALL/2
        d.rectangle((bx-r, by-r, bx+r, by+r), fill='white')
        if self.running and (now < self.flash_until or game.stuck):
            centred(d, cy0 + game.height*.6, self.flash if now < self.flash_until
                    else 'TAP TO LAUNCH', SMALL, WARN)
        if self.state != 'running':
            title, left, right = {
                'ready': ('BRICK BREAKER', 'START', 'QUIT'),
                'paused': ('PAUSED', 'RESUME', 'QUIT'),
                'over': ('GAME OVER', 'PLAY AGAIN', 'QUIT')}[self.state]
            draw_game_dialog(d, title, f'Score {game.score}   Best {self.best}', left, right,
                             'Drag to move the paddle, tap to launch.' if self.state == 'ready' else '')
        self.delay = 1/30 if self.running and not game.stuck else .1 if self.running else .25
        return image


class FlappyApp:
    """Flappy bird: touch the screen to flap (on press, not on release)."""
    def __init__(self, best=0, save_best=lambda best: None, seed=None):
        _, width, height = court_rect()
        self.game = FlappyGame(width, height, seed=seed)
        self.best, self.save_best = best, save_best
        self.state, self.delay, self.last = 'ready', .25, None
        self.down = False          # Finger currently on the court.
        self.pressed = False       # A press flapped since the last release tap.

    @property
    def running(self):
        return self.state == 'running'

    def tap(self, x, y, now):
        if self.state == 'running':
            if pause_tapped(x, y):
                self.state = 'paused'
            elif not self.pressed and y >= 26:
                self.game.flap()  # No press seen (no live touch position): flap on release.
            self.pressed = False
            return None
        choice = dialog_choice(x, y)
        if choice == 0:
            if self.state != 'paused':
                self.game.reset()
            self.state, self.last = 'running', now
            self.game.flap()
            self.pressed = False
        elif choice == 1:
            if self.state == 'paused':
                self.state = 'ready'
            return 'exit'
        return None

    def hold(self, point, now):
        down = point is not None and point[1] >= 26 and not pause_tapped(*point)
        if down and not self.down and self.running:
            self.game.flap()
            self.pressed = True
        self.down = down

    def update(self, now):
        _, width, height = court_rect()
        self.game.resize(width, height)
        if self.state != 'running':
            self.last = None
            return
        dt = 0 if self.last is None else now - self.last
        self.last = now
        if self.game.update(dt) == 'crash':
            self.state = 'over'
            if self.game.score > self.best:
                self.best = self.game.score
                self.save_best(self.best)

    def render(self, now, wall_time, alerts):
        self.update(now)
        game = self.game
        (_, cy0, _, cy1), _, _ = court_rect()
        image = Image.new('RGB', (W, H), SCREEN)
        d = ImageDraw.Draw(image)
        for y in range(cy0, cy1+1, 4):  # Dusk sky in bands (cheap, and tiny over SPI).
            t = (y-cy0)/max(1, cy1-cy0)
            d.rectangle((0, y, W, y+3), fill=(round(40+50*t), round(70+60*t), round(120+40*t)))
        for x, centre, _ in game.pipes:
            gap_top, gap_bottom = cy0+centre-game.gap/2, cy0+centre+game.gap/2
            left, right = x-game.PIPE/2, x+game.PIPE/2
            for y0, y1 in ((cy0, gap_top), (gap_bottom, cy1)):
                d.rectangle((left, y0, right, y1), fill='#4fae4a', outline='#2c6a2a')
            d.rectangle((left-3, gap_top-8, right+3, gap_top), fill='#5fc35a', outline='#2c6a2a')
            d.rectangle((left-3, gap_bottom, right+3, gap_bottom+8), fill='#5fc35a', outline='#2c6a2a')
        bx, by, r = game.x, cy0+game.y, game.RADIUS
        tilt = max(-3, min(3, game.vy/80))
        d.ellipse((bx-r-1, by-r, bx+r+1, by+r), fill=YELLOW, outline='#7a5a10')
        d.ellipse((bx+2, by-4+tilt/2, bx+5, by-1+tilt/2), fill='white')
        d.polygon([(bx+r, by), (bx+r+5, by+1+tilt), (bx+r, by+3)], fill=ORANGE)
        d.rectangle((0, 0, W, 25), fill=SCREEN)
        game_header(d, f'FLAPPY  {game.score}', wall_time, self.running)
        if self.state != 'running':
            title, left, right = {
                'ready': ('FLAPPY', 'START', 'QUIT'),
                'paused': ('PAUSED', 'RESUME', 'QUIT'),
                'over': ('CRASHED!', 'FLY AGAIN', 'QUIT')}[self.state]
            draw_game_dialog(d, title, f'Score {game.score}   Best {self.best}', left, right,
                             'Touch anywhere to flap.' if self.state == 'ready' else '')
        self.delay = 1/30 if self.running else .25
        return image


class GameBase:
    """Start / pause / quit handling shared by the newer games."""
    TITLE, HINT = 'GAME', ''
    LOWER_IS_BETTER = False  # True for best times.

    def __init__(self, best=0, save_best=lambda best: None):
        self.best, self.save_best = best, save_best
        self.state, self.delay, self.last = 'ready', .25, None
        self.elapsed = 0.

    @property
    def running(self):
        return self.state == 'running'

    def new_game(self, now):
        raise NotImplementedError

    def tick(self, now):
        """Advance the play clock; returns seconds since the last call while running."""
        if self.state != 'running':
            self.last = None
            return 0.
        dt = 0. if self.last is None else now - self.last
        self.last = now
        self.elapsed += dt
        return dt

    def finish(self, result, won=True):
        """End the game and keep the record if it is better (0 = no record yet)."""
        self.state, self.won = 'over', won
        if won and result is not None and (
                not self.best or (result < self.best if self.LOWER_IS_BETTER else result > self.best)):
            self.best = result
            self.save_best(result)

    def tap_dialog(self, x, y, now):
        choice = dialog_choice(x, y)
        if choice == 0:
            if self.state != 'paused':
                self.elapsed, self.won = 0., False
                self.new_game(now)
            self.state, self.last = 'running', now
        elif choice == 1:
            if self.state == 'paused':
                self.state = 'ready'
            return 'exit'
        return None

    def draw_dialog(self, d, summary, over_title):
        title, left = {'ready': (self.TITLE, 'START'), 'paused': ('PAUSED', 'RESUME'),
                       'over': (over_title, 'PLAY AGAIN')}[self.state]
        draw_game_dialog(d, title, summary, left, 'QUIT', self.HINT if self.state == 'ready' else '')


def clock_text(seconds):
    seconds = int(seconds)
    return f'{seconds//60}:{seconds % 60:02}'


ALIEN_SPRITES = (
    (["...XX...", "..XXXX..", ".XX..XX.", "XXXXXXXX", ".X.XX.X."],
     ["...XX...", "..XXXX..", ".XX..XX.", "XXXXXXXX", "X.X..X.X"], RED),
    (["X......X", ".XXXXXX.", "XX.XX.XX", "XXXXXXXX", ".X....X."],
     [".X....X.", ".XXXXXX.", "XX.XX.XX", "XXXXXXXX", "X......X"], YELLOW),
    (["..XXXX..", "XXXXXXXX", "XX.XX.XX", "XXXXXXXX", ".XX..XX."],
     ["..XXXX..", "XXXXXXXX", "XX.XX.XX", "XXXXXXXX", "XX....XX"], GREEN),
)
_ALIEN_CACHE = {}


def alien_sprite(row, frame):
    kind = 0 if row == 0 else 1 if row < 3 else 2
    key = (kind, frame)
    if key not in _ALIEN_CACHE:
        rows, colour = ALIEN_SPRITES[kind][frame], ALIEN_SPRITES[kind][2]
        image = Image.new('RGBA', (8, 5), (0, 0, 0, 0))
        for y, line in enumerate(rows):
            for x, c in enumerate(line):
                if c == 'X':
                    image.putpixel((x, y), Image.new('RGB', (1, 1), colour).getpixel((0, 0)) + (255,))
        _ALIEN_CACHE[key] = image.resize((16, 10), Image.NEAREST)
    return _ALIEN_CACHE[key]


class InvadersApp(GameBase):
    """Drag to move; keep a finger down to keep firing (one shot in the air at a time)."""
    TITLE, HINT = 'SPACE INVADERS', 'Drag to move. Hold a finger down to fire.'

    def __init__(self, best=0, save_best=lambda best: None, seed=None):
        super().__init__(best, save_best)
        _, width, height = court_rect()
        self.game = InvadersGame(width, height, seed=seed)
        self.flash, self.flash_until = '', 0.

    def new_game(self, now):
        _, width, height = court_rect()
        self.game = InvadersGame(width, height)

    def tap(self, x, y, now):
        if self.running:
            if pause_tapped(x, y):
                self.state = 'paused'
            elif y >= 26:
                self.game.fire()
            return None
        return self.tap_dialog(x, y, now)

    def hold(self, point, now):
        if point is not None and self.running and point[1] >= 26:
            self.game.aim(point[0])
            self.game.fire()

    def render(self, now, wall_time, alerts):
        _, width, height = court_rect()
        self.game.resize(width, height)
        dt = self.tick(now)
        game = self.game
        if self.running:
            event = game.update(dt)
            if event == 'wave':
                self.flash, self.flash_until = f'WAVE {game.wave}', now + 1.5
            elif event == 'hit' and not game.over:
                self.flash, self.flash_until = 'HIT!', now + 1
            if game.over:
                self.finish(game.score)
        top = 26
        image = Image.new('RGB', (W, H), 'black')
        d = ImageDraw.Draw(image)
        game_header(d, f'INVADERS  {game.score}', wall_time, self.running)
        d.text((W-84 if self.running else W-56, 7), '▲'*max(0, game.lives), font=SMALL,
               fill=ACCENT, anchor='ra')
        for row, col in game.aliens:
            x0, y0, _, _ = game.alien_rect(row, col)
            sprite = alien_sprite(row, game.frame)
            image.paste(sprite, (round(x0), round(top+y0)), sprite)
        for bx, by in game.shields:
            d.rectangle((bx, top+by, bx+3, top+by+3), fill='#3fa34d')
        px, py = game.player, top+game.player_y
        d.rectangle((px-9, py, px+9, py+6), fill=ACCENT)
        d.rectangle((px-2, py-4, px+2, py), fill=ACCENT)
        if game.bullet:
            d.line((game.bullet[0], top+game.bullet[1], game.bullet[0], top+game.bullet[1]+5),
                   fill='white', width=2)
        for bx, by in game.bombs:
            d.line((bx-1, top+by, bx+1, top+by+3, bx-1, top+by+6), fill='#ff8a6a', width=2)
        d.line((0, H-3, W, H-3), fill='#3fa34d')
        if self.running and now < self.flash_until:
            centred(d, top+height*.55, self.flash, FONT, WARN)
        if not self.running:
            self.draw_dialog(d, f'Score {game.score}   Best {self.best}', 'GAME OVER')
        self.delay = 1/30 if self.running else .25
        return image


def mines_layout():
    """(cell, cols, rows, mines, origin, toolbar rects) sized for a fingertip on each rotation."""
    if portrait():
        cell, cols, rows, mines = 24, 9, 10, 14
    else:
        cell, cols, rows, mines = 22, 13, 8, 16
    origin = ((W - cols*cell)//2, 28)
    bottom = origin[1] + rows*cell
    toolbar = row_rects(2, bottom+6, min(H-3, bottom+36), margin=8, gap=8)
    return cell, cols, rows, mines, origin, toolbar


MINE_COLOURS = {1: BLUE, 2: GREEN, 3: RED, 4: PURPLE, 5: ORANGE,
                6: CYAN, 7: TEXT, 8: MUTED}


class MinesApp(GameBase):
    """Tap to dig. Press and hold (or switch to FLAG mode) to flag. Tap a number whose
    mines are all flagged to open its other neighbours. Best time is kept."""
    TITLE, HINT = 'MINESWEEPER', 'Tap to dig, hold to flag. First tap is always safe.'
    LOWER_IS_BETTER = True
    HOLD_FLAG = .45

    def __init__(self, best=0, save_best=lambda best: None, seed=None):
        super().__init__(best, save_best)
        self.seed = seed
        self.flag_mode = False
        self.press = None  # (cell, started, flagged_already)
        self.new_game(0)

    def new_game(self, now):
        _, cols, rows, mines, _, _ = mines_layout()
        self.game = MinesGame(cols, rows, mines, seed=self.seed)
        self.press = None

    def cell_at(self, x, y):
        cell, cols, rows, _, (ox, oy), _ = mines_layout()
        cx, cy = int((x-ox)//cell), int((y-oy)//cell)
        return (cx, cy) if 0 <= cx < cols and 0 <= cy < rows and x >= ox and y >= oy else None

    def after(self, result, now):
        if result == 'boom':
            self.finish(None, won=False)
        elif result == 'win':
            self.finish(max(1, round(self.elapsed)))

    def tap(self, x, y, now):
        if not self.running:
            return self.tap_dialog(x, y, now)
        if pause_tapped(x, y):
            self.state = 'paused'
            return None
        if self.press and self.press[2]:  # This release ends a long-press flag.
            self.press = None
            return None
        self.press = None
        toolbar = mines_layout()[5]
        choice = index_at(toolbar, x, y, slop=3)
        if choice == 0:
            self.flag_mode = not self.flag_mode
        elif choice == 1:
            self.new_game(now)
            self.elapsed = 0.
        else:
            cell = self.cell_at(x, y)
            if cell is not None:
                if self.flag_mode and cell not in self.game.revealed:
                    self.game.toggle_flag(cell)
                elif cell in self.game.revealed:
                    self.after(self.game.chord(cell), now)
                else:
                    self.after(self.game.reveal(cell), now)
        return None

    def hold(self, point, now):
        if point is None or not self.running:
            if point is None and self.press and not self.press[2]:
                self.press = None
            return
        cell = self.cell_at(*point)
        if cell is None:
            return
        if self.press is None or self.press[0] != cell:
            self.press = (cell, now, False)
        elif not self.press[2] and now - self.press[1] >= self.HOLD_FLAG:
            if cell not in self.game.revealed:
                self.game.toggle_flag(cell)
            self.press = (cell, self.press[1], True)

    def render(self, now, wall_time, alerts):
        if (self.game.cols, self.game.rows) != mines_layout()[1:3] and self.state != 'over':
            self.new_game(now)  # Rotation changed the board shape.
            if self.running:
                self.state = 'ready'
        self.tick(now)
        game = self.game
        cell, cols, rows, _, (ox, oy), toolbar = mines_layout()
        image = Image.new('RGB', (W, H), BG)
        d = ImageDraw.Draw(image)
        left = len(game.mines or range(game.mine_count)) - len(game.flags)
        game_header(d, f'MINES  ⚑{left}  {clock_text(self.elapsed)}', wall_time, self.running)
        number_font = SMALL if cell < 24 else FONT
        for x in range(cols):
            for y in range(rows):
                rect = (ox+x*cell, oy+y*cell, ox+x*cell+cell-2, oy+y*cell+cell-2)
                spot = (x, y)
                centre = ((rect[0]+rect[2])//2, (rect[1]+rect[3])//2)
                if spot in game.revealed:
                    d.rectangle(rect, fill=SCREEN)  # Dug: dark and flat.
                    count = game.count(spot)
                    if count:
                        d.text(centre, str(count), font=number_font, fill=MINE_COLOURS[count], anchor='mm')
                elif game.lost and spot in game.mines:
                    d.rectangle(rect, fill=DANGER if spot == game.exploded else '#3a2a2a')
                    d.ellipse((centre[0]-4, centre[1]-4, centre[0]+4, centre[1]+4), fill='black')
                else:
                    pressed = self.press and self.press[0] == spot and not self.press[2]
                    d.rectangle(rect, fill='#5d7a8f' if pressed else '#3b5266')  # Covered: raised.
                    d.line((rect[0], rect[1], rect[2], rect[1]), fill='#557086')
                    if spot in game.flags:
                        wrong = game.lost and spot not in game.mines
                        d.text(centre, '⚑', font=number_font, fill=WARN if not wrong else MUTED,
                               anchor='mm')
        button(d, toolbar[0], 'MODE: FLAG' if self.flag_mode else 'MODE: DIG', active=self.flag_mode)
        button(d, toolbar[1], 'NEW BOARD')
        if not self.running:
            summary = (f'Time {clock_text(self.elapsed)}   Best {clock_text(self.best) if self.best else "--"}')
            self.draw_dialog(d, summary, 'CLEARED!' if getattr(self, 'won', False) else 'BOOM!')
        # Long presses need a check-in even if the finger does not move.
        self.delay = .1 if self.press and not self.press[2] else 1. if self.running else .25
        return image


def solitaire_layout():
    gap = 4
    width = (W - 8*gap) // 7
    height = round(width*1.36)
    top = 30
    xs = [gap + i*(width+gap) for i in range(7)]
    return width, height, xs, top, top + height + 8


SUIT_COLOURS = ('#202830', '#d8443c', '#d8443c', '#202830')


class SolitaireApp(GameBase):
    """Klondike, draw one. Tap a card to pick it up, then tap where it should go. Tap the
    picked-up card again to send it to the foundations (or the first legal pile)."""
    LOWER_IS_BETTER = True  # Record = fewest moves for a win.
    TITLE, HINT = 'SOLITAIRE', 'Tap a card, then where it goes. Tap twice to auto-move.'

    def __init__(self, best=0, save_best=lambda best: None, seed=None):
        super().__init__(best, save_best)
        self.seed = seed
        self.selected = None
        self.new_game(0)

    def new_game(self, now):
        self.game = Klondike(seed=self.seed)
        self.selected = None

    def fan(self, pile):
        """Vertical offsets for each card in a tableau pile, squeezed to fit the screen."""
        width, height, xs, top, tab_top = solitaire_layout()
        cards = self.game.tableau[pile]
        down = sum(1 for _, up in cards if not up)
        up = len(cards) - down
        room = H - 4 - height - tab_top
        down_step, up_step = 5, max(height*.3, 12)
        if down*down_step + max(0, up-1)*up_step > room and up > 1:
            up_step = (room - down*down_step) / (up-1)
            if up_step < 6:  # Very long pile: squeeze the face-down cards too.
                down_step = 2
                up_step = max(3, (room - down*down_step) / (up-1))
        offsets, y = [], tab_top
        for _, face_up in cards:
            offsets.append(y)
            y += up_step if face_up else down_step
        return offsets

    def target_at(self, x, y):
        """('stock',), ('waste',), ('found', s), ('tab', pile, index) or ('tab', pile, None)."""
        width, height, xs, top, tab_top = solitaire_layout()
        col = next((i for i, x0 in enumerate(xs) if x0 <= x <= x0+width), None)
        if col is None:
            return None
        if top <= y <= top+height:
            if col == 0:
                return ('stock',)
            if col == 1:
                return ('waste',)
            if col >= 3:
                return ('found', col-3)
            return None
        if y >= tab_top:
            offsets = self.fan(col)
            # Each card shows from its own offset to the next card's; the last is fully visible.
            for index, start in enumerate(offsets):
                end = offsets[index+1] if index+1 < len(offsets) else start + height
                if start <= y < end:
                    return ('tab', col, index)
            return ('tab', col, None)  # Below the pile (or an empty pile): a destination.
        return None

    def source_ok(self, target):
        if target[0] == 'tab':
            return target[2] is not None and bool(self.game.cards_at(target))
        return target[0] in ('waste', 'found') and bool(self.game.cards_at(target))

    def tap(self, x, y, now):
        if not self.running:
            return self.tap_dialog(x, y, now)
        if pause_tapped(x, y):
            self.state = 'paused'
            return None
        target = self.target_at(x, y)
        game = self.game
        if target is None:
            self.selected = None
        elif target[0] == 'stock':
            game.draw()
            self.selected = None
        elif self.selected is not None:
            same = (target[:2] == self.selected[:2] if target[0] == 'tab' else
                    target == self.selected)
            if same:
                game.auto_move(self.selected)
                self.selected = None
            else:
                destination = ('found',) if target[0] == 'found' else ('tab', target[1]) \
                    if target[0] == 'tab' else None
                if destination and game.move(self.selected, destination):
                    self.selected = None
                else:
                    self.selected = target if self.source_ok(target) else None
        elif self.source_ok(target):
            self.selected = target
        if game.won:
            self.finish(game.moves, won=True)
        return None

    def draw_card(self, d, x, y, card, face_up=True, highlight=False):
        width, height, *_ = solitaire_layout()
        rect = (x, y, x+width-1, y+height-1)
        if not face_up:
            d.rounded_rectangle(rect, radius=3, fill='#2f5f9e', outline='#9ab8dd')
            d.rectangle((x+3, y+3, x+width-4, y+height-4), outline='#5f86bd')
            return
        d.rounded_rectangle(rect, radius=3, fill='#fbfbf6' if not highlight else '#fff2b3',
                            outline=ACCENT if highlight else '#9aa4ac', width=2 if highlight else 1)
        colour = SUIT_COLOURS[card[1]]
        label = rank_name(card) + SUITS[card[1]]
        small = TINY if width < 36 else SMALL
        d.text((x+2, y+1), label, font=small, fill=colour)
        if height > 44:
            d.text((x+width//2, y+height*.62), SUITS[card[1]], font=FONT, fill=colour, anchor='mm')

    def render(self, now, wall_time, alerts):
        self.tick(now)
        game = self.game
        width, height, xs, top, tab_top = solitaire_layout()
        image = Image.new('RGB', (W, H), '#14532d')
        d = ImageDraw.Draw(image)
        d.rectangle((0, 0, W, 25), fill=BG)
        game_header(d, f'SOLITAIRE  {game.moves}', wall_time, self.running)
        sel = self.selected

        def slot(x):
            d.rounded_rectangle((x, top, x+width-1, top+height-1), radius=3, outline='#3f8a5a')

        slot(xs[0])
        if game.stock:
            self.draw_card(d, xs[0], top, None, face_up=False)
        else:
            d.text((xs[0]+width//2, top+height//2), '↻', font=FONT, fill='#7fc79a', anchor='mm')
        slot(xs[1])
        if game.waste:
            self.draw_card(d, xs[1], top, game.waste[-1], highlight=sel == ('waste',))
        for suit in range(4):
            x = xs[3+suit]
            slot(x)
            pile = game.foundations[suit]
            if pile:
                self.draw_card(d, x, top, pile[-1], highlight=sel == ('found', suit))
            else:
                d.text((x+width//2, top+height//2), SUITS[suit], font=FONT, fill='#3f8a5a', anchor='mm')
        for col in range(7):
            x = xs[col]
            d.rounded_rectangle((x, tab_top, x+width-1, tab_top+height-1), radius=3, outline='#2f7048')
            for index, ((card, up), y) in enumerate(zip(game.tableau[col], self.fan(col))):
                lit = sel is not None and sel[0] == 'tab' and sel[1] == col and index >= sel[2]
                self.draw_card(d, x, round(y), card, up, lit)
        if not self.running:
            summary = f'Moves {game.moves}   Best {self.best or "--"}'
            self.draw_dialog(d, summary, 'YOU WIN!')
        self.delay = 1. if self.running else .25
        return image


def sudoku_layout():
    """(cell, origin, keypad [(rect, value)]) - value 0 erases."""
    if portrait():
        cell, origin = 25, (7, 28)
        bottom = origin[1] + 9*cell
        keys = list(range(1, 10)) + [0]
        rects = row_rects(5, bottom+5, bottom+33) + row_rects(5, bottom+37, min(H-3, bottom+65))
    else:
        cell, origin = 22, (6, 30)
        left = origin[0] + 9*cell + 8
        width = (W - 6 - left - 8) / 3
        keys, rects = list(range(1, 10)) + [0], []
        for i in range(10):
            row, col = divmod(i, 3)
            x0 = left + col*(width+4)
            y0 = 30 + row*52
            rects.append((round(x0), y0, round(x0+width), y0+46))
        rects[9] = (left, 30+3*52, W-6, 30+3*52+46)  # ERASE spans the last row.
    return cell, origin, list(zip(rects, keys))


class SudokuApp(GameBase):
    """Tap a square, then a number. Wrong numbers show in red. Best time is kept."""
    TITLE, HINT = 'SUDOKU', 'Tap a square, then a number.'
    LOWER_IS_BETTER = True

    def __init__(self, best=0, save_best=lambda best: None, seed=None):
        super().__init__(best, save_best)
        self.seed = seed
        self.game = None

    def new_game(self, now):
        self.game = SudokuGame(seed=self.seed)

    def tap(self, x, y, now):
        if not self.running:
            return self.tap_dialog(x, y, now)
        if pause_tapped(x, y):
            self.state = 'paused'
            return None
        cell, (ox, oy), keys = sudoku_layout()
        col, row = int((x-ox)//cell), int((y-oy)//cell)
        if x >= ox and y >= oy and 0 <= col < 9 and 0 <= row < 9:
            self.game.selected = row*9 + col
            return None
        for rect, value in keys:
            if index_at([rect], x, y, slop=3) is not None:
                if self.game.place(value) == 'win':
                    self.finish(max(1, round(self.elapsed)))
                break
        return None

    def render(self, now, wall_time, alerts):
        self.tick(now)
        if self.game is None:
            self.new_game(now)
        game = self.game
        cell, (ox, oy), keys = sudoku_layout()
        image = Image.new('RGB', (W, H), BG)
        d = ImageDraw.Draw(image)
        mistakes = f'  ✗{game.mistakes}' if game.mistakes else ''
        game_header(d, f'SUDOKU  {clock_text(self.elapsed)}{mistakes}', wall_time, self.running)
        sel = game.selected
        sel_value = game.entries[sel] if sel is not None else 0
        digit_font = font(16, bold=True) if cell >= 25 else SMALL
        for i in range(81):
            row, col = divmod(i, 9)
            rect = (ox+col*cell, oy+row*cell, ox+col*cell+cell-1, oy+row*cell+cell-1)
            fill = '#0e161d'
            if sel is not None:
                srow, scol = divmod(sel, 9)
                if row == srow or col == scol or (row//3, col//3) == (srow//3, scol//3):
                    fill = '#182634'
                if sel_value and game.entries[i] == sel_value:
                    fill = '#23445a'
                if i == sel:
                    fill = '#2f6f63'
            d.rectangle(rect, fill=fill, outline=GRID)
            value = game.entries[i]
            if value:
                colour = TEXT if game.given(i) else DANGER if game.wrong(i) else ACCENT
                d.text(((rect[0]+rect[2])//2+1, (rect[1]+rect[3])//2+1), str(value),
                       font=digit_font, fill=colour, anchor='mm')
        for k in range(0, 10, 3):  # Heavier 3x3 box lines.
            d.line((ox+k*cell, oy, ox+k*cell, oy+9*cell), fill=MUTED, width=2)
            d.line((ox, oy+k*cell, ox+9*cell, oy+k*cell), fill=MUTED, width=2)
        for rect, value in keys:
            complete = value and game.entries.count(value) == 9 and all(
                game.entries[i] == value for i in range(81) if game.solution[i] == value)
            button(d, rect, str(value) if value else 'ERASE',
                   fill=SHADE if complete else None)
        if not self.running:
            best = clock_text(self.best) if self.best else '--'
            self.draw_dialog(d, f'Time {clock_text(self.elapsed)}   Best {best}', 'SOLVED!')
        self.delay = 1. if self.running else .25
        return image


class GamesMenu:
    GAMES = (('SNAKE', 'snake'), ('TETRIS', 'tetris'), ('PONG', 'pong'),
             ('BRICKS', 'bricks'), ('FLAPPY', 'flappy'), ('INVADERS', 'invaders'),
             ('MINES', 'mines'), ('SOLITAIRE', 'solitaire'), ('SUDOKU', 'sudoku'))
    LABEL, LABEL_SMALL = font(16, bold=True), font(13, bold=True)

    def __init__(self, notes):
        self.notes, self.delay = notes, 1.

    def rects(self):
        """Three columns in landscape, two in portrait; BACK follows the last game."""
        cols = 2 if portrait() else 3
        count = len(self.GAMES) + 1
        rows = -(-count // cols)
        gap, top, bottom = 6, 30, H-4
        width = (W - 12 - (cols-1)*gap) / cols
        height = (bottom - top - (rows-1)*gap) / rows
        rects = []
        for i in range(count):
            row, col = divmod(i, cols)
            x0, y0 = 6 + col*(width+gap), top + row*(height+gap)
            rects.append((round(x0), round(y0), round(x0+width), round(y0+height)))
        return rects[:-1], rects[-1]

    def tap(self, x, y, now):
        games, back = self.rects()
        choice = index_at(games, x, y, slop=3)
        if choice is not None:
            return self.GAMES[choice][1]
        if index_at([back], x, y, slop=3) is not None:
            return 'exit'
        return None

    def render(self, now, wall_time, alerts):
        image = Image.new('RGB', (W, H), BG)
        d = ImageDraw.Draw(image)
        if alerts:
            draw_banner(d, alerts, wall_time.strftime('%H:%M'))
        else:
            d.text((8, 4), 'PANEL / GAMES', font=FONT, fill=ACCENT)
            d.text((W-8, 6), wall_time.strftime('%H:%M:%S'), font=SMALL, fill=TEXT, anchor='ra')
        games, back = self.rects()
        notes = self.notes()
        for rect, (label, key) in zip(games, self.GAMES):
            d.rounded_rectangle(rect, radius=8, fill=PANEL)
            mid_x, mid_y = (rect[0]+rect[2])//2, (rect[1]+rect[3])//2
            room = rect[2]-rect[0]-8
            label_font = self.LABEL if d.textlength(label, font=self.LABEL) <= room else self.LABEL_SMALL
            d.text((mid_x, mid_y-7), label, font=label_font, fill=TEXT, anchor='mm')
            d.text((mid_x, mid_y+12), fit_text(d, notes.get(key, ''), room, TINY),
                   font=TINY, fill=MUTED, anchor='mm')
        button(d, back, 'BACK')
        return image


def desktop_user():
    uid = int(os.environ.get('SUDO_UID', getattr(os, 'getuid', lambda: 0)()))
    gid = int(os.environ.get('SUDO_GID', getattr(os, 'getgid', lambda: 0)()))
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
    if getattr(os, 'getuid', lambda: 0)() == 0 and uid != 0:
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
        if action == 'detect':
            self.backend = self.detect()
            return
        kind, control = self.backend
        if action.startswith(('volume:', 'muted:')):
            name, value = action.split(':', 1)
            if name == 'volume' and value.isdecimal() and 0 <= int(value) <= 100:
                cmd = (['wpctl', 'set-volume', '-l', '1.0', self.SINK, value+'%'] if kind == 'wpctl'
                       else ['amixer', '-M', 'sset', control or '', value+'%'])
            elif name == 'muted' and value in ('0', '1'):
                cmd = (['wpctl', 'set-mute', self.SINK, value] if kind == 'wpctl'
                       else ['amixer', 'sset', control or '', 'mute' if value == '1' else 'unmute'])
            else:
                return
            if kind in ('wpctl', 'amixer'): self.run(cmd)
            return
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
        draw_bars(d, (4, 28, W-4, H-50), self.capture.bands)
        wave = self.capture.wave
        points = [(4+i*(W-8)/(len(wave)-1), H-26+v*40) for i, v in enumerate(wave)]
        d.line(points, fill=BLUE, width=1)
        saver_header(d, fit_text(d, self.audio.state.get('title') or self.capture.status, 240, SMALL),
                     MUTED, wall_time, alerts)
        return image


GAME_PAGES = ('snake', 'tetris', 'pong', 'bricks', 'flappy', 'invaders', 'mines', 'solitaire',
              'sudoku')
APP_TILES = (('AUDIO', 'audio'), ('GAMES', 'games'), ('PHOTO ALBUM', 'album'), ('GIF PLAYER', 'gifs'), ('TOOLS', 'tools'), ('QUICK CONTROLS', 'quick'))
AUDIO_BUTTONS = (('down', 'mute', 'up', 'source'), ('prev', 'play', 'next', 'full'))


def saver_due(options, idle_seconds, page):
    return (options['saver_style'] != 'off' and idle_seconds >= options['saver_delay']
            and page not in ('exit','shutdown','calconfirm','savers','saverstyle','colours',
                             'photodelete','album','gifs','visual'))


TABS = ('CAMERA', 'PHOTOS', 'HOME', 'STATUS', 'OPTIONS', 'EXIT')
TAB_WIDTHS = (58, 56, 44, 52, 62, 48)  # Sized to their labels; sums to 320.
TAB_EDGES = tuple(sum(TAB_WIDTHS[:i]) for i in range(len(TABS)+1))
TAB_PARENT = {'health': 'status', 'history': 'status', 'savers': 'options', 'saverstyle': 'options',
              'calconfirm': 'options', 'photodelete': 'photos', 'shutdown': 'exit',
              'audio': 'home', 'apps': 'home'}
# Home page launcher: (label, key, accent colour).
HOME_TILES = (('CAMERA', 'camera', ACCENT), ('PHOTOS', 'photos', BLUE),
              ('GAMES', 'games', YELLOW), ('AUDIO', 'audio', PURPLE),
              ('ALBUM', 'album', ORANGE), ('GIFS', 'gifs', RED),
              ('TOOLS', 'tools', GREEN), ('QUICK', 'quick', CYAN),
              ('ASK', 'ask', '#ff8fb8'))


def home_rects():
    """3x3 launcher grid under the clock band, in either orientation."""
    top = 104 if portrait() else 80
    bottom, gap = content_bottom(), 6
    width = (W - 16 - 2*gap) / 3
    height = (bottom - top - 2*gap) / 3
    return [(round(8 + c*(width+gap)), round(top + r*(height+gap)),
             round(8 + c*(width+gap) + width), round(top + r*(height+gap) + height))
            for r in range(3) for c in range(3)]
TAB_FONT = font(11)


def timer_label(seconds):
    return f'TIMER {seconds}s' if seconds else 'TIMER OFF'


def preview_area():
    """(top, height) of the camera/photo picture area for the current orientation."""
    return (26, min(180, W*3//4)) if portrait() else (25, 132)


BOOT_LABEL = ['?']  # 'On' / 'Off' / '?': boot-service state, refreshed by main().


def boot_service_enabled(run=subprocess.run):
    """True/False for the systemd boot service, None if it cannot be checked."""
    try:
        result = run(['systemctl', 'is-enabled', 'cyberdeck-panel.service'],
                     capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() == 'enabled'


def set_boot_service(enabled, app_path, data, run=subprocess.run):
    """Turn start-at-boot on (installing the unit if needed) or off. Never starts or
    stops the service, so it is safe to call from the copy that is running right now."""
    if getattr(os, 'geteuid', lambda: 0)() != 0:
        return 'Needs sudo: run the app with sudo to change this'
    try:
        if enabled:
            import setup_panel_service
            uid, gid = desktop_user()
            setup_panel_service.install(Path(app_path).resolve(), uid, gid, data=data,
                                        run=lambda cmd: run(cmd, check=True))
            return 'Will start at boot. (Takes effect next boot.)'
        run(['systemctl', 'disable', 'cyberdeck-panel.service'], check=True,
            capture_output=True, text=True, timeout=20)
        return 'Will not start at boot.'
    except (ImportError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print('Boot start change failed:', exc, flush=True)
        return 'Could not change boot start; see terminal'


def option_labels(options):
    if portrait():  # Same settings, shorter words for 108 px buttons.
        return [f"{'Bright' if BACKLIGHT_PWM else 'Pixels'}: {options['pixel_level']}%",
                'Savers...', f"Boot: {BOOT_LABEL[0]}",
                'Test colours', 'Calibrate...', f"Graph: {options['history_seconds']}s",
                'Photo: ' + ('Full' if options['photo_size'] == 'full' else '320x240'),
                'Alerts: ' + options['alerts'].title(),
                'Screen: ' + ROTATION_NAMES[options['rotation']],
                f"Camera: {options['camera_turn']}°"]
    return [f"{'Brightness' if BACKLIGHT_PWM else 'Pixel level'}: {options['pixel_level']}%",
            'Screensavers...', f"Start at boot: {BOOT_LABEL[0]}",
            'Test colours', 'Calibrate touch...',
            f"Graph: {options['history_seconds']}s",
            'Photo size: ' + ('Full' if options['photo_size'] == 'full' else '320x240'),
            'Alert banner: ' + options['alerts'].title(),
            'Screen: ' + ROTATION_NAMES[options['rotation']],
            f"Camera turn: {options['camera_turn']}°"]


def preview_box(size):
    """(width, height) and on-screen box of a camera frame fitted into the preview area."""
    top, height = preview_area()
    scale = min(W/size[0], height/size[1])
    width, tall = max(1, round(size[0]*scale)), max(1, round(size[1]*scale))
    left, upper = (W-width)//2, top+(height-tall)//2
    return (width, tall), (left, upper, left+width-1, upper+tall-1)


def portrait_thumb_origin(thumb):
    top, height = preview_area()
    return 4, top+height-thumb.height-4


def draw_camera_live(image, d, frame, box, cam):
    """Everything on or over the moving picture. Redrawn for every camera frame; the rest
    of the page comes from a cache (see render), which is most of the per-frame cost."""
    if frame is not None:
        size = (box[2]-box[0]+1, box[3]-box[1]+1)
        # Bilinear is plenty for shrinking a 320x240 preview and ~3x cheaper than bicubic.
        image.paste(frame if frame.size == size else frame.resize(size, Image.BILINEAR), box[:2])
        if cam.get('flash'):
            d.rectangle(box, fill='white')
    thumb = cam.get('thumb')
    if portrait() and thumb is not None:
        tx, ty = portrait_thumb_origin(thumb)
        d.rectangle((tx-1, ty-1, tx+thumb.width, ty+thumb.height), outline=TEXT)
        image.paste(thumb, (tx, ty))
    top, height = preview_area()
    centre = ((box[0]+box[2])//2, (box[1]+box[3])//2) if box else (W//2, top+height//2)
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


AI_COLOUR = PURPLE
HISTORY_RANGES = ((3600, '1H'), (6*3600, '6H'), (86400, '24H'), (7*86400, '7D'))
EVENT_COLOURS = {'undervoltage': DANGER, 'throttled': WARN, 'ai': AI_COLOUR,
                 'search': BLUE, 'camera': ACCENT}


def trend(d, rect, points, low, high, colour, start, seconds, gap):
    """Line chart of (time, value) over [start, start+seconds]; gaps longer than `gap`
    seconds (backend stopped, Pi off) stay gaps."""
    x0, y0, x1, y1 = rect
    d.rectangle(rect, fill=INSET)
    d.line((x0, (y0+y1)//2, x1, (y0+y1)//2), fill=PANEL)
    previous = None
    for t, value in points:
        if value is None:
            previous = None
            continue
        x = x0 + (x1-x0)*(t-start)/seconds
        y = y1 - (y1-y0)*max(0., min(1., (value-low)/(high-low)))
        if previous and t - previous[0] <= gap:
            d.line((previous[1], previous[2], x, y), fill=colour, width=1)
        else:
            d.point((x, y), fill=colour)
        previous = (t, x, y)


def draw_history(d, rows, bottom):
    """STATUS -> HISTORY: temperature, CPU and events from the backend's telemetry log."""
    chosen = rows.get('history_range', 3600)
    for (seconds, label), rect in zip(HISTORY_RANGES, row_rects(4, 28, 50)):
        button(d, rect, label, active=seconds == chosen, action=f'hist:{seconds}')
    online, data = rows.get('backend_online'), rows.get('history')
    middle = (54 + bottom) // 2
    if 'backend_online' not in rows or online is False:
        centred(d, middle-20, 'Backend not running', FONT)
        centred(d, middle+2, 'History is recorded by cyberdeck-core:', SMALL, MUTED)
        centred(d, middle+20, 'sudo python3 setup_core_service.py --now', TINY, ACCENT)
        return
    if not data or data.get('seconds') != chosen:
        centred(d, middle, 'Loading...', FONT, MUTED)
        return
    seconds, start = data['seconds'], data['now'] - data['seconds']
    samples, events = data['samples'], data['events']
    gap = 3 * max(10., seconds / 150)
    tall = (bottom - 54 - 74) // 2
    temps = [(s[0], s[2]) for s in samples]
    latest = next((v for _, v in reversed(temps) if v is not None), None)
    top = 54
    d.text((8, top), 'TEMP C (max)', font=TINY, fill=ORANGE)
    d.text((W-10, top), '--' if latest is None else f'{latest:.1f} C', font=TINY, fill=TEXT, anchor='ra')
    rect = (28, top+12, W-10, top+12+tall)
    d.text((2, rect[1]-2), '90', font=TINY, fill=MUTED)
    d.text((2, rect[3]-8), '30', font=TINY, fill=MUTED)
    warm = rect[3] - (rect[3]-rect[1])*(80-30)/60
    trend(d, rect, temps, 30, 90, ORANGE, start, seconds, gap)
    d.line((rect[0], warm, rect[2], warm), fill='#7a2e2e')  # 80 C: the Pi starts to slow down.
    top = rect[3] + 6
    cpu = [(s[0], s[1]) for s in samples]
    d.text((8, top), 'CPU % (average)', font=TINY, fill=ACCENT)
    rect = (28, top+12, W-10, top+12+tall)
    d.text((2, rect[1]-2), '100', font=TINY, fill=MUTED)
    d.text((14, rect[3]-8), '0', font=TINY, fill=MUTED)
    trend(d, rect, cpu, 0, 100, ACCENT, start, seconds, gap)
    # Event strip: one lane, coloured ticks or spans; undervoltage drawn last so it shows.
    strip = (28, rect[3]+6, W-10, rect[3]+16)
    d.rectangle(strip, fill=INSET)
    order = ('camera', 'search', 'ai', 'throttled', 'undervoltage')
    for event in sorted(events, key=lambda e: order.index(e['kind']) if e['kind'] in order else -1):
        colour = EVENT_COLOURS.get(event['kind'], MUTED)
        a = strip[0] + (strip[2]-strip[0])*max(0., (event['start']-start)/seconds)
        b = strip[0] + (strip[2]-strip[0])*min(1., (event['end']-start)/seconds)
        d.rectangle((a, strip[1], max(a+1, b), strip[3]), fill=colour)
    labels = {3600: '-1h', 6*3600: '-6h', 86400: '-24h', 7*86400: '-7d'}
    d.text((strip[0], strip[3]+2), labels.get(seconds, ''), font=TINY, fill=MUTED)
    d.text((strip[2], strip[3]+2), 'now', font=TINY, fill=MUTED, anchor='ra')
    counts = {kind: sum(e['kind'] == kind for e in events) for kind in EVENT_COLOURS}
    x = 8
    for kind, label in (('undervoltage', 'Low V'), ('throttled', 'Throttled'), ('ai', 'AI'),
                        ('search', 'Search'), ('camera', 'Photo')):
        text = f'{label} {counts[kind]}'
        width = d.textlength(text, font=TINY)
        if x + width + 10 > W:
            break
        d.rectangle((x, bottom-11, x+5, bottom-6), fill=EVENT_COLOURS[kind])
        d.text((x+8, bottom-14), text, font=TINY, fill=WARN if kind == 'undervoltage' and counts[kind] else TEXT)
        x += width + 16


CAMERA_CHROME = {}  # The live camera page's static layer, rebuilt only when its inputs change.


def render(page, frame, paused, fps, rows, message, history=(), now=None,
           options=None, wall_time=None, alerts=(), cam=None, photo=None,
           service=False, extra=None):
    """Draw one page on the W x H logical canvas and register its buttons in HOTSPOTS.

    The camera page is drawn up to 20 times a second but its text and buttons change at
    most once a second (the clock), so those are cached and only the picture is redrawn.
    """
    if page != 'camera' or frame is None:
        return render_page(page, frame, paused, fps, rows, message, history, now, options,
                           wall_time, alerts, cam, photo, service, extra)
    options = options or DEFAULT_OPTIONS
    wall_time = wall_time or datetime.now().astimezone()
    cam = cam or {}
    key = (ROTATION, frame.size, paused, fps, message, options['photo_size'],
           options['snap_timer'], tuple(alerts), wall_time.strftime('%H:%M:%S'),
           id(cam.get('thumb')), bool(cam.get('countdown')), tuple(rows.get('busy') or ()))
    if CAMERA_CHROME.get('key') != key:
        chrome = render_page(page, frame, paused, fps, rows, message, history, now, options,
                             wall_time, alerts, cam, photo, service, extra, chrome=True)
        CAMERA_CHROME.update(key=key, image=chrome, hotspots=list(HOTSPOTS))
    else:
        HOTSPOTS[:] = CAMERA_CHROME['hotspots']
        HOTSPOTS_PAGE[0] = page
    image = CAMERA_CHROME['image'].copy()
    draw_camera_live(image, ImageDraw.Draw(image), frame, preview_box(frame.size)[1], cam)
    return image


def render_page(page, frame, paused, fps, rows, message, history=(), now=None,
                options=None, wall_time=None, alerts=(), cam=None, photo=None,
                service=False, extra=None, chrome=False):
    """Draw one page on the W x H logical canvas and register its buttons in HOTSPOTS.

    cam: camera overlays. photo: review state. extra: apps/audio page data.
    """
    extra = extra or {}
    options = options or DEFAULT_OPTIONS
    wall_time = wall_time or datetime.now().astimezone()
    cam, photo = cam or {}, photo or {}
    HOTSPOTS.clear()
    HOTSPOTS_PAGE[0] = page
    image = Image.new('RGB', (W, H), BG)
    d = ImageDraw.Draw(image)
    bottom = content_bottom()
    dy = (bottom-204)//2  # Centres dialog-style pages in the taller portrait content area.
    if page == 'photofull':
        if photo.get('image') is not None:
            full = ImageOps.contain(photo['image'], (W, H))
            image.paste(full, ((W-full.width)//2, (H-full.height)//2))
        d.text((6, 4), photo.get('label', ''), font=TINY, fill=TEXT,
               stroke_width=2, stroke_fill='black')
        d.text((W-6, H-6), 'tap to return', font=TINY, fill=TEXT, anchor='rs',
               stroke_width=2, stroke_fill='black')
        return image
    title = {'calconfirm':'TOUCH SETUP', 'savers':'SAVERS', 'saverstyle':'SAVER STYLE',
             'photodelete':'PHOTOS',
             'shutdown':'POWER', 'poweroff':'POWER'}.get(page, page.upper())
    if alerts and page not in ('stopped', 'poweroff'):
        draw_banner(d, alerts, wall_time.strftime('%H:%M'))
        if page not in ('calconfirm', 'exit', 'shutdown', 'photodelete', 'colours'):
            hotspot((0, 0, W-1, 23), 'banner')
    else:
        clock = wall_time.strftime('%H:%M:%S')
        room = W-24-d.textlength(clock, font=SMALL)
        busy = rows.get('busy') or ()
        if 'ai' in busy or 'search' in busy:  # The backend is answering a question.
            label = 'AI' if 'ai' in busy else 'SEARCH'
            pill = d.textlength(label, font=TINY) + 10
            px = W - 14 - d.textlength(clock, font=SMALL) - pill
            d.rounded_rectangle((px, 5, px+pill, 19), radius=4, fill=AI_COLOUR)
            d.text((px+pill/2, 12), label, font=TINY, fill=BG, anchor='mm')
            room -= pill + 6
        heading = 'PANEL / ' + title
        if d.textlength(heading, font=FONT) > room:
            heading = title
        d.text((8, 4), fit_text(d, heading, room, FONT), font=FONT, fill=ACCENT)
        d.text((W-8, 6), clock, font=SMALL, fill=TEXT, anchor='ra')
    if page == 'stopped':
        top = H//2-35
        centred(d, top, 'Panel app is not running', FONT)
        if message:
            centred(d, top+23, message, SMALL, WARN)
        if service:
            centred(d, top+50, 'Restart it from a terminal, or reboot:', SMALL, MUTED)
            centred(d, top+73, f'sudo systemctl start {SERVICE}', SMALL, ACCENT)
        else:
            centred(d, top+50, 'Restart from your VNC terminal.', SMALL, MUTED)
            centred(d, top+73, 'sudo python3 ~/panel_app.py', SMALL, ACCENT)
        return image
    if page == 'poweroff':
        top = H//2-50
        centred(d, top, 'Shutting down...', FONT)
        lines = (('Wait until the green ACT LED stops', 'flashing, then switch off the power.')
                 if not portrait() else
                 ('Wait until the green ACT LED', 'stops flashing, then switch', 'off the power.'))
        for i, line in enumerate(lines):
            centred(d, top+35+i*18, line, SMALL)
        notes = (('This screen may stay lit after shutdown;', 'the PWM pin keeps its last state.')
                 if BACKLIGHT_PWM else
                 ('This screen stays lit after shutdown;', 'its backlight is wired straight to 3.3 V.'))
        for i, line in enumerate(notes):
            centred(d, top+55+len(lines)*18+i*14, line, TINY, MUTED)
        return image
    if page == 'camera':
        top, height = preview_area()
        box = preview_box(frame.size)[1] if frame is not None else None
        if not chrome:
            draw_camera_live(image, d, frame, box, cam)
        if frame is None:
            centred(d, top+height//2-6, 'Camera is not available', FONT, MUTED)
        thumb = cam.get('thumb')
        size_label = 'FULL' if options['photo_size'] == 'full' else '320x240'
        timer_value = f"{options['snap_timer']}s" if options['snap_timer'] else 'OFF'
        if portrait():
            # No side gutters: the last photo sits in the preview's corner (drawn by
            # draw_camera_live, since it is on top of the moving picture).
            if thumb is not None:
                tx, ty = portrait_thumb_origin(thumb)
                hotspot((tx-4, ty-4, tx+thumb.width+4, ty+thumb.height+4), 'cam:last')
            d.text((W-6, top+height+3), f'{size_label} | T {timer_value}', font=TINY,
                   fill=MUTED, anchor='ra')
        else:
            # Left gutter: last snapshot (tap to review). Right gutter: capture settings.
            d.text((36, 33), 'LAST', font=TINY, fill=MUTED, anchor='ma')
            if thumb is not None:
                image.paste(thumb, (36-thumb.width//2, 46+(48-thumb.height)//2))
                d.rectangle((36-thumb.width//2-1, 45+(48-thumb.height)//2,
                             36+(thumb.width+1)//2, 46+(48+thumb.height)//2), outline=MUTED)
                d.text((36, 100), 'tap to view', font=TINY, fill=MUTED, anchor='ma')
                hotspot((0, 25, 69, 112), 'cam:last')
            else:
                d.rectangle((6, 46, 66, 93), outline=PANEL)
                d.text((36, 70), 'none', font=TINY, fill=MUTED, anchor='mm')
            for y, label, value in ((33, 'SIZE', size_label), (75, 'TIMER', timer_value)):
                d.text((W-36, y), label, font=TINY, fill=MUTED, anchor='ma')
                d.text((W-36, y+14), value, font=SMALL, fill=TEXT, anchor='ma')
        message_room = W-16-(70 if portrait() else 0)
        d.text((8, top+height+1), fit_text(d, message, message_room, SMALL), font=SMALL, fill=TEXT)
        labels = ('CANCEL' if cam.get('countdown') else 'SNAP', timer_label(options['snap_timer']),
                  'RESUME' if paused else 'PAUSE', f'{fps:g} FPS')
        for i, (rect, label) in enumerate(zip(row_rects(4, bottom-28, bottom-1), labels)):
            button(d, rect, label, active=(i == 0 and bool(cam.get('countdown'))),
                   action=('cam:snap', 'cam:timer', 'cam:pause', 'cam:fps')[i])
    elif page in ('photos', 'photodelete'):
        picture, count = photo.get('image'), photo.get('count', 0)
        if page == 'photodelete':
            if picture is not None:
                small = ImageOps.contain(picture, (120, 90))
                image.paste(small, ((W-small.width)//2, 28+dy))
            centred(d, 132+dy, 'Delete this photo?', FONT)
            centred(d, 151+dy, photo.get('label', ''), SMALL, MUTED)
            button(d, (12, 164+dy, W//2-9, 202+dy), 'CANCEL', active=True, action='del:cancel')
            button(d, (W//2+8, 164+dy, W-13, 202+dy), 'DELETE', fill=DANGER, action='del:confirm')
        elif not count:
            centred(d, 80+dy, 'No photos yet', FONT)
            centred(d, 106+dy, 'Use SNAP on the Camera page.', SMALL, MUTED)
            centred(d, 130+dy, message, SMALL)
        else:
            top, height = preview_area()
            if picture is not None:
                shown = ImageOps.contain(picture, (W, height))
                image.paste(shown, ((W-shown.width)//2, top+(height-shown.height)//2))
                hotspot((0, top, W-1, top+height), 'photo:full')
            else:
                centred(d, top+height//2, 'Cannot open this photo', FONT, MUTED)
            original = photo.get('size')
            info = f"{photo['index']+1}/{count}  {photo.get('label', '')}"
            if original:
                info += f'  {original[0]}x{original[1]}'
            info_font = TINY if portrait() else SMALL
            d.text((8, top+height+2), fit_text(d, message or info, W-16, info_font),
                   font=info_font, fill=TEXT)
            actions = ('photo:older', 'photo:newer', 'photo:full', 'photo:delete')
            for i, (rect, label) in enumerate(zip(row_rects(4, bottom-28, bottom-1),
                                                  ('< OLD', 'NEW >', 'FULL', 'DELETE') if portrait()
                                                  else ('< OLDER', 'NEWER >', 'FULL', 'DELETE'))):
                enabled = not ((i == 0 and photo['index'] == 0) or
                               (i == 1 and photo['index'] == count-1))
                button(d, rect, label if enabled else '', fill=None if enabled else SHADE,
                       action=actions[i] if enabled else None)
    elif page == 'status':
        now = time.monotonic() if now is None else now
        tall = 52 if portrait() else 32
        cpu = '--' if rows.get('cpu') is None else f"{rows['cpu']:.0f}%"
        d.text((8, 25), f'CPU  {cpu}', font=SMALL, fill=ACCENT)
        d.text((W-12, 25), rows.get('temp', '--'), font=SMALL, fill=TEXT, anchor='ra')
        graph(d, (28, 43, W-10, 43+tall), history, 1, now, ACCENT, options['history_seconds'])
        ram_y = 43+tall+7
        d.text((8, ram_y), 'RAM', font=SMALL, fill=BLUE)
        d.text((W-12, ram_y), f"{rows.get('ram', '--')}   {rows.get('ram_percent', 0):.0f}%",
               font=SMALL, fill=TEXT, anchor='ra')
        graph(d, (28, ram_y+18, W-10, ram_y+18+tall), history, 2, now, BLUE,
              options['history_seconds'])
        disk_y = ram_y+18+tall+8
        d.text((8, disk_y), 'Disk free  ' + rows.get('disk_free', '--'), font=SMALL, fill=TEXT)
        bar = W-85
        d.rounded_rectangle((bar, disk_y+5, W-10, disk_y+12), radius=3, fill=PANEL)
        used_width = round(.75*max(0, min(100, rows.get('disk_used_percent',0))))
        if used_width:
            d.rectangle((bar, disk_y+5, bar+used_width, disk_y+12), fill=BLUE)
        d.text((8, disk_y+17), 'Up ' + rows.get('uptime', '--'), font=SMALL, fill=TEXT)
        d.text((W-12, disk_y+18), 'bar = disk used', font=TINY, fill=MUTED, anchor='ra')
        hint = f"{options['history_seconds']}s history | 0-100%"
        if portrait():  # Too narrow for the date beside two buttons: one line above them.
            d.text((8, bottom-46), wall_time.strftime('%Y/%m/%d'), font=SMALL, fill=TEXT)
            d.text((W-10, bottom-44), hint, font=TINY, fill=MUTED, anchor='ra')
            history_rect, details_rect = row_rects(2, bottom-30, bottom-2, margin=8)
        else:
            d.text((8, bottom-28), wall_time.strftime('%Y/%m/%d'), font=SMALL, fill=TEXT)
            d.text((8, bottom-10), hint, font=TINY, fill=MUTED)
            history_rect, details_rect = (W-204, bottom-30, W-114, bottom-2), (W-107, bottom-30, W-10, bottom-2)
        button(d, history_rect, 'HISTORY', action='status:history')
        button(d, details_rect, 'DETAILS', action='status:details')
    elif page == 'history':
        draw_history(d, rows, bottom)
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
        pitch = 24 if portrait() else 19
        for i, (label, value) in enumerate(details):
            y = 28+i*pitch
            d.text((8,y),label,font=TINY,fill=MUTED)
            text = fit_text(d, value, W-98, SMALL)
            colour = WARN if value in ('LOW VOLTAGE','Not synchronized') else TEXT
            d.text((88,y),text,font=SMALL,fill=colour)
    elif page in ('options', 'savers', 'saverstyle'):
        if page == 'options':
            labels = option_labels(options)
            actions = [f'opt:{i}' for i in range(len(labels))]
            active = [False]*len(labels)
            note = message or ('Saved automatically. Brightness dims the backlight.' if BACKLIGHT_PWM
                               else 'Saved automatically. Dimming leaves backlight on.')
        elif page == 'savers':
            delay = options['saver_delay']
            labels = ['Style: ' + SAVER_NAMES[options['saver_style']] + '...',
                      f"After: {str(delay)+'s' if delay < 60 else str(delay//60)+' min'}",
                      'Speed: '+options['saver_speed'].title(),
                      f"{'Brightness' if BACKLIGHT_PWM else 'Pixel level'}: {options['saver_level']}%",
                      f"Bunnies: {options['bunny_count']}",
                      f"Slides: {'' if portrait() else 'every '}{options['slide_seconds']}s",
                      f"Clock: {options['clock_format']}", 'PREVIEW']
            actions = [f'saver:{i}' for i in range(len(labels))]
            active = [i == 7 for i in range(len(labels))]
            note = message or 'Tap once to wake. Bunnies: tap one to make it jump.'
        else:
            labels = [SAVER_NAMES[style].upper() for style in SAVER_STYLES]
            actions = [f'style:{style}' for style in SAVER_STYLES]
            active = [options['saver_style'] == style for style in SAVER_STYLES]
            note = 'Pictures: camera + cyberdeck-data/album. GIFs: cyberdeck-data/gifs'
        note_lines = wrap_text(d, note, W-16, TINY)
        # A two-line note takes 12 px from the buttons rather than being cut off.
        rects = grid_rects(len(labels), bottom=bottom-12*(len(note_lines)-1))
        for rect, label, action, on in zip(rects, labels, actions, active):
            button(d, rect, label, on, action=action)
        for i, line in enumerate(note_lines):
            d.text((8, rects[-1][3]+4+12*i), line, font=TINY,
                   fill=TEXT if page == 'options' and message else MUTED)
    elif page == 'home':
        notes = extra.get('home', {})
        clock = wall_time.strftime('%H:%M')
        day = wall_time.strftime('%A')
        date = wall_time.strftime('%d %B %Y').lstrip('0')
        if portrait():
            d.text((W//2, 52), clock, font=BIG_PORTRAIT, fill=ACCENT, anchor='mm')
            centred(d, 84, f'{day} {date}', SMALL)
            status_y = 96
        else:
            d.text((8, 46), clock, font=HOME_CLOCK, fill=ACCENT, anchor='lm')
            d.text((W-8, 37), day, font=SMALL, fill=TEXT, anchor='rm')
            d.text((W-8, 54), date, font=SMALL, fill=MUTED, anchor='rm')
            status_y = 71
        status = '  |  '.join(part for part in (
            rows.get('temp') and f"CPU {rows['temp']}", notes.get('wifi'),
            rows.get('disk_free') and f"{rows['disk_free']} free") if part)
        d.text((W//2, status_y), fit_text(d, status or 'Reading status...', W-16, TINY),
               font=TINY, fill=MUTED, anchor='mm')
        for rect, (label, key, colour) in zip(home_rects(), HOME_TILES):
            d.rounded_rectangle(rect, radius=7, fill=PANEL)
            d.rounded_rectangle((rect[0], rect[1], rect[0]+4, rect[3]), radius=2, fill=colour)
            mid_x, mid_y = (rect[0]+rect[2])//2 + 2, (rect[1]+rect[3])//2
            room = rect[2]-rect[0]-12
            note = notes.get(key, '')
            label_font = HOME_LABEL if d.textlength(label, font=HOME_LABEL) <= room else HOME_LABEL_SMALL
            d.text((mid_x, mid_y - (6 if note else 0)), fit_text(d, label, room, label_font),
                   font=label_font, fill=TEXT, anchor='mm')
            if note:
                d.text((mid_x, mid_y+10), fit_text(d, note, room, TINY), font=TINY,
                       fill=MUTED, anchor='mm')
            hotspot(rect, 'home:' + key)
    elif page == 'apps':
        notes = extra.get('notes', {})
        for rect, (label, key) in zip(grid_rects(len(APP_TILES), 30, bottom+4, max_pitch=200), APP_TILES):
            d.rounded_rectangle(rect, radius=8, fill=PANEL)
            middle, room = (rect[1]+rect[3])//2, rect[2]-rect[0]-8
            title_lines = wrap_text(d, label, room, FONT, 2)
            note_lines = wrap_text(d, notes.get(key, ''), room, TINY, 2)
            y = middle - (len(title_lines)*18 + len(note_lines)*13)//2 + 2
            for line in title_lines:
                d.text(((rect[0]+rect[2])//2, y+8), line, font=FONT, fill=TEXT, anchor='mm')
                y += 18
            y += 4
            for line in note_lines:
                d.text(((rect[0]+rect[2])//2, y+6), line, font=TINY, fill=MUTED, anchor='mm')
                y += 13
            hotspot(rect, 'app:' + key)
    elif page == 'audio':
        state, capture = extra.get('audio', {}), extra.get('capture')
        vis_bottom = 104 + (bottom-204)
        d.rectangle((8, 27, W-8, vis_bottom), fill=SCREEN)
        if capture is not None and capture.source:
            draw_bars(d, (10, 30, W-10, vis_bottom-1), capture.bands)
        status = capture.status if capture is not None else 'Visualizer off'
        source = {'output': 'Visualizer: what is playing', 'mic': 'Visualizer: microphone',
                  'off': 'Visualizer off'}[options['vis_source']]
        d.text((10, vis_bottom+3), fit_text(d, f'{source} | {status}', W-20, TINY), font=TINY, fill=MUTED)
        if state.get('player') in ('Playing', 'Paused'):
            playing = f"{state['player']}: {state.get('title') or 'unknown track'}"
        elif state.get('player'):
            playing = 'No media player running'
        else:
            playing = 'Install playerctl for media keys'  # Fits the portrait width too.
        d.text((10, vis_bottom+16), fit_text(d, playing, W-20, SMALL), font=SMALL, fill=TEXT)
        volume = state.get('volume')
        bar_y = vis_bottom+33
        d.text((10, bar_y-1), 'VOL', font=TINY, fill=MUTED)
        bar_end = W-70
        d.rounded_rectangle((40, bar_y, bar_end, bar_y+8), radius=3, fill=PANEL)
        if volume is not None:
            d.rectangle((40, bar_y, 40+round((bar_end-40)*max(0, min(100, volume))/100), bar_y+8),
                        fill=MUTED if state.get('muted') else ACCENT)
        label = (state.get('backend', '') if volume is None else
                 f"{volume}%" + (' MUTED' if state.get('muted') else ''))
        d.text((W-10, bar_y-2), fit_text(d, label, 56, TINY), font=TINY,
               fill=WARN if state.get('muted') else TEXT, anchor='ra')
        labels = (('VOL -', 'MUTE', 'VOL +', 'SOURCE'),
                  ('|< PREV', 'PAUSE' if state.get('player') == 'Playing' else 'PLAY', 'NEXT >|', 'FULL'))
        for (y0, y1), row, names in zip(((bottom-54, bottom-29), (bottom-25, bottom)), labels,
                                         AUDIO_BUTTONS):
            for rect, text, name in zip(row_rects(4, y0, y1), row, names):
                button(d, rect, text, active=(text == 'MUTE' and state.get('muted')),
                       action='audio:' + name)
    elif page == 'calconfirm':
        centred(d, 57+dy, 'Recalibrate the touchscreen?', FONT)
        centred(d, 83+dy, 'Only needed if taps miss their targets.', SMALL, MUTED)
        button(d, (12, 121+dy, W//2-9, 174+dy), 'CANCEL', active=True, action='cal:cancel')
        button(d, (W//2+8, 121+dy, W-13, 174+dy), 'START', action='cal:start')
    elif page == 'colours':
        colours = ['white','yellow','cyan','lime','magenta','red','blue','black']
        bar = W/8
        for i, colour in enumerate(colours):
            d.rectangle((round(i*bar), 30+dy, round((i+1)*bar)-1, 104+dy), fill=colour)
            d.text((round((i+.5)*bar), 110+dy), ('W','Y','C','G','M','R','B','K')[i], font=SMALL,
                   fill=TEXT, anchor='ma')
        for x in range(W):
            v = round(x*255/(W-1))
            d.line((x, 133+dy, x, 168+dy), fill=(v,v,v))
        centred(d, 187+dy, 'Full intensity | Tap to return', SMALL)
        return image
    elif page == 'shutdown':
        centred(d, 52+dy, 'Shut down the Raspberry Pi?', FONT)
        centred(d, 78+dy, 'Save your work in VNC first.', SMALL, MUTED)
        centred(d, 96+dy, 'Wait for the current operation...' if extra.get('busy') else
                'Power can be removed once the', SMALL, WARN if extra.get('busy') else MUTED)
        centred(d, 112+dy, 'Then tap SHUT DOWN again.' if extra.get('busy') else
                'green LED stops flashing.', SMALL, MUTED)
        button(d, (12, 132+dy, W//2-9, 185+dy), 'CANCEL', active=True, action='shut:cancel')
        button(d, (W//2+8, 132+dy, W-13, 185+dy), 'SHUT DOWN', fill=DANGER, action='shut:confirm')
    else:
        top = 12 if portrait() else 0
        centred(d, 52+top, 'Close the app or shut down?', FONT)
        centred(d, 80+top, 'Close releases the camera and touch.', SMALL, MUTED)
        centred(d, 98+top, 'Busy: wait before closing.' if extra.get('busy') else
                'Shut down asks once more first.', SMALL, MUTED)
        # Portrait: start below the second line of text, which ends near y=122.
        rects = ([(20, 128+i*43, W-21, 165+i*43) for i in range(3)] if portrait() else
                 row_rects(3, 121, 174, margin=12, gap=11))
        for rect, label, action in zip(rects,
                                       ('CANCEL', 'CLOSE APP', 'SHUT DOWN'),
                                       ('exit:cancel', 'exit:close', 'exit:shutdown')):
            button(d, rect, label, active=(label == 'CANCEL'),
                   fill=DANGER if label == 'SHUT DOWN' else None, action=action)
    for rect, label in zip(tab_rects(), TABS):
        active = TAB_PARENT.get(page, page) == label.lower()
        button(d, rect, label, active, font_=TAB_FONT, action='tab:' + label.lower())
    return image


def wrap_text(draw, value, width, selected_font, max_lines=2):
    """Greedy word wrap; the last allowed line is truncated with ... if needed."""
    lines, current = [], ''
    for word in str(value).split():
        trial = f'{current} {word}'.strip()
        if current and draw.textlength(trial, font=selected_font) > width:
            lines.append(current)
            current = word
        else:
            current = trial
    if current:
        lines.append(current)
    if len(lines) > max_lines:
        lines = lines[:max_lines-1] + [' '.join(lines[max_lines-1:])]
    return [fit_text(draw, line, width, selected_font) for line in lines]


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
    parser.add_argument('--data', help='Data folder (default: ~/cyberdeck-data for the invoking user)')
    parser.add_argument('--rotation', type=int, choices=(0, 90, 180, 270),
                        help='Turn the UI clockwise for this run only (overrides OPTIONS -> Screen)')
    parser.add_argument('--backend',
                        help="cyberdeck-core address (default http://127.0.0.1:8765; 'off' disables)")
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
    data = data_home(args.data, __file__)
    photos = data / 'photos'
    photos.mkdir(parents=True, exist_ok=True)
    user_owns(data)
    user_owns(photos)
    calibration_file = data / 'touch.json'
    options_file = data / 'options.json'
    options = load_options(options_file)
    set_rotation(options['rotation'] if args.rotation is None else args.rotation)
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
    restart_requested = False
    tools_ui = None
    backend = None
    photo_job = Job()
    photo_pending = False
    lapse_shot = False
    def terminate(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, terminate)
    global BACKLIGHT_PWM
    backlight = light = None
    panel_off = False  # Black saver with a PWM backlight: LEDs off and panel blanked.
    try:
        screen = Framebuffer()
        backlight = load_backlight(data)
        BACKLIGHT_PWM = backlight.available
        light = Brightness(screen, backlight)
        light.apply(100)  # Lit for calibration; pages apply the chosen level when drawn.
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
            camera = CameraWorker(start_camera, args.fps)
            if camera is None:
                message = 'Camera unavailable; see terminal'
        page = 'camera' if options['start_page'] == 'camera' and camera else 'home'
        frame, paused, fps = None, False, args.fps
        stats, rows, alerts = Stats(), {}, []
        stats.start()
        if args.backend != 'off':
            backend = Backend(*([args.backend] if args.backend else []))
            backend.start()
        history_range = 3600
        next_stats, next_draw = 0, 0
        frame_sequence = -1

        def wait_for_touch(until, cap):
            """Idle until `until` (at most `cap` s), returning early on any touch input.
            Replaces a fixed 50 Hz poll: an idle page now wakes ~4x a second, black ~1x."""
            timeout = min(max(0., until - time.monotonic()), cap)
            waiter = getattr(touch, 'wait', None)
            if waiter is not None:
                waiter(timeout)
            else:
                time.sleep(timeout)
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

        try:
            scores = json.loads(games_file.read_text())
            scores = {k: v for k, v in scores.items() if type(v) is int and v >= 0}
        except (OSError, ValueError, TypeError, AttributeError):
            scores = {}

        def score_saver(key):
            """All games share games.json; each save keeps the other games' records."""
            def save(value):
                scores[key] = value
                try:
                    temporary = games_file.with_suffix('.tmp')
                    temporary.write_text(json.dumps(scores))
                    user_owns(temporary)
                    temporary.replace(games_file)
                except OSError as exc:
                    print('Could not save high score:', exc, flush=True)
            return save
        audio, capture = AudioControl(), AudioCapture()
        audio.start()
        album_show = Slideshow(options, photos, album_dir, library)
        gif_player, saver_gifs = GifPlayer(gif_dir), GifPlayer(gif_dir)
        players.extend((gif_player, saver_gifs))
        bunny_frames = custom_bunny_frames(data)  # None: random built-in breeds and coats.
        apps = {'snake': SnakeApp(scores.get('snake_best', 0), score_saver('snake_best')),
                'tetris': TetrisApp(scores.get('tetris_best', 0), score_saver('tetris_best')),
                'pong': PongApp(scores.get('pong_wins', 0), score_saver('pong_wins')),
                'bricks': BrickApp(scores.get('bricks_best', 0), score_saver('bricks_best')),
                'flappy': FlappyApp(scores.get('flappy_best', 0), score_saver('flappy_best')),
                'invaders': InvadersApp(scores.get('invaders_best', 0), score_saver('invaders_best')),
                'mines': MinesApp(scores.get('mines_best_seconds', 0), score_saver('mines_best_seconds')),
                'solitaire': SolitaireApp(scores.get('solitaire_fewest_moves', 0),
                                          score_saver('solitaire_fewest_moves')),
                'sudoku': SudokuApp(scores.get('sudoku_best_seconds', 0),
                                    score_saver('sudoku_best_seconds')),
                'album': AlbumApp(album_show, cycle_option),
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

        def apply_rotation():
            """Re-lay out everything for a new Screen setting (calibration is unaffected)."""
            nonlocal next_draw
            set_rotation(options['rotation'])
            album_show.previous = None  # A crossfade cannot mix two canvas sizes.
            for player in players:
                player.close()
            next_draw = 0

        def open_app(name, now):
            if name in ('tools', 'quick'):
                tools_ui.page = 'quick' if name == 'quick' else 'tools'
                return 'tools'
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
            return {'tools': 'Timers, network, GNSS and more', 'quick': 'Camera, sound, sleep',
                    'audio': 'Volume, media, visualizer',
                    'games': '9 games: arcade and puzzles',
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
                light.apply(options['pixel_level'])
                screen.show(apps[page].render(now, datetime.now().astimezone(), alerts))
                return apps[page].delay
            extra = None
            if page == 'home':
                wifi = rows.get('wifi', '')
                signal = re.search(r'(-?\d+) dBm', wifi)
                extra = {'home': {
                    'wifi': f'Wi-Fi {signal[1]} dBm' if signal else 'No Wi-Fi',
                    'camera': ('Live view' if camera is not None and camera.ready
                               else 'Connecting...' if camera is not None else 'Off'),
                    'photos': f'{len(library.items)} saved',
                    'games': f'{len(GAME_PAGES)} games',
                    'audio': (f"Vol {audio.state['volume']}%"
                              if audio.state.get('volume') is not None else 'Music, volume'),
                    'album': 'Slideshow', 'gifs': 'Animations', 'tools': 'Timers & more',
                    'quick': 'Shortcuts', 'ask': apps['ask'].note()}}
            elif page == 'apps':
                extra = {'notes': app_notes()}
            elif page == 'audio':
                extra = {'audio': audio.state, 'capture': capture}
            elif page in ('exit', 'shutdown'):
                extra = {'busy': photo_pending or tools_ui.job.busy}
            if page == 'camera':
                thumb = library.preview(library.latest(), (60, 45))[0]
                countdown = (max(1, math.ceil(countdown_end-now))
                             if countdown_end is not None else None)
                cam = {'thumb': thumb, 'countdown': countdown, 'capturing': capturing or ('Saving...' if photo_pending else None),
                       'flash': now < flash_until,
                       'saved': saved_text if now < saved_until and not capturing else ''}
            else:
                cam = None
            photo = photo_state() if page in ('photos', 'photofull', 'photodelete') else None
            text = {'options': option_message, 'savers': option_message,
                    'photos': photo_message}.get(page, message)
            if page == 'camera' and camera is not None and not camera.ready:
                text = camera.error or 'Connecting camera...'
            light.apply(100 if page == 'colours' else options['pixel_level'])
            screen.show(render(page, frame, paused, fps, rows, text, stats.history, now,
                               options, alerts=alerts, cam=cam, photo=photo, extra=extra))
            return 1/15 if page == 'audio' else 1. if page in ('apps', 'home', 'history') else .25

        def take_photo(timelapse=False):
            """Schedule one capture/save; publish results on the UI thread."""
            nonlocal photo_pending, lapse_shot, message
            if photo_pending:
                return False
            full = timelapse or (options['photo_size'] == 'full' and not paused)
            angle = options['camera_turn']
            def save_image(image):
                # Separate library instance keeps UI caches owned by the UI thread.
                path = PhotoLibrary(photos).save(image)
                return path, image.size
            if full:
                if camera is None or not camera.snapshot(
                        lambda driver: save_image(turn_image(driver.capture_full(), angle))):
                    message = 'Camera busy or unavailable; see Tools > Recovery'
                    return False
            else:
                if frame is None:
                    message = 'No preview frame to save'
                    return False
                image = frame.copy()
                if not photo_job.start(lambda: save_image(image)):
                    return False
            photo_pending, lapse_shot = True, timelapse
            message = 'Capturing in background...'
            if backend is not None:
                backend.mark('camera', 'timelapse' if timelapse else 'photo')
            return True

        def retry_camera():
            nonlocal camera
            if camera is None:
                camera = CameraWorker(start_camera, fps)
            else:
                camera.retry()

        def apply_preset(value):
            nonlocal fps
            if not isinstance(value, dict) or not isinstance(value.get('options'), dict):
                return 'Invalid preset'
            previous = dict(options)
            for key, item in value['options'].items():
                allowed = OPTION_VALUES.get(key, ())
                if allowed and type(item) is type(allowed[0]) and item in allowed:
                    options[key] = item
            try:
                save_options(options_file, options)
            except OSError:
                options.clear(); options.update(previous)
                return 'Could not save preset settings'
            apply_rotation()
            if value.get('fps') in (10, 15, 20):
                fps = value['fps']
                if camera: camera.set_fps(fps)
            volume = value.get('volume')
            if type(volume) is int and 0 <= volume <= 100:
                audio.request('volume:' + str(volume))
            if type(value.get('muted')) is bool:
                audio.request('muted:' + str(int(value['muted'])))
            return 'Preset applied'

        tools_ui = ToolsUI(sys.modules[__name__], data, Path(__file__).resolve().parent,
                           library, options, audio,
                           {'snap': take_photo, 'busy': lambda: photo_pending,
                            'fps': lambda: fps, 'preset': apply_preset, 'retry': retry_camera})
        apps['tools'] = tools_ui
        apps['ask'] = AskApp(sys.modules[__name__], backend.base if backend is not None else None)
        apps['games'] = GamesMenu(lambda: {
            'snake': f"Best score: {apps['snake'].best}",
            'tetris': f"Best score: {apps['tetris'].best}",
            'pong': f"Games won: {apps['pong'].wins}",
            'bricks': f"Best score: {apps['bricks'].best}",
            'flappy': f"Best score: {apps['flappy'].best}",
            'invaders': f"Best score: {apps['invaders'].best}",
            'mines': f"Best time: {clock_text(apps['mines'].best) if apps['mines'].best else '--'}",
            'solitaire': f"Fewest moves: {apps['solitaire'].best or '--'}",
            'sudoku': f"Best time: {clock_text(apps['sudoku'].best) if apps['sudoku'].best else '--'}"})

        state = boot_service_enabled()
        BOOT_LABEL[0] = '?' if state is None else 'On' if state else 'Off'
        print('Ready. Tap buttons or press Ctrl+C to exit. Photos:', photos, flush=True)
        while running:
            now = time.monotonic()
            for raw in touch.poll():
                if now < ignore_touch_until:
                    continue
                last_touch = now
                x, y = to_logical(*(np.array([*raw, 1.0]) @ matrix))
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
                if tools_ui.clocks.alarm and y < 24:
                    tools_ui.clocks.alarm = False
                    alerts = [a for a in alerts if a[1] != 'TIMER DONE - tap to dismiss']
                    next_draw = 0
                    break
                if page == 'colours':
                    page = 'options' if page == 'colours' else 'photos'
                    next_draw = 0
                    ignore_touch_until = now + .3
                    break
                if not (0 <= x < W and 0 <= y < H):
                    continue
                if page in apps:
                    result = apps[page].tap(x, y, now)
                    if result == 'restart':
                        if not tools_ui.job.busy and not photo_pending:
                            restart_requested, running = True, False
                        else:
                            tools_ui.message = 'Wait for the current operation'
                    elif result == 'shutdown':
                        exit_return_page, page = page, 'shutdown'
                    elif result == 'saver':
                        active_saver, next_saver_draw = make_saver(options['saver_style'], now), 0
                    elif result in ('camera', 'photos'):
                        page = result
                    elif result in GAME_PAGES:  # Chosen in the Games menu.
                        page = result
                        ignore_touch_until = now + .3
                    elif result == 'exit':
                        if page == 'gifs':
                            gif_player.close()
                        page = 'audio' if page == 'visual' else 'games' if page in GAME_PAGES else 'home'
                        ignore_touch_until = now + .3
                    else:
                        ignore_touch_until = now + .12
                    next_draw = 0
                    break
                action = hit(x, y) if HOTSPOTS_PAGE[0] == page else None
                kind, _, arg = (action or '').partition(':')
                if kind == 'banner':
                    page = 'health'
                elif kind == 'cal':
                    if arg == 'cancel':
                        page = 'options'
                    else:
                        light.apply(100)
                        matrix, option_message = recalibrate_safely(screen, touch, matrix, save_calibration)
                        page = 'options'
                        last_touch = time.monotonic()
                        ignore_touch_until = last_touch + .5
                elif kind == 'saver':
                    index = int(arg)
                    if index == 0:
                        page = 'saverstyle'
                    elif index < 7:
                        option_message = cycle_option({1:'saver_delay',2:'saver_speed',3:'saver_level',
                                                       4:'bunny_count',5:'slide_seconds',
                                                       6:'clock_format'}[index])
                    else:
                        active_saver, next_saver_draw = make_saver(options['saver_style'], now), 0
                    ignore_touch_until = now + .3
                elif kind == 'style':
                    option_message = set_option('saver_style', arg)
                    page = 'savers'
                    ignore_touch_until = now + .3
                elif kind == 'app':
                    page = open_app(arg, now)
                    ignore_touch_until = now + .3
                elif kind == 'home':
                    if arg == 'camera':
                        page = 'camera'
                    elif arg == 'photos':
                        library.refresh()
                        photo_index = len(library.items)-1
                        photo_message = ''
                        page = 'photos'
                    else:
                        page = open_app(arg, now)
                    ignore_touch_until = now + .3
                elif kind == 'audio':
                    if arg == 'source':
                        cycle_option('vis_source')
                    elif arg == 'full':
                        page = 'visual'
                        ignore_touch_until = now + .3
                    else:
                        audio.request(arg)
                elif kind == 'opt':
                    index = int(arg)
                    key = {0:'pixel_level', 5:'history_seconds', 6:'photo_size', 7:'alerts',
                           8:'rotation', 9:'camera_turn'}.get(index)
                    if key:
                        option_message = cycle_option(key)
                        if key == 'rotation':
                            apply_rotation()
                            ignore_touch_until = now + .6  # The layout just moved.
                    elif index == 1:
                        page = 'savers'
                        option_message = ''
                    elif index == 2:
                        option_message = set_boot_service(BOOT_LABEL[0] != 'On', __file__, data)
                        state = boot_service_enabled()
                        BOOT_LABEL[0] = '?' if state is None else 'On' if state else 'Off'
                    elif index == 3:
                        page = 'colours'
                    elif index == 4:
                        page = 'calconfirm'
                    ignore_touch_until = max(ignore_touch_until, now + .3)
                elif kind == 'status':
                    page = 'history' if arg == 'history' else 'health'
                elif kind == 'hist':
                    history_range = int(arg)
                    if backend is not None:
                        backend.want_history(history_range)
                        rows['history_range'] = history_range
                elif kind == 'exit':
                    if arg == 'cancel':
                        page = exit_return_page
                    elif arg == 'close':
                        if not photo_pending and not tools_ui.job.busy:
                            running = False
                    else:
                        page = 'shutdown'
                        ignore_touch_until = now + .5
                elif kind == 'shut':
                    if arg == 'cancel':
                        page = exit_return_page
                    else:
                        if not photo_pending and not tools_ui.job.busy:
                            shutdown_requested = True
                            running = False
                elif kind == 'del':
                    if arg == 'confirm':
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
                elif kind == 'tab':
                    if arg == 'exit':
                        if page not in ('exit', 'shutdown'):
                            exit_return_page = page
                        page = 'exit'
                        ignore_touch_until = time.monotonic() + .35
                    else:
                        if arg == 'photos':
                            library.refresh()
                            photo_index = len(library.items)-1
                            photo_message = ''
                        elif arg == 'options':
                            option_message = ''
                            ignore_touch_until = now + .3
                        page = arg
                elif kind == 'cam':
                    if arg == 'snap':
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
                    elif arg == 'timer':
                        cycle_option('snap_timer')
                        message = ('Self-timer: ' + (f"{options['snap_timer']} s" if options['snap_timer']
                                                     else 'off'))
                    elif arg == 'pause' and camera:
                        paused = not paused
                        message = 'Preview paused | SNAP saves this frame' if paused else ready_message
                    elif arg == 'fps' and camera:
                        fps = {10: 15, 15: 20, 20: 10}[fps]
                        camera.set_fps(fps)
                        message = f'Camera target: {fps} FPS'
                    elif arg == 'last' and library.items:
                        library.refresh()
                        photo_index = len(library.items)-1
                        photo_message = ''
                        page = 'photos'
                elif kind == 'photo' and library.items:
                    if arg == 'older':
                        photo_index = max(0, photo_index-1)
                    elif arg == 'newer':
                        photo_index = min(len(library.items)-1, photo_index+1)
                    elif arg == 'full':
                        if tools_ui.open_zoom(current_photo()):
                            page = 'tools'
                    elif arg == 'delete':
                        page = 'photodelete'
                        ignore_touch_until = now + .35
                    photo_message = ''
                next_draw = 0
                break  # Avoid a second queued tap triggering a newly opened screen.
            if not running:
                break
            now = time.monotonic()
            if page in apps and hasattr(apps[page], 'hold') and active_saver is None:
                # Games follow a finger while it is down (Pong paddle, Tetris auto-repeat).
                raw = getattr(touch, 'current', lambda: None)()
                apps[page].hold(None if raw is None else
                                to_logical(*(np.array([*raw, 1.0]) @ matrix)), now)
            completed = photo_job.consume()
            if camera:
                try:
                    completed = camera.results.get_nowait()
                except queue.Empty:
                    pass
            if completed is not None:
                result, error = completed
                photo_pending = False
                if lapse_shot:
                    tools_ui.lapse.completed(now, error)
                    lapse_shot = False
                if error:
                    message = 'Photo failed: ' + error
                    print(message, flush=True)
                else:
                    path, size = result
                    library.refresh()
                    photo_index = len(library.items)-1
                    saved_text = f'{size[0]}x{size[1]}'
                    message = 'Saved ' + path.name
                    flash_until, saved_until = now+.15, now+1.8
                tools_ui.message = message
                next_draw = 0
            tools_ui.tick(now, rows, camera)
            if now >= next_stats:
                rows = stats.read()
                if backend is not None:
                    backend.want_history(history_range if page == 'history' else None)
                    rows.update(busy=backend.busy, backend_online=backend.online,
                                history=backend.history, history_range=history_range)
                alerts = health_alerts(rows, now) if options['alerts'] == 'on' else []
                next_stats = now + 1
            alerts = [a for a in alerts if a[1] != 'TIMER DONE - tap to dismiss']
            if tools_ui.clocks.alarm:
                alerts.insert(0, ('warning', 'TIMER DONE - tap to dismiss'))
                if active_saver:
                    active_saver.close()
                    active_saver = None
                    next_draw = 0
                last_touch = now
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
            audio.active = listening or (page == 'tools' and tools_ui.page in ('quick', 'presets', 'recovery'))
            if listening:
                capture.ensure(options['vis_source'])
            elif capture.source is not None:
                capture.stop()
            if (active_saver is None and countdown_end is None
                    and not photo_pending and not tools_ui.clocks.alarm
                    and not (page == 'tools' and tools_ui.confirm)
                    and not (page in GAME_PAGES and apps[page].running)
                    and not (page == 'ask' and apps['ask'].running)
                    and saver_due(options, now-last_touch, page)):
                active_saver, next_saver_draw = make_saver(options['saver_style'], now), 0
            if active_saver is not None:
                if camera:
                    camera.preview = False  # The sensor stops after the worker's idle delay.
                if now >= next_saver_draw:
                    image, delay = active_saver.render(now, datetime.now().astimezone(), alerts)
                    if active_saver.style == 'black' and backlight.available:
                        # Really off: black frame, LEDs at 0%, panel blanked if the driver can.
                        screen.show(image)
                        backlight.set(0)
                        getattr(screen, 'set_blank', lambda blank: False)(True)
                        panel_off = True
                    else:
                        light.apply(min(options['pixel_level'], options['saver_level']))
                        screen.show(image)
                    next_saver_draw = now + delay
                # Nothing to animate while off: wake once a second (timers, alerts) or on touch.
                wait_for_touch(next_saver_draw, 1. if panel_off else .25)
                continue
            if panel_off:  # Woken: unblank first; the redraw below restores the brightness.
                getattr(screen, 'set_blank', lambda blank: False)(False)
                panel_off = False
                next_draw = 0
            live = page == 'camera' and camera is not None and not paused
            if camera:
                camera.preview = live and active_saver is None
            new_frame = False
            if live and camera.frame() is not None and camera.sequence != frame_sequence:
                frame_sequence = camera.sequence
                frame = turn_image(camera.frame(), options['camera_turn'])
                new_frame = True
            if new_frame or now >= next_draw:
                # Redraw for each new camera frame, otherwise only when the page asks to.
                next_draw = now + draw(now)
            # Live: check for the next camera frame at twice its rate. Otherwise sleep until
            # the next redraw or a touch; quick check-ins only while a photo is in flight.
            wait_for_touch(min(next_draw, now + .5/fps) if live else next_draw,
                           .05 if photo_pending else .25)
    except KeyboardInterrupt:
        print('\nStopped.', flush=True)
        shutdown_requested = False
    finally:
        if tools_ui is not None:
            tools_ui.close()
        if photo_job.thread and photo_job.thread.is_alive():
            photo_job.thread.join(timeout=15)
        if stats is not None:
            stats.close()
        if backend is not None:
            backend.close()
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
                if panel_off:
                    getattr(screen, 'set_blank', lambda blank: False)(False)
                if light is not None:
                    light.apply(100)  # Leave the panel readable once the app has stopped.
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
    if restart_requested:
        # Older revisions also understand --data; keep rollback on the same library.
        arguments = list(sys.argv)
        if args.data is None:
            arguments.extend(['--data', str(data)])
        os.execv(sys.executable, [sys.executable, *arguments])
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
