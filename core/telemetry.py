"""Telemetry log: system samples every few seconds plus events (undervoltage, throttling,
and activities such as AI answers, searches and camera captures), kept in SQLite.

Samples are buffered in memory and written once a minute to spare the SD card; reads
merge the buffer in, so callers always see the latest data.
"""
import itertools
import sqlite3
import threading
import time
from contextlib import contextmanager

from .sysinfo import read_throttled, undervoltage_seen

ACTIVITY_KINDS = ('ai', 'search', 'camera', 'other')
EVENT_KINDS = ('undervoltage', 'throttled') + ACTIVITY_KINDS
SAMPLE_FIELDS = ('cpu', 'temp_c', 'mem_percent', 'disk_free_gib', 'load1', 'freq_mhz', 'throttled')

SCHEMA = '''
CREATE TABLE IF NOT EXISTS samples (ts REAL PRIMARY KEY, cpu REAL, temp_c REAL,
    mem_percent REAL, disk_free_gib REAL, load1 REAL, freq_mhz REAL, throttled INTEGER);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, kind TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '', start REAL NOT NULL, end REAL NOT NULL);
CREATE INDEX IF NOT EXISTS events_end ON events (end);
'''


class TelemetryStore:
    """SQLite file shared by the sampling thread and the web server threads."""
    def __init__(self, path):
        self.lock = threading.Lock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        with self.lock, self.db:
            self.db.executescript(SCHEMA)

    def write(self, samples, events):
        with self.lock, self.db:
            self.db.executemany(
                f"INSERT OR REPLACE INTO samples VALUES (?{', ?'*len(SAMPLE_FIELDS)})",
                [(s['ts'], *(s.get(k) for k in SAMPLE_FIELDS)) for s in samples])
            self.db.executemany('INSERT INTO events (kind, detail, start, end) VALUES (?, ?, ?, ?)',
                                [(e['kind'], e['detail'], e['start'], e['end']) for e in events])

    def prune(self, before):
        with self.lock, self.db:
            self.db.execute('DELETE FROM samples WHERE ts < ?', (before,))
            self.db.execute('DELETE FROM events WHERE end < ?', (before,))

    def buckets(self, since, width, last=None):
        """Per time bucket: (index, count, cpu sum, cpu count, max temp, mem sum, mem count,
        undervoltage seen, throttled seen)."""
        with self.lock:
            return self.db.execute('''
                SELECT MIN(CAST((ts - ?) / ? AS INTEGER), ?) AS b, COUNT(*), TOTAL(cpu), COUNT(cpu),
                       MAX(temp_c), TOTAL(mem_percent), COUNT(mem_percent),
                       MAX(throttled & 1), MAX((throttled & 14) != 0)
                FROM samples WHERE ts >= ? GROUP BY b ORDER BY b''',
                (since, width, 2**62 if last is None else last, since)).fetchall()

    def events(self, since, limit=500):
        with self.lock:
            rows = self.db.execute('SELECT kind, detail, start, end FROM events WHERE end >= ? '
                                   'ORDER BY start DESC, id DESC LIMIT ?', (since, limit)).fetchall()
        return [{'kind': k, 'detail': d, 'start': s, 'end': e} for k, d, s, e in reversed(rows)]

    def close(self):
        with self.lock:
            self.db.close()


