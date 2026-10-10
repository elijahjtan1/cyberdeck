#!/usr/bin/env python3
"""Start the panel app automatically at boot, and restart it if it crashes.

Install: sudo python3 setup_panel_service.py          (starts at next boot)
         sudo python3 setup_panel_service.py --now    (also starts it immediately)
Undo:    sudo python3 setup_panel_service.py --undo

Installs one systemd unit, cyberdeck-panel.service, that runs panel_app.py from
this folder as root (it needs the framebuffer, touch grab and camera). CLOSE APP
and Shut down exit cleanly and are not restarted; crashes restart after 5 s.
"""
import argparse
import os
import subprocess
from pathlib import Path

NAME = 'cyberdeck-panel.service'
MARK = '# Managed by setup_panel_service.py'


def unit_path(root=Path('/')):
    return root / 'etc/systemd/system' / NAME


def default_data(uid):
    try:
        import pwd
        home = Path(pwd.getpwuid(uid).pw_dir)
    except (ImportError, KeyError):
        home = Path.home()
    return home / 'cyberdeck-data'


def unit_text(app, uid, gid, data=None):
    app = Path(app)
    data = Path(data) if data else default_data(uid)
    for path in (app, data):
        if any(c.isspace() for c in str(path)) or any(c in str(path) for c in '%"'):
            raise RuntimeError(f'Use an app/data path without spaces, quotes or %: {path}')
    return f'''{MARK}
[Unit]
Description=Cyberdeck TFT panel app (camera, photos, status)
After=local-fs.target systemd-udev-trigger.service
StartLimitIntervalSec=300
StartLimitBurst=10

[Service]
Type=simple
ExecStart=/usr/bin/python3 {app} --data {data}
WorkingDirectory={app.parent}
# Photos and settings stay owned by your normal user, as with sudo.
Environment=SUDO_UID={uid} SUDO_GID={gid} PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=5
TimeoutStopSec=15

[Install]
WantedBy=multi-user.target
'''


def check_ours(path):
    if path.is_symlink() or (path.exists() and not path.read_text().startswith(MARK)):
        raise RuntimeError(f'{path} was not created by this script; left unchanged.')


def install(app, uid, gid, root=Path('/'), run=None, start=False, data=None):
    run = run or (lambda cmd: subprocess.run(cmd, check=True))
    path = unit_path(root)
    check_ours(path)
    content = unit_text(app, uid, gid, data)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(content)
    temporary.chmod(0o644)
    temporary.replace(path)
    run(['systemctl', 'daemon-reload'])
    run(['systemctl', 'enable', NAME])
    if start:
        run(['systemctl', 'restart', NAME])
    return path


def undo(root=Path('/'), run=None):
    run = run or (lambda cmd: subprocess.run(cmd, check=True))
    path = unit_path(root)
    check_ours(path)
    if path.exists():
        run(['systemctl', 'disable', '--now', NAME])
        path.unlink()
        run(['systemctl', 'daemon-reload'])


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--undo', action='store_true', help='disable and remove the service')
    parser.add_argument('--now', action='store_true', help='start (or restart) it immediately')
    parser.add_argument('--app', help='path to panel_app.py (default: beside this script)')
    parser.add_argument('--data', help='explicit photo/settings folder (default: user home/cyberdeck-data)')
    args = parser.parse_args()
    if not hasattr(os, 'geteuid') or os.geteuid() != 0:
        raise RuntimeError('Run this on the Pi using sudo.')
    if args.undo:
        undo()
        print('Boot service removed. Run the app manually with: sudo python3 ~/panel_app.py')
        return
    app = Path(args.app or Path(__file__).resolve().parent / 'panel_app.py').resolve()
    if not app.is_file():
        raise RuntimeError(f'{app} not found. Copy panel_app.py next to this script.')
    owner = app.stat()
    uid = int(os.environ.get('SUDO_UID', owner.st_uid))
    gid = int(os.environ.get('SUDO_GID', owner.st_gid))
    data = Path(args.data).expanduser().resolve() if args.data else default_data(uid)
    # Migrate before recording an explicit path so the first service start does not
    # bypass the app's legacy data migration. The original folder stays untouched.
    if not args.data:
        from panel_tools.runtime import data_home
        data = data_home(None, app, home=data.parent)
        os.chown(data, uid, gid)
    install(app, uid, gid, start=args.now, data=data)
    print(f'Installed and enabled {NAME} for {app}.')
    if args.now:
        print('Started. Status: systemctl status cyberdeck-panel')
    else:
        print('It starts at the next boot. To start now, first stop any copy running in a')
        print('terminal (Ctrl+C), then: sudo systemctl start cyberdeck-panel')
    print('Logs: journalctl -u cyberdeck-panel -f')


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f'Service setup stopped: {exc}')
