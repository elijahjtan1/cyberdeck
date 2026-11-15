"""TOOLS for the main app: timers, network, sun & moon, GNSS and updates at full screen size.

The clocks, the network history, the sky maths and the updater are the quick panel's
(runtime.Clocks, network.NetworkHistory, sky, updates.Updates), so both apps behave the
same. GNSS shows what cyberdeck-core reads from the receiver (/api/gps). Tools that need the
camera or the small panel's settings (timelapse, photo sharing, presets, quick controls,
recovery) stay in the quick panel; QUICK PANEL TOOLS opens them.
`ui` is main_app; `rows` returns its latest stats (ip, wifi, sync)."""
import json
import re
import threading
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, build_opener

from PIL import Image, ImageDraw

from . import sky
from .main_pages import MARGIN, button_rects, draw_buttons, fit, index_at, wrap
from .network import NetworkHistory
from .runtime import Clocks, Job
from .ui import CONFIRMATIONS, ToolsUI, duration
from .updates import Updates

# (label, page or result, key, note)
MENU = (('TIMERS', 'timers', '1', 'Stopwatch and countdown'),
        ('NETWORK', 'network', '2', 'Address, Wi-Fi signal, traffic'),
        ('SUN & MOON', 'sky', '3', 'Sunrise, sunset, moon phase'),
        ('GNSS', 'gnss', '4', 'Position from the GPS receiver'),
        ('UPDATES', 'updates', '5', 'Check GitHub, update, roll back'),
        ('QUICK PANEL TOOLS', 'quick:tools', '6', 'Camera tools, presets, recovery'))
COLS = 3
LENGTHS = {60: 300, 300: 600, 600: 1500, 1500: 3600, 3600: 60}  # LENGTH steps to the next one.
TIMER_ALERT = ('warning', 'TIMER DONE: open Tools')
TIMER_KEYS = {'s': 0, 'space': 0, 'r': 1, 'l': 2, 't': 3, 'enter': 3}
# The UPDATES buttons, in order; the last is BACK. 'restart' needs no job: the main app does it.
UPDATE_ACTIONS = ('check', 'update', 'restart', 'rollback')
UPDATE_KEYS = {'c': 0, 'u': 1, 'r': 2, 'b': 3}
# The quick panel's wording, except for restart: this app has no camera or timelapse.
CONFIRM_TEXT = dict(CONFIRMATIONS, restart=('Restart the main app?', ['Loads an installed update.',
                                                                    'Running timers stop.']))
GPS_POLL_S = 1.   # Receivers report once a second.
GPS_STALE_S = 10  # The backend's own limit (core.gps.STALE_S): older data is not a fix.
GPS_IDLE_S = 3.   # The poller stops this long after the page was last drawn.


class GpsFeed:
    """The backend's /api/gps, asked once a second while the GNSS page is on screen."""
    def __init__(self, base_url, opener=None, clock=time.monotonic):
        self.base_url, self.clock = base_url, clock
        self.opener = opener or build_opener(ProxyHandler({}))  # Local only: never via a proxy.
        self.lock = threading.Lock()
        self.data, self.problem = None, None if base_url else 'off'
        self.drawn, self.thread = 0., None

    def fetch(self):
        """One request. Never raises: problems become a word for the page to explain."""
        if self.base_url is None:
            return
        try:
            with self.opener.open(self.base_url.rstrip('/') + '/api/gps', timeout=.5) as response:
                data = json.loads(response.read().decode('utf-8'))
            if not isinstance(data, dict):
                raise ValueError('not an object')
            problem = None
        except HTTPError as exc:
            data, problem = None, 'old' if exc.code == 404 else f'error {exc.code}'
        except (OSError, ValueError):
            data, problem = None, 'offline'
        with self.lock:
            self.data, self.problem = data, problem

    def poll_loop(self):
        while self.clock() - self.drawn < GPS_IDLE_S:
            started = self.clock()
            self.fetch()
            time.sleep(max(0., GPS_POLL_S - (self.clock() - started)))
        with self.lock:  # Nobody is looking: reopening the page must not show this reading.
            self.data = self.problem = None

    def keep_polling(self):
        self.drawn = self.clock()
        if self.base_url is not None and (self.thread is None or not self.thread.is_alive()):
            self.thread = threading.Thread(target=self.poll_loop, name='gps-page', daemon=True)
            self.thread.start()

    def read(self):
        with self.lock:
            return self.data, self.problem


