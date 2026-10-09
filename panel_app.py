#!/usr/bin/env python3
"""320x240 RGB565 camera + system monitor for Eli's ILI9341 Pi.

Run: sudo python3 panel_app.py
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
import threading
import re
import math
from datetime import datetime
from collections import deque

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps

W, H = 320, 240
BG, PANEL, TEXT, MUTED, ACCENT = '#101923', '#253747', '#eef5fa', '#a9bbc9', '#44d4ad'
DEFAULT_OPTIONS = {'pixel_level': 100, 'history_seconds': 60,
                   'saver_style': 'off', 'saver_delay': 60,
                   'saver_speed': 'normal', 'saver_level': 70}
OPTION_VALUES = {'pixel_level': (100, 70, 40), 'history_seconds': (30, 60, 120),
                 'saver_style': ('radar', 'stars', 'black', 'off'),
                 'saver_delay': (30, 60, 300), 'saver_speed': ('slow', 'normal', 'fast'),
                 'saver_level': (100, 70, 40)}


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
        return {'power': 'Unknown', 'limits': 'Unknown', 'prior_alerts': 'Unknown'}
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
            'prior_alerts': 'Yes, since boot' if flags & 0xf0000 else 'None since boot'}


class HealthPoller:
    """Bounded slow commands run away from the camera/touch rendering loop."""
    def __init__(self):
        self.snapshot = {'ip': 'Checking...', 'wifi': 'Checking...', 'sync': 'Unknown',
                         **decode_throttle('')}
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

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
            temp = f"{int(Path('/sys/class/thermal/thermal_zone0/temp').read_text())/1000:.1f} C"
        except (OSError, ValueError):
            temp = 'Unavailable'
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
                'disk_free': f'{disk.free/2**30:.1f} GiB',
                'disk_used_percent': 100*disk.used/disk.total,
                'uptime': f'{seconds//3600}h {(seconds//60)%60}m', 'freq': freq,
                **self.health.snapshot}


def button(d, rect, label, active=False):
    d.rounded_rectangle(rect, radius=5, fill=ACCENT if active else PANEL)
    d.text(((rect[0]+rect[2])//2, (rect[1]+rect[3])//2), label,
           font=SMALL, fill=BG if active else TEXT, anchor='mm')


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


def screensaver_frame(style, elapsed, speed='normal', wall_time=None):
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
    d.text((8,7),label,font=SMALL,fill=colour)
    d.text((312,7),wall_time.strftime('%H:%M'),font=SMALL,fill=colour,anchor='ra')
    d.text((160,226),'SCREENSAVER / TAP TO WAKE',font=TINY,fill=colour,anchor='mm')
    return image


def saver_due(options, idle_seconds, page):
    return (options['saver_style'] != 'off' and idle_seconds >= options['saver_delay']
            and page not in ('exit','calconfirm','savers','colours'))


def saver_control_at(x, y):
    for i in range(8):
        col,row = i%2,i//2
        if 8+col*156 <= x <= 155+col*156 and 30+row*40 <= y <= 64+row*40:
            return i
    return None


def render(page, frame, paused, fps, rows, message, history=(), now=None,
           options=None, wall_time=None):
    options = options or DEFAULT_OPTIONS
    wall_time = wall_time or datetime.now().astimezone()
    image = Image.new('RGB', (W, H), BG)
    d = ImageDraw.Draw(image)
    title = {'calconfirm':'TOUCH SETUP', 'savers':'SAVERS'}.get(page, page.upper())
    d.text((8, 4), 'PANEL / ' + title, font=FONT, fill=ACCENT)
    d.text((312, 6), wall_time.strftime('%H:%M:%S'), font=SMALL, fill=TEXT, anchor='ra')
    if page == 'stopped':
        d.text((160, 91), 'Panel app is not running', font=FONT, fill=TEXT, anchor='mm')
        d.text((160, 122), 'Restart from your VNC terminal.', font=SMALL, fill=MUTED, anchor='mm')
        d.text((160, 148), 'sudo python3 ~/panel_app.py', font=SMALL, fill=ACCENT, anchor='mm')
        return image
    if page == 'camera':
        if frame is not None:
            preview = ImageOps.contain(frame, (W, 132))
            image.paste(preview, ((W-preview.width)//2, 25+(132-preview.height)//2))
        else:
            d.text((40, 75), 'Camera is not available', font=FONT, fill=MUTED)
        d.text((8, 158), message[:45], font=SMALL, fill=TEXT)
        for i, label in enumerate(('SNAP', 'RESUME' if paused else 'PAUSE', f'{fps:g} FPS')):
            button(d, (i*106+3, 176, i*106+103, 203), label)
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
            colour = '#ffbb66' if value in ('LOW VOLTAGE','Not synchronized') else TEXT
            d.text((88,y),text,font=SMALL,fill=colour)
    elif page == 'options':
        labels = [f"Pixel level: {options['pixel_level']}%", 'Screensavers...',
                  'Preview saver', 'Test colours', 'Calibrate touch...',
                  f"Graph: {options['history_seconds']}s"]
        for i, label in enumerate(labels):
            col, row = i%2, i//2
            button(d, (8+col*156, 31+row*47, 155+col*156, 73+row*47), label)
        d.text((8,176), 'Pixel dim / blank leaves backlight on.',font=TINY,fill=MUTED)
        d.text((8,191),fit_text(d,message or 'Options are saved automatically.',304,TINY),font=TINY,fill=TEXT)
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
    else:
        d.text((160, 58), 'Close the panel app?', font=FONT, fill=TEXT, anchor='mm')
        d.text((160, 87), 'Camera and touch will be released.', font=SMALL, fill=MUTED, anchor='mm')
        button(d, (12, 121, 151, 174), 'CANCEL', active=True)
        button(d, (168, 121, 307, 174), 'CLOSE APP')
    for i, label in enumerate(('CAMERA', 'STATUS', 'OPTIONS', 'EXIT')):
        active = (page == label.lower() or (page == 'health' and label == 'STATUS')
                  or (page == 'savers' and label == 'OPTIONS'))
        button(d, (i*80+2, 207, i*80+77, 238), label, active)
    return image


def fit_text(draw, value, width, selected_font):
    value = str(value)
    if draw.textlength(value, font=selected_font) <= width:
        return value
    while value and draw.textlength(value+'...', font=selected_font) > width:
        value = value[:-1]
    return value+'...'


def option_at(x, y):
    for i in range(6):
        col, row = i%2, i//2
        if 8+col*156 <= x <= 155+col*156 and 31+row*47 <= y <= 73+row*47:
            return i
    return None


def recalibrate_safely(screen, touch, previous, save):
    """A timeout or save failure must not exit the running app or release touch."""
    try:
        result = calibrate(screen, touch)
        save(result)
        return result, 'Touch calibration saved'
    except (RuntimeError, OSError, ValueError) as exc:
        print('Calibration not changed:', exc, flush=True)
        return previous, 'Calibration unchanged; see terminal'


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
    options_file = data / 'options.json'
    options = load_options(options_file)
    screen = touch = camera = stats = None
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
        stats, rows = Stats(), {}
        stats.start()
        next_stats, next_draw = 0, 0
        running = True
        exit_return_page = page
        ignore_touch_until = 0
        active_saver = None
        saver_started = next_saver_draw = 0
        last_touch = time.monotonic()
        option_message = ''
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
                if page == 'colours':
                    page = 'options'
                    next_draw = 0
                    ignore_touch_until = now + .3
                    break
                x, y = np.array([*raw, 1.0]) @ matrix
                if not (0 <= x < W and 0 <= y < H):
                    continue
                if page == 'calconfirm' and 121 <= y <= 174:
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
                        key = {4:'saver_delay',5:'saver_speed',6:'saver_level'}[action]
                        values = OPTION_VALUES[key]
                        option_message = set_option(key, values[(values.index(options[key])+1)%len(values)])
                    else:
                        active_saver = options['saver_style'] if options['saver_style'] != 'off' else 'radar'
                        saver_started, next_saver_draw = now, 0
                    ignore_touch_until = now + .3
                elif page == 'options' and option_at(x, y) is not None:
                    action = option_at(x, y)
                    key = {0:'pixel_level', 5:'history_seconds'}.get(action)
                    if key:
                        values = OPTION_VALUES[key]
                        option_message = set_option(key, values[(values.index(options[key])+1)%len(values)])
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
                    if 12 <= x <= 151:
                        page = exit_return_page
                    elif 168 <= x <= 307:
                        running = False
                elif y >= 207:
                    tab = int(x)//80
                    if tab == 0:
                        page = 'camera'
                    elif tab == 1:
                        page = 'status'
                    elif tab == 2:
                        page = 'options'
                        option_message = ''
                        ignore_touch_until = now + .3
                    else:
                        if page != 'exit':
                            exit_return_page = page
                        page = 'exit'
                        ignore_touch_until = time.monotonic() + .35
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
            now = time.monotonic()
            if now >= next_stats:
                rows = stats.read()
                next_stats = now + 1
            if active_saver is None and saver_due(options,now-last_touch,page):
                active_saver = options['saver_style']
                saver_started, next_saver_draw = now, 0
            if active_saver is not None:
                if now >= next_saver_draw:
                    screen.pixel_level = min(options['pixel_level'],options['saver_level'])
                    screen.show(screensaver_frame(active_saver,now-saver_started,options['saver_speed']))
                    # Black is written once; animations are capped at eight frames/sec.
                    next_saver_draw = float('inf') if active_saver == 'black' else time.monotonic()+.125
                time.sleep(.02)
                continue
            live = page == 'camera' and camera is not None and not paused
            if live:
                frame = Image.fromarray(camera.capture_array('main'), 'RGB')
            if live or now >= next_draw:
                screen.pixel_level = 100 if page == 'colours' else options['pixel_level']
                text = option_message if page in ('options','savers') else message
                screen.show(render(page, frame, paused, fps, rows, text, stats.history, now, options))
                next_draw = now + (1/fps if live else .25)
            if not live:
                time.sleep(.02)
    except KeyboardInterrupt:
        print('\nStopped.', flush=True)
    finally:
        if stats is not None:
            stats.close()
        if screen is not None:
            try:
                screen.pixel_level = 100
                screen.show(render('stopped', None, False, 0, {}, ''))
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


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        raise SystemExit(f'Cyberdeck stopped: {exc}')
