"""Kiwix full-text RSS search. No catalog search or online fallback."""
from contextlib import nullcontext
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
import ipaddress
from urllib.error import URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import xml.etree.ElementTree as ET


class KiwixError(RuntimeError):
    """A readable source failure that callers can display without a traceback."""


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str
    source: str = 'kiwix'


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def plain_text(text):
    parser = _Text()
    parser.feed(text)
    return ' '.join(unescape(''.join(parser.parts)).split())


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise KiwixError('Kiwix redirected the request; configure its direct local URL')


class KiwixClient:
    def __init__(self, base_url, opener=None, *, book='', book_parameter='books.name',
                 timeout=2.0, max_bytes=2_000_000, telemetry=None):
        base = urlsplit(base_url)
        try:
            local = base.hostname == 'localhost' or ipaddress.ip_address(base.hostname).is_loopback
        except ValueError:
            local = False
        if base.scheme != 'http' or not local or base.username or base.password or base.query or base.fragment:
            raise ValueError('Kiwix URL must be a loopback HTTP URL')
        if book_parameter not in {'books.name', 'content'}:
            raise ValueError('book_parameter must be books.name or content')
        if not 0 < timeout <= 10 or not 0 < max_bytes <= 8_000_000:
            raise ValueError('timeout or max_bytes outside safe bounds')
        self.base_url = base_url.rstrip('/') + '/'
        self.book, self.book_parameter = book, book_parameter
        self.timeout, self.max_bytes, self.telemetry = timeout, max_bytes, telemetry
        self.opener = opener or build_opener(ProxyHandler({}), _NoRedirect())

    def offline_url(self, link):
        url = urljoin(self.base_url, link)
        base, target = urlsplit(self.base_url), urlsplit(url)
        if ((target.scheme, target.netloc) != (base.scheme, base.netloc)
                or not target.path.startswith(base.path) or '\\' in url
                or any(ord(c) < 32 for c in url)):
            raise KiwixError('Kiwix returned a link outside its configured local origin')
        return url

    def fetch(self, url):
        url = self.offline_url(url)
        try:
            with self.opener.open(Request(url, headers={'Accept-Encoding': 'identity'}),
                                  timeout=self.timeout) as response:
                if hasattr(response, 'geturl') and response.geturl():
                    self.offline_url(response.geturl())
                data = response.read(self.max_bytes + 1)
            if len(data) > self.max_bytes:
                raise KiwixError('Kiwix response exceeds the configured size limit')
            return data
        except (URLError, OSError, ValueError) as exc:
            raise KiwixError('Kiwix is unavailable or timed out; check kiwix-serve') from exc

    def search(self, query, limit=5):
        if not query.strip() or limit <= 0:
            return []
        if len(query) > 1000:
            raise ValueError('query must be at most 1000 characters')
        params = {'pattern': query, 'format': 'xml', 'pageLength': min(limit, 20)}
        if self.book:
            params[self.book_parameter] = self.book
        activity = self.telemetry.activity('search', query) if self.telemetry else nullcontext()
        with activity:
            data = self.fetch(self.base_url + 'search?' + urlencode(params))
            if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
                raise KiwixError('Kiwix returned unsupported XML declarations')
            try:
                root = ET.fromstring(data)
            except ET.ParseError as exc:
                raise KiwixError('Kiwix did not return valid XML search results') from exc
            if root.tag != 'rss' or root.find('channel') is None:
                raise KiwixError('Kiwix returned an unexpected search format; check its version')
            results = []
            for item in root.findall('./channel/item'):
                link = item.findtext('link', '').strip()
                title = item.findtext('title', '').strip()
                if not title or not link:
                    continue
                description = item.find('description')
                snippet = '' if description is None else ''.join(description.itertext())
                results.append(SearchResult(title, self.offline_url(link), plain_text(snippet)))
                if len(results) >= min(limit, 20):
                    break
            return results
