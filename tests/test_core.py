import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import sysinfo  # noqa: E402
from core.telemetry import Telemetry, TelemetryStore  # noqa: E402
from panel_tools.backend import Backend  # noqa: E402
import setup_core_service  # noqa: E402

try:
    from core.server import create_app  # Needs Flask.
except ImportError:
    create_app = None


class Clock:
    def __init__(self, t=1000.):
        self.t = t

    def __call__(self):
        return self.t


class FakeSampler:
    def __init__(self):
        self.temp = 50.

    def read(self):
        return {'cpu': 20., 'temp_c': self.temp, 'mem_percent': 30., 'disk_free_gib': 10.,
                'load1': .5, 'freq_mhz': 1500.}


def make_telemetry(folder, flags=None, **kw):
    clock = Clock()
    flags = flags if flags is not None else []
    telemetry = Telemetry(TelemetryStore(Path(folder) / 't.sqlite3'), FakeSampler(),
                          throttled=lambda: flags.pop(0) if flags else 0, clock=clock, **kw)
    return telemetry, clock


class SysinfoTest(unittest.TestCase):
    def test_sampler_reads_proc_and_sys(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'proc').mkdir()
            (root / 'sys/class/thermal/thermal_zone0').mkdir(parents=True)
            stat = root / 'proc/stat'
            stat.write_text('cpu  100 0 100 700 100 0 0 0 0 0\n')
            (root / 'proc/meminfo').write_text('MemTotal: 4000000 kB\nMemAvailable: 3000000 kB\n')
            (root / 'proc/uptime').write_text('3725.5 100.0\n')
            (root / 'proc/loadavg').write_text('0.42 0.30 0.20 1/100 999\n')
            (root / 'sys/class/thermal/thermal_zone0/temp').write_text('51234\n')
            sampler = sysinfo.SystemSampler(root, disk=folder)
            first = sampler.read()
            self.assertIsNone(first['cpu'])
            self.assertAlmostEqual(first['temp_c'], 51.234)
            self.assertAlmostEqual(first['mem_percent'], 25.)
            self.assertEqual(first['uptime_s'], 3725)
            self.assertEqual(first['load1'], .42)
            self.assertIsNone(first['freq_mhz'])  # Missing file: unknown, not an error.
            stat.write_text('cpu  150 0 150 750 150 0 0 0 0 0\n')  # 100 busy of 200 ticks.
            self.assertAlmostEqual(sampler.read()['cpu'], 50.)

    def test_throttle_flags(self):
        self.assertEqual(sysinfo.decode_flags(0x50005)['power'], 'LOW VOLTAGE')
        self.assertEqual(sysinfo.decode_flags(None)['power'], 'Unknown')
        self.assertEqual(sysinfo.read_throttled(lambda args: 'throttled=0x4'), 4)
        self.assertIsNone(sysinfo.read_throttled(lambda args: ''))
        self.assertFalse(sysinfo.undervoltage_seen(None, 0x50000))  # Old news at start.
        self.assertTrue(sysinfo.undervoltage_seen(0, 0x50000))  # Dip between readings.
        self.assertTrue(sysinfo.undervoltage_seen(0x50000, 0x50001))
        self.assertFalse(sysinfo.undervoltage_seen(0x50000, 0x50000))


