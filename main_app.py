#!/usr/bin/env python3
"""Main app: the cyberdeck's full-screen home on the 5" HDMI screen (1024x600).

It draws straight to the framebuffer like the quick panel (panel_app.py), with no desktop,
so Ollama keeps the memory. While both share one screen only one runs at a time: QUICK PANEL
hands the screen to the quick panel, and its EXIT page's MAIN APP button hands it back
(docs/MAIN-APP.md). ASK and WIKIPEDIA are drawn here (panel_tools/main_pages.py); apps
not moved here yet open in the quick panel."""
import argparse
import json
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

import panel_app as panel
from panel_app import ACCENT, BG, BLUE, CYAN, DANGER, GREEN, MUTED, ORANGE, PURPLE, RED, TEXT, WARN, YELLOW, font
from panel_tools.backend import Backend
from panel_tools.keyboard import Keyboard
from panel_tools.main_pages import AskPage, WikiPage
from panel_tools.radar_page import FAN, RadarPage
from panel_tools.runtime import data_home

HERE = Path(__file__).resolve().parent
MIN_SIZE = (640, 400)  # Smaller screens get the quick panel instead.
HEADER = 56
FOOTER = 34
TITLE, LABEL, BODY, SMALL = font(26, bold=True), font(28, bold=True), font(20), font(16)
BIG = font(40, bold=True)
READ, READ_LINE, SMALL_BOLD = font(22), 31, font(18, bold=True)  # Article and answer text.
TILE_FONTS = (LABEL, TITLE, font(22, bold=True), SMALL_BOLD)
# (label, key, colour, where): where is 'here' for a page drawn by this app, or the quick
# panel page that opens it until it moves here.
TILES = (('ASK', 'ask', '#ff8fb8', 'here'), ('WIKIPEDIA', 'wiki', ACCENT, 'here'),
         ('PLANNER', 'planner', BLUE, 'planner'), ('TOOLS', 'tools', GREEN, 'tools'),
         ('GAMES', 'games', YELLOW, 'games'), ('CAMERA', 'camera', CYAN, 'camera'),
         ('PHOTOS', 'photos', PURPLE, 'photos'), ('STATUS', 'status', ORANGE, 'here'),
         ('QUICK PANEL', 'quick', MUTED, 'home'), ('CLOSE', 'close', RED, 'here'),
         ('RADAR', 'radar', FAN, 'here'))
TILE_KEYS = '1234567890R'  # R is also handled on its own, so lower-case r works too.
NOTES = {'ask': 'Questions answered from the offline library', 'wiki': 'Search and read offline articles',
         'status': 'Temperature, memory, disk', 'quick': 'The small panel: camera, savers',
         'close': 'Back to the console', 'radar': 'Movement around the deck'}
LATER = 'In the quick panel for now'
COLS = 4


class Screen(panel.Framebuffer):
    """The whole screen at its own resolution. Touch calibration is saved in the quick
    panel's coordinates (its page scaled up and centred), so from_panel() maps taps
    through that same placement."""
    def __init__(self, base=None, device=None, display='hdmi'):
        super().__init__(base, device, display)
        self.panel = (self.scale, self.left, self.top)
        width, height = (int(v) for v in (self.base / 'virtual_size').read_text().strip().split(','))
        if self.spi or width < MIN_SIZE[0] or height < MIN_SIZE[1]:
            self.close()
            raise RuntimeError(f'The screen is {width}x{height}; the main app needs at least '
                               f'{MIN_SIZE[0]}x{MIN_SIZE[1]} (the 2.8" panel runs the quick panel).')
        self.size, self.scale, self.left, self.top = (width, height), 1, 0, 0

    def from_panel(self, px, py):
        scale, left, top = self.panel
        return left + px*scale, top + py*scale

    def show(self, image):
        """Send only the rows that changed since the last frame (all of them now and then)."""
        rgb = np.asarray(image.convert('RGB'))
        now = time.monotonic()
        first, end = 0, rgb.shape[0]
        if self.last is not None and now - self.refreshed < self.FULL_REFRESH:
            changed = np.flatnonzero((rgb != self.last).any(axis=(1, 2)))
            if not len(changed):
                return
            first, end = int(changed[0]), int(changed[-1]) + 1
        else:
            self.refreshed = now
        self.write(first * self.stride, self.pixels(image.crop((0, first, image.width, end))).tobytes())
        self.last = rgb
        self.rows_sent += end - first


