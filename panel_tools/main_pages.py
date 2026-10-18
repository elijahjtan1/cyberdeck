"""ASK and WIKIPEDIA for the main app, drawn at the full screen size.

They reuse the quick panel's apps for everything but drawing and input: the backend calls,
streaming, honesty notes and paging come from AskApp and ReaderApp, so both apps say the
same things. Typing is on the keyboard; touch picks results, examples and the buttons
along the bottom. `ui` is main_app (fonts, colours, header and footer)."""
from PIL import Image, ImageDraw

from .ask import AskApp
from .reader import ReaderApp

MARGIN = 28
TOP = 136  # Below the header and the query line.
BUTTON_H = 52
RESULTS_PER_PAGE = 6
WARN_PREFIXES = ('UNVERIFIED', 'PARTIAL', 'QUOTED', 'Warning')


def wrap(d, text, width, chosen):
    """Lines of `text` no wider than `width`; a word too long for a line is cut."""
    lines, line = [], ''
    for word in text.split():
        while d.textlength(word, font=chosen) > width and len(word) > 1:
            cut = len(word) - 1
            while cut > 1 and d.textlength(word[:cut], font=chosen) > width:
                cut -= 1
            if line:
                lines.append(line)
                line = ''
            lines.append(word[:cut])
            word = word[cut:]
        trial = f'{line} {word}'.strip()
        if line and d.textlength(trial, font=chosen) > width:
            lines.append(line)
            line = word
        else:
            line = trial
    return lines + [line] if line else lines


def fit(d, text, width, chosen):
    if d.textlength(text, font=chosen) <= width:
        return text
    while text and d.textlength(text + '...', font=chosen) > width:
        text = text[:-1]
    return text + '...'


def button_rects(size, count=4):
    width, height = size
    gap = 12
    w = (width - 2*MARGIN - (count-1)*gap) / count
    y1 = height - 14
    return [(round(MARGIN + i*(w+gap)), y1 - BUTTON_H, round(MARGIN + i*(w+gap) + w), y1)
            for i in range(count)]


def text_rect(size):
    width, height = size
    return (MARGIN, TOP, width - MARGIN - 12, height - BUTTON_H - 30)


def index_at(rects, x, y):
    for i, r in enumerate(rects):
        if r[0] <= x <= r[2] and r[1] <= y <= r[3]:
            return i
    return None