class TelemetryTest(unittest.TestCase):
    def test_buffers_then_writes_once_a_minute(self):
        with tempfile.TemporaryDirectory() as folder:
            telemetry, clock = make_telemetry(folder)
            for _ in range(12):  # One minute at 5 s.
                telemetry.tick()
                clock.t += 5
            self.assertEqual(telemetry.store.buckets(0, 10**6), [])  # Nothing on disk yet.
            history = telemetry.history(600, 600)  # 1 s buckets: one per sample.
            self.assertEqual(len(history['samples']), 12)  # Readers see the buffer.
            telemetry.tick()
            on_disk = telemetry.store.buckets(0, 10**6)
            self.assertEqual(on_disk[0][1], 13)
            self.assertEqual(len(telemetry.history(600, 600)['samples']), 13)
            telemetry.store.close()

    def test_history_buckets_average_cpu_and_keep_max_temperature(self):
        with tempfile.TemporaryDirectory() as folder:
            telemetry, clock = make_telemetry(folder)
            for temp in (40., 70., 50.):
                telemetry.sampler.temp = temp
                telemetry.tick()
                clock.t += 1
            telemetry.flush()
            telemetry.sampler.temp = 80.
            telemetry.tick()  # Still in the buffer; same bucket as the stored ones.
            samples = telemetry.history(600, 1)['samples']
            self.assertEqual(len(samples), 1)
            self.assertEqual(samples[0][1], 20.)
            self.assertEqual(samples[0][2], 80.)
            telemetry.store.close()

    def test_undervoltage_and_throttling_become_events(self):
        with tempfile.TemporaryDirectory() as folder:
            flags = [0, 0x50005, 0x50005, 0x50000, 0x50000, 0x50000]
            telemetry, clock = make_telemetry(folder, flags)
            for _ in range(6):
                telemetry.tick()
                clock.t += 5
            telemetry.close()
            events = telemetry.store.events(0)
            kinds = sorted(e['kind'] for e in events)
            self.assertEqual(kinds, ['throttled', 'undervoltage'])
            low = next(e for e in events if e['kind'] == 'undervoltage')
            self.assertEqual((low['start'], low['end']), (1005., 1010.))
            history = telemetry.history(3600)
            self.assertEqual(sum(s[4] for s in history['samples']), 1)  # Both in one 15 s bucket.
            telemetry.store.close()

    def test_old_undervoltage_from_before_start_is_not_an_event(self):
        with tempfile.TemporaryDirectory() as folder:
            telemetry, clock = make_telemetry(folder, [0x50000, 0x50000])
            telemetry.tick()
            clock.t += 5
            telemetry.tick()
            telemetry.close()
            self.assertEqual(telemetry.store.events(0), [])
            telemetry.store.close()

    def test_activities_show_while_running_and_are_logged(self):
        with tempfile.TemporaryDirectory() as folder:
            telemetry, clock = make_telemetry(folder)
            with telemetry.activity('ai', 'How does a sonobuoy work?'):
                clock.t += 12
                self.assertEqual([a['kind'] for a in telemetry.now()['active']], ['ai'])
                running = telemetry.history(600)['events']
                self.assertEqual(running[0]['end'], clock.t)
            self.assertEqual(telemetry.now()['active'], [])
            telemetry.mark('camera', 'photo')
            self.assertEqual(telemetry.begin('nonsense'), 3)
            self.assertEqual(telemetry.now()['active'][0]['kind'], 'other')
            telemetry.close()  # Unfinished activities are saved, ending now.
            events = telemetry.store.events(0)
            self.assertEqual([e['kind'] for e in events], ['ai', 'camera', 'other'])
            self.assertEqual(events[0]['end'] - events[0]['start'], 12)
            telemetry.store.close()

    def test_old_rows_are_pruned(self):
        with tempfile.TemporaryDirectory() as folder:
            telemetry, clock = make_telemetry(folder, keep_days=1)
            telemetry.tick()
            telemetry.mark('camera')
            telemetry.flush()
            clock.t += 2*86400
            telemetry.tick()  # Prune is due on the first tick after an hour.
            self.assertEqual(telemetry.store.events(0), [])
            self.assertEqual([row[1] for row in telemetry.store.buckets(0, 10**6)], [1])  # Only the new one.
            telemetry.store.close()


@unittest.skipIf(create_app is None, 'Flask not installed (sudo apt install python3-flask)')
class ServerTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.telemetry, self.clock = make_telemetry(self.folder.name, [0x5])
        self.client = create_app(self.telemetry).test_client()

    def tearDown(self):
        self.telemetry.store.close()
        self.folder.cleanup()

    def test_health_now_and_history(self):
        self.assertTrue(self.client.get('/api/health').get_json()['ok'])
        self.assertIsNone(self.client.get('/api/telemetry/now').get_json()['sample'])
        self.telemetry.tick()
        state = self.client.get('/api/telemetry/now').get_json()
        self.assertEqual(state['sample']['throttled'], 5)
        self.assertIn(['critical', 'LOW VOLTAGE'], state['alerts'])
        self.assertIn(['warning', 'THROTTLED'], state['alerts'])
        history = self.client.get('/api/telemetry/history?seconds=600&points=20').get_json()
        self.assertEqual(len(history['samples']), 1)
        for bad in ('seconds=5', 'points=5000', 'seconds=abc'):
            self.assertEqual(self.client.get('/api/telemetry/history?' + bad).status_code, 400)

    def test_activity_endpoints(self):
        started = self.client.post('/api/activity/start', json={'kind': 'ai', 'detail': 'q'})
        ident = started.get_json()['id']
        self.assertEqual(self.client.get('/api/telemetry/now').get_json()['busy'], ['ai'])
        self.assertTrue(self.client.post('/api/activity/end', json={'id': ident}).get_json()['ended'])
        self.assertFalse(self.client.post('/api/activity/end', json={'id': ident}).get_json()['ended'])
        self.assertEqual(self.client.post('/api/activity/mark', json={'kind': 'camera'}).status_code, 200)
        self.assertEqual(self.client.post('/api/activity/start', json={'kind': 'rm -rf'}).status_code, 400)
        self.assertEqual(self.client.post('/api/activity/end', json={'id': 'x'}).status_code, 400)
        self.assertEqual(self.client.post('/api/activity/mark', data='not json').status_code, 400)


