#!/usr/bin/env python3
"""Run cyberdeck-core (the local backend: telemetry log and API) at boot.

Install: sudo python3 setup_core_service.py          (starts at next boot)
         sudo python3 setup_core_service.py --now    (also starts it immediately)
Undo:    sudo python3 setup_core_service.py --undo

Offline search: add --kiwix-url http://127.0.0.1:8080 --kiwix-book <book key> to give the
service the librarian's Kiwix settings. Re-running the installer keeps the saved settings;
--no-kiwix removes them.

Needs Flask: sudo apt install python3-flask
Installs cyberdeck-core.service, which runs `python3 -m core` from this folder as your
normal user (not root), listening on 127.0.0.1 only. Crashes restart after 5 s.
"""
import argparse
import os
import subprocess
from pathlib import Path

from core import DEFAULT_PORT
from core.librarian.kiwix import KiwixClient
from setup_panel_service import default_data

NAME = 'cyberdeck-core.service'
MARK = '# Managed by setup_core_service.py'
# Librarian settings the unit forwards to the backend as environment variables.
KIWIX_KEYS = ('CYBERDECK_KIWIX_URL', 'CYBERDECK_KIWIX_BOOK', 'CYBERDECK_KIWIX_BOOK_PARAMETER')


def unit_path(root=Path('/')):
    return root / 'etc/systemd/system' / NAME


def kiwix_settings(url='', book='', book_parameter=''):
    """Validated environment for the librarian, or {} when Kiwix is off."""
    if not url:
        if book or book_parameter:
            raise RuntimeError('Set --kiwix-url as well as the book settings.')
        return {}
    try:
        KiwixClient(url, book=book, book_parameter=book_parameter or 'books.name')
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    settings = {'CYBERDECK_KIWIX_URL': url, 'CYBERDECK_KIWIX_BOOK': book,
                'CYBERDECK_KIWIX_BOOK_PARAMETER': book_parameter}
    for key, value in settings.items():
        # One unquoted Environment= word each: refuse anything systemd would split or expand.
        if any(c.isspace() or not c.isprintable() for c in value) or any(c in value for c in '%"\'\\$'):
            raise RuntimeError(f'{key} must not contain spaces, quotes, %, $ or backslashes')
    return {k: v for k, v in settings.items() if v}


def saved_settings(path):
    """Kiwix settings in an installed unit, so a re-install without options keeps them."""
    if path.is_symlink() or not path.exists():
        return {}
    settings = {}
    for line in path.read_text().splitlines():
        key, _, value = line.removeprefix('Environment=').partition('=')
        if line.startswith('Environment=') and key in KIWIX_KEYS:
            settings[key] = value
    return settings


def unit_text(repo, user, data, port=DEFAULT_PORT, env=None):
    for path in (repo, data):
        if any(c.isspace() for c in str(path)) or any(c in str(path) for c in '%"'):
            raise RuntimeError(f'Use a path without spaces, quotes or %: {path}')
    extra = ''.join(f'Environment={k}={env[k]}\n' for k in KIWIX_KEYS if (env or {}).get(k))
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
{extra}# Below the panel and Ollama in CPU priority; no privileges needed.
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


def install(repo, user, data, root=Path('/'), run=None, start=False, port=DEFAULT_PORT,
            kiwix=None):
    """kiwix: settings from kiwix_settings(); None keeps whatever the unit already has."""
    run = run or (lambda cmd: subprocess.run(cmd, check=True))
    path = unit_path(root)
    check_ours(path)
    if kiwix is None:
        kiwix = saved_settings(path)
    content = unit_text(repo, user, data, port, kiwix)
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
    parser.add_argument('--kiwix-url', help='kiwix-serve address for offline search, '
                        'e.g. http://127.0.0.1:8080 (loopback only)')
    parser.add_argument('--kiwix-book', default='',
                        help='book key from a working Kiwix article URL (default: all books)')
    parser.add_argument('--kiwix-book-parameter', default='', choices=('books.name', 'content'),
                        help='search parameter for the book (default books.name)')
    parser.add_argument('--no-kiwix', action='store_true', help='remove saved Kiwix settings')
    args = parser.parse_args()
    if args.no_kiwix and (args.kiwix_url or args.kiwix_book or args.kiwix_book_parameter):
        raise RuntimeError('Use either --no-kiwix or the --kiwix-* options, not both.')
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
    kiwix = None  # Keep saved settings unless asked to change them.
    if args.no_kiwix:
        kiwix = {}
    elif args.kiwix_url or args.kiwix_book or args.kiwix_book_parameter:
        kiwix = kiwix_settings(args.kiwix_url or '', args.kiwix_book, args.kiwix_book_parameter)
    path = install(repo, account.pw_name, data, start=args.now, port=args.port, kiwix=kiwix)
    print(f'Installed and enabled {NAME} (runs as {account.pw_name}).')
    saved = saved_settings(path)
    print('Offline search: Kiwix at %s, book %s.' % (saved['CYBERDECK_KIWIX_URL'],
          saved.get('CYBERDECK_KIWIX_BOOK', '(all)')) if saved else
          'Offline search: Kiwix is off. Add --kiwix-url (see docs/LIBRARIAN.md) to turn it on.')
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