def draw_buttons(ui, d, size, labels, danger=()):
    for rect, label in zip(button_rects(size, len(labels)), labels):
        if label:
            d.rounded_rectangle(rect, 8, fill='#2a1214' if label in danger else '#0e161d',
                                outline=ui.DANGER if label in danger else '#2a3a48', width=2)
            d.text(((rect[0]+rect[2])//2, (rect[1]+rect[3])//2), label, font=ui.SMALL_BOLD,
                   fill=ui.TEXT, anchor='mm')


def query_box(ui, d, size, text, hint):
    width = size[0]
    box = (MARGIN, ui.HEADER + 14, width - MARGIN, ui.HEADER + 66)
    d.rounded_rectangle(box, 8, fill='#0b1218', outline=ui.ACCENT if text else '#2a3a48', width=2)
    shown = fit(d, text + '_', box[2]-box[0]-24, ui.BODY) if text else hint
    d.text((box[0]+14, (box[1]+box[3])//2), shown, font=ui.BODY, fill=ui.TEXT if text else ui.MUTED,
           anchor='lm')


def scroll_bar(ui, d, rect, first, shown, total):
    if total > shown:
        span = rect[3] - rect[1]
        top = rect[1] + span*first//total
        d.rectangle((rect[2]+6, top, rect[2]+9, top + max(10, span*shown//total)), fill=ui.MUTED)


class WikiPage(ReaderApp):
    """Search the offline library and read an article a screenful at a time."""
    def __init__(self, ui, base_url, opener=None, size=(1024, 600)):
        super().__init__(ui, base_url, opener)
        self.ui, self.size = ui, size
        self.result_rects = []

    def lines(self):
        """Wrapped article lines for the screen width, cached per article."""
        rect = text_rect(self.size)
        with self.lock:
            paragraphs, truncated = list(self.paragraphs), self.truncated
        key = (self.title, len(paragraphs), truncated, self.size)
        if self.wrapped[0] != key:
            d = ImageDraw.Draw(Image.new('RGB', (1, 1)))
            lines = []
            for paragraph in paragraphs:
                lines += wrap(d, paragraph, rect[2]-rect[0], self.ui.READ) + ['']
            lines = lines[:-1]
            self.note_from = len(lines) + 1 if truncated else None
            if truncated:
                lines += [''] + wrap(d, 'The article continues; this shows the first part only.',
                                     rect[2]-rect[0], self.ui.READ)
            self.wrapped = (key, lines)
        return self.wrapped[1]

    def per_page(self):
        rect = text_rect(self.size)
        return max(1, (rect[3]-rect[1]) // self.ui.READ_LINE)

    def results_per_page(self):
        return RESULTS_PER_PAGE

    def tap(self, x, y, now):
        buttons = button_rects(self.size)
        choice = index_at(buttons, x, y)
        if self.view == 'article':
            if choice is None and y > self.ui.HEADER and not self.running:
                choice = 1 if x < self.size[0] // 3 else 2  # Tap the left or right of the text.
            return self.button(choice)
        if self.view == 'results':
            if choice is None:
                hit = index_at(self.result_rects, x, y)
                shown = self.shown_results()
                if hit is not None and hit < len(shown):
                    self.open(shown[hit])
            return self.button(choice)
        if choice == 3:
            return 'exit'
        if choice == 0 and self.title:
            self.view = 'article'
        elif choice == 2:
            self.search(self.query)
        return None

    def render(self, now, wall_time, alerts):
        ui = self.ui
        image = Image.new('RGB', self.size, ui.BG)
        d = ImageDraw.Draw(image)
        ui.header(d, self.size[0], 'WIKIPEDIA', alerts, wall_time.strftime('%H:%M'))
        with self.lock:
            status, error = self.status, self.error
        if self.view == 'article':
            self.draw_article(d, status, error)
        elif self.view == 'results':
            self.draw_results(d, status, error)
        else:
            query_box(ui, d, self.size, self.query, 'Type to search the offline library, then Enter')
            tips = ['Enter searches. Number keys open a result. Arrows or Space turn pages.',
                    'Esc goes back a step, and from here back to home.']
            if self.title:
                tips.insert(0, f'Open: {self.title}. Tap ARTICLE to go back to it.')
            for i, tip in enumerate(tips):
                d.text((MARGIN, TOP + i*36), tip, font=ui.BODY, fill=ui.MUTED)
            draw_buttons(ui, d, self.size, ('ARTICLE' if self.title else '', '', 'SEARCH', 'HOME'))
        return image

    def message(self, d, text, colour):
        rect = text_rect(self.size)
        for i, line in enumerate(wrap(d, text, rect[2]-rect[0], self.ui.READ)[:6]):
            d.text((rect[0], rect[1] + i*self.ui.READ_LINE), line, font=self.ui.READ, fill=colour)

    def draw_results(self, d, status, error):
        ui, width = self.ui, self.size[0]
        query_box(ui, d, self.size, self.query, '')
        shown = self.shown_results()
        self.result_rects = []
        pitch = (text_rect(self.size)[3] - TOP + 8) / RESULTS_PER_PAGE
        for i, row in enumerate(shown):
            rect = (MARGIN, round(TOP + i*pitch), width - MARGIN, round(TOP + (i+1)*pitch) - 8)
            self.result_rects.append(rect)
            d.rounded_rectangle(rect, 8, fill='#0e161d')
            d.text((rect[0]+14, (rect[1]+rect[3])//2), str(i+1), font=ui.BODY, fill=ui.MUTED, anchor='lm')
            source = 'local' if row.get('source') == 'local' else ''
            if source:
                d.text((rect[2]-14, (rect[1]+rect[3])//2), source, font=ui.SMALL, fill=ui.MUTED, anchor='rm')
            d.text((rect[0]+50, (rect[1]+rect[3])//2), fit(d, str(row.get('title', '')), rect[2]-rect[0]-140, ui.READ),
                   font=ui.READ, fill=ui.TEXT, anchor='lm')
        if status:
            self.message(d, status, ui.WARN)
        elif error and not shown:
            self.message(d, error, ui.WARN)
        elif error:
            d.text((MARGIN, self.size[1] - BUTTON_H - 40), fit(d, error, width - 2*MARGIN, ui.SMALL),
                   font=ui.SMALL, fill=ui.WARN)
        draw_buttons(ui, d, self.size, ('SEARCH', '< MORE' if self.result_page else '',
                                        'MORE >' if self.more_results() else '', 'HOME'))

    def draw_article(self, d, status, error):
        ui = self.ui
        lines = [] if status or error else self.lines()
        per = self.per_page()
        pages = max(1, -(-len(lines) // per))
        self.page = min(self.page, pages - 1)
        counter = f'Page {self.page+1} of {pages}' if lines else ''
        width = self.size[0]
        d.text((width - MARGIN, ui.HEADER + 40), counter, font=ui.BODY, fill=ui.MUTED, anchor='rm')
        d.text((MARGIN, ui.HEADER + 40), fit(d, self.title, width - 2*MARGIN - d.textlength(counter, font=ui.BODY) - 20,
                                             ui.TITLE), font=ui.TITLE, fill=ui.ACCENT, anchor='lm')
        rect = text_rect(self.size)
        if status or error:
            self.message(d, status or error, ui.WARN)
        for i, line in enumerate(lines[self.page*per:(self.page+1)*per]):
            muted = self.note_from is not None and self.page*per + i >= self.note_from
            d.text((rect[0], rect[1] + i*ui.READ_LINE), line, font=ui.READ, fill=ui.MUTED if muted else ui.TEXT)
        scroll_bar(ui, d, rect, self.page*per, per, len(lines))
        draw_buttons(ui, d, self.size, ('RESULTS' if self.results else 'SEARCH', '< PAGE', 'PAGE >', 'HOME'))


class AskPage(AskApp):
    """Ask the librarian and read the answer as it streams, with its sources and notes."""
    def __init__(self, ui, base_url, size=(1024, 600), **kwargs):
        super().__init__(ui, base_url, **kwargs)
        self.ui, self.size = ui, size
        self.example_rects = []

    def layout(self):
        """(first line, lines, visible line count, text rect) for the answer view."""
        rect = text_rect(self.size)
        with self.lock:
            paragraphs = self.body()
        key = (tuple(paragraphs), self.size)
        if self.wrapped[0] != key:
            d = ImageDraw.Draw(Image.new('RGB', (1, 1)))
            lines = []
            for paragraph in paragraphs:
                lines += wrap(d, paragraph, rect[2]-rect[0], self.ui.READ) or ['']
            self.wrapped = (key, lines)
        lines = self.wrapped[1]
        visible = max(1, (rect[3]-rect[1]) // self.ui.READ_LINE)
        last = max(0, len(lines) - visible)
        if self.scroll is not None and self.scroll >= last:
            self.scroll = None  # Back at the end: follow new text again.
        first = last if self.scroll is None else max(0, self.scroll)
        return first, lines, visible, rect

    def tap(self, x, y, now):
        choice = index_at(button_rects(self.size), x, y)
        if self.view == 'answer':
            return self.button(choice)
        hit = index_at(self.example_rects, x, y)
        if hit is not None:
            self.ask(self.examples()[hit])
        elif choice == 2:
            self.ask(self.question)
        elif choice == 0 and self.asked:
            self.view = 'answer'
        elif choice == 3:
            return 'exit'
        return None

    def key(self, key, now):
        if self.view == 'examples':
            self.view = 'type'  # The examples are always shown under the question here.
        return super().key(key, now)

    def render(self, now, wall_time, alerts):
        ui = self.ui
        image = Image.new('RGB', self.size, ui.BG)
        d = ImageDraw.Draw(image)
        ui.header(d, self.size[0], 'ASK', alerts, wall_time.strftime('%H:%M'))
        if self.view == 'answer':
            self.draw_answer(d)
        else:
            self.draw_question(d)
        return image

    def draw_question(self, d):
        ui, width = self.ui, self.size[0]
        query_box(ui, d, self.size, self.question, 'Type a question, then Enter')
        d.text((MARGIN, TOP), 'Recent and example questions (tap to ask):', font=ui.BODY, fill=ui.MUTED)
        self.example_rects = []
        for i, question in enumerate(self.examples()):
            rect = (MARGIN, TOP + 40 + i*56, width - MARGIN, TOP + 88 + i*56)
            if rect[3] > button_rects(self.size)[0][1] - 8:
                break
            self.example_rects.append(rect)
            d.rounded_rectangle(rect, 8, fill='#0e161d')
            d.text((rect[0]+16, (rect[1]+rect[3])//2), fit(d, question, rect[2]-rect[0]-32, ui.READ),
                   font=ui.READ, fill=ui.TEXT, anchor='lm')
        draw_buttons(ui, d, self.size, ('ANSWER' if self.asked else '', '', 'ASK', 'HOME'))

    def draw_answer(self, d):
        ui, width = self.ui, self.size[0]
        status = self.status()
        colour = ui.DANGER if self.stage == 'error' else ui.WARN if self.running else ui.ACCENT
        status_width = d.textlength(status, font=ui.BODY)
        d.text((width - MARGIN, ui.HEADER + 40), status, font=ui.BODY, fill=colour, anchor='rm')
        d.text((MARGIN, ui.HEADER + 40), fit(d, self.asked, width - 2*MARGIN - status_width - 20, ui.TITLE),
               font=ui.TITLE, fill=ui.TEXT, anchor='lm')
        first, lines, visible, rect = self.layout()
        for i, line in enumerate(lines[first:first+visible]):
            d.text((rect[0], rect[1] + i*ui.READ_LINE), line, font=ui.READ,
                   fill=ui.WARN if line.startswith(WARN_PREFIXES) else ui.TEXT)
        scroll_bar(ui, d, rect, first, visible, len(lines))
        draw_buttons(ui, d, self.size, ('STOP' if self.running else 'EDIT', 'UP', 'DOWN', 'HOME'),
                     danger=('STOP',))