def gps_view(data, problem):
    """(state, headline, rows) for the GNSS page. Position rows appear only for a current
    fix from the backend; nothing here is ever filled in or remembered by the page."""
    if problem == 'off':
        return 'off', 'BACKEND OFF', ['The main app was started with --backend off.']
    if problem == 'offline':
        return 'offline', 'GPS OFFLINE', ["cyberdeck-core isn't answering.",
                                         'Check: systemctl status cyberdeck-core']
    if problem == 'old':
        return 'offline', 'NO GPS SUPPORT', ['The running backend is too old for /api/gps.',
                                             'Update, then restart cyberdeck-core.']
    if problem:
        return 'offline', 'GPS OFFLINE', [f'The backend answered with {problem}.']
    if data is None:
        return 'waiting', 'Asking the backend...', []
    if not data.get('configured'):
        rows = ['cyberdeck-core has no GPS source set.', 'Set one with --gps or CYBERDECK_GPS (docs/CORE.md).']
        if data.get('error'):
            rows.append(f"Backend: {data['error']}")
        return 'none', 'NO RECEIVER', rows
    rows = [f"Source: {data.get('source') or 'unknown'}"]
    if data.get('error'):
        rows.append(f"Problem: {data['error']}")
    age = data.get('age_s')
    if not isinstance(age, (int, float)):
        return 'quiet', 'NO SIGNAL', rows + ['Nothing received from the receiver yet.']
    if age > GPS_STALE_S:
        return 'quiet', 'NO SIGNAL', rows + [f'The receiver went quiet {age:.0f} s ago (wiring or power).']
    if not data.get('fix'):
        sats = data.get('satellites')
        rows.append(f"Satellites: {'--' if sats is None else sats}    Last data {age:.0f} s ago")
        return 'nofix', 'NO FIX', rows + ['Waiting for satellites: it needs a view of the sky.']

    def number(key, pattern, unit=''):
        value = data.get(key)
        return '--' if value is None else pattern.format(value) + unit

    lat, lon = data.get('lat'), data.get('lon')
    place = ('--' if lat is None or lon is None else
             f"{abs(lat):.5f}° {'N' if lat >= 0 else 'S'}   {abs(lon):.5f}° {'E' if lon >= 0 else 'W'}")
    rows += [f"Altitude {number('altitude_m', '{:.0f}', ' m')}    Speed {number('speed_kmh', '{:.1f}', ' km/h')}"
             f"    Course {number('course_deg', '{:.0f}', '°')}",
             f"Satellites {number('satellites', '{}')}    HDOP {number('hdop', '{:.1f}')}",
             f"UTC {data.get('utc') or '--'}    Last data {age:.0f} s ago"]
    return 'fix', place, rows


