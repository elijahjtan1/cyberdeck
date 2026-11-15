"""TOOLS for the main app: timers, network and sun & moon at the full screen size.

The clocks, the network history and the sky maths are the quick panel's (runtime.Clocks,
network.NetworkHistory, sky), so both apps show the same numbers. Tools that need the
camera or the small panel's settings (timelapse, photo sharing, presets, quick controls,
recovery) stay in the quick panel, and so do updates for now; QUICK PANEL TOOLS opens them.
`ui` is main_app; `rows` returns its latest stats (ip, wifi, sync)."""
import re

from PIL import Image, ImageDraw

from . import sky
from .main_pages import MARGIN, button_rects, draw_buttons, fit, index_at
from .network import NetworkHistory
from .runtime import Clocks
from .ui import ToolsUI, duration

# (label, page or result, key, note)
MENU = (('TIMERS', 'timers', '1', 'Stopwatch and countdown'),
        ('NETWORK', 'network', '2', 'Address, Wi-Fi signal, traffic'),
        ('SUN & MOON', 'sky', '3', 'Sunrise, sunset, moon phase'),
        ('QUICK PANEL TOOLS', 'quick:tools', '4', 'Timelapse, photo sharing, presets, updates, recovery'))
LENGTHS = {60: 300, 300: 600, 600: 1500, 1500: 3600, 3600: 60}  # LENGTH steps to the next one.
TIMER_ALERT = ('warning', 'TIMER DONE: open Tools')
TIMER_KEYS = {'s': 0, 'space': 0, 'r': 1, 'l': 2, 't': 3, 'enter': 3}


class ToolsPage:
    def __init__(self, ui, data, rows=lambda: {}, size=(1024, 600)):
        self.ui, self.data, self.rows, self.size = ui, data, rows, size
        self.page = 'menu'
        self.clocks, self.network = Clocks(), NetworkHistory()
        self.timer_seconds = 300
        self.next_network = 0

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

    def alerts(self):
        """Header alert for every page while a finished countdown waits to be dismissed."""
        return [TIMER_ALERT] if self.clocks.alarm else []

    # Layout and input -----------------------------------------------------------

    def menu_rects(self):
        width = self.size[0]
        top, gap = self.ui.HEADER + 24, 14
        h = (button_rects(self.size)[0][1] - 16 - top - gap) / 2
        w = (width - 2*MARGIN - gap) / 2
        return [(round(MARGIN + c*(w+gap)), round(top + r*(h+gap)),
                 round(MARGIN + c*(w+gap) + w), round(top + r*(h+gap) + h))
                for r in range(2) for c in range(2)]

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
        return None

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
        if index_at(button_rects(self.size), x, y) == 3:
            self.page = 'menu'
        return None

    def key(self, key, now):
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
        return None

    # Drawing --------------------------------------------------------------------

    def render(self, now, wall_time, alerts):
        ui = self.ui
        image = Image.new('RGB', self.size, ui.BG)
        d = ImageDraw.Draw(image)
        titles = {'timers': 'TOOLS / TIMERS', 'network': 'TOOLS / NETWORK', 'sky': 'TOOLS / SUN & MOON'}
        ui.header(d, self.size[0], titles.get(self.page, 'TOOLS'), alerts, wall_time.strftime('%H:%M'))
        if self.page == 'timers':
            self.draw_timers(d, now)
        elif self.page == 'network':
            self.draw_network(d)
        elif self.page == 'sky':
            self.draw_sky(d, wall_time)
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
            d.text((middle, rect[1] + (rect[3]-rect[1])*.42), label, font=ui.LABEL,
                   fill=ui.MUTED if ':' in target else ui.GREEN, anchor='mm')
            d.text((middle, rect[1] + (rect[3]-rect[1])*.7), fit(d, note, rect[2]-rect[0]-24, ui.SMALL),
                   font=ui.SMALL, fill=ui.MUTED, anchor='mm')
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
