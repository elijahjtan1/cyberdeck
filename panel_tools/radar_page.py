"""RADAR for the main app: the LD2450 radar drawn like a heartbeat sensor.

A top-down fan with the deck at the bottom centre, range arcs, a sweep, and a pulsing dot
for each contact the radar reports. The data comes from cyberdeck-core's /api/radar, polled
on a background thread only while the page is being drawn. The radar reports movement and
position, not who or what moves, so the page says "contact" and never "person". A silent
sensor shows NO SIGNAL, never an empty screen, and replays say SIMULATED the whole time."""
import json
import math
import threading
import time
from urllib.error import HTTPError
from urllib.request import ProxyHandler, build_opener

from PIL import Image, ImageDraw

POLL_S = 0.2       # About 5 requests a second while the page is open.
IDLE_S = 1.0       # The poller stops this long after the last draw.
FRAME_S = 1 / 15   # Animation pace; the data itself only changes ~10 times a second.
FADE_S = 1.0       # A contact that disappears fades out over this long...
FRESH_S = 0.5      # ...starting once it has missed a couple of readings.
SAME_M = 0.8       # A new reading this close to an old dot is the same contact moving.
BEAT_S = 1.0       # One "lub-dub" a second.
SWEEP_S = 1.0      # One pass across the fan a second.
ARCS_M = (2, 4, 6)
SPOKES = (-60, -30, 0, 30, 60)
FAN = '#5fd36b'
CONTACT = (255, 70, 50)
ORANGE = '#f29b42'


def beat(t):
    """Heartbeat brightness 0..1 at time t: a strong beat, a softer one 0.25 s later."""
    p = (t % BEAT_S) / BEAT_S
    lub = math.exp(-p / .08)
    dub = .7 * math.exp(-(p - .25) / .08) if p >= .25 else 0.
    return min(1., lub + dub)


def dim(colour, level, bg=(16, 25, 35)):
    """`colour` mixed toward the background; level 1 is full colour."""
    if isinstance(colour, str):
        colour = tuple(int(colour[i:i+2], 16) for i in (1, 3, 5))
    level = max(0., min(1., level))
    return tuple(round(b + (c - b) * level) for c, b in zip(colour, bg))


def side_text(speed_cms):
    if speed_cms < 0:
        return 'approaching'
    return 'moving away' if speed_cms > 0 else 'still'


def bearing(angle_deg):
    side = 'L' if angle_deg < 0 else 'R'
    return 'ahead' if round(angle_deg) == 0 else f'{abs(angle_deg):.0f}° {side}'


