import io
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.hardware import ld2450  # noqa: E402
from core.hardware.ld2450 import FrameReader, Target, encode_frame, parse_frame  # noqa: E402

# The example frame from the LD2450 serial protocol manual: one target 0.78 m to the left,
# 1.71 m ahead, approaching at 16 cm/s. Synthetic, not recorded from Eli's sensor.
MANUAL_FRAME = bytes.fromhex('aaff0300 0e03b186 10004001' + '00' * 16 + '55cc')


class Clock:
    def __init__(self):
        self.t = 100.

    def __call__(self):
        return self.t


class FrameTest(unittest.TestCase):
    def test_manual_example(self):
        targets = parse_frame(MANUAL_FRAME)
        self.assertEqual(targets, [Target(-782, 1713, -16, 320)])
        t = targets[0].as_dict()
        self.assertEqual((t['distance_m'], t['angle_deg'], t['moving']), (1.88, -24.5, True))

    def test_encode_round_trip(self):
        targets = [Target(1200, 3000, 0, 320), Target(-50, 600, 25, 320), Target(0, 1, -1, 0)]
        self.assertEqual(parse_frame(encode_frame(targets)), targets)
        self.assertEqual(parse_frame(encode_frame([])), [])
        self.assertEqual(encode_frame([Target(-782, 1713, -16, 320)]), MANUAL_FRAME)

    def test_not_a_frame(self):
        self.assertIsNone(parse_frame(MANUAL_FRAME[:-1]))
        self.assertIsNone(parse_frame(b'\0' + MANUAL_FRAME[1:]))
        self.assertIsNone(parse_frame(MANUAL_FRAME[:-2] + b'\0\0'))

    def test_reader_resyncs_through_noise_and_splits(self):
        reader = FrameReader()
        stream = b'\x01\x02\xaa\xff\x03' + MANUAL_FRAME + MANUAL_FRAME[:10]
        self.assertEqual(len(reader.feed(stream)), 1)
        self.assertEqual(reader.feed(MANUAL_FRAME[10:]), [[Target(-782, 1713, -16, 320)]])
        self.assertEqual(reader.feed(b'\x00' * 1000), [])
        self.assertLess(len(reader.buffer), 4)  # Noise doesn't pile up.


class SourceTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def test_specs(self):
        self.assertIsNone(ld2450.source_from_spec(''))
        self.assertEqual(ld2450.source_from_spec('serial:/dev/ttyAMA3').baud, 256000)
        self.assertEqual(ld2450.source_from_spec('serial:/dev/ttyUSB0:115200').baud, 115200)
        self.assertTrue(ld2450.source_from_spec('replay:/x.bin').simulated)
        for bad in ('usb', 'serial:COM3', 'serial:/dev/x:fast', 'serial:/dev/x:0', 'replay:'):
            with self.assertRaises(ValueError, msg=bad):
                ld2450.source_from_spec(bad)

    def test_serial_reads_and_records(self):
        stream = self.dir / 'port'
        stream.write_bytes(b'noise' + MANUAL_FRAME * 2)  # A plain file stands in for the tty.
        record = self.dir / 'rec.bin'
        source = ld2450.SerialSource('/dev/null', record=str(record))
        source.device = str(stream)
        radar = ld2450.Radar(source)
        for chunk in source.chunks(threading.Event()):
            radar.feed(chunk)
        self.assertEqual(radar.frames, 2)
        self.assertEqual(record.read_bytes(), stream.read_bytes())

    def test_replay_loops_at_sensor_pace(self):
        log = self.dir / 'rec.bin'
        log.write_bytes(MANUAL_FRAME + encode_frame([]))
        sleeps, stop = [], threading.Event()
        source = ld2450.ReplaySource(str(log), sleep=sleeps.append)
        chunks = []
        for chunk in source.chunks(stop):
            chunks.append(chunk)
            if len(chunks) == 5:
                stop.set()
        self.assertEqual(chunks[0], MANUAL_FRAME)
        self.assertEqual(chunks[2], MANUAL_FRAME)  # Looped.
        self.assertEqual(sleeps, [0.1] * 5)  # One per frame.
        log.write_bytes(b'nothing')
        with self.assertRaises(ValueError):
            next(ld2450.ReplaySource(str(log)).chunks(threading.Event()))

    def test_set_baud_uses_termios2(self):
        import termios
        calls = []

        def ioctl(fd, request, arg):
            calls.append((request, arg))
            return ld2450.TERMIOS2.pack(0, 0, termios.B9600, 0, 0, bytes(19), 9600, 9600)
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        ld2450.set_baud(slave, 256000, ioctl=ioctl)
        request, packed = calls[-1]
        self.assertEqual(request, ld2450.TCSETS2)
        _, _, cflag, _, _, _, ispeed, ospeed = ld2450.TERMIOS2.unpack(packed)
        self.assertEqual((ispeed, ospeed), (256000, 256000))
        self.assertEqual(cflag & ld2450.CBAUD, ld2450.BOTHER)


