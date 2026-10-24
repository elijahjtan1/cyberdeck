"""setup_main_service.py: the main app's boot unit, without touching the real system."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import setup_main_service as service  # noqa: E402
import setup_panel_service  # noqa: E402


class MainServiceTest(unittest.TestCase):
    def test_install_disables_the_quick_panel_and_undo_removes_only_ours(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            app = root / 'home/eli/cyberdeck-repo/main_app.py'
            app.parent.mkdir(parents=True)
            app.write_text('# app')
            panel = setup_panel_service.install(app.with_name('panel_app.py'), 1000, 1000, root=root,
                                                run=lambda cmd: None)
            calls = []
            unit = service.install(app, 1000, 1000, root=root, run=calls.append)
            text = unit.read_text()
            self.assertIn(f'ExecStart=/usr/bin/python3 {app} --data', text)
            self.assertIn('Conflicts=lightdm.service cyberdeck-panel.service', text)
            self.assertIn('Restart=on-failure', text)
            self.assertIn('WantedBy=multi-user.target', text)
            self.assertIn(['systemctl', 'disable', 'cyberdeck-panel.service'], calls)
            self.assertEqual(calls[-1], ['systemctl', 'enable', 'cyberdeck-main.service'])
            self.assertNotIn(['systemctl', 'set-default', 'multi-user.target'], calls)  # Only when asked.
            self.assertTrue(panel.exists())  # Disabled, not removed.
            calls.clear()
            service.install(app, 1000, 1000, root=root, run=calls.append, console=True, start=True)
            self.assertEqual(calls[-2:], [['systemctl', 'set-default', 'multi-user.target'],
                                          ['systemctl', 'restart', 'cyberdeck-main.service']])
            service.undo(root=root, run=calls.append)
            self.assertFalse(unit.exists())
            self.assertTrue(panel.exists())
            self.assertIn(['systemctl', 'disable', '--now', 'cyberdeck-main.service'], calls)

    def test_refuses_a_foreign_unit_and_odd_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            unit = service.unit_path(root)
            unit.parent.mkdir(parents=True)
            unit.write_text('[Service]\nExecStart=/bin/true\n')
            with self.assertRaises(RuntimeError):
                service.install(root / 'main_app.py', 1000, 1000, root=root, run=lambda c: None)
            with self.assertRaises(RuntimeError):
                service.undo(root=root, run=lambda c: None)
            self.assertIn('/bin/true', unit.read_text())
            with self.assertRaises(RuntimeError):
                service.unit_text(root / 'my app.py', 1000, 1000)


if __name__ == '__main__':
    unittest.main()
