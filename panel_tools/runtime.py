"""Data paths, serialized camera work, and monotonic clocks."""
import json
import os
import queue
import shutil
import threading
import time
from pathlib import Path


def data_home(explicit, script, home=None):
    """Copy legacy data once, never merge/overwrite two existing libraries."""
    if explicit:
        return Path(explicit).expanduser().resolve()
    if home is None:
        if os.name == 'posix' and os.environ.get('SUDO_UID'):
            import pwd
            home = Path(pwd.getpwuid(int(os.environ['SUDO_UID'])).pw_dir)
        else:
            home = Path.home()
    target = Path(home) / 'cyberdeck-data'
    legacy = Path(script).resolve().parent / 'cyberdeck-data'
    if not target.exists() and legacy.exists() and legacy.resolve() != target.resolve():
        stage = target.with_name('cyberdeck-data.migrating')
        if stage.exists():
            raise RuntimeError(f'Incomplete data migration: inspect {stage}; original is {legacy}')
        shutil.copytree(legacy, stage, symlinks=True)
        if os.name == 'posix' and os.environ.get('SUDO_UID'):
            uid, gid = int(os.environ['SUDO_UID']), int(os.environ['SUDO_GID'])
            for item in [stage, *stage.rglob('*')]:
                if not item.is_symlink():
                    os.chown(item, uid, gid)
        stage.rename(target)
    target.mkdir(parents=True, exist_ok=True)
    return target


def atomic_json(path, value, owns=lambda p: None):
    path = Path(path)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2), encoding='utf-8')
    owns(temporary)
    temporary.replace(path)


class Job:
    """One background operation; result consumed on the UI thread."""
    def __init__(self):
        self.thread = None
        self.result = None
        self.error = None
        self.done = False

    @property
    def busy(self):
        return self.thread is not None and self.thread.is_alive()

    def start(self, fn):
        if self.busy or self.done:
            return False
        self.result = self.error = None
        def run():
            try:
                self.result = fn()
            except Exception as exc:
                self.error = str(exc)
            finally:
                self.done = True
        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        return True

    def consume(self):
        if not self.done or self.busy:
            return None
        value = (self.result, self.error)
        self.done = False
        return value


class CameraWorker:
    """Only this thread touches the camera driver, including close/recovery."""
    def __init__(self, factory, fps):
        self.factory, self.fps = factory, fps
        self.latest = None
        self.error = ''
        self.ready = False
        self.commands = queue.Queue(maxsize=4)
        self.results = queue.Queue()
        self.stopping = threading.Event()
        self.capture_pending = False
        self.preview = True
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()

    def frame(self):
        return self.latest

    def set_fps(self, fps):
        self.fps = fps
        self.enqueue(('fps', fps))

    def enqueue(self, cmd):
        try:
            self.commands.put_nowait(cmd)
            return True
        except queue.Full:
            return False

    def retry(self):
        return self.enqueue(('retry', None))

    def snapshot(self, save):
        if self.capture_pending or not self.ready:
            return False
        self.capture_pending = True
        if not self.enqueue(('shot', save)):
            self.capture_pending = False
            return False
        return True

    def loop(self):
        camera = None
        try:
            self.commands.put(('retry', None))
            while not self.stopping.is_set():
                try:
                    kind, value = self.commands.get(timeout=.01)
                except queue.Empty:
                    kind, value = None, None
                try:
                    if kind == 'retry':
                        self.ready = False
                        if camera is not None:
                            camera.close()
                        camera = None
                        camera = self.factory(self.fps)
                        if camera is None:
                            raise RuntimeError('Camera unavailable; check ribbon and retry')
                        self.ready, self.error = True, ''
                    elif kind == 'fps' and camera:
                        camera.set_fps(value)
                    elif kind == 'shot':
                        try:
                            self.results.put((value(camera), None))
                        except Exception as exc:
                            self.results.put((None, str(exc)))
                            if camera and not camera.recover():
                                self.ready = False
                                self.error = 'Capture failed; retry camera'
                        finally:
                            self.capture_pending = False
                    if camera and self.ready and self.preview:
                        self.latest = camera.frame()
                except Exception as exc:
                    self.error, self.ready = str(exc), False
                if not self.preview or not self.ready:
                    self.stopping.wait(.03)
        finally:
            self.ready = False
            if camera:
                try:
                    camera.close()
                except Exception:
                    pass

    def close(self):
        self.stopping.set()
        self.thread.join(timeout=10)
        if self.thread.is_alive():
            raise RuntimeError('Camera did not stop in time; app restart cancelled')


class Clocks:
    def __init__(self):
        self.started = None
        self.elapsed = 0.
        self.deadline = None
        self.alarm = False

    def stopwatch(self, now):
        return self.elapsed + (now-self.started if self.started is not None else 0)

    def toggle(self, now):
        if self.started is None:
            self.started = now
        else:
            self.elapsed = self.stopwatch(now)
            self.started = None

    def countdown(self, seconds, now):
        self.deadline, self.alarm = now + seconds, False

    def tick(self, now):
        if self.deadline is not None and now >= self.deadline:
            self.deadline, self.alarm = None, True


class Timelapse:
    def __init__(self):
        self.interval, self.duration = 10, 300
        self.active = False
        self.saved = 0
        self.started = self.next_at = 0.
        self.error = ''

    @property
    def total(self):
        return max(1, (self.duration + self.interval - 1)//self.interval)

    def start(self, now):
        self.active, self.saved, self.error = True, 0, ''
        self.started = self.next_at = now

    def due(self, now):
        if self.active and (now >= self.started+self.duration or self.saved >= self.total):
            self.active = False
        return self.active and now >= self.next_at

    def completed(self, now, error=None):
        if error:
            self.active, self.error = False, error
        else:
            self.saved += 1
            # No catch-up bursts if a full-resolution shot took too long.
            self.next_at = now+self.interval
            if self.saved >= self.total:
                self.active = False