class FakeOpener:
    """Answers urllib requests from a dict of path -> JSON, or fails like a stopped server."""
    def __init__(self, answers=None):
        self.answers, self.requests = answers, []

    def open(self, request, timeout):
        path = request.full_url.split('8765', 1)[1]
        self.requests.append((path, request.data and json.loads(request.data)))
        if self.answers is None:
            raise urllib.error.URLError('Connection refused')
        return io.BytesIO(json.dumps(self.answers.get(path.split('?')[0], {})).encode())


class BackendClientTest(unittest.TestCase):
    def test_offline_backend_is_reported_and_marks_are_dropped(self):
        backend = Backend(opener=FakeOpener(None))
        self.assertIsNone(backend.online)
        backend.step()
        self.assertFalse(backend.online)
        self.assertEqual(backend.busy, [])
        backend.mark('camera', 'photo')
        self.assertTrue(backend.outbox.empty())

    def test_state_marks_and_history_only_when_wanted(self):
        opener = FakeOpener({'/api/telemetry/now': {'busy': ['ai']},
                             '/api/telemetry/history': {'seconds': 3600, 'samples': []}})
        clock = Clock(0)
        backend = Backend(opener=opener, clock=clock)
        backend.mark('camera', 'photo')
        backend.step()
        self.assertTrue(backend.online)
        self.assertEqual(backend.busy, ['ai'])
        paths = [p for p, _ in opener.requests]
        self.assertEqual(paths, ['/api/activity/mark', '/api/telemetry/now'])
        self.assertEqual(opener.requests[0][1], {'kind': 'camera', 'detail': 'photo'})
        backend.want_history(3600)
        backend.step()
        self.assertEqual(backend.history['seconds'], 3600)
        count = len(opener.requests)
        backend.step()  # History is not due again yet.
        self.assertEqual(len(opener.requests), count + 1)
        backend.want_history(None)
        clock.t = 999
        backend.step()
        self.assertFalse(any('history' in p for p, _ in opener.requests[count+1:]))


