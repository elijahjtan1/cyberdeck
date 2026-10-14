"""Home page and start-at-boot switch."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
import panel_app as app  # noqa: E402


def centre(rect):
    return (rect[0]+rect[2])/2, (rect[1]+rect[3])/2


class HomePageTest(unittest.TestCase):
    rows = {'temp': '48.2 C', 'disk_free': '18 GiB'}

    def tearDown(self):
        app.set_rotation(0)

    def test_tiles_fit_and_lead_to_their_pages(self):
        for rotation in (0, 90):
            app.set_rotation(rotation)
            image = app.render('home', None, False, 20, self.rows, '', extra={'home': {}})
            self.assertEqual(image.size, (app.W, app.H))
            rects = app.home_rects()
            self.assertEqual(len(rects), 9)
            for rect in rects:
                self.assertTrue(0 <= rect[0] < rect[2] < app.W and 30 < rect[1] < rect[3]
                                <= app.content_bottom(), (rotation, rect))
            self.assertEqual([app.hit(*centre(r)) for r in rects],
                             ['home:' + key for _, key, _ in app.HOME_TILES])
            self.assertIn('tab:home', [a for _, a in app.HOTSPOTS])

    def test_every_app_is_reachable_once(self):
        home = [key for _, key, _ in app.HOME_TILES]
        more = [key for _, key in app.APP_TILES]
        tabs = [tab.lower() for tab in app.TABS]
        self.assertIn('apps', home)  # MORE is the only way to the apps listed there.
        everything = home + more + tabs
        for key in ('reader', 'planner', 'ask', 'tools', 'quick', 'audio', 'games', 'sos',
                    'album', 'gifs', 'camera', 'photos'):
            self.assertEqual(everything.count(key), 1, key)  # Reachable, and not listed twice.

    def test_home_tab_sits_where_apps_was(self):
        app.render('status', None, False, 20, self.rows, '')
        self.assertEqual(app.hit(136, 222), 'tab:home')


class BootSwitchTest(unittest.TestCase):
    def test_reads_systemd_state(self):
        def run(stdout):
            return lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=stdout, stderr='')
        self.assertTrue(app.boot_service_enabled(run('enabled\n')))
        self.assertFalse(app.boot_service_enabled(run('disabled\n')))
        def missing(*a, **k):
            raise FileNotFoundError('systemctl')
        self.assertIsNone(app.boot_service_enabled(missing))

    def test_turning_on_installs_and_enables_without_starting(self):
        calls = []
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(app.os, 'geteuid', return_value=0, create=True), \
                mock.patch('setup_panel_service.unit_path',
                           return_value=Path(folder) / 'cyberdeck-panel.service'):
            message = app.set_boot_service(True, ROOT / 'panel_app.py', Path(folder),
                                           run=lambda cmd, **k: calls.append(cmd))
            unit = (Path(folder) / 'cyberdeck-panel.service').read_text()
        self.assertIn('Will start at boot', message)
        self.assertIn(['systemctl', 'enable', 'cyberdeck-panel.service'], calls)
        self.assertFalse(any('start' in c or 'restart' in c for c in calls))  # Not now.
        self.assertIn('--data', unit)

    def test_turning_off_only_disables(self):
        calls = []
        with mock.patch.object(app.os, 'geteuid', return_value=0, create=True):
            message = app.set_boot_service(False, ROOT / 'panel_app.py', ROOT,
                                           run=lambda cmd, **k: calls.append(cmd))
        self.assertEqual(calls, [['systemctl', 'disable', 'cyberdeck-panel.service']])
        self.assertIn('not start', message)

    def test_needs_root(self):
        with mock.patch.object(app.os, 'geteuid', return_value=1000, create=True):
            self.assertIn('sudo', app.set_boot_service(True, ROOT / 'panel_app.py', ROOT))


class StartPageTest(unittest.TestCase):
    def test_app_opens_on_home_and_tiles_work(self):
        import test_panel_app as t
        # Default start page is Home: GAMES tile -> BACK to Home -> CAMERA tab -> Exit.
        games_back = centre(app.GamesMenu(lambda: {}).rects()[1])
        keys = [key for _, key, _ in app.HOME_TILES]
        games_tile = centre(app.home_rects()[keys.index('games')])
        script = [(.5, *games_tile), (1.1, *games_back), (1.7, 29, 222),
                  (2.3, 300, 222), (2.9, 160, 150)]
        t.FakeTouch.script, t.FakeScreen.frames = script, []
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder)
            (data / 'photos').mkdir()
            (data / 'touch.json').write_text(json.dumps(
                {'device': ['fake', [0, 320], [0, 240]], 'matrix': [[1, 0], [0, 1], [0, 0]]}))
            (data / 'options.json').write_text(json.dumps({'rotation': 0}))
            pages = []
            real_render = app.render
            def spy(page, *a, **k):
                if not pages or pages[-1] != page:
                    pages.append(page)
                return real_render(page, *a, **k)
            real_menu = app.GamesMenu.render

            def spy_menu(menu, *a):
                if not pages or pages[-1] != 'games':
                    pages.append('games')
                return real_menu(menu, *a)
            with mock.patch.object(app, 'Framebuffer', t.FakeScreen), \
                    mock.patch.object(app, 'Touch', t.FakeTouch), \
                    mock.patch.object(app, 'Camera', t.FakeCamera), \
                    mock.patch.object(app, 'render', spy), \
                    mock.patch.object(app.GamesMenu, 'render',
                                      lambda self, *a: spy_menu(self, *a)), \
                    mock.patch.object(app, 'boot_service_enabled', return_value=False), \
                    mock.patch.object(app, 'command_output', return_value=''), \
                    mock.patch.object(sys, 'argv', ['panel_app.py', '--data', str(data)]):
                try:
                    app.main()
                finally:
                    app.set_rotation(0)
        self.assertEqual(pages[0], 'home')
        self.assertIn('camera', pages)
        self.assertEqual(pages[:4], ['home', 'games', 'home', 'camera'])  # BACK returns Home.


if __name__ == '__main__':
    unittest.main()