class RadarTest(unittest.TestCase):
    def test_off_by_default(self):
        snap = ld2450.Radar.from_environment('').snapshot()
        self.assertEqual((snap['configured'], snap['targets']), (False, []))

    def test_targets_go_stale(self):
        clock = Clock()
        radar = ld2450.Radar(ld2450.SerialSource('/dev/null'), clock=clock)
        self.assertEqual(ld2450.describe(radar.snapshot()), 'waiting for the first frame')
        radar.feed(MANUAL_FRAME)
        snap = radar.snapshot()
        self.assertTrue(snap['movement'])
        self.assertNotIn('label', snap)
        self.assertIn('1.88 m at -24 deg, -16 cm/s', ld2450.describe(snap))
        clock.t += ld2450.STALE_S + 1
        snap = radar.snapshot()
        self.assertEqual((snap['targets'], snap['stale'], snap['movement']), ([], True, False))
        # A sensor that went silent must not read as "no movement" (review of #36).
        self.assertEqual(ld2450.describe(snap), 'no data for 3 s (sensor silent or unplugged?)')
        radar.feed(encode_frame([]))
        snap = radar.snapshot()
        self.assertEqual((snap['stale'], ld2450.describe(snap)), (False, 'no movement'))

    def test_port_setup_failure_is_reported_and_retried(self):
        import termios

        class BadPort(ld2450.SerialSource):
            def chunks(self, stop):
                self.attempts = getattr(self, 'attempts', 0) + 1
                ld2450.set_baud(0, 256000, ioctl=self.ioctl)
                yield b''

            def ioctl(self, *args):
                raise termios.error(22, 'Invalid argument')
        source = BadPort('/dev/null')
        radar = ld2450.Radar(source, retry_s=0.01).start()
        self.addCleanup(radar.stop)
        with mock.patch('tty.setraw'):
            for _ in range(200):
                if getattr(source, 'attempts', 0) >= 2:
                    break
                threading.Event().wait(0.01)
        self.assertIn('cannot configure the port', radar.snapshot()['error'])
        self.assertGreaterEqual(source.attempts, 2)  # Still retrying.

    def test_recording_stops_growing_at_the_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            stream, record = Path(tmp) / 'port', Path(tmp) / 'rec.bin'
            stream.write_bytes(MANUAL_FRAME * 20)
            source = ld2450.SerialSource('/dev/null', record=str(record))
            source.device = str(stream)
            with mock.patch.object(ld2450, 'RECORD_MAX', 100):
                list(source.chunks(threading.Event()))
            self.assertLess(record.stat().st_size, 100 + 256)

    def test_still_target_is_not_movement(self):
        radar = ld2450.Radar(ld2450.SerialSource('/dev/null'))
        radar.feed(encode_frame([Target(0, 900, 0, 320)]))
        snap = radar.snapshot()
        self.assertEqual((len(snap['targets']), snap['movement']), (1, False))

    def test_replay_is_labelled_simulated(self):
        with tempfile.NamedTemporaryFile(suffix='.bin', delete=False) as log:
            log.write(MANUAL_FRAME)
        self.addCleanup(Path(log.name).unlink)
        radar = ld2450.Radar(ld2450.ReplaySource(log.name, interval=0.01)).start()
        self.addCleanup(radar.stop)
        for _ in range(200):
            if radar.snapshot()['targets']:
                break
            threading.Event().wait(0.01)
        snap = radar.snapshot()
        self.assertEqual((snap['simulated'], snap['label']), (True, 'SIMULATED'))
        self.assertTrue(ld2450.describe(snap).startswith('SIMULATED movement'))

    def test_errors_are_reported_and_retried(self):
        radar = ld2450.Radar(ld2450.SerialSource('/dev/no-such-radar'), retry_s=0.01).start()
        self.addCleanup(radar.stop)
        for _ in range(200):
            if radar.snapshot()['error']:
                break
            threading.Event().wait(0.01)
        self.assertIn('serial:', radar.snapshot()['error'])
        self.assertTrue(ld2450.describe(radar.snapshot()).startswith('error'))

    def test_command_line_replay(self):
        with tempfile.NamedTemporaryFile(suffix='.bin', delete=False) as log:
            log.write(MANUAL_FRAME)
        self.addCleanup(Path(log.name).unlink)
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            ld2450.main([f'replay:{log.name}', '--seconds', '0.7'])
        self.assertIn('SIMULATED movement', out.getvalue())


try:
    from core.server import create_app
except ImportError:  # Flask is only needed for the backend.
    create_app = None


@unittest.skipIf(create_app is None, 'Flask not installed (sudo apt install python3-flask)')
class RadarApiTest(unittest.TestCase):
    def test_api_without_and_with_a_source(self):
        from core.telemetry import Telemetry, TelemetryStore
        from tests.test_core import FakeSampler
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        telemetry = Telemetry(TelemetryStore(Path(tmp.name) / 't.sqlite3'), FakeSampler())
        self.addCleanup(telemetry.store.close)
        data = create_app(telemetry).test_client().get('/api/radar').get_json()
        self.assertEqual((data['configured'], data['targets']), (False, []))
        self.assertEqual((data['range_m'], data['fov_deg']), (6.0, 120))
        radar = ld2450.Radar(ld2450.ReplaySource('unused'))
        radar.feed(MANUAL_FRAME)
        data = create_app(telemetry, radar=radar).test_client().get('/api/radar').get_json()
        self.assertEqual((data['simulated'], data['label'], data['movement']),
                         (True, 'SIMULATED', True))
        self.assertEqual(data['targets'][0]['y_mm'], 1713)


if __name__ == '__main__':
    unittest.main()
