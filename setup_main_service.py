#!/usr/bin/env python3
"""Start the main app on the 5" screen at boot, instead of the desktop.

Install: sudo python3 setup_main_service.py --console-boot   (from the next boot)
         sudo python3 setup_main_service.py --now            (also start it now)
Undo:    sudo python3 setup_main_service.py --undo

Installs one systemd unit, cyberdeck-main.service, that runs main_app.py from this folder as
root (it needs the framebuffer and the touch and keyboard grabs), and disables the quick
panel's boot service so only one of them starts. The quick panel still opens from the main
app (Q) and hands the screen back, inside the same service.

The unit conflicts with the desktop (lightdm): `sudo systemctl start lightdm` stops the main
app and starts the desktop (for SDR++), and `sudo systemctl start cyberdeck-main` goes back.
--console-boot also makes the Pi boot to the console (multi-user.target) instead of the
desktop; without it the desktop still starts at boot and wins the screen.

CLOSE exits cleanly and is not restarted; crashes restart after 5 s.
"""
import argparse
import os
import subprocess
from pathlib import Path

from setup_panel_service import NAME as PANEL_NAME, default_data

NAME = 'cyberdeck-main.service'
MARK = '# Managed by setup_main_service.py'
DESKTOP = 'lightdm.service'


def unit_path(root=Path('/'), name=NAME):
    return root / 'etc/systemd/system' / name


def unit_text(app, uid, gid, data=None):
    app = Path(app)
    data = Path(data) if data else default_data(uid)
    for path in (app, data):
        if any(c.isspace() for c in str(path)) or any(c in str(path) for c in '%"'):
            raise RuntimeError(f'Use an app/data path without spaces, quotes or %: {path}')
    return f'''{MARK}
[Unit]
Description=Cyberdeck main app on the 5-inch screen
After=local-fs.target systemd-udev-trigger.service cyberdeck-core.service
Wants=cyberdeck-core.service
# One owner for the screen, touch and keys: starting either side stops the other.
Conflicts={DESKTOP} {PANEL_NAME}
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


def install(app, uid, gid, root=Path('/'), run=None, start=False, data=None, console=False):
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
    if unit_path(root, PANEL_NAME).exists():
        run(['systemctl', 'disable', PANEL_NAME])  # Kept installed; only one starts at boot.
    run(['systemctl', 'enable', NAME])
    if console:
        run(['systemctl', 'set-default', 'multi-user.target'])
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
    parser.add_argument('--console-boot', action='store_true',
                        help='boot to the console instead of the desktop (multi-user.target)')
    parser.add_argument('--data', help='explicit photo/settings folder (default: user home/cyberdeck-data)')
    args = parser.parse_args()
    if not hasattr(os, 'geteuid') or os.geteuid() != 0:
        raise RuntimeError('Run this on the Pi using sudo.')
    if args.undo:
        undo()
        print('Main app boot service removed.')
        print('To boot to the desktop again: sudo systemctl set-default graphical.target')
        return
    app = (Path(__file__).resolve().parent / 'main_app.py').resolve()
    if not app.is_file():
        raise RuntimeError(f'{app} not found. Keep this script in the repository folder.')
    owner = app.stat()
    uid = int(os.environ.get('SUDO_UID', owner.st_uid))
    gid = int(os.environ.get('SUDO_GID', owner.st_gid))
    data = Path(args.data).expanduser().resolve() if args.data else default_data(uid)
    install(app, uid, gid, start=args.now, data=data, console=args.console_boot)
    print(f'Installed and enabled {NAME} for {app}.')
    if not args.console_boot:
        print('The desktop still starts at boot and covers the main app; add --console-boot to change that.')
    print('Desktop for SDR++: sudo systemctl start lightdm (stops the main app).')
    print('Back to the main app: sudo systemctl start cyberdeck-main (stops the desktop).')
    print('Logs: journalctl -u cyberdeck-main -f')


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f'Service setup stopped: {exc}')