def tile_rects(width, height):
    """Four columns by three rows under the header, one rect per tile (the last cell stays empty)."""
    margin, gap, cols, rows = 20, 14, COLS, 3
    top, bottom = HEADER + 16, height - FOOTER - 8
    w = (width - 2*margin - (cols-1)*gap) / cols
    h = (bottom - top - (rows-1)*gap) / rows
    return [(round(margin + c*(w+gap)), round(top + r*(h+gap)),
             round(margin + c*(w+gap) + w), round(top + r*(h+gap) + h))
            for r in range(rows) for c in range(cols)][:len(TILES)]


def wrap(d, text, width, chosen):
    lines, line = [], ''
    for word in text.split():
        trial = f'{line} {word}'.strip()
        if line and d.textlength(trial, font=chosen) > width:
            lines.append(line)
            line = word
        else:
            line = trial
    return lines + [line] if line else lines


def header(d, width, title, alerts, clock):
    d.rectangle((0, 0, width, HEADER-1), fill='#0b1016')
    d.line((0, HEADER-1, width, HEADER-1), fill='#1f2b36')
    d.text((20, HEADER//2), title, font=TITLE, fill=ACCENT, anchor='lm')
    d.text((width-20, HEADER//2), clock, font=TITLE, fill=TEXT, anchor='rm')
    x = width - 20 - d.textlength(clock, font=TITLE) - 24
    for level, text in reversed(alerts):
        d.text((x, HEADER//2), text, font=SMALL, fill=DANGER if level == 'critical' else WARN, anchor='rm')
        x -= d.textlength(text, font=SMALL) + 16


def footer(d, width, height, text):
    d.text((20, height - FOOTER//2 - 4), text, font=SMALL, fill=MUTED, anchor='lm')


def render(page, size, state, now=None):
    """The whole screen for `page` ('home' or 'status'). `state` holds 'alerts', 'stats',
    'backend' (True, False or None), 'busy', 'selected' and 'message'."""
    width, height = size
    now = now or datetime.now()
    image = Image.new('RGB', size, BG)
    d = ImageDraw.Draw(image)
    alerts = state.get('alerts', [])
    if page == 'status':
        header(d, width, 'STATUS', alerts, now.strftime('%H:%M'))
        draw_status(d, width, height, state)
        footer(d, width, height, 'Esc or tap anywhere: home')
        return image
    header(d, width, 'CYBERDECK', alerts, now.strftime('%H:%M'))
    for i, (rect, (label, key, colour, where)) in enumerate(zip(tile_rects(width, height), TILES)):
        chosen = i == state.get('selected')
        d.rounded_rectangle(rect, 10, fill='#101820' if chosen else '#0b1016',
                            outline=colour if chosen else '#1f2b36', width=3 if chosen else 2)
        d.text((rect[0]+16, rect[1]+14), TILE_KEYS[i], font=SMALL, fill=MUTED)
        name_font = next((f for f in TILE_FONTS if d.textlength(label, font=f) < rect[2]-rect[0]-20),
                         TILE_FONTS[-1])
        d.text(((rect[0]+rect[2])//2, rect[1] + (rect[3]-rect[1])*.45), label, font=name_font,
               fill=colour, anchor='mm')
        note = NOTES.get(key, LATER if where != 'here' else '')
        for n, line in enumerate(wrap(d, note, rect[2]-rect[0]-20, SMALL)[:2]):
            d.text(((rect[0]+rect[2])//2, rect[1] + (rect[3]-rect[1])*.68 + n*20), line, font=SMALL,
                   fill=MUTED, anchor='mm')
    footer(d, width, height, state.get('message') or
           'Keys 1-0 or arrows and Enter open a tile. R: radar. Q: quick panel. Esc: home.')
    return image


def draw_status(d, width, height, state):
    s = state.get('stats') or {}
    left, top = 40, HEADER + 30
    rows = (('Temperature', s.get('temp', '-')), ('CPU', f"{s['cpu']:.0f}%" if s.get('cpu') is not None else '-'),
            ('Memory', s.get('ram', '-')), ('Disk free', s.get('disk_free', '-')),
            ('Clock speed', s.get('freq', '-')), ('Uptime', s.get('uptime', '-')))
    for n, (name, value) in enumerate(rows):
        y = top + n*52
        d.text((left, y), name, font=BODY, fill=MUTED)
        d.text((left + 220, y - 6), str(value), font=BIG if n == 0 else LABEL, fill=TEXT)
    x = width//2 + 40
    online = state.get('backend')
    d.text((x, top), 'Backend', font=BODY, fill=MUTED)
    d.text((x + 160, top - 4), {True: 'running', False: 'not running', None: 'checking...'}[online],
           font=LABEL, fill=GREEN if online else WARN)
    busy = state.get('busy') or []
    d.text((x, top + 52), 'Busy', font=BODY, fill=MUTED)
    d.text((x + 160, top + 48), ', '.join(busy) if busy else 'idle', font=LABEL, fill=TEXT)
    d.text((x, top + 104), 'Alerts', font=BODY, fill=MUTED)
    alerts = state.get('alerts') or []
    for n, (level, text) in enumerate(alerts[:4] or [('ok', 'none')]):
        d.text((x + 160, top + 100 + n*34), text, font=LABEL if not alerts else BODY,
               fill={'critical': DANGER, 'warning': WARN}.get(level, TEXT))


def tile_at(size, x, y):
    for i, rect in enumerate(tile_rects(*size)):
        if rect[0] <= x <= rect[2] and rect[1] <= y <= rect[3]:
            return i
    return None


def move(selected, key, count=len(TILES), cols=None):
    """Arrow keys walk the tile grid; the first arrow press just shows the highlight."""
    if selected is None:
        return 0
    cols = cols or COLS
    step = {'left': -1, 'right': 1, 'up': -cols, 'down': cols}[key]
    target = selected + step
    return target if 0 <= target < count else selected


def quick_panel_command(where, display, data=None):
    """Arguments that start the quick panel on `where` and bring this app back on exit."""
    command = [sys.executable, str(HERE / 'panel_app.py'), '--display', display, '--main-app']
    if where != 'home':
        command += ['--open', where]
    if data:
        command += ['--data', str(data)]
    return command


def load_matrix(path, touch):
    """The quick panel's saved calibration for this touch device, or None."""
    try:
        saved = json.loads(Path(path).read_text())
        if saved['device'] == touch.signature:
            matrix = np.asarray(saved['matrix'], dtype=float)
            if matrix.shape == (3, 2) and np.isfinite(matrix).all():
                return matrix
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--display', choices=('hdmi', 'auto'), default='hdmi')
    parser.add_argument('--touch', help='Override touchscreen event device')
    parser.add_argument('--data', help='Data folder (default: ~/cyberdeck-data for the invoking user)')
    parser.add_argument('--backend', help="cyberdeck-core address (default http://127.0.0.1:8765; 'off' disables)")
    args = parser.parse_args()
    import fcntl
    # The quick panel's lock: while both share one screen, touch and keys, only one may run.
    lock = open('/run/lock/cyberdeck-camera-monitor.lock', 'a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise RuntimeError('The quick panel or the main app is already running. Stop it first '
                           f'(boot service: sudo systemctl stop {panel.SERVICE}).')
    data = data_home(args.data, panel.__file__)

    def terminate(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, terminate)
    screen = touch = keyboard = backend = stats = None
    apps = {}
    handoff = None
    try:
        screen = Screen(display=args.display)
        try:
            touch = panel.Touch(args.touch, prefer_spi=False)
        except Exception as exc:  # Keyboard only; the quick panel explains touch problems.
            print('Touch unavailable:', exc, flush=True)
        keyboard = Keyboard(skip=[getattr(getattr(touch, 'device', None), 'path', None)])
        matrix = load_matrix(data / 'touch.json', touch) if touch else None
        stats = panel.Stats()
        stats.start()
        if args.backend != 'off':
            backend = Backend(*([args.backend] if args.backend else []))
            backend.start()
        page, state = 'home', {'selected': None, 'message': ''}
        base = backend.base if backend is not None else None
        apps = {'ask': AskPage(sys.modules[__name__], base, size=screen.size),
                'wiki': WikiPage(sys.modules[__name__], base, size=screen.size),
                'radar': RadarPage(sys.modules[__name__], base, size=screen.size)}
        if touch and matrix is None:
            state['message'] = 'Touch is not calibrated yet: press Q, then calibrate in the quick panel.'
        next_stats = 0

        def open_tile(index):
            nonlocal page, handoff
            label, key, colour, where = TILES[index]
            if key == 'status' or key in apps:
                page = key
            elif key == 'close':
                return False
            else:
                handoff = quick_panel_command(where, args.display, args.data)
                return False
            return True

        running = True
        while running:
            now = time.monotonic()
            if now >= next_stats:
                state['stats'] = stats.read()
                state['backend'] = None if backend is None else backend.online
                state['alerts'] = panel.health_alerts(state['stats'])
                state['busy'] = backend.busy if backend is not None else []
                next_stats = now + 5
            app = apps.get(page)
            if app is not None:
                screen.show(app.render(now, datetime.now(), state['alerts']))
            else:
                screen.show(render(page, screen.size, state))
            waiting = [touch.device] if touch else []
            panel.select.select(waiting + keyboard.devices, [], [], app.delay if app else 1.)
            for key in keyboard.poll():
                app = apps.get(page)
                if key == 'quit':
                    running = False
                elif app is not None:
                    if app.key(key, time.monotonic()) == 'exit':
                        page = 'home'
                elif key == 'esc':
                    page, state['selected'] = 'home', None
                elif page == 'status':
                    page = 'home'
                elif key in ('r', 'R'):
                    page = 'radar'
                elif key in ('q', 'Q'):
                    handoff = quick_panel_command('home', args.display, args.data)
                    running = False
                elif key in TILE_KEYS:
                    running = open_tile(TILE_KEYS.index(key))
                elif key in ('left', 'right', 'up', 'down'):
                    state['selected'] = move(state['selected'], key)
                elif key == 'enter' and state['selected'] is not None:
                    running = open_tile(state['selected'])
            for raw in (touch.poll() if touch else []):
                if matrix is None:
                    continue
                px, py = np.append(raw, 1) @ matrix
                x, y = screen.from_panel(px, py)
                if page in apps:
                    if apps[page].tap(x, y, time.monotonic()) == 'exit':
                        page = 'home'
                    continue
                if page == 'status':
                    page = 'home'
                    continue
                index = tile_at(screen.size, x, y)
                if index is not None:
                    state['selected'] = index
                    running = open_tile(index) and running
    except KeyboardInterrupt:
        pass
    finally:
        if apps.get('ask') is not None and apps['ask'].running:
            apps['ask'].stop()  # Drops the connection, which stops the model.
        for thing in (backend, stats, keyboard, touch, screen):
            if thing is not None:
                try:
                    (getattr(thing, 'stop', None) or thing.close)()
                except Exception as exc:
                    print('Cleanup:', exc, flush=True)
        lock.close()
    if handoff:
        os.execv(handoff[0], handoff)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        raise SystemExit(f'Main app stopped: {exc}')