class Telemetry:
    def __init__(self, store, sampler, throttled=read_throttled, clock=time.time,
                 interval=5., flush_every=60., keep_days=7.):
        self.store, self.sampler, self.read_throttled, self.clock = store, sampler, throttled, clock
        self.interval, self.flush_every, self.keep = interval, flush_every, keep_days*86400
        self.lock = threading.Lock()
        self.pending_samples, self.pending_events = [], []
        self.writing = ([], [])  # Being written right now; still visible to readers.
        self.latest = None
        self.previous_flags = None
        self.conditions = {}  # 'undervoltage'/'throttled' -> event dict while it lasts.
        self.active = {}  # Activity id -> event dict while running.
        self.ids = itertools.count(1)
        self.next_flush = self.next_prune = 0.
        self.stop_event = threading.Event()
        self.thread = None

    # ---- sampling -------------------------------------------------------------------
    def tick(self):
        """Take one sample, note conditions, and write to disk when due."""
        now = self.clock()
        sample = {'ts': now, **self.sampler.read(), 'throttled': self.read_throttled()}
        flags = sample['throttled']
        with self.lock:
            self.latest = sample
            self.pending_samples.append({k: sample.get(k) for k in ('ts',) + SAMPLE_FIELDS})
            self.note('undervoltage', undervoltage_seen(self.previous_flags, flags), now)
            self.note('throttled', flags is not None and bool(flags & 14), now)
            if flags is not None:
                self.previous_flags = flags
        if now >= self.next_flush:
            if self.next_flush:  # Not on the very first tick: give the buffer a minute.
                self.flush()
            self.next_flush = now + self.flush_every
        if now >= self.next_prune:
            self.store.prune(now - self.keep)
            self.next_prune = now + 3600
        return sample

    def note(self, kind, happening, now):
        """Open, extend or close a condition event (caller holds the lock)."""
        event = self.conditions.get(kind)
        if happening:
            if event is None:
                self.conditions[kind] = {'kind': kind, 'detail': '', 'start': now, 'end': now}
            else:
                event['end'] = now
        elif event is not None:
            self.pending_events.append(self.conditions.pop(kind))

    def flush(self):
        with self.lock:
            samples, events = self.pending_samples, self.pending_events
            self.pending_samples, self.pending_events = [], []
            self.writing = (samples, events)
        try:
            if samples or events:
                self.store.write(samples, events)
        finally:
            with self.lock:
                self.writing = ([], [])

    # ---- activities -------------------------------------------------------------------
    def begin(self, kind, detail=''):
        kind = kind if kind in ACTIVITY_KINDS else 'other'
        now = self.clock()
        with self.lock:
            ident = next(self.ids)
            self.active[ident] = {'kind': kind, 'detail': str(detail)[:200], 'start': now, 'end': now}
        return ident

    def end(self, ident):
        with self.lock:
            event = self.active.pop(ident, None)
            if event is not None:
                event['end'] = self.clock()
                self.pending_events.append(event)
        return event is not None

    def mark(self, kind, detail=''):
        """An instant event, such as a photo being taken."""
        self.end(self.begin(kind, detail))

    @contextmanager
    def activity(self, kind, detail=''):
        """with telemetry.activity('ai', question): ... marks the AI busy while it runs."""
        ident = self.begin(kind, detail)
        try:
            yield
        finally:
            self.end(ident)

    # ---- reading ----------------------------------------------------------------------
    def now(self):
        with self.lock:
            return {'sample': dict(self.latest) if self.latest else None,
                    'active': [dict(e, id=i) for i, e in self.active.items()],
                    'conditions': sorted(self.conditions)}

    def history(self, seconds, points=240):
        """Samples averaged into at most `points` buckets, plus events, for the last
        `seconds`. Each sample row: [ts, cpu, max temp, mem, undervoltage, throttled]."""
        now = self.clock()
        since, width = now - seconds, max(1., seconds / max(1, points))
        last = max(0, int(seconds // width) - 1)  # A sample at exactly `now` joins the last bucket.
        merged = {row[0]: list(row) for row in self.store.buckets(since, width, last)}
        with self.lock:
            pending = [s for s in self.writing[0] + self.pending_samples if s['ts'] >= since]
            events = [dict(e) for e in self.writing[1] + self.pending_events if e['end'] >= since]
            live = [dict(e) for e in list(self.conditions.values()) + list(self.active.values())]
        for s in pending:
            b = min(last, int((s['ts'] - since) // width))
            row = merged.setdefault(b, [b, 0, 0., 0, None, 0., 0, 0, 0])
            flags = s['throttled'] or 0
            row[1] += 1
            if s['cpu'] is not None:
                row[2] += s['cpu']
                row[3] += 1
            if s['temp_c'] is not None:
                row[4] = s['temp_c'] if row[4] is None else max(row[4], s['temp_c'])
            if s['mem_percent'] is not None:
                row[5] += s['mem_percent']
                row[6] += 1
            row[7] = max(row[7] or 0, flags & 1)
            row[8] = max(row[8] or 0, int(bool(flags & 14)))
        samples = []
        for b in sorted(merged):
            _, _, cpu, cpus, temp, mem, mems, uv, thr = merged[b]
            samples.append([round(since + (b + .5)*width, 1),
                            round(cpu/cpus, 1) if cpus else None,
                            None if temp is None else round(temp, 1),
                            round(mem/mems, 1) if mems else None, int(uv or 0), int(thr or 0)])
        for event in live:
            event['end'] = now
        return {'now': now, 'seconds': seconds, 'samples': samples,
                'events': self.store.events(since) + events + live}

    # ---- thread -----------------------------------------------------------------------
    def start(self):
        self.thread = threading.Thread(target=self.run, name='telemetry', daemon=True)
        self.thread.start()

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception as exc:  # Keep logging through one bad reading.
                print('Telemetry sample failed:', exc, flush=True)
            self.stop_event.wait(self.interval)

    def close(self):
        """Stop sampling and write everything still in memory (open events end now)."""
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=5)
        now = self.clock()
        with self.lock:
            for event in list(self.conditions.values()) + list(self.active.values()):
                event['end'] = now
                self.pending_events.append(event)
            self.conditions.clear()
            self.active.clear()
        self.flush()