class CoreServiceSetupTest(unittest.TestCase):
    def test_unit_runs_as_user_on_localhost(self):
        text = setup_core_service.unit_text(Path('/home/eli/cyberdeck-repo'), 'eli',
                                            Path('/home/eli/cyberdeck-data'))
        self.assertIn('User=eli', text)
        self.assertIn('ExecStart=/usr/bin/python3 -m core --data /home/eli/cyberdeck-data --port 8765', text)
        self.assertIn('WorkingDirectory=/home/eli/cyberdeck-repo', text)
        with self.assertRaises(RuntimeError):
            setup_core_service.unit_text(Path('/home/eli/my repo'), 'eli', Path('/x'))

    def test_install_and_undo(self):
        with tempfile.TemporaryDirectory() as folder:
            root, commands = Path(folder), []
            path = setup_core_service.install(Path('/r'), 'eli', Path('/d'), root=root,
                                              run=commands.append, start=True)
            self.assertTrue(path.read_text().startswith(setup_core_service.MARK))
            self.assertIn(['systemctl', 'restart', setup_core_service.NAME], commands)
            setup_core_service.undo(root=root, run=commands.append)
            self.assertFalse(path.exists())
            path.write_text('someone else\n')
            with self.assertRaises(RuntimeError):
                setup_core_service.install(Path('/r'), 'eli', Path('/d'), root=root, run=commands.append)

    def test_kiwix_settings_reach_the_service_environment(self):
        kiwix = setup_core_service.kiwix_settings('http://127.0.0.1:8080',
                                                  'restarters_en_all_maxi_2026-04')
        text = setup_core_service.unit_text(Path('/r'), 'eli', Path('/d'), env=kiwix)
        self.assertIn('Environment=CYBERDECK_KIWIX_URL=http://127.0.0.1:8080\n', text)
        self.assertIn('Environment=CYBERDECK_KIWIX_BOOK=restarters_en_all_maxi_2026-04\n', text)
        self.assertNotIn('BOOK_PARAMETER', text)  # Unset keeps the librarian's default.
        self.assertNotIn('CYBERDECK_KIWIX', setup_core_service.unit_text(Path('/r'), 'eli', Path('/d')))
        # The variables the unit sets are the ones the librarian reads.
        environ = dict(line.removeprefix('Environment=').split('=', 1)
                       for line in text.splitlines() if line.startswith('Environment=CYBERDECK'))
        from core.librarian.service import Retriever
        with mock.patch.dict(os.environ, environ, clear=True):
            client = Retriever.from_environment().kiwix
        self.assertEqual(client.base_url, 'http://127.0.0.1:8080/')
        self.assertEqual((client.book, client.book_parameter),
                         ('restarters_en_all_maxi_2026-04', 'books.name'))

    def test_kiwix_settings_are_checked(self):
        bad = [('http://192.168.1.5:8080', ''), ('https://127.0.0.1:8080', ''),
               ('http://127.0.0.1:8080', 'two words'), ('http://127.0.0.1:8080', '50%'),
               ('http://127.0.0.1:8080', 'a"b'), ('', 'book-without-url')]
        for url, book in bad:
            with self.subTest(url=url, book=book), self.assertRaises(RuntimeError):
                setup_core_service.kiwix_settings(url, book)
        with self.assertRaises(RuntimeError):
            setup_core_service.kiwix_settings('http://127.0.0.1:8080', book_parameter='pattern')
        self.assertEqual(setup_core_service.kiwix_settings(), {})

    def test_reinstall_keeps_or_clears_kiwix_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            root, commands = Path(folder), []
            kiwix = setup_core_service.kiwix_settings('http://127.0.0.1:8080', 'book', 'content')
            path = setup_core_service.install(Path('/r'), 'eli', Path('/d'), root=root,
                                              run=commands.append, kiwix=kiwix)
            self.assertEqual(setup_core_service.saved_settings(path), kiwix)
            setup_core_service.install(Path('/r'), 'eli', Path('/d'), root=root, run=commands.append)
            self.assertEqual(setup_core_service.saved_settings(path), kiwix)
            setup_core_service.install(Path('/r'), 'eli', Path('/d'), root=root,
                                       run=commands.append, kiwix={})
            self.assertEqual(setup_core_service.saved_settings(path), {})
            self.assertNotIn('KIWIX', path.read_text())

    def test_radar_setting_is_kept_apart_from_kiwix(self):
        spec = 'serial:/dev/serial/by-id/usb-Silicon_Labs_CP2102-if00-port0'
        with tempfile.TemporaryDirectory() as folder:
            root, commands = Path(folder), []
            kiwix = setup_core_service.kiwix_settings('http://127.0.0.1:8080', 'book')
            path = setup_core_service.install(Path('/r'), 'eli', Path('/d'), root=root,
                                              run=commands.append, kiwix=kiwix)
            radar = setup_core_service.radar_settings(spec)
            setup_core_service.install(Path('/r'), 'eli', Path('/d'), root=root,
                                       run=commands.append, radar=radar)
            self.assertIn(f'Environment=CYBERDECK_RADAR={spec}\n', path.read_text())
            self.assertEqual(setup_core_service.saved_settings(path), {**kiwix, **radar})
            setup_core_service.install(Path('/r'), 'eli', Path('/d'), root=root,
                                       run=commands.append, kiwix={})
            self.assertEqual(setup_core_service.saved_settings(path), radar)
            setup_core_service.install(Path('/r'), 'eli', Path('/d'), root=root,
                                       run=commands.append, radar={})
            self.assertEqual(setup_core_service.saved_settings(path), {})

    def test_radar_setting_is_checked(self):
        self.assertEqual(setup_core_service.radar_settings(), {})
        for bad in ('ttyUSB0', 'serial:/tmp/x', 'serial:/dev/ttyUSB0:0', 'serial:/dev/my port',
                    'replay:/home/eli/$x'):
            with self.assertRaises(RuntimeError, msg=bad):
                setup_core_service.radar_settings(bad)


if __name__ == '__main__':
    unittest.main()
