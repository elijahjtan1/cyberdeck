import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import gps  # noqa: E402

try:
    from core.server import create_app  # Needs Flask.
except ImportError:
    create_app = None

# Recorded sentences (the classic NMEA reference fix, Munich, 23 March 1994).
GGA = '$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47'
RMC = '$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*6A'
# A receiver with no fix yet (u-blox, multi-constellation talker).


def nmea(body):
    total = 0
    for char in body:
        total ^= ord(char)
    return f'${body}*{total:02X}'


# A receiver with no fix yet (u-blox style, multi-constellation talker).
GGA_NO_FIX = nmea('GNGGA,083000.00,,,,,0,03,99.99,,,,,,')
RMC_VOID = nmea('GNRMC,083000.00,V,,,,,,,111026,,,N')


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class ParseTest(unittest.TestCase):
    def test_gga(self):
        data = gps.parse(GGA)
        self.assertTrue(data['fix'])
        self.assertAlmostEqual(data['lat'], 48.1173, places=4)
        self.assertAlmostEqual(data['lon'], 11.516667, places=5)
        self.assertEqual((data['altitude_m'], data['satellites'], data['hdop']), (545.4, 8, 0.9))
        self.assertEqual(data['time'].isoformat(), '12:35:19')

    def test_rmc(self):
        data = gps.parse(RMC)
        self.assertTrue(data['fix'])
        self.assertEqual(data['date'].isoformat(), '1994-03-23')
        self.assertEqual(data['speed_kmh'], round(22.4 * 1.852, 2))
        self.assertEqual(data['course_deg'], 84.4)

    def test_bad_checksum_and_garbage_are_rejected(self):
        self.assertIsNone(gps.parse(GGA[:-2] + '48'))
        self.assertIsNone(gps.parse(GGA.replace('4807', '4808')))  # Corrupted in transit.
        self.assertIsNone(gps.parse(GGA[:-3]))  # No checksum at all.
        self.assertIsNone(gps.parse('$GPGGA,*ZZ'))
        self.assertIsNone(gps.parse(''))
        self.assertIsNone(gps.parse('\x00\xff$GP'))

    def test_other_sentences_are_ignored(self):
        self.assertIsNone(gps.parse(nmea('GPGSV,3,1,11,03,03,111,00')))

    def test_no_fix_has_no_position(self):
        for sentence in (GGA_NO_FIX, RMC_VOID):
            data = gps.parse(sentence)
            self.assertFalse(data['fix'])
            self.assertIsNone(data['lat'])
        self.assertEqual(gps.parse(GGA_NO_FIX)['satellites'], 3)

    def test_southern_and_western_hemispheres(self):
        data = gps.parse(nmea('GNGGA,010203,3352.000,S,15112.600,W,1,05,1.2,10.0,M,,M,,'))
        self.assertAlmostEqual(data['lat'], -33.866667, places=5)
        self.assertAlmostEqual(data['lon'], -151.21, places=5)

    def test_impossible_coordinates_are_not_a_fix(self):
        self.assertFalse(gps.parse(nmea('GPGGA,010203,4875.000,N,01131.000,E,1,05,1,1,M,,M,,'))['fix'])
        self.assertFalse(gps.parse(nmea('GPGGA,010203,9100.000,N,01131.000,E,1,05,1,1,M,,M,,'))['fix'])


class TrackerTest(unittest.TestCase):
    def test_merges_sentences_and_goes_stale(self):
        clock = Clock()
        tracker = gps.Tracker('serial', clock=clock)
        self.assertFalse(tracker.snapshot()['fix'])  # Nothing received: no position.
        self.assertTrue(tracker.feed(RMC))
        self.assertTrue(tracker.feed(GGA))
        self.assertFalse(tracker.feed('$GPGGA,garbage*00'))
        r = tracker.snapshot()
        self.assertTrue(r['fix'])
        self.assertEqual((r['satellites'], r['altitude_m'], r['utc']), (8, 545.4, '1994-03-23T12:35:19Z'))
        self.assertEqual(r['speed_kmh'], 41.48)
        clock.t += gps.STALE_S + 1
        r = tracker.snapshot()
        self.assertFalse(r['fix'])
        self.assertIsNone(r['lat'])
        self.assertEqual(r['age_s'], gps.STALE_S + 1)

    def test_losing_the_fix_clears_the_position(self):
        tracker = gps.Tracker('serial', clock=Clock())
        tracker.feed(GGA)
        tracker.feed(GGA_NO_FIX)
        self.assertEqual((tracker.snapshot()['fix'], tracker.snapshot()['lat']), (False, None))


def run_source(source, until):
    """Feed a source into a Tracker until `until(tracker)` or the stream ends."""
    tracker, stop = gps.Tracker(source.name, source.simulated), threading.Event()
    for line in source.lines(stop):
        tracker.feed(line)
        if until(tracker):
            stop.set()
            break
    return tracker


class SourceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = Path(self.tmp.name) / 'track.nmea'
        self.log.write_text('\n'.join([
            nmea('GPGSV,3,1,11,03,03,111,00'), RMC, GGA,
            nmea('GPRMC,123520,A,4807.040,N,01131.010,E,022.4,084.4,230394,003.1,W'),
            nmea('GPGGA,123520,4807.040,N,01131.010,E,1,08,0.9,545.6,M,46.9,M,,'),
            'not nmea', '']), encoding='ascii')

    def test_replay_is_simulated_and_paced(self):
        sleeps = []
        source = gps.ReplaySource(str(self.log), sleep=sleeps.append)
        tracker = run_source(source, lambda t: t.reading.altitude_m == 545.6)
        r = tracker.snapshot()
        self.assertTrue(r['simulated'])
        self.assertEqual(r['source'], 'replay')
        self.assertEqual(r['utc'], '1994-03-23T12:35:20Z')
        self.assertEqual(sleeps, [1.0])  # One pause between the two fixes.

    def test_replay_loops_and_rejects_empty_logs(self):
        sleeps = []
        source = gps.ReplaySource(str(self.log), sleep=sleeps.append)
        count = [0]

        def until(tracker):
            count[0] += 1
            return count[0] == 9  # Past the end of the 4-sentence log, twice.
        run_source(source, until)
        self.assertEqual(len(sleeps), 4)
        self.log.write_text('nothing here\n')
        with self.assertRaises(ValueError):
            next(gps.ReplaySource(str(self.log), sleep=sleeps.append).lines(threading.Event()))

    def test_serial_reads_a_device_file(self):
        source = gps.SerialSource('/dev/null')
        self.assertEqual(list(source.lines(threading.Event())), [])
        source.device = str(self.log)  # A plain file stands in for the tty.
        tracker = run_source(source, lambda t: False)
        self.assertFalse(tracker.reading.simulated)
        self.assertEqual(tracker.reading.altitude_m, 545.6)
        with self.assertRaises(ValueError):
            gps.SerialSource('ttyUSB0')

    def test_gpsd_watches_nmea_and_skips_json(self):
        server, client = socket.socketpair()
        self.addCleanup(server.close)
        sent = []

        def connect(address, timeout):
            sent.append(address)
            return client
        server.sendall(('{"class":"VERSION","release":"3.25"}\n' + RMC + '\r\n' + GGA + '\r\n'
                        + '{"class":"WATCH","enable":true}\n').encode())
        server.shutdown(socket.SHUT_WR)
        source = gps.GpsdSource(connect=connect)
        tracker = run_source(source, lambda t: False)
        self.assertEqual(sent, [('127.0.0.1', 2947)])
        self.assertIn(b'?WATCH={"enable":true,"nmea":true};', server.recv(200))
        self.assertTrue(tracker.snapshot()['fix'])
        self.assertFalse(tracker.snapshot()['simulated'])

    def test_gpsd_must_be_local(self):
        with self.assertRaises(ValueError):
            gps.GpsdSource('192.168.1.5')
        gps.GpsdSource('localhost')
        gps.GpsdSource('::1')

    def test_source_specs(self):
        self.assertIsNone(gps.source_from_spec(''))
        self.assertEqual(gps.source_from_spec('serial:/dev/ttyACM0').baud, 9600)
        self.assertEqual(gps.source_from_spec('serial:/dev/ttyAMA0:38400').baud, 38400)
        self.assertEqual(gps.source_from_spec('gpsd').port, 2947)
        self.assertEqual(gps.source_from_spec('gpsd:localhost:3000').port, 3000)
        self.assertTrue(gps.source_from_spec(f'replay:{self.log}').simulated)
        for bad in ('usb', 'serial:/dev/x:fast', 'gpsd:10.0.0.2', 'replay:', 'serial:COM3'):
            with self.assertRaises(ValueError, msg=bad):
                gps.source_from_spec(bad)


class GpsServiceTest(unittest.TestCase):
    def test_no_source_means_no_position(self):
        self.assertEqual(gps.Gps.from_environment('').snapshot(),
                         {'configured': False, 'source': None, 'simulated': False, 'fix': False,
                          'error': ''})

    def test_background_replay_is_labelled_simulated(self):
        with tempfile.NamedTemporaryFile('w', suffix='.nmea', delete=False) as log:
            log.write(RMC + '\n' + GGA + '\n')
        self.addCleanup(Path(log.name).unlink)
        source = gps.ReplaySource(log.name, interval=0.01)
        service = gps.Gps(source).start()
        self.addCleanup(service.stop)
        for _ in range(200):
            if service.snapshot()['fix']:
                break
            threading.Event().wait(0.01)
        r = service.snapshot()
        self.assertEqual((r['configured'], r['fix'], r['simulated'], r['label']),
                         (True, True, True, 'SIMULATED'))

    def test_errors_are_reported_and_retried(self):
        service = gps.Gps(gps.SerialSource('/dev/no-such-gps'), retry_s=0.01).start()
        self.addCleanup(service.stop)
        for _ in range(200):
            if service.snapshot()['error']:
                break
            threading.Event().wait(0.01)
        r = service.snapshot()
        self.assertIn('serial:', r['error'])
        self.assertFalse(r['fix'])
        self.assertNotIn('label', r)


@unittest.skipIf(create_app is None, 'Flask not installed (sudo apt install python3-flask)')
class GpsApiTest(unittest.TestCase):
    def test_api_without_and_with_a_source(self):
        from core.telemetry import Telemetry, TelemetryStore
        from tests.test_core import FakeSampler
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        telemetry = Telemetry(TelemetryStore(Path(tmp.name) / 't.sqlite3'), FakeSampler())
        self.addCleanup(telemetry.store.close)
        data = create_app(telemetry).test_client().get('/api/gps').get_json()
        self.assertEqual((data['configured'], data['fix']), (False, False))
        service = gps.Gps(gps.ReplaySource('unused'))
        service.tracker.feed(GGA)
        data = create_app(telemetry, gps=service).test_client().get('/api/gps').get_json()
        self.assertEqual((data['fix'], data['simulated'], data['label']), (True, True, 'SIMULATED'))
        self.assertAlmostEqual(data['lat'], 48.1173, places=4)


if __name__ == '__main__':
    unittest.main()
