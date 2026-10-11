"""ASK page: type a question on the panel and read the librarian's answer as it streams.

The answer comes from cyberdeck-core's /api/ask on a background thread, so the panel keeps
drawing and taking touches during the minute the model can take on the Pi. If the backend
isn't running the page says so; it never answers by itself."""
import socket
import threading
import time
from urllib.request import ProxyHandler, build_opener

from PIL import Image, ImageDraw

from core.cli import BackendMissing, CitationFilter, backend_events

LETTERS = ('qwertyuiop', "asdfghjkl'", 'zxcvbnm?')
SYMBOLS = ('1234567890', '-/:;()&@"$', '.,!%+#=*')
KEY_LABELS = {'del': 'DEL', 'back': 'BACK', 'space': 'SPACE', 'ask': 'ASK'}
# Questions the Restarters archive answers in the benchmark; recent questions come first.
EXAMPLES = ('Why does my coffee maker leak?', 'My table lamp flickers. What should I check?',
            "Why won't my kettle switch on?", 'How do I replace a laptop battery?')
MAX_QUESTION = 200
RECENT = 3
LINE = 15  # Pixels per line of answer text.


def keyboard_rects(width, height, symbols=False):
    """(rect, key) for every key. Rows are ten units wide; DEL ends the third row and the
    bottom row is BACK, 123/ABC, SPACE and ASK."""
    top = 76 if height > width else 58
    gap = 3
    pitch = (height - 2 - top + gap) / 4
    unit = (width - 4 + gap) / 10
    rows = [[(key, 1) for key in row] for row in (SYMBOLS if symbols else LETTERS)]
    rows[2].append(('del', 2))
    rows.append([('back', 2), ('mode', 2), ('space', 3), ('ask', 3)])
    keys = []
    for r, row in enumerate(rows):
        y0, x = top + r*pitch, 2.
        for key, span in row:
            keys.append(((round(x), round(y0), round(x + span*unit - gap), round(y0 + pitch - gap)), key))
            x += span*unit
    return keys


def field_rect(width, height):
    """The question box above the keyboard: two lines in landscape, three in portrait."""
    return (2, 26, width-3, 70 if height > width else 52)


def answer_buttons(width, height):
    gap, count = 4, 4
    size = (width - 4 - (count-1)*gap) / count
    return [(round(2 + i*(size+gap)), height-31, round(2 + i*(size+gap) + size), height-3)
            for i in range(count)]


def example_rects(width, height, count):
    """One full-width row per question, then BACK."""
    top, bottom, gap = 28, height-3, 4
    pitch = min(34, (bottom - top + gap) / (count + 1))
    return [(2, round(top + i*pitch), width-3, round(top + i*pitch + pitch - gap))
            for i in range(count + 1)]


class _Capture:
    """Opener that keeps the response, so STOP can cut a read that is waiting on the model."""
    def __init__(self, app, opener):
        self.app, self.opener = app, opener

    def open(self, url, timeout):
        response = self.opener.open(url, timeout=timeout)
        self.app.response = response
        return response