class ToolsPage:
    def __init__(self, ui, data, rows=lambda: {}, size=(1024, 600), base_url=None, repo=None,
                 owns=lambda p: None, opener=None):
        self.ui, self.data, self.rows, self.size = ui, data, rows, size
        self.page = 'menu'
        self.clocks, self.network = Clocks(), NetworkHistory()
        self.timer_seconds = 300
        self.next_network = 0
        self.gps = GpsFeed(base_url, opener)
        self.repo, self.owns = repo or Path(__file__).resolve().parents[1], owns
        self.updates = None    # Made when UPDATES first opens: it runs git.
        self.job = Job()
        self.message = ''
        self.confirm = None    # The UPDATE_ACTIONS entry waiting for CONFIRM.

    @property
    def delay(self):
        """Redraw fast only while a clock on screen is running."""
        running = self.clocks.started is not None or self.clocks.deadline is not None
        return .2 if self.page == 'timers' and running else 1.

    def tick(self, now):
        """Called on every pass of the main loop, whichever page is open, so a countdown
        still ends and the traffic graph has no gap."""
        self.clocks.tick(now)
        if now >= self.next_network:
            match = re.search(r'(-?\d+)\s*dBm', self.rows().get('wifi', ''))
            self.network.sample(now, float(match[1]) if match else None)
            self.next_network = now + 1
        result = self.job.consume()
        if result:
            value, error = result
            self.message = error or str(value)
            if error:
                print('Update tool failed:', error, flush=True)

    def alerts(self):
        """Header alert for every page while a finished countdown waits to be dismissed."""
        return [TIMER_ALERT] if self.clocks.alarm else []

    # Layout and input -----------------------------------------------------------

    def menu_rects(self):
        width = self.size[0]
        top, gap = self.ui.HEADER + 24, 14
        h = (button_rects(self.size)[0][1] - 16 - top - gap) / 2
        w = (width - 2*MARGIN - (COLS-1)*gap) / COLS
        return [(round(MARGIN + c*(w+gap)), round(top + r*(h+gap)),
                 round(MARGIN + c*(w+gap) + w), round(top + r*(h+gap) + h))
                for r in range(2) for c in range(COLS)][:len(MENU)]

    def timer_labels(self):
        if self.clocks.alarm:
            timer = 'DISMISS'
        else:
            timer = 'STOP TIMER' if self.clocks.deadline else 'START TIMER'
        return ('PAUSE' if self.clocks.started is not None else 'START', 'RESET',
                f'LENGTH {self.timer_seconds//60}m', timer, 'BACK')

    def choose(self, index):
        """A menu tile: open its page, or hand its result ('quick:tools') to the main app."""
        target = MENU[index][1]
        if ':' in target:
            return target
        self.page = target
        if target == 'updates':
            self.message = ''
            if self.updates is None:
                self.updates = Updates(self.repo, self.data, self.owns)
        return None

    def update_labels(self):
        if self.confirm:
            # CONFIRM sits under BACK, the one button that never asks, so a double tap on
            # UPDATE, RESTART or ROLL BACK can't land on it.
            return ('CANCEL', '', '', '', 'CONFIRM')
        return ('CHECK', 'UPDATE', 'RESTART', 'ROLL BACK', 'BACK')  # The last three ask first.

    def update_button(self, index):
        """UPDATES buttons. Changing the checkout or restarting asks first; 'restart' is
        returned to the main app, which starts itself again."""
        if self.confirm:
            if index not in (0, 4):
                return None  # The blank buttons between CANCEL and CONFIRM do nothing.
            action, self.confirm = self.confirm, None
            if index == 0:
                return None
            if action == 'restart':
                return 'restart'
            self.start(action)
        elif index == 4:
            self.page = 'menu'
        elif self.job.busy or self.job.done:
            self.message = 'Wait for the current operation'
        elif index == 0:
            self.start('check')
        elif index is not None:
            self.confirm = UPDATE_ACTIONS[index]
        return None

    def start(self, action):
        work = getattr(self.updates, {'update': 'apply'}.get(action, action))
        if not self.job.start(work):
            self.message = 'Wait for the current operation'

    def close(self):
        """Before the main app exits or restarts: never leave midway through a Git change."""
        if self.job.thread and self.job.thread.is_alive():
            self.job.thread.join()

    def timer_button(self, index, now):
        clocks = self.clocks
        if index == 0:
            clocks.toggle(now)
        elif index == 1:
            clocks.started, clocks.elapsed = None, 0.
        elif index == 2:
            self.timer_seconds = LENGTHS[self.timer_seconds]
        elif index == 3:
            if clocks.alarm:
                clocks.alarm = False
            elif clocks.deadline:
                clocks.deadline = None
            else:
                clocks.countdown(self.timer_seconds, now)
        elif index == 4:
            self.page = 'menu'
        return None

    def tap(self, x, y, now):
        if self.page == 'menu':
            hit = index_at(self.menu_rects(), x, y)
            if hit is not None:
                return self.choose(hit)
            return 'exit' if index_at(button_rects(self.size), x, y) == 3 else None
        if self.page == 'timers':
            choice = index_at(button_rects(self.size, 5), x, y)
            return None if choice is None else self.timer_button(choice, now)
        if self.page == 'updates':
            choice = index_at(button_rects(self.size, 5), x, y)
            return None if choice is None else self.update_button(choice)
        if index_at(button_rects(self.size), x, y) == 3:
            self.page = 'menu'
        return None

    def key(self, key, now):
        if self.page == 'updates' and self.confirm:
            return self.update_button(4 if key == 'enter' else 0)
        if key == 'esc':
            if self.page == 'menu':
                return 'exit'
            self.page = 'menu'
        elif self.page == 'menu':
            keys = [item[2] for item in MENU]
            if key in keys:
                return self.choose(keys.index(key))
        elif self.page == 'timers' and key.lower() in TIMER_KEYS:
            return self.timer_button(TIMER_KEYS[key.lower()], now)
        elif self.page == 'updates' and key.lower() in UPDATE_KEYS:
            return self.update_button(UPDATE_KEYS[key.lower()])
        return None

    # Drawing --------------------------------------------------------------------

    def render(self, now, wall_time, alerts):
        ui = self.ui
        image = Image.new('RGB', self.size, ui.BG)
        d = ImageDraw.Draw(image)
        titles = {'timers': 'TOOLS / TIMERS', 'network': 'TOOLS / NETWORK', 'sky': 'TOOLS / SUN & MOON',
                  'gnss': 'TOOLS / GNSS', 'updates': 'TOOLS / UPDATES'}
        ui.header(d, self.size[0], titles.get(self.page, 'TOOLS'), alerts, wall_time.strftime('%H:%M'))
        if self.page == 'timers':
            self.draw_timers(d, now)
        elif self.page == 'network':
            self.draw_network(d)
        elif self.page == 'sky':
            self.draw_sky(d, wall_time)
        elif self.page == 'gnss':
            self.draw_gnss(d)
        elif self.page == 'updates':
            self.draw_updates(d)
        else:
            self.draw_menu(d)
        return image

    def centred(self, d, y, text, chosen, colour):
        d.text((self.size[0]//2, y), fit(d, text, self.size[0] - 2*MARGIN, chosen), font=chosen,
               fill=colour, anchor='mm')

    def hint(self, d, text, colour=None):
        """The line just above the buttons."""
        self.centred(d, button_rects(self.size)[0][1] - 22, text, self.ui.BODY, colour or self.ui.MUTED)

    def draw_menu(self, d):
        ui = self.ui
        for rect, (label, target, key, note) in zip(self.menu_rects(), MENU):
            d.rounded_rectangle(rect, 10, fill='#0b1016', outline='#1f2b36', width=2)
            d.text((rect[0]+16, rect[1]+14), key, font=ui.SMALL, fill=ui.MUTED)
            middle = (rect[0]+rect[2])//2
            room = rect[2] - rect[0] - 20
            name_font = next((f for f in ui.TILE_FONTS if d.textlength(label, font=f) < room), ui.TILE_FONTS[-1])
            d.text((middle, rect[1] + (rect[3]-rect[1])*.42), label, font=name_font,
                   fill=ui.MUTED if ':' in target else ui.GREEN, anchor='mm')
            for n, text in enumerate(wrap(d, note, rect[2]-rect[0]-24, ui.SMALL)[:2]):
                d.text((middle, rect[1] + (rect[3]-rect[1])*.66 + n*20), text, font=ui.SMALL,
                       fill=ui.MUTED, anchor='mm')
        draw_buttons(ui, d, self.size, ('', '', '', 'HOME'))

    def draw_timers(self, d, now):
        ui, clocks = self.ui, self.clocks
        width = self.size[0]
        top = ui.HEADER + 30
        room = button_rects(self.size)[0][1] - 50 - top
        for i, (name, value, colour) in enumerate((
                ('Stopwatch', duration(clocks.stopwatch(now)), ui.TEXT),
                ('Countdown', 'DONE' if clocks.alarm else duration(
                    clocks.deadline - now if clocks.deadline else self.timer_seconds),
                 ui.WARN if clocks.alarm else ui.TEXT))):
            cx = width*(1 + 2*i)//4
            d.text((cx, top + room*.25), name, font=ui.TITLE, fill=ui.MUTED, anchor='mm')
            d.text((cx, top + room*.6), value, font=ui.BIG, fill=colour, anchor='mm')
        if clocks.alarm:
            self.hint(d, 'Countdown finished. T or DISMISS clears it.', ui.WARN)
        else:
            self.hint(d, 'Keys: S start or pause, R reset, L length, T timer.')
        # exec replaces this process, so the quick panel starts with fresh clocks.
        self.centred(d, top + room*.92, 'Timers stop when the main app closes or opens the quick panel.',
                     ui.SMALL, ui.MUTED)
        draw_buttons(ui, d, self.size, self.timer_labels())

    def graph(self, d, rect, series, colours):
        x0, y0, x1, y1 = rect
        d.rectangle(rect, fill='#0b1218', outline='#1f2b36')
        top = max([v for values in series for v in values if v is not None] or [1]) or 1
        for values, colour in zip(series, colours):
            points = []
            for i, v in enumerate(values + [None]):  # The None draws the last run.
                if v is None:
                    if len(points) > 1:
                        d.line(points, fill=colour, width=2)
                    points = []
                else:
                    points.append((x0 + i*(x1-x0)/max(1, len(values)-1), y1 - (y1-y0-4)*v/top))

    def draw_network(self, d):
        ui, rows, net = self.ui, self.rows(), self.network
        width = self.size[0]
        y = ui.HEADER + 30
        d.text((MARGIN, y), fit(d, rows.get('ip', 'No address'), width//2 - MARGIN, ui.READ), font=ui.READ,
               fill=ui.TEXT, anchor='lm')
        d.text((width - MARGIN, y), fit(d, rows.get('wifi', 'Wi-Fi unavailable'), width//2 - MARGIN, ui.READ),
               font=ui.READ, fill=ui.TEXT, anchor='rm')
        rate = (f'Down {net.rx/1024:.1f} KiB/s    Up {net.tx/1024:.1f} KiB/s' if net.available
                else 'Network counters unavailable')
        self.centred(d, y + 36, rate, ui.BODY, ui.TEXT if net.available else ui.WARN)
        samples = list(net.samples)
        top, bottom = y + 62, button_rects(self.size)[0][1] - 16
        each = (bottom - top - 2*28) // 2
        self.graph(d, (MARGIN, top, width - MARGIN, top + each),
                   [[v[0] for v in samples], [v[1] for v in samples]], [ui.ACCENT, ui.WARN])
        self.centred(d, top + each + 14, 'Traffic: green down, orange up', ui.SMALL, ui.MUTED)
        second = top + each + 28
        self.graph(d, (MARGIN, second, width - MARGIN, second + each),
                   [[None if v[2] is None else max(0, min(70, v[2] + 100)) for v in samples]], ['#70aaff'])
        self.centred(d, second + each + 14, 'Wi-Fi signal, last 120 seconds', ui.SMALL, ui.MUTED)
        draw_buttons(ui, d, self.size, ('', '', '', 'BACK'))

    def draw_sky(self, d, wall_time):
        ui = self.ui
        # Read on each draw: the file is tiny and editing it takes effect at once.
        info = sky.sky_lines(wall_time, sky.load_location(self.data/'location.json'),
                             self.rows().get('sync', 'Unknown'))
        y = ui.HEADER + 30
        if info['clock']:
            self.centred(d, y, info['clock'], ui.BODY, ui.WARN)
            y += 34
        self.centred(d, y, info['where'], ui.BODY, ui.MUTED)
        rows = info['sun'] or ['Sun times need a position:', 'save it in cyberdeck-data/location.json']
        for i, text in enumerate(rows):
            self.centred(d, y + 40 + i*34, text, ui.READ, ui.TEXT if info['sun'] else ui.WARN)
        bottom = button_rects(self.size)[0][1]
        radius = max(24, min(44, (bottom - (y + 40 + len(rows)*34) - 90)//2))
        centre = (self.size[0]//2, bottom - 86 - radius)
        # The southern hemisphere sees the moon upside down: lit from the left while waxing.
        ToolsUI.draw_moon(self, d, centre, radius, info['lit'], info['waxing'] != info.get('south', False))
        self.centred(d, bottom - 62, info['moon'], ui.READ, ui.TEXT)
        self.centred(d, bottom - 30, info['next'], ui.BODY, ui.MUTED)
        draw_buttons(ui, d, self.size, ('', '', '', 'BACK'))

    def draw_gnss(self, d):
        ui = self.ui
        self.gps.keep_polling()
        data, problem = self.gps.read()
        state, headline, rows = gps_view(data, problem)
        y = ui.HEADER + 44
        if data and data.get('simulated'):  # A replayed track must never pass for a live fix.
            self.centred(d, y, 'SIMULATED', ui.BIG, ui.ORANGE)
            y += 62
        colour = ui.TEXT if state == 'fix' else ui.MUTED if state == 'waiting' else ui.WARN
        self.centred(d, y + 10, headline, ui.BIG if state != 'waiting' else ui.TITLE, colour)
        for i, text in enumerate(rows):
            self.centred(d, y + 76 + i*38, text, ui.READ, ui.TEXT if state == 'fix' else ui.MUTED)
        draw_buttons(ui, d, self.size, ('', '', '', 'BACK'))

    def draw_updates(self, d):
        ui = self.ui
        width = self.size[0]
        y = ui.HEADER + 40
        if self.confirm:
            title, lines = CONFIRM_TEXT[self.confirm]
            self.centred(d, y + 30, title, ui.TITLE, ui.WARN)
            for i, text in enumerate(lines):
                self.centred(d, y + 90 + i*38, text, ui.READ, ui.TEXT)
            self.hint(d, 'Enter confirms. Any other key cancels.')
            draw_buttons(ui, d, self.size, self.update_labels(), danger=('CONFIRM',))
            return
        self.centred(d, y, f'Running: {self.updates.running}', ui.TITLE, ui.TEXT)
        self.centred(d, y + 44, 'Nothing updates by itself. CHECK asks GitHub, so it needs the internet.',
                     ui.BODY, ui.MUTED)
        # This app can restart itself; the backend is a separate service it has no say over.
        self.centred(d, y + 76, 'RESTART reloads this app only. For the backend: sudo systemctl restart cyberdeck-core',
                     ui.SMALL, ui.MUTED)
        message = 'Working...' if self.job.busy else self.message
        for i, text in enumerate(wrap(d, message, width - 2*MARGIN, ui.READ)[:5]):  # Git errors run long.
            self.centred(d, y + 130 + i*34, text, ui.READ, ui.WARN)
        self.hint(d, 'Keys: C check, U update, R restart, B roll back.')
        draw_buttons(ui, d, self.size, self.update_labels())
