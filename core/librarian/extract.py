"""Bounded, dependency-free article text extraction."""
from html.parser import HTMLParser
import re

VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link',
        'meta', 'param', 'source', 'track', 'wbr'}
DROP_TAGS = {'script', 'style', 'nav', 'header', 'footer', 'aside', 'table',
             'form', 'noscript', 'sup'}
DROP_CLASSES = {'toc', 'infobox', 'navbox', 'sidebar', 'references', 'reflist',
                'mw-editsection', 'noprint', 'metadata', 'hatnote', 'thumb',
                'reference', 'mw-references-wrap', 'catlinks'}
END_SECTIONS = {'references', 'notes', 'external links', 'further reading',
                'bibliography', 'see also'}


class ArticleParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.parts = []
        self.paragraphs = []
        self.heading = None
        self.heading_parts = []
        self.skip_section = None

    def flush(self):
        text = ' '.join(''.join(self.parts).split())
        if text:
            self.paragraphs.append(text)
        self.parts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        parent_drop = self.stack and self.stack[-1][1]
        classes = set(attrs.get('class', '').lower().split())
        drop = bool(parent_drop or tag in DROP_TAGS or classes & DROP_CLASSES
                    or (not self.heading and not re.fullmatch('h[1-6]', tag)
                        and attrs.get('id', '').lower() in DROP_CLASSES)
                    or attrs.get('role') == 'navigation' or 'hidden' in attrs)
        if tag not in VOID:
            self.stack.append((tag, drop))
        if drop:
            return
        if re.fullmatch('h[1-6]', tag):
            self.flush()
            self.heading = int(tag[1])
            self.heading_parts = []
        elif tag in {'p', 'li', 'div', 'section', 'article', 'blockquote', 'pre'}:
            self.flush()
        elif tag == 'br':
            self.parts.append(' ')

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        dropped = self.stack and self.stack[-1][1]
        if not dropped:
            if re.fullmatch('h[1-6]', tag) and self.heading:
                name = ' '.join(''.join(self.heading_parts).split()).lower()
                if self.skip_section and self.heading <= self.skip_section:
                    self.skip_section = None
                if name in END_SECTIONS:
                    self.skip_section = self.heading
                self.heading = None
            elif tag in {'p', 'li', 'div', 'section', 'article', 'blockquote', 'pre'}:
                self.flush()
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if self.stack and self.stack[-1][1]:
            return
        if self.heading:
            self.heading_parts.append(data)
        elif not self.skip_section and any(tag in {'p', 'li', 'pre', 'blockquote'}
                                           for tag, _ in self.stack):
            self.parts.append(data)


def paragraphs(html):
    parser = ArticleParser()
    parser.feed(html)
    parser.close()
    parser.flush()
    return parser.paragraphs


def fetch_article(client, url):
    """Fetch through the client's same-origin, timed, size-limited transport."""
    return paragraphs(client.fetch(url).decode('utf-8', errors='replace'))
