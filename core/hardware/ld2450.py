"""HLK-LD2450 24 GHz radar: up to three moving targets with position and speed.

The sensor streams one 30-byte frame about ten times a second at 256000 baud:
    AA FF 03 00 | target 1 | target 2 | target 3 | 55 CC
Each target is four little-endian 16-bit words: x (mm), y (mm), speed (cm/s) and the distance
resolution (mm). For x, y and speed the top bit set means positive and clear means negative,
with the size in the other 15 bits. An all-zero target is an empty slot.

The radar sees movement and distance, not identity: callers say "movement", never "person".
Nothing runs unless a source is configured (CYBERDECK_RADAR), and a replayed recording is
always labelled SIMULATED.

    python3 -m core.hardware.ld2450 serial:/dev/ttyAMA3      watch a real sensor
    python3 -m core.hardware.ld2450 serial:/dev/ttyUSB0 --record radar.bin
    python3 -m core.hardware.ld2450 replay:radar.bin          play a recording back
"""
import argparse
from collections import namedtuple
import math
import os
import struct
import sys
import threading
import time

HEADER, TAIL = b'\xaa\xff\x03\x00', b'\x55\xcc'
FRAME_LEN = 30
BAUD = 256000
STALE_S = 2.0  # Frames arrive every ~0.1 s; after this long the targets aren't reported.
RETRY_S = 5  # Wait between reconnects, so an unplugged sensor doesn't spin the CPU.
FRAME_S = 0.1  # Replay pace.


class Target(namedtuple('Target', 'x_mm y_mm speed_cms resolution_mm')):
    """x is left (-) / right (+) of the sensor's axis, y is the distance straight ahead.
    speed is negative when the target comes closer."""

    @property
    def distance_m(self):
        return math.hypot(self.x_mm, self.y_mm) / 1000

    @property
    def angle_deg(self):
        return math.degrees(math.atan2(self.x_mm, self.y_mm))

    def as_dict(self):
        return {'x_mm': self.x_mm, 'y_mm': self.y_mm, 'speed_cms': self.speed_cms,
                'distance_m': round(self.distance_m, 2), 'angle_deg': round(self.angle_deg, 1),
                'moving': self.speed_cms != 0}


def _signed(word):
    return word & 0x7FFF if word & 0x8000 else -(word & 0x7FFF)


def parse_frame(frame):
    """Targets in one 30-byte frame (empty slots dropped), or None if it isn't a frame."""
    if len(frame) != FRAME_LEN or not frame.startswith(HEADER) or not frame.endswith(TAIL):
        return None
    targets = []
    for x, y, speed, resolution in struct.iter_unpack('<HHHH', frame[4:28]):
        if x or y or speed or resolution:
            targets.append(Target(_signed(x), _signed(y), _signed(speed), resolution))
    return targets


def encode_frame(targets):
    """The bytes the sensor would send for these targets; for tests and fake sources."""
    def word(value):
        return (0x8000 | value) if value >= 0 else -value
    body = b''.join(struct.pack('<HHHH', word(t.x_mm), word(t.y_mm), word(t.speed_cms),
                                t.resolution_mm) for t in targets[:3])
    return HEADER + body.ljust(24, b'\0') + TAIL


class FrameReader:
    """Cuts a byte stream into frames, skipping noise and partial frames."""

    def __init__(self):
        self.buffer = bytearray()

    def feed(self, data):
        self.buffer += data
        frames = []
        while True:
            start = self.buffer.find(HEADER)
            if start < 0:
                del self.buffer[:max(0, len(self.buffer) - len(HEADER) + 1)]
                return frames
            del self.buffer[:start]
            if len(self.buffer) < FRAME_LEN:
                return frames
            targets = parse_frame(bytes(self.buffer[:FRAME_LEN]))
            if targets is None:
                del self.buffer[:1]  # A header inside noise: look for the next one.
                continue
            frames.append(targets)
            del self.buffer[:FRAME_LEN]


# --- sources ------------------------------------------------------------------------------

# Linux termios2: 256000 baud has no B-constant, so set the rate directly (BOTHER).
TCGETS2, TCSETS2, BOTHER, CBAUD = 0x802C542A, 0x402C542B, 0o010000, 0o010017
TERMIOS2 = struct.Struct('IIIIB19sII')


def set_baud(fd, baud, ioctl=None):
    """Raw mode at any baud rate. Raises OSError if the port refuses."""
    import termios
    import tty
    if ioctl is None:
        import fcntl
        ioctl = fcntl.ioctl
    tty.setraw(fd)
    raw = ioctl(fd, TCGETS2, bytes(TERMIOS2.size))
    iflag, oflag, cflag, lflag, line, cc, _, _ = TERMIOS2.unpack(raw)
    cflag = (cflag & ~CBAUD) | BOTHER
    cflag |= termios.CLOCAL | termios.CREAD
    ioctl(fd, TCSETS2, TERMIOS2.pack(iflag, oflag, cflag, lflag, line, cc, baud, baud))