class AskApp:
    def __init__(self, api, base_url, events=backend_events, opener=None, clock=time.monotonic):
        self.a, self.base_url, self.events, self.clock = api, base_url, events, clock
        self.opener = opener or build_opener(ProxyHandler({}))
        self.view, self.question, self.symbols = 'type', '', False
        self.recent = []
        self.lock = threading.Lock()
        self.cancel = threading.Event()
        self.thread = self.response = None
        self.scroll = None  # First line shown; None follows the end of the answer.
        self.wrapped = (None, [])
        self.rects = []
        self.clear_answer()

    def clear_answer(self):
        self.asked, self.stage, self.text, self.error = '', '', '', ''
        self.sources, self.notes, self.source_label = [], [], 'Sources'
        self.passages, self.model = 0, ''
        self.started = self.finished = None

    @property
    def running(self):
        return self.thread is not None and self.thread.is_alive()

    @property
    def delay(self):
        return .5 if self.running else 1.

    def note(self):
        """Line for the HOME tile."""
        if self.running:
            return 'Answering...'
        return 'Backend off' if self.base_url is None else 'Offline answers'

    # Asking ---------------------------------------------------------------

    def ask(self, question):
        question = ' '.join(question.split())
        if self.running:
            self.view = 'answer'  # One question at a time: show the one still answering.
            return
        if not question:
            return
        with self.lock:
            self.clear_answer()
            self.asked, self.stage, self.started = question, 'searching', self.clock()
        self.recent = [question] + [q for q in self.recent if q != question][:RECENT-1]
        self.view, self.scroll = 'answer', None
        if self.base_url is None:
            self.failed('The panel was started with --backend off, so it cannot ask.')
            return
        self.cancel.clear()
        self.thread = threading.Thread(target=self.run, args=(question,), name='ask', daemon=True)
        self.thread.start()

    def failed(self, error):
        with self.lock:
            self.stage, self.error, self.finished = 'error', error, self.clock()

    def run(self, question):
        citations = CitationFilter(0)
        events = self.events(question, self.base_url, opener=_Capture(self, self.opener), timeout=240)
        try:
            for event in events:
                if self.cancel.is_set():
                    break
                kind = event.get('event')
                with self.lock:
                    if kind == 'passages':
                        passages = event.get('passages') or []
                        citations = CitationFilter(len(passages))
                        self.passages, self.model = len(passages), event.get('model') or ''
                        self.stage = 'reading' if passages else 'answering'
                    elif kind == 'text':
                        self.text += citations.feed(str(event.get('text', '')))
                        self.stage = 'answering'
                    elif kind == 'error':
                        self.stage, self.error = 'error', str(event.get('error') or 'the backend failed')
                        return
                    elif kind == 'done':
                        self.finish(event)
                        return
            with self.lock:
                if self.cancel.is_set():
                    self.stage = 'stopped'
                else:
                    self.stage, self.error = 'error', 'The answer ended early.'
        except BackendMissing:
            self.failed('The backend is not running, so there is no one to ask. '
                        'Start cyberdeck-core (see docs/CORE.md) and try again.')
        except (OSError, ValueError) as exc:
            self.failed(f'Lost the backend: {exc}')
        finally:
            close = getattr(events, 'close', None)
            if close:
                close()  # Drops the connection, which stops the model.
            self.response = None
            with self.lock:
                self.finished = self.clock()

    def finish(self, event):
        """Called with the lock held: the checked answer replaces the streamed text."""
        self.text = str(event.get('answer') or '')
        cited = event.get('cited') or []
        self.sources = [f"[{s.get('n')}] {s.get('title', '')} ({s.get('source', '')})"
                        for s in event.get('sources') or [] if s.get('n') in cited or not cited]
        self.source_label = 'Sources' if cited else 'Searched'
        if event.get('uncited'):
            self.notes.append('UNVERIFIED: the answer cites no source.')
        self.notes.extend(f'Warning: {w}' for w in event.get('warnings') or [])
        self.stage = 'done'

    def stop(self):
        self.cancel.set()
        sock = getattr(getattr(getattr(self.response, 'fp', None), 'raw', None), '_sock', None)
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)  # Wakes a read that is waiting for the first word.
            except OSError:
                pass
        with self.lock:
            if self.stage not in ('done', 'error'):
                self.stage = 'stopped'

    # Touch ----------------------------------------------------------------

    def tap(self, x, y, now):
        a = self.a
        if self.view == 'answer':
            choice = a.index_at(answer_buttons(a.W, a.H), x, y, slop=3)
            if choice == 0:
                if self.running:
                    self.stop()
                else:
                    self.view = 'type'
                    self.question = self.asked
            elif choice in (1, 2):
                first, _, visible, _ = self.layout()
                step = max(1, visible-1)
                self.scroll = max(0, first - step) if choice == 1 else first + step
            elif choice == 3:
                return 'exit'
            return None
        if self.view == 'examples':
            questions = self.examples()
            choice = a.index_at(example_rects(a.W, a.H, len(questions)), x, y, slop=2)
            if choice is not None and choice < len(questions):
                self.ask(questions[choice])
            elif choice is not None:
                self.view = 'type'
            return None
        if a.index_at([field_rect(a.W, a.H)], x, y, slop=2) is not None:
            if self.question:
                self.question = ''
            else:
                self.view = 'examples'
            return None
        keys = keyboard_rects(a.W, a.H, self.symbols)
        choice = a.index_at([rect for rect, _ in keys], x, y, slop=1)
        if choice is None:
            return None
        key = keys[choice][1]
        if key == 'back':
            return 'exit'
        if key == 'mode':
            self.symbols = not self.symbols
        elif key == 'del':
            self.question = self.question[:-1]
        elif key == 'ask':
            self.ask(self.question)
        elif len(self.question) < MAX_QUESTION:
            if key != 'space':
                self.question += key
            elif self.question and not self.question.endswith(' '):
                self.question += ' '
        return None

    def examples(self):
        return (self.recent + [q for q in EXAMPLES if q not in self.recent])[:5]

    # Drawing --------------------------------------------------------------

    def status(self):
        now = self.finished if self.finished is not None and not self.running else self.clock()
        seconds = int(now - self.started) if self.started is not None else 0
        return {'searching': f'Searching... {seconds}s',
                'reading': f'Reading... {seconds}s',
                'answering': f'Answering... {seconds}s', 'done': f'Done in {seconds}s',
                'stopped': 'Stopped', 'error': 'Error'}.get(self.stage, '')

    def body(self):
        """Paragraphs for the answer view."""
        if self.stage == 'error':
            return [self.error]
        if self.text:
            parts = [self.text]
        elif self.stage == 'searching':
            parts = ['Searching the offline library...']
        elif self.stage == 'reading':
            parts = [f'Reading {self.passages} source(s) with {self.model or "the model"}. '
                     'The first word can take a minute on the Pi.']
        elif self.stage == 'stopped':
            parts = ['Stopped before the answer started.']
        else:
            parts = ['Waiting for the answer...']
        parts += self.notes
        if self.sources:
            parts += ['', f'{self.source_label}:'] + self.sources
        return parts

    def layout(self):
        """(first line, lines, visible line count, text rect) for the answer view."""
        a = self.a
        rect = (6, 44, a.W-10, a.H-36)
        with self.lock:
            paragraphs = self.body()
        key = (tuple(paragraphs), a.W)
        if self.wrapped[0] != key:
            d = ImageDraw.Draw(Image.new('RGB', (1, 1)))
            lines = []
            for paragraph in paragraphs:
                lines += a.wrap_text(d, paragraph, rect[2]-rect[0], a.SMALL, max_lines=999) or ['']
            self.wrapped = (key, lines)
        lines = self.wrapped[1]
        visible = max(1, (rect[3]-rect[1]) // LINE)
        last = max(0, len(lines) - visible)
        if self.scroll is not None and self.scroll >= last:
            self.scroll = None  # Scrolled back to the end: follow new text again.
        first = last if self.scroll is None else max(0, self.scroll)
        return first, lines, visible, rect

    def header(self, d, title, wall_time, alerts):
        a = self.a
        if alerts:
            a.draw_banner(d, alerts, wall_time.strftime('%H:%M'))
        else:
            d.text((8, 4), title, font=a.FONT, fill=a.ACCENT)
            d.text((a.W-8, 6), wall_time.strftime('%H:%M:%S'), font=a.SMALL, fill=a.TEXT, anchor='ra')

    def render(self, now, wall_time, alerts):
        a = self.a
        image = Image.new('RGB', (a.W, a.H), a.BG)
        d = ImageDraw.Draw(image)
        if self.view == 'answer':
            self.draw_answer(d, wall_time, alerts)
        elif self.view == 'examples':
            self.header(d, 'ASK / EXAMPLES', wall_time, alerts)
            questions = self.examples()
            rects = example_rects(a.W, a.H, len(questions))
            for rect, question in zip(rects, questions):
                a.button(d, rect, question)
            a.button(d, rects[-1], 'BACK')
        else:
            self.draw_keyboard(d, wall_time, alerts)
        return image

    def draw_keyboard(self, d, wall_time, alerts):
        a = self.a
        self.header(d, 'ASK THE LIBRARIAN', wall_time, alerts)
        box = field_rect(a.W, a.H)
        d.rounded_rectangle(box, radius=5, fill='#0b1218', outline=a.PANEL)
        room = box[2]-box[0]-26
        rows = (box[3]-box[1]-4) // LINE
        if self.question:
            lines = a.wrap_text(d, self.question + '_', room, a.SMALL, max_lines=999)[-rows:]
            for i, line in enumerate(lines):
                d.text((box[0]+6, box[1]+4 + i*LINE), line, font=a.SMALL, fill=a.TEXT)
            d.text((box[2]-8, (box[1]+box[3])//2), 'x', font=a.SMALL, fill=a.MUTED, anchor='mm')
        else:
            hint = a.wrap_text(d, 'Type a question, or tap here for examples', room+18, a.SMALL, rows)
            for i, line in enumerate(hint):
                d.text((box[0]+6, box[1]+4 + i*LINE), line, font=a.SMALL, fill=a.MUTED)
        letter_font = a.SMALL if a.portrait() else a.FONT  # Portrait keys are 21 px wide.
        for rect, key in keyboard_rects(a.W, a.H, self.symbols):
            if key == 'mode':
                label = 'ABC' if self.symbols else '123'
            else:
                label = KEY_LABELS.get(key, key.upper())
            a.button(d, rect, label, active=key == 'ask' and bool(self.question.strip()),
                     font_=letter_font if len(label) == 1 else a.SMALL)

    def draw_answer(self, d, wall_time, alerts):
        a = self.a
        self.header(d, 'ASK', wall_time, alerts)
        status = self.status()
        colour = a.DANGER if self.stage == 'error' else a.WARN if self.running else a.ACCENT
        status_width = d.textlength(status, font=a.SMALL)
        d.text((a.W-6, 28), status, font=a.SMALL, fill=colour, anchor='ra')
        d.text((6, 28), a.fit_text(d, self.asked, a.W-18-status_width, a.SMALL),
               font=a.SMALL, fill=a.MUTED)
        first, lines, visible, rect = self.layout()
        for i, line in enumerate(lines[first:first+visible]):
            d.text((rect[0], rect[1] + i*LINE), line, font=a.SMALL,
                   fill=a.WARN if line.startswith(('UNVERIFIED', 'Warning')) else a.TEXT)
        if len(lines) > visible:  # Scroll bar, so text below the fold isn't missed.
            span = rect[3]-rect[1]
            top = rect[1] + span*first//len(lines)
            d.rectangle((a.W-5, top, a.W-3, top + max(6, span*visible//len(lines))), fill=a.MUTED)
        labels = ('STOP' if self.running else 'EDIT', 'UP', 'DOWN', 'HOME')
        for rect, label in zip(answer_buttons(a.W, a.H), labels):
            a.button(d, rect, label, fill=a.DANGER if label == 'STOP' else None)
