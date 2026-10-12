"""READER app: search the offline library and read articles on the panel, a page at a time.

Search and article text come from cyberdeck-core (/api/search and /api/article), which
reads kiwix-serve and the local library. Requests run on a background thread so the panel
keeps drawing. Pictures, tables and links are left out; the text is what fits a 2.8" screen.
If the backend or Kiwix is down the page says so and never makes text up."""
import json
import threading
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, build_opener

from PIL import Image, ImageDraw

from .ask import keyboard_rects, field_rect

LINE = 15
MAX_QUERY = 100
MAX_RESULTS = 8
TIMEOUT = 20
CUT_NOTE = '[The article continues; the reader shows the first part only.]'


def bottom_buttons(width, height, count=4):
    gap = 4
    size = (width - 4 - (count-1)*gap) / count
    return [(round(2 + i*(size+gap)), height-31, round(2 + i*(size+gap) + size), height-3)
            for i in range(count)]


def result_rects(width, height, count):
    top, pitch = 46, 34 if height > width else 30
    rows = min(count, (height - 36 - top) // pitch)
    return [(2, top + i*pitch, width-3, top + i*pitch + pitch - 4) for i in range(rows)]


def text_rect(width, height):
    return (6, 44, width-10, height-36)


class ReaderApp:
    def __init__(self, api, base_url, opener=None):
        self.a, self.base_url = api, base_url
        self.opener = opener or build_opener(ProxyHandler({}))
        self.view, self.query, self.symbols = 'type', '', False
        self.results, self.result_page = [], 0
        self.title, self.paragraphs, self.truncated = '', [], False
        self.page = 0
        self.status, self.error = '', ''
        self.thread = None
        self.lock = threading.Lock()
        self.wrapped, self.note_from = (None, []), None

    @property
    def running(self):
        return self.thread is not None and self.thread.is_alive()

    @property
    def delay(self):
        return .3 if self.running else 1.

    def note(self):
        if self.base_url is None:
            return 'Backend off'
        return f'Reading: {self.title}' if self.title else 'Offline articles'

    # Fetching ------------------------------------------------------------------

    def get(self, path):
        """JSON from the backend, or a readable error. Never raises."""
        try:
            with self.opener.open(self.base_url.rstrip('/') + path, timeout=TIMEOUT) as response:
                body = response.read(2_000_000)
            if path.startswith('/api/library/'):
                return {'paragraphs': [p.strip() for p in body.decode('utf-8', 'replace').split('\n\n') if p.strip()]}
            return json.loads(body)
        except HTTPError as exc:
            try:
                message = json.loads(exc.read(4096)).get('error', '')
            except (ValueError, OSError, AttributeError):
                message = ''
            if exc.code == 404 and not message:
                message = 'The backend has no reader support yet; update cyberdeck-core.'
            return {'error': message or f'The backend refused ({exc.code}).'}
        except (URLError, OSError, ValueError):
            return {'error': 'The backend is not running. Start cyberdeck-core (see docs/CORE.md).'}

    def start(self, work):
        if self.running:
            return
        self.thread = threading.Thread(target=work, name='reader', daemon=True)
        self.thread.start()

    def search(self, query):
        query = ' '.join(query.split())
        if not query or self.running:
            return
        self.view, self.status, self.error, self.results, self.result_page = 'results', 'Searching...', '', [], 0
        if self.base_url is None:
            self.status, self.error = '', 'The panel was started with --backend off.'
            return

        def work():
            data = self.get('/api/search?' + urlencode({'q': query}))
            rows = [r for r in data.get('results') or [] if isinstance(r, dict) and r.get('url')]
            with self.lock:
                self.results = rows
                self.status = ''
                if data.get('error'):
                    self.error = str(data['error'])
                elif not rows:
                    self.error = f'Nothing in the offline library matches "{query}".'
                elif data.get('warnings'):
                    self.error = 'Some sources were unavailable: ' + '; '.join(map(str, data['warnings']))
        self.start(work)

    def open(self, result):
        if self.running:
            return
        url = str(result.get('url', ''))
        self.view, self.status, self.error = 'article', 'Opening...', ''
        self.title, self.paragraphs, self.truncated, self.page = str(result.get('title', '')), [], False, 0

        def work():
            if url.startswith('/api/library/'):
                data = self.get(url)
            else:
                data = self.get('/api/article?' + urlencode({'url': url}))
            with self.lock:
                if data.get('error'):
                    self.error = str(data['error'])
                else:
                    self.paragraphs = [str(p) for p in data.get('paragraphs') or []]
                    self.truncated = bool(data.get('truncated'))
                    if not self.paragraphs:
                        self.error = 'This article has no readable text.'
            self.lines()  # Wrap here, not on the main loop; a long article takes a second on the Pi.
            with self.lock:
                self.status = ''
        self.start(work)

    # Layout --------------------------------------------------------------------

    def lines(self):
        """Wrapped lines of the open article, cached per article and screen width."""
        a = self.a
        rect = text_rect(a.W, a.H)
        with self.lock:
            paragraphs, truncated = list(self.paragraphs), self.truncated
        key = (self.title, len(paragraphs), truncated, a.W)
        if self.wrapped[0] != key:
            d = ImageDraw.Draw(Image.new('RGB', (1, 1)))
            lines = []
            for paragraph in paragraphs:
                lines += a.wrap_text(d, paragraph, rect[2]-rect[0], a.SMALL, max_lines=999) + ['']
            lines = lines[:-1]
            self.note_from = len(lines) + 1 if truncated else None
            if truncated:
                lines += [''] + a.wrap_text(d, CUT_NOTE, rect[2]-rect[0], a.SMALL, max_lines=4)
            self.wrapped = (key, lines)
        return self.wrapped[1]

    def per_page(self):
        rect = text_rect(self.a.W, self.a.H)
        return max(1, (rect[3]-rect[1]) // LINE)

    def pages(self):
        return max(1, -(-len(self.lines()) // self.per_page()))

    # Touch ---------------------------------------------------------------------

    def tap(self, x, y, now):
        a = self.a
        buttons = bottom_buttons(a.W, a.H)
        if self.view == 'article':
            choice = a.index_at(buttons, x, y, slop=2)
            if choice == 0:
                self.view = 'results' if self.results else 'type'
            elif choice == 1:
                self.page = max(0, self.page - 1)
            elif choice == 2:
                self.page = min(self.pages() - 1, self.page + 1)
            elif choice == 3:
                return 'exit'
            elif y > a.H // 2 and not self.running:  # Tapping the text turns the page too.
                self.page = min(self.pages() - 1, self.page + 1)
            return None
        if self.view == 'results':
            choice = a.index_at(buttons, x, y, slop=2)
            shown = self.shown_results()
            if choice == 0:
                self.view = 'type'
            elif choice == 1:
                self.result_page = max(0, self.result_page - 1)
            elif choice == 2 and self.more_results():
                self.result_page += 1
            elif choice == 3:
                return 'exit'
            else:
                hit = a.index_at(result_rects(a.W, a.H, len(shown)), x, y, slop=1)
                if hit is not None and hit < len(shown):
                    self.open(shown[hit])
            return None
        keys = keyboard_rects(a.W, a.H, self.symbols)
        choice = a.index_at([rect for rect, _ in keys], x, y, slop=1)
        if choice is None:
            box = field_rect(a.W, a.H)
            if self.title and a.index_at([box], x, y, slop=0) is not None:
                self.view = 'article'  # Back to the article that is still open.
            return None
        key = keys[choice][1]
        if key == 'back':
            return 'exit'
        if key == 'mode':
            self.symbols = not self.symbols
        elif key == 'del':
            self.query = self.query[:-1]
        elif key == 'ask':
            self.search(self.query)
        elif len(self.query) < MAX_QUERY:
            if key != 'space':
                self.query += key
            elif self.query and not self.query.endswith(' '):
                self.query += ' '
        return None

    def results_per_page(self):
        return max(1, len(result_rects(self.a.W, self.a.H, MAX_RESULTS)))

    def shown_results(self):
        per = self.results_per_page()
        self.result_page = max(0, min(self.result_page, (len(self.results)-1) // per))
        return self.results[self.result_page*per:(self.result_page+1)*per]

    def more_results(self):
        return (self.result_page+1) * self.results_per_page() < len(self.results)

    # Drawing -------------------------------------------------------------------

    def header(self, d, title, wall_time, alerts):
        a = self.a
        if alerts:
            a.draw_banner(d, alerts, wall_time.strftime('%H:%M'))
        else:
            d.text((8, 4), title, font=a.FONT, fill=a.ACCENT)
            d.text((a.W-8, 7), wall_time.strftime('%H:%M'), font=a.SMALL, fill=a.TEXT, anchor='ra')

    def render(self, now, wall_time, alerts):
        a = self.a
        image = Image.new('RGB', (a.W, a.H), a.BG)
        d = ImageDraw.Draw(image)
        if self.view == 'article':
            self.draw_article(d, wall_time, alerts)
        elif self.view == 'results':
            self.draw_results(d, wall_time, alerts)
        else:
            self.draw_keyboard(d, wall_time, alerts)
        return image

    def message(self, d, text, colour):
        a = self.a
        rect = text_rect(a.W, a.H)
        for i, line in enumerate(a.wrap_text(d, text, rect[2]-rect[0], a.SMALL, max_lines=6)):
            d.text((rect[0], rect[1] + 8 + i*LINE), line, font=a.SMALL, fill=colour)

    def draw_keyboard(self, d, wall_time, alerts):
        a = self.a
        self.header(d, 'READER', wall_time, alerts)
        box = field_rect(a.W, a.H)
        d.rounded_rectangle(box, radius=5, fill=a.SCREEN, outline=a.PANEL)
        rows = (box[3]-box[1]-4) // LINE
        if self.query:
            lines, colour = a.wrap_text(d, self.query + '_', box[2]-box[0]-12, a.SMALL, max_lines=999)[-rows:], a.TEXT
        elif self.title:
            lines, colour = a.wrap_text(d, f'Search, or tap here to go back to {self.title}',
                                        box[2]-box[0]-12, a.SMALL, rows), a.MUTED
        else:
            lines, colour = ['Search the offline library'], a.MUTED
        for i, line in enumerate(lines):
            d.text((box[0]+6, box[1]+4 + i*LINE), line, font=a.SMALL, fill=colour)
        letter_font = a.SMALL if a.portrait() else a.FONT
        names = {'del': 'DEL', 'back': 'EXIT', 'space': 'SPACE', 'ask': 'SEARCH'}
        for rect, key in keyboard_rects(a.W, a.H, self.symbols):
            label = ('ABC' if self.symbols else '123') if key == 'mode' else names.get(key, key.upper())
            a.button(d, rect, label, active=key == 'ask' and bool(self.query.strip()),
                     font_=letter_font if len(label) == 1 else a.SMALL)

    def draw_results(self, d, wall_time, alerts):
        a = self.a
        self.header(d, 'READER / RESULTS', wall_time, alerts)
        d.text((6, 26), a.fit_text(d, self.query, a.W-12, a.SMALL), font=a.SMALL, fill=a.MUTED)
        with self.lock:
            status, error = self.status, self.error
        shown = self.shown_results()
        if status:
            self.message(d, status, a.WARN)
        for rect, row in zip(result_rects(a.W, a.H, len(shown)), shown):
            d.rounded_rectangle(rect, radius=4, fill=a.INSET)
            source = 'local' if row.get('source') == 'local' else ''
            room = rect[2] - rect[0] - 12 - (d.textlength(source, font=a.TINY) + 6 if source else 0)
            d.text((rect[0]+6, (rect[1]+rect[3])//2), a.fit_text(d, str(row.get('title', '')), room, a.SMALL),
                   font=a.SMALL, fill=a.TEXT, anchor='lm')
            if source:
                d.text((rect[2]-6, (rect[1]+rect[3])//2), source, font=a.TINY, fill=a.MUTED, anchor='rm')
        if error and not shown:
            self.message(d, error, a.WARN)
        elif error:
            d.text((6, a.H-48), a.fit_text(d, error, a.W-12, a.TINY), font=a.TINY, fill=a.WARN)
        labels = ('SEARCH', '<' if self.result_page else '', '>' if self.more_results() else '', 'EXIT')
        for rect, label in zip(bottom_buttons(a.W, a.H), labels):
            if label:
                a.button(d, rect, label)

    def draw_article(self, d, wall_time, alerts):
        a = self.a
        self.header(d, 'READER', wall_time, alerts)
        with self.lock:
            status, error = self.status, self.error
        lines = [] if status or error else self.lines()
        pages = max(1, -(-len(lines) // self.per_page()))
        self.page = min(self.page, pages - 1)
        counter = f'{self.page+1}/{pages}' if lines else ''
        width = d.textlength(counter, font=a.SMALL)
        d.text((6, 26), a.fit_text(d, self.title, a.W-18-width, a.SMALL), font=a.SMALL, fill=a.ACCENT)
        d.text((a.W-6, 26), counter, font=a.SMALL, fill=a.MUTED, anchor='ra')
        rect = text_rect(a.W, a.H)
        if status:
            self.message(d, status, a.WARN)
        elif error:
            self.message(d, error, a.WARN)
        per = self.per_page()
        for i, line in enumerate(lines[self.page*per:(self.page+1)*per]):
            muted = self.note_from is not None and self.page*per + i >= self.note_from
            d.text((rect[0], rect[1] + i*LINE), line, font=a.SMALL, fill=a.MUTED if muted else a.TEXT)
        if pages > 1:  # Where this page sits in the article.
            span = rect[3]-rect[1]
            top = rect[1] + span*self.page//pages
            d.rectangle((a.W-5, top, a.W-3, top + max(6, span//pages)), fill=a.MUTED)
        labels = ('RESULTS' if self.results else 'SEARCH', '< PAGE', 'PAGE >', 'EXIT')
        for rect, label in zip(bottom_buttons(a.W, a.H), labels):
            a.button(d, rect, label)