class SerialSource:
    """Bytes from the sensor's serial port (a GPIO UART or a USB adapter)."""
    simulated = False
    name = 'serial'

    def __init__(self, device, baud=BAUD, record=None):
        if not device.startswith('/dev/'):
            raise ValueError('radar serial device must be under /dev/')
        self.device, self.baud, self.record = device, baud, record

    def chunks(self, stop):
        fd = os.open(self.device, os.O_RDONLY | os.O_NOCTTY)
        log = open(self.record, 'ab') if self.record else None
        try:
            if os.isatty(fd):
                set_baud(fd, self.baud)
            while not stop.is_set():
                data = os.read(fd, 256)
                if not data:
                    return
                if log:
                    log.write(data)
                yield data
        finally:
            os.close(fd)
            if log:
                log.close()


class ReplaySource:
    """A recording of raw sensor bytes, played in a loop at the sensor's pace. SIMULATED."""
    simulated = True
    name = 'replay'

    def __init__(self, path, interval=FRAME_S, sleep=None):
        self.path, self.interval, self.sleep = path, interval, sleep

    def chunks(self, stop):
        sleep = self.sleep or stop.wait
        with open(self.path, 'rb') as log:
            data = log.read()
        frames = [encode_frame(f) for f in FrameReader().feed(data)]
        if not frames:
            raise ValueError(f'{self.path} has no LD2450 frames')
        while not stop.is_set():
            for frame in frames:
                yield frame
                sleep(self.interval)
                if stop.is_set():
                    return


def source_from_spec(spec, record=None):
    """A source for a CYBERDECK_RADAR value, or None when it's empty (radar off)."""
    spec = (spec or '').strip()
    if not spec:
        return None
    kind, _, rest = spec.partition(':')
    if kind == 'serial':
        device, _, baud = rest.partition(':')
        if baud and not baud.isdigit():
            raise ValueError('radar baud rate must be a number')
        return SerialSource(device, int(baud or BAUD), record)
    if kind == 'replay' and rest:
        return ReplaySource(rest)
    raise ValueError('CYBERDECK_RADAR must be serial:/dev/... or replay:/path/to/recording')


# --- service ------------------------------------------------------------------------------

class Radar:
    """Reads a source on a background thread and keeps the latest frame."""

    def __init__(self, source=None, clock=time.monotonic, retry_s=RETRY_S, error=''):
        self.source, self.clock, self.retry_s, self.error = source, clock, retry_s, error
        self.targets, self.updated, self.frames = [], None, 0
        self._reader = FrameReader()
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()

    @classmethod
    def from_environment(cls, spec=None):
        spec = os.environ.get('CYBERDECK_RADAR', '') if spec is None else spec
        return cls(source_from_spec(spec))

    def start(self):
        if self.source and not self._thread:
            self._thread = threading.Thread(target=self._run, name='radar', daemon=True)
            self._thread.start()
        return self

    def stop(self):
        self._stop.set()

    def feed(self, data):
        frames = self._reader.feed(data)
        if frames:
            with self._lock:
                self.targets, self.updated = frames[-1], self.clock()
                self.frames += len(frames)
                self.error = ''
        return frames

    def _run(self):
        while not self._stop.is_set():
            try:
                for data in self.source.chunks(self._stop):
                    self.feed(data)
                if not self._stop.is_set():
                    self.error = f'{self.source.name}: stream ended'
            except (OSError, ValueError) as exc:
                self.error = f'{self.source.name}: {exc}'
            self._stop.wait(self.retry_s)

    def snapshot(self):
        """Targets and status; no targets once the last frame is older than STALE_S."""
        if not self.source:
            return {'configured': False, 'source': None, 'simulated': False, 'targets': [],
                    'error': self.error}
        with self._lock:
            age = None if self.updated is None else self.clock() - self.updated
            fresh = age is not None and age <= STALE_S
            targets = [t.as_dict() for t in self.targets] if fresh else []
            data = {'configured': True, 'source': self.source.name,
                    'simulated': self.source.simulated, 'targets': targets,
                    'movement': any(t['moving'] for t in targets),
                    'age_s': None if age is None else round(age, 1), 'frames': self.frames,
                    'error': self.error}
        if data['simulated']:
            data['label'] = 'SIMULATED'
        return data


def describe(snapshot):
    """One line for the terminal."""
    if snapshot.get('error'):
        return f"error: {snapshot['error']}"
    label = ' SIMULATED' if snapshot['simulated'] else ''
    if snapshot['age_s'] is None:
        return 'waiting for the first frame'
    if not snapshot['targets']:
        return f'no movement{label}'
    parts = [f"{t['distance_m']:.2f} m at {t['angle_deg']:+.0f} deg, {t['speed_cms']:+d} cm/s"
             for t in snapshot['targets']]
    return f'movement{label}: ' + '; '.join(parts)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python3 -m core.hardware.ld2450', description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('source', help='serial:/dev/ttyAMA3[:baud] or replay:file')
    parser.add_argument('--record', help='append the raw bytes to this file (serial only)')
    parser.add_argument('--seconds', type=float, default=0, help='stop after this long')
    args = parser.parse_args(argv)
    try:
        radar = Radar(source_from_spec(args.source, args.record))
    except ValueError as exc:
        raise SystemExit(str(exc))
    radar.start()
    end = time.monotonic() + args.seconds if args.seconds else None
    last = None
    try:
        while end is None or time.monotonic() < end:
            line = describe(radar.snapshot())
            if line != last:
                print(line, flush=True)
                last = line
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        radar.stop()
    print(f'{radar.frames} frames', file=sys.stderr)


if __name__ == '__main__':
    main()
