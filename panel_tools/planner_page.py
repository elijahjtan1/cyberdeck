"""PLANNER for the main app, drawn at the full screen size.

It reuses the quick panel's PlannerApp for the entries, the views and the keys, so both
apps read and write the same cyberdeck-data/planner.json. Only layout, drawing and touch
are new. A new entry is typed on the keyboard, not on an on-screen one. `ui` is main_app."""
from PIL import Image, ImageDraw

from .main_pages import MARGIN, button_rects, draw_buttons, fit, index_at, query_box
from .planner import DAY_NAMES, MAX_PER_DAY, MAX_TEXT, TIME_STEPS, PlannerApp, month_cells, step_time

NAME_ROW = 30  # Day names above the month grid.
PITCH = 52     # One entry row in the day view.


class PlannerPage(PlannerApp):
    """A month calendar, each day's timed entries, and adding or deleting them."""
    def __init__(self, ui, data, sync=lambda: 'Unknown', owns=lambda p: None, size=(1024, 600)):
        super().__init__(ui, data, sync, owns)
        self.ui, self.size = ui, size

    # Layout -------------------------------------------------------------------

    def body_bottom(self):
        """Lowest y for page content: above the message line over the buttons."""
        return button_rects(self.size)[0][1] - 40

    def grid_rect(self):
        width = self.size[0]
        return (MARGIN, self.ui.HEADER + 64, width - MARGIN, self.body_bottom())

    def day_rects(self, month):
        x0, y0, x1, y1 = self.grid_rect()
        weeks = month_cells(month.year, month.month)
        cell_w, cell_h = (x1-x0) / 7, (y1-y0-NAME_ROW) / len(weeks)
        return [((round(x0 + c*cell_w), round(y0 + NAME_ROW + r*cell_h),
                  round(x0 + (c+1)*cell_w) - 6, round(y0 + NAME_ROW + (r+1)*cell_h) - 6), day)
                for r, week in enumerate(weeks) for c, day in enumerate(week) if day]

    def entry_layout(self):
        top = self.ui.HEADER + 64
        return top, PITCH, max(1, (self.body_bottom() - top) // PITCH)

    def entry_rects(self, count):
        top, pitch, rows = self.entry_layout()
        right = self.size[0] - MARGIN - (76 if count > rows else 0)
        return [(MARGIN, top + i*pitch, right, top + i*pitch + pitch - 8) for i in range(min(count, rows))]

    def scroll_rects(self, count):
        top, pitch, rows = self.entry_layout()
        if count <= rows:
            return []
        bottom, x = top + rows*pitch - 8, self.size[0] - MARGIN - 64
        middle = (top + bottom) // 2
        return [(x, top, self.size[0] - MARGIN, middle - 4), (x, middle + 4, self.size[0] - MARGIN, bottom)]

    def time_rects(self):
        width, height = self.size
        gap, w = 12, 150
        left = (width - 4*w - 3*gap) // 2
        top = height // 2 + 10
        return [(left + i*(w+gap), top, left + i*(w+gap) + w, top + 60) for i in range(4)]

    # Touch --------------------------------------------------------------------

    def tap(self, x, y, now):
        choice = index_at(button_rects(self.size), x, y)
        if self.view == 'type':
            return self.press({0: 'back', 3: 'ask'}[choice]) if choice in (0, 3) else None
        if self.view == 'time' and self.time:
            step = index_at(self.time_rects(), x, y)
            if step is not None:
                self.time = step_time(self.time, TIME_STEPS[step][1])
                return None
        if choice is not None or self.selected is not None:
            return self.button(choice)
        if self.view == 'month':
            days = self.day_rects(self.shown or self.now().date().replace(day=1))
            hit = index_at([rect for rect, _ in days], x, y)
            if hit is not None:
                self.open_day(days[hit][1])
        elif self.view == 'day':
            count = len(self.planner.entries(self.day))
            scroll = index_at(self.scroll_rects(count), x, y)
            if scroll is not None:
                self.scroll(-1 if scroll == 0 else 1)
                return None
            hit = index_at(self.entry_rects(count), x, y)
            if hit is not None:
                self.selected = self.first + hit
        return None

    # Drawing ------------------------------------------------------------------

    def render(self, now, wall_time, alerts):
        ui = self.ui
        self.wall = wall_time
        image = Image.new('RGB', self.size, ui.BG)
        d = ImageDraw.Draw(image)
        titles = {'time': 'PLANNER / TIME', 'type': 'PLANNER / NEW ENTRY'}
        ui.header(d, self.size[0], titles.get(self.view, 'PLANNER'), alerts, wall_time.strftime('%H:%M'))
        {'month': self.draw_month, 'day': self.draw_day,
         'time': self.draw_time}.get(self.view, self.draw_keyboard)(d, wall_time)
        return image

    def line(self, d, text, colour):
        """The message line just above the buttons."""
        d.text((self.size[0]//2, self.body_bottom() + 20), fit(d, text, self.size[0] - 2*MARGIN, self.ui.BODY),
               font=self.ui.BODY, fill=colour, anchor='mm')

    def subtitle(self, d, text):
        """The line under the header, with the clock warning on the right when there is one."""
        ui = self.ui
        y, room = ui.HEADER + 32, self.size[0] - 2*MARGIN
        note = self.clock_note()
        if note:
            d.text((self.size[0] - MARGIN, y), note, font=ui.BODY, fill=ui.WARN, anchor='rm')
            room -= d.textlength(note, font=ui.BODY) + 24
        d.text((MARGIN, y), fit(d, text, room, ui.TITLE), font=ui.TITLE, fill=ui.TEXT, anchor='lm')

    def draw_month(self, d, wall_time):
        ui = self.ui
        today = wall_time.date()
        month = self.shown or today.replace(day=1)
        self.subtitle(d, month.strftime('%B %Y'))
        x0, y0, x1, _ = self.grid_rect()
        cell_w = (x1-x0) / 7
        for c, name in enumerate(DAY_NAMES):
            d.text((x0 + (c+.5)*cell_w - 3, y0 + NAME_ROW//2), name, font=ui.SMALL_BOLD,
                   fill=ui.WARN if c >= 5 else ui.MUTED, anchor='mm')
        for rect, day in self.day_rects(month):
            count = len(self.planner.entries(day))
            is_today = day == today
            d.rounded_rectangle(rect, 8, fill='#132331' if count else '#0b1016',
                                outline=ui.ACCENT if is_today else '#1f2b36', width=3 if is_today else 1)
            d.text((rect[0]+10, rect[1]+6), str(day.day), font=ui.BODY,
                   fill=ui.ACCENT if is_today else ui.TEXT)
            if count:
                d.text((rect[2]-10, rect[3]-6), f'{count} planned' if rect[2]-rect[0] > 110 else str(count),
                       font=ui.SMALL, fill=ui.YELLOW, anchor='rd')
        self.line(d, 'Tap a day to open it. Arrows change month, Enter opens today.', ui.MUTED)
        draw_buttons(ui, d, self.size, ('< MONTH', 'TODAY', 'MONTH >', 'HOME'))

    def draw_day(self, d, wall_time):
        ui = self.ui
        today = ' (today)' if self.day == wall_time.date() else ''
        label = self.day.strftime('%A %d %B %Y') + today
        room = self.size[0] - 2*MARGIN - 24 - d.textlength(self.clock_note() or '', font=ui.BODY)
        if d.textlength(label, font=ui.TITLE) > room:  # Keep "(today)" beside the clock warning.
            label = self.day.strftime('%a %d %b %Y') + today
        self.subtitle(d, label)
        entries = self.planner.entries(self.day)
        self.clamp_first(len(entries))
        rects = self.entry_rects(len(entries))
        if not entries:
            d.text((self.size[0]//2, (self.entry_layout()[0] + self.body_bottom())//2),
                   'Nothing planned. Tap ADD or start typing.', font=ui.READ, fill=ui.MUTED, anchor='mm')
        time_width = d.textlength('all day', font=ui.READ)
        for i, (rect, entry) in enumerate(zip(rects, entries[self.first:]), self.first):
            chosen = i == self.selected
            d.rounded_rectangle(rect, 8, fill='#2a1214' if chosen else '#0e161d',
                                outline=ui.DANGER if chosen else None, width=2)
            middle = (rect[1]+rect[3])//2
            d.text((rect[0]+16, middle), entry['time'] or 'all day', font=ui.READ, fill=ui.BLUE, anchor='lm')
            left = rect[0] + 36 + time_width
            d.text((left, middle), fit(d, entry['text'], rect[2]-left-16, ui.READ), font=ui.READ,
                   fill=ui.TEXT, anchor='lm')
        scroll = self.scroll_rects(len(entries))
        ends = (self.first > 0, self.first + len(rects) < len(entries))
        for rect, label, more in zip(scroll, ('UP', 'DOWN'), ends):
            d.rounded_rectangle(rect, 8, fill='#0e161d', outline='#2a3a48', width=2)
            d.text(((rect[0]+rect[2])//2, (rect[1]+rect[3])//2), label, font=ui.SMALL_BOLD,
                   fill=ui.TEXT if more else ui.MUTED, anchor='mm')
        if self.selected is not None:
            self.line(d, 'Delete this entry? Backspace or DELETE removes it.', ui.WARN)
            draw_buttons(ui, d, self.size, ('CANCEL', '', 'DELETE', ''), danger=('DELETE',))
            return
        if self.message:
            self.line(d, self.message, ui.WARN)
        elif len(entries) >= MAX_PER_DAY:
            self.line(d, f'A day holds up to {MAX_PER_DAY} entries', ui.MUTED)
        else:
            self.line(d, 'Tap an entry to delete it. Left and right change day, Esc: month.', ui.MUTED)
        draw_buttons(ui, d, self.size, ('MONTH', '< DAY', 'DAY >', 'ADD'))

    def draw_time(self, d, wall_time):
        ui = self.ui
        self.subtitle(d, self.text)
        top = self.time_rects()[0][1]
        d.text((self.size[0]//2, top - 70), self.time or 'All day', font=ui.BIG, fill=ui.TEXT, anchor='mm')
        if self.time:
            for rect, (label, _) in zip(self.time_rects(), TIME_STEPS):
                d.rounded_rectangle(rect, 8, fill='#0e161d', outline='#2a3a48', width=2)
                d.text(((rect[0]+rect[2])//2, (rect[1]+rect[3])//2), label, font=ui.LABEL,
                       fill=ui.TEXT, anchor='mm')
            self.line(d, 'Up and down: 15 minutes. Page up and down: an hour. Enter saves.', ui.MUTED)
        else:
            d.text((self.size[0]//2, top + 30), 'No time: shown at the top of the day', font=ui.READ,
                   fill=ui.MUTED, anchor='mm')
            self.line(d, 'Enter saves. Esc goes back to the text.', ui.MUTED)
        draw_buttons(ui, d, self.size, ('EDIT', 'ALL DAY' if self.time else 'SET TIME', '', 'SAVE'))

    def draw_keyboard(self, d, wall_time):
        ui = self.ui
        query_box(ui, d, self.size, self.text, f"What's on {self.day.strftime('%a %d %b')}? Type it, then Enter")
        tips = ('Enter goes on to the time. Esc goes back to the day.', f'Up to {MAX_TEXT} characters.')
        for i, tip in enumerate(tips):
            d.text((MARGIN, ui.HEADER + 100 + i*36), tip, font=ui.BODY, fill=ui.MUTED)
        draw_buttons(ui, d, self.size, ('BACK', '', '', 'NEXT'))
