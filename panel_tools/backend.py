"""The panel's link to cyberdeck-core. Everything runs on one background thread with short
timeouts, so a missing or slow backend never delays drawing or touch; the panel simply
shows that the backend is offline."""
import json
import queue
import threading
import time
import urllib.request

from core import DEFAULT_PORT


class Backend:
    def __init__(self, base=f'http://127.0.0.1:{DEFAULT_PORT}', poll=5., timeout=.8,
                 opener=None, clock=time.monotonic):
        self.base, self.poll, self.timeout, self.clock = base.rstrip('/'), poll, timeout, clock
        # No proxies: the backend is on this Pi, whatever http_proxy says.
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.online = None  # None until the first answer; then True or False.
        self.state = {}  # Latest /api/telemetry/now.
        self.history = None  # Latest /api/telemetry/history for the wanted range.
        self.history_seconds = None  # Range the History page wants; None = not needed.
        self.history_due = 0.
        self.outbox = queue.Queue(maxsize=50)
        self.wake = threading.Event()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, name='backend', daemon=True)

    def call(self, path, payload=None):
        data = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(self.base + path, data=data,
                                         headers={'Content-Type': 'application/json'})
        with self.opener.open(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode())

    @property
    def busy(self):
        """Kinds of work running in the backend, such as ['ai'] while it answers."""
        return self.state.get('busy', []) if self.online else []

    def mark(self, kind, detail=''):
        """Record an instant event (a photo was taken). Dropped if the backend is offline."""
        if self.online is not False:
            try:
                self.outbox.put_nowait(('/api/activity/mark', {'kind': kind, 'detail': detail}))
            except queue.Full:
                pass
            self.wake.set()

    def want_history(self, seconds):
        """Ask for history while the History page is open (None when it closes)."""
        if seconds != self.history_seconds:
            self.history_seconds, self.history_due = seconds, 0.
            if seconds is not None:
                self.history = None
                self.wake.set()

    def step(self):
        """One round: send queued marks, refresh the state, and history if wanted."""
        try:
            while True:
                path, payload = self.outbox.get_nowait()
                self.call(path, payload)
        except queue.Empty:
            pass
        except (OSError, ValueError):
            self.online = False
            return
        try:
            self.state = self.call('/api/telemetry/now')
            self.online = True
            seconds = self.history_seconds
            if seconds is not None and self.clock() >= self.history_due:
                points = 150
                self.history = self.call(f'/api/telemetry/history?seconds={seconds}&points={points}')
                self.history_due = self.clock() + min(60., max(10., seconds/points))
        except (OSError, ValueError):
            self.online = False

    def start(self):
        self.thread.start()

    def run(self):
        while not self.stop_event.is_set():
            self.step()
            self.wake.wait(self.poll if self.online else self.poll*3)
            self.wake.clear()

    def close(self):
        self.stop_event.set()
        self.wake.set()
        if self.thread.is_alive():
            self.thread.join(timeout=2)
