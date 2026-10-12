"""GPS position from NMEA: one parser, three sources (serial port, gpsd, log replay).

No source configured means no position: nothing here ever makes up a fix. Positions from
a replayed log carry simulated=True, and anything that shows them must say SIMULATED.

Choose the source with CYBERDECK_GPS (or `python3 -m core --gps ...`):
    serial:/dev/ttyACM0[:9600]   a receiver on a serial port or USB (default 9600 baud)
    gpsd[:host[:port]]           gpsd on this Pi (default 127.0.0.1:2947)
    replay:/path/to/log.nmea     a recorded NMEA log, played back at one fix per second
"""
from dataclasses import asdict, dataclass
from datetime import date, datetime, time as dtime, timezone
import ipaddress
import os
import socket
import threading
import time

KNOT_KMH = 1.852
STALE_S = 10  # Receivers send every second; after this long the last fix is not reported.
RETRY_S = 5  # Wait between reconnects, so an unplugged receiver doesn't spin the CPU.
GARBLED_LINES = 20  # Lines in a row with no valid checksum before blaming the baud rate.


def checksum_ok(sentence):
    """True for "$...*hh" where hh is the XOR of everything between $ and *."""
    if not sentence.startswith(('$', '!')) or len(sentence) < 4 or sentence[-3] != '*':
        return False
    total = 0
    for char in sentence[1:-3]:
        total ^= ord(char)
    try:
        return total == int(sentence[-2:], 16)
    except ValueError:
        return False


def _coordinate(value, hemisphere):
    """ddmm.mmmm (or dddmm.mmmm) and N/S/E/W to signed degrees; None if empty or bad."""
    if not value or hemisphere not in ('N', 'S', 'E', 'W'):
        return None
    try:
        dot = value.index('.') if '.' in value else len(value)
        whole, minutes = float(value[:dot - 2]), float(value[dot - 2:])
    except ValueError:
        return None
    if not 0 <= minutes < 60:
        return None
    degrees = whole + minutes / 60
    limit = 90 if hemisphere in ('N', 'S') else 180
    if not 0 <= degrees <= limit:
        return None
    return round(-degrees if hemisphere in ('S', 'W') else degrees, 7)


def _float(value):
    try:
        return float(value) if value else None
    except ValueError:
        return None


def _utc_time(value):
    """hhmmss(.ss) to a time, or None."""
    try:
        seconds = float(value[4:])
        return dtime(int(value[:2]), int(value[2:4]), int(seconds),
                     int(round((seconds % 1) * 1e6)) % 1000000)
    except (ValueError, IndexError):
        return None


def parse(sentence):
    """Fields from one GGA or RMC sentence (any talker: GP, GN, GL, ...), or None for other
    sentences, bad checksums and malformed lines."""
    sentence = sentence.strip()
    if not checksum_ok(sentence):
        return None
    fields = sentence[1:-3].split(',')
    kind = fields[0][2:]
    if len(fields[0]) != 5:
        return None
    if kind == 'GGA' and len(fields) >= 10:
        quality = int(fields[6]) if fields[6].isdigit() else 0
        lat, lon = _coordinate(fields[2], fields[3]), _coordinate(fields[4], fields[5])
        # 1-5 are measured fixes; 6 is dead reckoning, 7 manual input and 8 the receiver's
        # own simulator, none of which may be shown as a real position.
        fix = 1 <= quality <= 5 and lat is not None and lon is not None
        satellites = int(fields[7]) if fields[7].isdigit() else None
        return {'kind': 'GGA', 'time': _utc_time(fields[1]), 'fix': fix,
                'lat': lat if fix else None, 'lon': lon if fix else None,
                'altitude_m': _float(fields[9]) if fix else None, 'satellites': satellites,
                'hdop': _float(fields[8])}
    if kind == 'RMC' and len(fields) >= 10:
        lat, lon = _coordinate(fields[3], fields[4]), _coordinate(fields[5], fields[6])
        # NMEA 2.3+ adds a mode letter: N none, E estimated, M manual, S simulator.
        mode = fields[12][:1] if len(fields) > 12 else 'A'
        fix = fields[2] == 'A' and mode not in ('N', 'E', 'M', 'S') and lat is not None and lon is not None
        day = None
        try:
            year = int(fields[9][4:6])  # Two digits; GPS time starts in 1980.
            day = date(year + (1900 if year >= 80 else 2000), int(fields[9][2:4]), int(fields[9][:2]))
        except (ValueError, IndexError):
            pass
        knots = _float(fields[7])
        return {'kind': 'RMC', 'time': _utc_time(fields[1]), 'date': day, 'fix': fix,
                'lat': lat if fix else None, 'lon': lon if fix else None,
                'speed_kmh': round(knots * KNOT_KMH, 2) if fix and knots is not None else None,
                'course_deg': _float(fields[8]) if fix else None}
    return None


