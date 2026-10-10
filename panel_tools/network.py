"""Network sampling and explicitly enabled, read-only photo sharing."""
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import html
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit


class NetworkHistory:
    def __init__(self):
        self.previous = {}
        self.at = None
        self.samples = deque(maxlen=120)
        self.rx = self.tx = 0.
        self.available = False

    def sample(self, now, signal=None, text=None):
        try:
            text = Path('/proc/net/dev').read_text() if text is None else text
            counters = {}
            for line in text.splitlines():
                if ':' not in line:
                    continue
                name, values = line.split(':', 1)
                name, fields = name.strip(), values.split()
                if name != 'lo' and len(fields) >= 16:
                    counters[name] = (int(fields[0]), int(fields[8]))
            self.rx = self.tx = 0.
            if self.at is not None and now > self.at:
                for name in counters.keys() & self.previous.keys():
                    old, new = self.previous[name], counters[name]
                    if new[0] >= old[0] and new[1] >= old[1]:
                        self.rx += (new[0]-old[0])/(now-self.at)
                        self.tx += (new[1]-old[1])/(now-self.at)
            self.previous, self.at = counters, now
            self.available = bool(counters)
        except (OSError, ValueError):
            self.available, self.rx, self.tx = False, 0., 0.
        self.samples.append((self.rx, self.tx, signal))


class PhotoShare:
    """Allowlisted files only; random session path; auto expires after 10 minutes.

    Plain HTTP is for a trusted private LAN only. Never port forward this service.
    No uploads, filesystem browsing, or photo deletion endpoints.
    """
    def __init__(self):
        self.server = None
        self.thread = None
        self.token = ''
        self.expires = 0.
        self.files = ()
        self.port = 0
        self.active = threading.Event()

    def start(self, paths, root, host='0.0.0.0', port=8765, seconds=600):
        if self.server:
            raise RuntimeError('Stop the current sharing session first')
        root = Path(root).resolve()
        files = tuple(Path(p).resolve() for p in paths
                      if Path(p).resolve().parent == root and not Path(p).is_symlink()
                      and Path(p).is_file() and Path(p).suffix.lower() in ('.jpg', '.jpeg', '.png'))
        if not files:
            raise RuntimeError('Select a photo first')
        token = secrets.token_urlsafe(12)
        expires = time.monotonic()+seconds
        active = threading.Event()
        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(10)

            def log_message(self, *args):
                pass  # Do not leak the session token into service logs.

            def do_GET(self):
                path = urlsplit(self.path).path
                if not active.is_set() or time.monotonic() >= expires or not path.startswith('/'+token+'/'):
                    self.send_error(404)
                    return
                suffix = path[len(token)+2:]
                if not suffix:
                    links = ''.join(f'<li><a href="{i}" download>{html.escape(p.name)}</a></li>'
                                    for i, p in enumerate(files))
                    body = ('<!doctype html><meta name="viewport" content="width=device-width">'
                            '<title>Cyberdeck photos</title><h1>Selected photos</h1><ul>'+links+
                            '</ul><p>Read-only session; expires in 10 minutes.</p>').encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html; charset=utf-8')
                    self.send_header('Content-Length', str(len(body)))
                    self.send_header('Cache-Control', 'no-store')
                    self.send_header('Content-Security-Policy', "default-src 'none'")
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if not suffix.isdecimal() or len(suffix) > 6 or int(suffix) >= len(files):
                    self.send_error(404)
                    return
                selected = files[int(suffix)]
                try:
                    if selected.is_symlink() or selected.resolve().parent != root:
                        raise OSError('File changed')
                    with selected.open('rb') as source:
                        self.send_response(200)
                        self.send_header('Content-Type', 'image/png' if selected.suffix.lower()=='.png'
                                         else 'image/jpeg')
                        self.send_header('Content-Length', str(selected.stat().st_size))
                        self.send_header('Content-Disposition', f'attachment; filename="photo-{suffix}{selected.suffix.lower()}"')
                        self.send_header('Cache-Control', 'no-store')
                        self.end_headers()
                        while active.is_set() and time.monotonic() < expires:
                            chunk = source.read(65536)
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                except (OSError, BrokenPipeError):
                    self.close_connection = True

        server = ThreadingHTTPServer((host, port), Handler)
        server.daemon_threads = True
        self.server, self.token, self.expires, self.files = server, token, expires, files
        self.port = server.server_address[1]
        self.active = active
        active.set()
        self.thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .05}, daemon=True)
        self.thread.start()

    def close(self):
        self.active.clear()
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=1)
            self.server = None
            self.files = ()

    def tick(self, now):
        if self.server and now >= self.expires:
            self.close()
