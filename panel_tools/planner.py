"""PLANNER app: a month calendar and a list of timed entries for each day.

Entries live in cyberdeck-data/planner.json and are written only when one is added or
deleted. "Today" comes from the Pi's clock, which keeps the last saved time when it is
offline, so the page says when the clock isn't synced instead of trusting it."""
import calendar
from datetime import date, datetime, timedelta
import json

from PIL import Image, ImageDraw

from .ask import keyboard_rects, field_rect
from .runtime import atomic_json

MAX_TEXT = 60
MAX_PER_DAY = 20
LINE = 15
DAY_NAMES = ('Mo', 'Tu', 'We', 'Th', 'Fr', 'Sa', 'Su')
TIME_STEPS = (('-1h', -60), ('-15m', -15), ('+15m', 15), ('+1h', 60))


class Planner:
    """The saved entries: {'YYYY-MM-DD': [{'time': 'HH:MM' or '', 'text': ...}]}."""
    def __init__(self, path, owns=lambda p: None):
        self.path, self.owns = path, owns
        self.days = {}
        try:
            saved = json.loads(path.read_text())
        except (OSError, ValueError):
            saved = {}
        days = saved.get('days') if isinstance(saved, dict) else None
        for day, entries in days.items() if isinstance(days, dict) else ():
            try:
                date.fromisoformat(day)
            except (TypeError, ValueError):
                continue
            kept = [clean_entry(e) for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []
            kept = [e for e in kept if e]
            if kept:
                self.days[day] = sorted(kept, key=entry_order)[:MAX_PER_DAY]

    def entries(self, day):
        return self.days.get(day.isoformat(), [])

    def add(self, day, text, time=''):
        entry = clean_entry({'text': text, 'time': time})
        if not entry:
            return False
        entries = self.days.get(day.isoformat(), [])
        if len(entries) >= MAX_PER_DAY:
            return False
        self.days[day.isoformat()] = sorted(entries + [entry], key=entry_order)
        self.save()
        return True

    def delete(self, day, index):
        entries = self.days.get(day.isoformat(), [])
        if not 0 <= index < len(entries):
            return
        del entries[index]
        if not entries:
            self.days.pop(day.isoformat(), None)
        self.save()

    def save(self):
        atomic_json(self.path, {'days': self.days}, self.owns)

    def next_entry(self, moment):
        """(day, entry) of the next timed entry from `moment` on, looking a week ahead."""
        for offset in range(8):
            day = moment.date() + timedelta(days=offset)
            for entry in self.entries(day):
                if entry['time'] and (offset or entry['time'] >= moment.strftime('%H:%M')):
                    return day, entry
        return None


def clean_entry(entry):
    text = ' '.join(str(entry.get('text', '')).split())[:MAX_TEXT]
    time = str(entry.get('time') or '')
    try:
        time = datetime.strptime(time, '%H:%M').strftime('%H:%M') if time else ''
    except ValueError:
        time = ''
    return {'time': time, 'text': text} if text else None


def entry_order(entry):
    return (entry['time'] != '', entry['time'])  # Untimed (all-day) entries come first.


def month_cells(year, month):
    """Weeks of the month as rows of 7 dates (Monday first); None outside the month."""
    return [[date(year, month, d) if d else None for d in week]
            for week in calendar.Calendar().monthdayscalendar(year, month)]


def shift_month(day, months):
    index = day.year*12 + day.month - 1 + months
    return date(index // 12, index % 12 + 1, 1)


def step_time(time, minutes):
    """HH:MM moved by `minutes`, wrapping round midnight."""
    hours, mins = map(int, time.split(':'))
    total = (hours*60 + mins + minutes) % (24*60)
    return f'{total//60:02}:{total%60:02}'


def bottom_buttons(width, height, count=4):
    gap = 4
    size = (width - 4 - (count-1)*gap) / count
    return [(round(2 + i*(size+gap)), height-31, round(2 + i*(size+gap) + size), height-3)
            for i in range(count)]


class PlannerApp:
    def __init__(self, api, data, sync=lambda: 'Unknown', owns=lambda p: None):
        self.a, self.sync = api, sync
        self.planner = Planner(data/'planner.json', owns)
        self.view = 'month'
        self.shown = None      # First day of the month on screen; None follows today.
        self.day = None        # Day open in the day view.
        self.selected = None   # Entry index waiting for delete confirmation.
        self.text, self.time, self.symbols = '', '', False
        self.message = ''
        self.wall = None       # Wall time of the last draw, so taps agree with what is shown.
        self.delay = 1.

    def note(self, moment):
        """Line for the APPS tile."""
        upcoming = self.planner.next_entry(moment)
        if not upcoming:
            return 'Calendar and day plans'
        day, entry = upcoming
        when = entry['time'] if day == moment.date() else f"{day.strftime('%a')} {entry['time']}"
        return f"Next: {when} {entry['text']}"

    def clock_note(self):
        sync = self.sync()
        if sync == 'Synchronized':
            return None
        return 'Clock not synced: check today' if sync == 'Not synchronized' else 'Clock sync unknown'

    # Layout -------------------------------------------------------------------

    def grid_rect(self):
        a = self.a
        top = 62 if a.portrait() else 44
        return (2, top, a.W-3, a.H-36)

    def day_rects(self, month):
        """(rect, date) for each day of `month` in the month grid."""
        x0, y0, x1, y1 = self.grid_rect()
        weeks = month_cells(month.year, month.month)
        cell_w, cell_h = (x1-x0) / 7, (y1-y0-14) / len(weeks)
        return [((round(x0 + c*cell_w), round(y0 + 14 + r*cell_h),
                  round(x0 + (c+1)*cell_w) - 2, round(y0 + 14 + (r+1)*cell_h) - 2), day)
                for r, week in enumerate(weeks) for c, day in enumerate(week) if day]

    def entry_rects(self, count):
        a = self.a
        top, pitch = (48, 24) if not a.portrait() else (62, 26)
        rows = min(count, max(1, (a.H - 36 - top) // pitch))
        return [(4, top + i*pitch, a.W-5, top + i*pitch + pitch - 3) for i in range(rows)]

    def time_rects(self):
        a = self.a
        top = 110 if a.portrait() else 84
        return bottom_buttons(a.W, top + 34, 4)

    # Touch --------------------------------------------------------------------

    def tap(self, x, y, now):
        a = self.a
        today = self.now().date()
        if self.view == 'month':
            month = self.shown or today.replace(day=1)
            buttons = bottom_buttons(a.W, a.H)
            choice = a.index_at(buttons, x, y, slop=2)
            if choice == 0:
                self.shown = shift_month(month, -1)
            elif choice == 1:
                self.shown = None
                self.open_day(today)
            elif choice == 2:
                self.shown = shift_month(month, 1)
            elif choice == 3:
                return 'exit'
            else:
                days = self.day_rects(month)
                hit = a.index_at([rect for rect, _ in days], x, y, slop=1)
                if hit is not None:
                    self.open_day(days[hit][1])
            return None
        if self.view == 'day':
            choice = a.index_at(bottom_buttons(a.W, a.H), x, y, slop=2)
            if self.selected is not None:
                if choice == 2:
                    self.planner.delete(self.day, self.selected)
                    self.message = 'Deleted'
                self.selected = None
                return None
            if choice == 0:
                self.view = 'month'
                self.shown = self.day.replace(day=1)
            elif choice in (1, 2):
                self.open_day(self.day + timedelta(days=-1 if choice == 1 else 1))
            elif choice == 3:
                if len(self.planner.entries(self.day)) >= MAX_PER_DAY:
                    self.message = f'A day holds up to {MAX_PER_DAY} entries'
                else:
                    self.text, self.symbols, self.view = '', False, 'type'
            else:
                entries = self.planner.entries(self.day)
                hit = a.index_at(self.entry_rects(len(entries)), x, y, slop=1)
                if hit is not None:
                    self.selected = hit
            return None
        if self.view == 'time':
            choice = a.index_at(self.time_rects(), x, y, slop=2) if self.time else None
            if choice is not None:
                self.time = step_time(self.time or '09:00', TIME_STEPS[choice][1])
                return None
            choice = a.index_at(bottom_buttons(a.W, a.H), x, y, slop=2)
            if choice == 0:
                self.view = 'type'
            elif choice == 1:
                self.time = '' if self.time else self.default_time()
            elif choice == 3:
                saved = self.planner.add(self.day, self.text, self.time)
                self.message = 'Saved' if saved else 'Not saved'
                self.view = 'day'
            return None
        # Typing the entry text.
        keys = keyboard_rects(a.W, a.H, self.symbols)
        choice = a.index_at([rect for rect, _ in keys], x, y, slop=1)
        if choice is None:
            return None
        key = keys[choice][1]
        if key == 'back':
            self.view = 'day'
        elif key == 'mode':
            self.symbols = not self.symbols
        elif key == 'del':
            self.text = self.text[:-1]
        elif key == 'ask':
            if self.text.strip():
                self.time = self.default_time()
                self.view = 'time'
        elif len(self.text) < MAX_TEXT:
            if key != 'space':
                self.text += key
            elif self.text and not self.text.endswith(' '):
                self.text += ' '
        return None

    def open_day(self, day):
        self.day, self.view, self.selected, self.message = day, 'day', None, ''

    def now(self):
        return self.wall or datetime.now().astimezone()

    def default_time(self):
        """Next whole hour for today, 09:00 for any other day."""
        now = self.now()
        if self.day == now.date() and now.hour < 23:
            return f'{now.hour+1:02}:00'
        return '09:00'

    # Drawing ------------------------------------------------------------------

    def header(self, d, title, wall_time, alerts):
        a = self.a
        if alerts:
            a.draw_banner(d, alerts, wall_time.strftime('%H:%M'))
        else:
            d.text((8, 4), title, font=a.FONT, fill=a.ACCENT)
            d.text((a.W-8, 7), wall_time.strftime('%H:%M'), font=a.SMALL, fill=a.TEXT, anchor='ra')

    def render(self, now, wall_time, alerts):
        a = self.a
        self.wall = wall_time
        image = Image.new('RGB', (a.W, a.H), a.BG)
        d = ImageDraw.Draw(image)
        if self.view == 'month':
            self.draw_month(d, wall_time, alerts)
        elif self.view == 'day':
            self.draw_day(d, wall_time, alerts)
        elif self.view == 'time':
            self.draw_time(d, wall_time, alerts)
        else:
            self.draw_keyboard(d, wall_time, alerts)
        return image

    def draw_month(self, d, wall_time, alerts):
        a = self.a
        today = wall_time.date()
        month = self.shown or today.replace(day=1)
        self.header(d, 'PLANNER', wall_time, alerts)
        note = self.clock_note()
        if a.portrait():
            a.centred(d, 28, month.strftime('%B %Y'), a.SMALL, a.TEXT)
        else:  # Left, so the clock warning fits on the same line.
            d.text((8, 26), month.strftime('%B %Y'), font=a.SMALL, fill=a.TEXT)
        if note and a.portrait():
            a.centred(d, 44, note, a.TINY, a.WARN)
        elif note:
            d.text((a.W-6, 28), note, font=a.TINY, fill=a.WARN, anchor='ra')
        x0, y0, x1, _ = self.grid_rect()
        cell_w = (x1-x0) / 7
        for c, name in enumerate(DAY_NAMES):
            d.text((x0 + (c+.5)*cell_w, y0), name, font=a.TINY,
                   fill=a.WARN if c >= 5 else a.MUTED, anchor='ma')
        for rect, day in self.day_rects(month):
            busy = bool(self.planner.entries(day))
            is_today = day == today
            d.rounded_rectangle(rect, radius=3, fill=a.PANEL if busy else a.INSET,
                                outline=a.ACCENT if is_today else None, width=2 if is_today else 1)
            d.text(((rect[0]+rect[2])//2, (rect[1]+rect[3])//2), str(day.day), font=a.SMALL,
                   fill=a.ACCENT if is_today else a.TEXT, anchor='mm')
            if busy:
                cx, bottom = (rect[0]+rect[2])//2, rect[3]-3
                d.ellipse((cx-2, bottom-4, cx+2, bottom), fill=a.YELLOW)
        for rect, label in zip(bottom_buttons(a.W, a.H), ('<', 'TODAY', '>', 'EXIT')):
            a.button(d, rect, label)

    def draw_day(self, d, wall_time, alerts):
        a = self.a
        self.header(d, 'PLANNER', wall_time, alerts)
        label = self.day.strftime('%a %d %b %Y')
        if self.day == wall_time.date():
            label += ' (today)'
        a.centred(d, 27, label, a.SMALL, a.TEXT)
        note = self.clock_note()
        if note and a.portrait():
            a.centred(d, 44, note, a.TINY, a.WARN)
        entries = self.planner.entries(self.day)
        rects = self.entry_rects(len(entries))
        if not entries:
            a.centred(d, a.H//2 - 10, 'Nothing planned. Tap ADD.', a.SMALL, a.MUTED)
        for i, (rect, entry) in enumerate(zip(rects, entries)):
            chosen = i == self.selected
            d.rounded_rectangle(rect, radius=4, fill=a.DANGER if chosen else a.INSET)
            time = entry['time'] or 'all day'
            d.text((rect[0]+6, (rect[1]+rect[3])//2), time, font=a.SMALL,
                   fill=a.TEXT if chosen else a.BLUE, anchor='lm')
            left = rect[0] + 12 + d.textlength('all day', font=a.SMALL)
            d.text((left, (rect[1]+rect[3])//2), a.fit_text(d, entry['text'], rect[2]-left-6, a.SMALL),
                   font=a.SMALL, fill=a.TEXT, anchor='lm')
        hidden = len(entries) - len(rects)
        if hidden > 0 and entries:
            d.text((a.W-6, rects[-1][3]+1), f'+{hidden} more', font=a.TINY, fill=a.MUTED, anchor='ra')
        if self.selected is not None:
            labels = ('CANCEL', '', 'DELETE', '')
        else:
            labels = ('MONTH', '<', '>', 'ADD')
        for rect, text in zip(bottom_buttons(a.W, a.H), labels):
            if text:
                a.button(d, rect, text, fill=a.DANGER if text == 'DELETE' else None)
        if self.selected is not None:
            a.centred(d, a.H-48, 'Delete this entry?', a.SMALL, a.WARN)
        elif self.message:
            a.centred(d, a.H-48, self.message, a.SMALL, a.WARN)
        elif note and not a.portrait():
            a.centred(d, a.H-48, note, a.SMALL, a.WARN)

    def draw_time(self, d, wall_time, alerts):
        a = self.a
        self.header(d, 'PLANNER / TIME', wall_time, alerts)
        a.centred(d, 28, self.text, a.SMALL, a.MUTED)
        top = self.time_rects()[0][1]
        a.centred(d, top - 34, self.time or 'All day', a.FONT, a.TEXT)
        if self.time:
            for rect, (label, _) in zip(self.time_rects(), TIME_STEPS):
                a.button(d, rect, label)
        else:
            a.centred(d, top + 8, 'No time: shown at the top of the day', a.SMALL, a.MUTED)
        labels = ('EDIT', 'ALL DAY' if self.time else 'SET TIME', '', 'SAVE')
        for rect, text in zip(bottom_buttons(a.W, a.H), labels):
            if text:
                a.button(d, rect, text, active=text == 'SAVE')

    def draw_keyboard(self, d, wall_time, alerts):
        a = self.a
        self.header(d, 'PLANNER / NEW ENTRY', wall_time, alerts)
        box = field_rect(a.W, a.H)
        d.rounded_rectangle(box, radius=5, fill=a.SCREEN, outline=a.PANEL)
        rows = (box[3]-box[1]-4) // LINE
        if self.text:
            lines = a.wrap_text(d, self.text + '_', box[2]-box[0]-12, a.SMALL, max_lines=999)[-rows:]
            colour = a.TEXT
        else:
            lines, colour = [f"What's on {self.day.strftime('%a %d %b')}?"], a.MUTED
        for i, line in enumerate(lines):
            d.text((box[0]+6, box[1]+4 + i*LINE), line, font=a.SMALL, fill=colour)
        letter_font = a.SMALL if a.portrait() else a.FONT
        names = {'del': 'DEL', 'back': 'BACK', 'space': 'SPACE', 'ask': 'NEXT'}
        for rect, key in keyboard_rects(a.W, a.H, self.symbols):
            label = ('ABC' if self.symbols else '123') if key == 'mode' else names.get(key, key.upper())
            a.button(d, rect, label, active=key == 'ask' and bool(self.text.strip()),
                     font_=letter_font if len(label) == 1 else a.SMALL)