class RadarPage:
    def __init__(self, ui, base_url, opener=None, size=(1024, 600), clock=time.monotonic):
        self.ui, self.base_url, self.size, self.clock = ui, base_url, size, clock
        self.opener = opener or build_opener(ProxyHandler({}))  # Local only: never via a proxy.
        self.lock = threading.Lock()
        self.data, self.problem = None, None if base_url else 'off'
        self.received = None   # Clock time of the last good answer.
        self.blips = []        # [x_mm, y_mm, moving, seen, speed_cms]
        self.drawn = 0.        # Clock time of the last draw; the poller stops when it gets old.
        self.thread = None
        self.background = None

    # Data ----------------------------------------------------------------------

    def fetch(self):
        """One request to /api/radar. Never raises: problems become a message."""
        if self.base_url is None:
            return
        try:
            with self.opener.open(self.base_url.rstrip('/') + '/api/radar', timeout=.5) as response:
                data = json.loads(response.read().decode('utf-8'))
            if not isinstance(data, dict):
                raise ValueError('not an object')
            problem = None
        except HTTPError as exc:
            data, problem = None, 'old' if exc.code == 404 else f'error {exc.code}'
        except (OSError, ValueError):
            data, problem = None, 'offline'
        now = self.clock()
        with self.lock:
            self.data, self.problem = data, problem
            if data is not None:
                self.received = now
                self.track(data, now)
            else:
                self.blips = []

    def track(self, data, now):
        """Match the new readings to the dots on screen, so a moving contact glides instead of
        leaving a trail; dots with no reading fade, and stale or bad data clears them."""
        if data.get('stale') or not data.get('configured'):
            self.blips = []
            return
        old = list(self.blips)
        new = []
        for t in data.get('targets') or []:
            try:
                x, y = float(t['x_mm']), float(t['y_mm'])
            except (KeyError, TypeError, ValueError):
                continue
            near = min(old, key=lambda b: math.hypot(b[0]-x, b[1]-y), default=None)
            if near is not None and math.hypot(near[0]-x, near[1]-y) < SAME_M * 1000:
                old.remove(near)
            new.append([x, y, bool(t.get('moving')), now, int(t.get('speed_cms') or 0)])
        self.blips = new + [b for b in old if now - b[3] < FRESH_S + FADE_S]

    def poll_loop(self):
        while self.clock() - self.drawn < IDLE_S:
            started = self.clock()
            self.fetch()
            time.sleep(max(0., POLL_S - (self.clock() - started)))

    def keep_polling(self):
        self.drawn = self.clock()
        if self.base_url is not None and (self.thread is None or not self.thread.is_alive()):
            self.thread = threading.Thread(target=self.poll_loop, name='radar', daemon=True)
            self.thread.start()

    @property
    def running(self):
        return False  # Nothing to stop when leaving: the poller ends on its own.

    @property
    def delay(self):
        return FRAME_S if self.status()[0] in ('live', 'quiet') else .5

    def status(self):
        """(state, detail): live, quiet (no movement), waiting, stale, unset, offline, old, off."""
        with self.lock:
            data, problem = self.data, self.problem
        if problem == 'off':
            return 'off', 'The backend is turned off (--backend off).'
        if problem == 'offline':
            return 'offline', 'Radar offline (backend not running).'
        if problem == 'old':
            return 'old', 'This backend has no radar support yet: update cyberdeck-core.'
        if problem:
            return 'offline', f'Radar offline (backend {problem}).'
        if data is None:
            return 'waiting', 'Connecting to the backend...'
        if not data.get('configured'):
            return 'unset', 'No radar set up.'
        if data.get('stale'):
            age = data.get('age_s')
            return 'stale', f'No data for {age:.0f} s: check the radar wiring.' if age else 'No data.'
        if data.get('age_s') is None:
            return 'waiting', 'Waiting for the radar...'
        return ('live', '') if data.get('targets') else ('quiet', 'No movement')

    # Input ---------------------------------------------------------------------

    def home_rect(self):
        width, height = self.size
        return (width - 170, height - 70, width - 20, height - 16)

    def tap(self, x, y, now):
        r = self.home_rect()
        return 'exit' if r[0] <= x <= r[2] and r[1] <= y <= r[3] else None

    def key(self, key, now):
        return 'exit' if key in ('esc', 'q', 'Q') else None

    # Drawing -------------------------------------------------------------------

    def geometry(self):
        """Apex (x, y) at the bottom centre and the radius that 6 m fills."""
        width, height = self.size
        apex = (width // 2, height - 16)
        return apex, apex[1] - (self.ui.HEADER + 14)

    def point(self, angle_deg, r):
        (cx, cy), _ = self.geometry()
        a = math.radians(angle_deg)
        return cx + r * math.sin(a), cy - r * math.cos(a)

    def scale(self, data=None):
        _, radius = self.geometry()
        range_m = (data or {}).get('range_m') or ARCS_M[-1]
        return radius / (float(range_m) * 1000)  # Pixels per millimetre.

    def draw_background(self):
        """The fan, arcs, spokes and labels: drawn once, copied every frame."""
        ui = self.ui
        image = Image.new('RGB', self.size, ui.BG)
        d = ImageDraw.Draw(image)
        (cx, cy), radius = self.geometry()
        box = (cx - radius, cy - radius, cx + radius, cy + radius)
        d.pieslice(box, -150, -30, fill=dim(FAN, .08))
        for n, metres in enumerate(ARCS_M):
            r = radius * metres / ARCS_M[-1]
            d.arc((cx - r, cy - r, cx + r, cy + r), -150, -30, fill=dim(FAN, .7 if n == 2 else .45), width=2)
            x, y = self.point(4, r)
            d.text((x + 4, y + 4), f'{metres} m', font=ui.SMALL, fill=dim(FAN, .8))
        for angle in SPOKES:
            d.line(((cx, cy), self.point(angle, radius)), fill=dim(FAN, .25 if angle else .35), width=1)
        d.ellipse((cx - 6, cy - 6, cx + 6, cy + 6), fill=FAN)
        r = self.home_rect()
        d.rounded_rectangle(r, 8, fill='#0e161d', outline='#2a3a48', width=2)
        d.text(((r[0]+r[2])//2, (r[1]+r[3])//2), 'HOME', font=ui.SMALL_BOLD, fill=ui.TEXT, anchor='mm')
        d.text((20, self.size[1] - 40), 'Esc: home', font=ui.SMALL, fill=ui.MUTED)
        return image

    def render(self, now, wall_time, alerts):
        ui = self.ui
        self.keep_polling()
        if self.background is None:
            self.background = self.draw_background()
        image = self.background.copy()
        d = ImageDraw.Draw(image)
        ui.header(d, self.size[0], 'RADAR', alerts, wall_time.strftime('%H:%M'))
        state, detail = self.status()
        with self.lock:
            data, blips = self.data or {}, [list(b) for b in self.blips]
        if state in ('live', 'quiet'):
            self.draw_sweep(d, now)
            self.draw_blips(d, blips, now, data)
        self.draw_status(d, state, detail, data)
        self.draw_readout(d, blips, now)
        return image

    def draw_sweep(self, d, now):
        """A line crossing the fan once a second, with a fading wedge behind it. The arcs and
        spokes are drawn again on top so the wedge doesn't hide them."""
        (cx, cy), radius = self.geometry()
        head = -60 + 120 * ((now % SWEEP_S) / SWEEP_S)
        box = (cx - radius, cy - radius, cx + radius, cy + radius)
        steps = 5  # Few, wide steps: each wedge fills a large area, and the Pi shares its CPU.
        for i in range(steps, 0, -1):
            start, end = max(-60, head - i * 6), head - (i - 1) * 6
            if end > start:
                d.pieslice(box, start - 90, end - 90, fill=dim(FAN, .08 + .3 * (1 - i / steps)))
        for n, metres in enumerate(ARCS_M):
            r = radius * metres / ARCS_M[-1]
            d.arc((cx - r, cy - r, cx + r, cy + r), max(-60, head - 30) - 90, head - 90,
                  fill=dim(FAN, .7 if n == 2 else .45), width=2)
        for angle in SPOKES:
            if head - 30 <= angle <= head:
                d.line(((cx, cy), self.point(angle, radius)), fill=dim(FAN, .25 if angle else .35), width=1)
        d.line(((cx, cy), self.point(head, radius)), fill=dim(FAN, .9), width=3)

    def draw_blips(self, d, blips, now, data):
        (cx, cy), _ = self.geometry()
        per_mm = self.scale(data)
        pulse = beat(now)
        for x, y, moving, seen, _speed in blips:
            fade = 1 - max(0., self.clock() - seen - FRESH_S) / FADE_S
            if fade <= 0:
                continue
            px, py = cx + x * per_mm, cy - y * per_mm
            if moving:
                level, r = (.45 + .55 * pulse) * fade, 9 + 7 * pulse
                glow = r + 10 * pulse
                d.ellipse((px - glow, py - glow, px + glow, py + glow), fill=dim(CONTACT, .25 * level))
            else:
                level, r = .45 * fade, 8
            d.ellipse((px - r, py - r, px + r, py + r), fill=dim(CONTACT, level))

    def draw_status(self, d, state, detail, data):
        ui = self.ui
        width, height = self.size
        (cx, cy), radius = self.geometry()
        if state in ('live', 'quiet'):
            count = len(data.get('targets') or [])
            d.text((width - 20, ui.HEADER + 16), detail or f"{count} contact{'s' if count > 1 else ''}",
                   font=ui.BODY, fill=ui.MUTED if state == 'quiet' else ui.TEXT, anchor='ra')
        else:
            big = {'stale': 'NO SIGNAL', 'offline': 'RADAR OFFLINE', 'unset': 'NO RADAR',
                   'old': 'NO RADAR SUPPORT', 'off': 'BACKEND OFF', 'waiting': 'WAITING'}[state]
            colour = ui.DANGER if state in ('stale', 'offline') else ui.WARN
            d.text((cx, cy - radius * .55), big, font=ui.BIG, fill=colour, anchor='mm')
            d.text((cx, cy - radius * .55 + 44), detail, font=ui.BODY, fill=ui.TEXT, anchor='mm')
            if state == 'unset':
                d.text((cx, cy - radius * .55 + 76), 'Start cyberdeck-core with --radar serial:/dev/ttyUSB0 '
                       '(docs/CORE.md).', font=ui.SMALL, fill=ui.MUTED, anchor='mm')
        error = data.get('error')
        if error:
            d.text((20, height - 64), str(error)[:44], font=ui.SMALL, fill=ui.WARN)
        if data.get('simulated'):
            d.text((width - 20, ui.HEADER + 44), data.get('label') or 'SIMULATED', font=ui.BIG,
                   fill=ORANGE, anchor='ra')

    def draw_readout(self, d, blips, now):
        """Distance, bearing and direction for each contact, in the top-left corner."""
        ui = self.ui
        top = ui.HEADER + 16
        live = [b for b in blips if self.clock() - b[3] <= FRESH_S]
        if not live:
            return
        d.text((20, top), 'CONTACTS', font=ui.SMALL_BOLD, fill=ui.MUTED)
        for n, (x, y, moving, _seen, speed) in enumerate(live[:3]):
            row = top + 28 + n * 52
            distance = math.hypot(x, y) / 1000
            angle = math.degrees(math.atan2(x, y))
            d.ellipse((20, row + 6, 32, row + 18), fill=CONTACT if moving else dim(CONTACT, .45))
            d.text((42, row), f'{distance:.1f} m  {bearing(angle)}', font=ui.BODY, fill=ui.TEXT)
            d.text((42, row + 24), side_text(speed), font=ui.SMALL, fill=ui.MUTED)
