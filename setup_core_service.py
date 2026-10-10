#!/usr/bin/env python3
"""Run cyberdeck-core (the local backend: telemetry log and API) at boot.

Install: sudo python3 setup_core_service.py          (starts at next boot)
         sudo python3 setup_core_service.py --now    (also starts it immediately)
Undo:    sudo python3 setup_core_service.py --undo

Needs Flask: sudo apt install python3-flask
Installs cyberdeck-core.service, which runs `python3 -m core` from this folder as your
normal user (not root), listening on 127.0.0.1 only. Crashes restart after 5 s.
"""
import argparse
import os
import subprocess
from pathlib import Path

from core import DEFAULT_PORT
from setup_panel_service import default_data

NAME = 'cyberdeck-core.service'
MARK = '# Managed by setup_core_service.py'


def unit_path(root=Path('/')):
    return root / 'etc/systemd/system' / NAME


def unit_text(repo, user, data, port=DEFAULT_PORT):
    for path in (repo, data):
        if any(c.isspace() for c in str(path)) or any(c in str(path) for c in '%"'):
            raise RuntimeError(f'Use a path without spaces, quotes or %: {path}')
    return f'''{MARK}
[Unit]
Description=Cyberdeck core backend (telemetry log and local API)
After=local-fs.target
StartLimitIntervalSec=300
StartLimitBurst=10

[Service]
Type=simple
User={user}
ExecStart=/usr/bin/python3 -m core --data {data} --port {port}
WorkingDirectory={repo}
Environment=PYTHONUNBUFFERED=1
# Below the panel and Ollama in CPU priority; no privileges needed.
Nice=5
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=full
Restart=on-failure
RestartSec=5
TimeoutStopSec=15

[Install]
WantedBy=multi-user.target
'''


def check_ours(path):
    if path.is_symlink() or (path.exists() and not path.read_text().startswith(MARK)):
        raise RuntimeError(f'{path} was not created by this script; left unchanged.')


def install(repo, user, data, root=Path('/'), run=None, start=False, port=DEFAULT_PORT):
    run = run or (lambda cmd: subprocess.run(cmd, check=True))
    path = unit_path(root)
    check_ours(path)
    content = unit_text(repo, user, data, port)
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


def in_group(account, name):
    import grp
    try:
        group = grp.getgrnam(name)
    except KeyError:
        return False
    return account.pw_name in group.gr_mem or account.pw_gid == group.gr_gid


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--undo', action='store_true', help='disable and remove the service')
    parser.add_argument('--now', action='store_true', help='start (or restart) it immediately')
    parser.add_argument('--data', help='data folder (default: user home/cyberdeck-data)')
    parser.add_argument('--port', type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    if not hasattr(os, 'geteuid') or os.geteuid() != 0:
        raise RuntimeError('Run this on the Pi using sudo.')
    if args.undo:
        undo()
        print('Backend service removed. Run it by hand with: python3 -m core')
        return
    if subprocess.run(['/usr/bin/python3', '-c', 'import flask'], capture_output=True).returncode:
        raise RuntimeError('Flask is missing. Install it first: sudo apt install python3-flask')
    import pwd
    repo = Path(__file__).resolve().parent
    uid = int(os.environ.get('SUDO_UID', repo.stat().st_uid))
    account = pwd.getpwuid(uid)
    if uid == 0:
        raise RuntimeError('Run with sudo from your normal account, not a root shell.')
    data = Path(args.data).expanduser().resolve() if args.data else default_data(uid)
    install(repo, account.pw_name, data, start=args.now, port=args.port)
    print(f'Installed and enabled {NAME} (runs as {account.pw_name}).')
    if not in_group(account, 'video'):
        print(f'Note: {account.pw_name} is not in the video group, so undervoltage flags will '
              f'read Unknown. Fix: sudo usermod -aG video {account.pw_name}')
    print('Started. Check: curl http://127.0.0.1:%d/api/health' % args.port if args.now
          else 'It starts at the next boot, or now with: sudo systemctl start cyberdeck-core')
    print('Logs: journalctl -u cyberdeck-core -f')


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f'Service setup stopped: {exc}')