@dataclass
class Reading:
    """The latest position. Fields are None when the receiver hasn't reported them."""
    source: str = ''
    simulated: bool = False
    fix: bool = False
    lat: float = None
    lon: float = None
    altitude_m: float = None
    satellites: int = None
    hdop: float = None
    speed_kmh: float = None
    course_deg: float = None
    utc: str = None  # ISO time from the last RMC sentence with a fix.


class Tracker:
    """Merges GGA and RMC sentences into one Reading."""

    def __init__(self, source='', simulated=False, clock=time.monotonic):
        self.clock = clock
        self.reading = Reading(source, simulated)
        self.updated = None  # Clock time of the last valid sentence.

    def feed(self, line):
        data = parse(line)
        if not data:
            return False
        r = self.reading
        if data['kind'] == 'RMC':
            r.speed_kmh, r.course_deg = data['speed_kmh'], data['course_deg']
            # Time only from a valid RMC, which carries its own date (a GGA just after
            # midnight paired with the previous RMC's date would be a day behind). Without a
            # fix the receiver's clock may be a guess, so it isn't reported.
            if data['fix'] and data['time'] and data['date']:
                r.utc = (datetime.combine(data['date'], data['time'], timezone.utc)
                         .isoformat().replace('+00:00', 'Z'))
        else:
            r.altitude_m, r.satellites, r.hdop = data['altitude_m'], data['satellites'], data['hdop']
        r.fix, r.lat, r.lon = data['fix'], data['lat'], data['lon']
        self.updated = self.clock()
        return True

    def snapshot(self):
        """The reading as a dict: position fields only with a current fix, and nothing from
        the receiver (not even its time) once the last sentence is older than STALE_S."""
        data = asdict(self.reading)
        age = None if self.updated is None else self.clock() - self.updated
        data['age_s'] = None if age is None else round(age, 1)
        if age is None or age > STALE_S:
            data.update(fix=False, satellites=None, hdop=None, utc=None)
        if not data['fix']:
            data.update(lat=None, lon=None, altitude_m=None, speed_kmh=None, course_deg=None)
        return data


class SerialSource:
    """NMEA lines from a serial port, read as a file (no pyserial)."""
    simulated = False

    def __init__(self, device, baud=9600):
        if not device.startswith('/dev/'):
            raise ValueError('GPS serial device must be under /dev/')
        self.device, self.baud, self.name = device, baud, 'serial'

    def _configure(self, fd):
        try:
            import termios
            import tty
        except ImportError:
            return
        if not os.isatty(fd):
            return  # A plain file or pipe: read it as it is.
        try:
            tty.setraw(fd)
            attrs = termios.tcgetattr(fd)
            attrs[4] = attrs[5] = getattr(termios, f'B{self.baud}')
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
        except AttributeError:
            raise ValueError(f'unsupported baud rate {self.baud}')
        except termios.error as exc:
            raise OSError(f'cannot configure {self.device}: {exc}')

    def lines(self, stop):
        with open(self.device, 'rb') as port:
            self._configure(port.fileno())
            for raw in iter(port.readline, b''):
                if stop.is_set():
                    return
                yield raw.decode('ascii', errors='replace')


class GpsdSource:
    """NMEA relayed by gpsd: WATCH with "nmea" makes it pass sentences through, so the same
    parser handles every source. Only a gpsd on this Pi is accepted."""
    simulated = False

    def __init__(self, host='127.0.0.1', port=2947, connect=socket.create_connection):
        try:
            local = host == 'localhost' or ipaddress.ip_address(host).is_loopback
        except ValueError:
            local = False
        if not local:
            raise ValueError('gpsd must be on this Pi (127.0.0.1 or localhost)')
        self.host, self.port, self.connect, self.name = host, port, connect, 'gpsd'

    def lines(self, stop):
        with self.connect((self.host, self.port), timeout=STALE_S) as sock:
            sock.sendall(b'?WATCH={"enable":true,"nmea":true};\n')
            with sock.makefile('rb') as stream:
                for raw in iter(stream.readline, b''):
                    if stop.is_set():
                        return
                    line = raw.decode('ascii', errors='replace')
                    if not line.startswith('{'):  # JSON lines are gpsd's own status replies.
                        yield line


class ReplaySource:
    """A recorded NMEA log played in a loop, one fix per second. Always SIMULATED."""
    simulated = True

    def __init__(self, path, interval=1.0, sleep=None):
        self.path, self.interval, self.name = path, interval, 'replay'
        self.sleep = sleep

    def lines(self, stop):
        sleep = self.sleep or stop.wait
        while not stop.is_set():
            with open(self.path, encoding='ascii', errors='replace') as log:
                rows = [line for line in log if parse(line)]
            if not rows:
                raise ValueError(f'{self.path} has no GGA or RMC sentences')
            last = None
            for line in rows:
                stamp = parse(line)['time']
                if last is not None and stamp != last:
                    sleep(self.interval)  # A new time stamp starts the next fix.
                    if stop.is_set():
                        return
                last = stamp
                yield line
            sleep(self.interval)


def source_from_spec(spec):
    """A source for a CYBERDECK_GPS value, or None for an empty one."""
    spec = (spec or '').strip()
    if not spec:
        return None
    kind, _, rest = spec.partition(':')
    if kind == 'serial':
        device, _, baud = rest.partition(':')
        if baud and not baud.isdigit():
            raise ValueError('GPS baud rate must be a number')
        return SerialSource(device, int(baud or 9600))
    if kind == 'gpsd':
        host, _, port = rest.partition(':')
        if port and not port.isdigit():
            raise ValueError('gpsd port must be a number')
        return GpsdSource(host or '127.0.0.1', int(port or 2947))
    if kind == 'replay':
        if not rest:
            raise ValueError('replay needs a file: replay:/path/to/log.nmea')
        return ReplaySource(rest)
    raise ValueError('CYBERDECK_GPS must start with serial:, gpsd or replay:')


class Gps:
    """Reads a source on a background thread, reconnecting after errors."""

    def __init__(self, source=None, clock=time.monotonic, retry_s=RETRY_S, error=''):
        self.source, self.retry_s = source, retry_s
        self.tracker = Tracker(source.name if source else '', bool(source and source.simulated),
                               clock)
        self.error = error
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()

    @classmethod
    def from_environment(cls, spec=None):
        spec = os.environ.get('CYBERDECK_GPS', '') if spec is None else spec
        return cls(source_from_spec(spec))

    def start(self):
        if self.source and not self._thread:
            self._thread = threading.Thread(target=self._run, name='gps', daemon=True)
            self._thread.start()
        return self

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            garbled = 0
            try:
                for line in self.source.lines(self._stop):
                    garbled = 0 if checksum_ok(line.strip()) else garbled + 1
                    with self._lock:
                        if self.tracker.feed(line):
                            self.error = ''
                        elif garbled >= GARBLED_LINES:
                            # Bytes arrive but never a valid sentence: almost always the baud rate.
                            self.error = f'{self.source.name}: no valid NMEA (wrong baud rate?)'
                if not self._stop.is_set() and garbled < GARBLED_LINES:
                    self.error = f'{self.source.name}: stream ended'
            except (OSError, ValueError) as exc:
                self.error = f'{self.source.name}: {exc}'
            self._stop.wait(self.retry_s)

    def snapshot(self):
        """For /api/gps. configured is False when no source is set; never a made-up fix."""
        if not self.source:
            return {'configured': False, 'source': None, 'simulated': False, 'fix': False,
                    'error': self.error}
        with self._lock:
            data = self.tracker.snapshot()
        data.update(configured=True, error=self.error)
        if data['simulated']:
            data['label'] = 'SIMULATED'
        return data
