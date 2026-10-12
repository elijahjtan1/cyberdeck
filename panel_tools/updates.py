"""Explicit Git updates. No reset, force, shell interpolation, or automatic pull."""
import json
import ast
import os
from pathlib import Path
import subprocess
from .runtime import atomic_json


class Updates:
    def __init__(self, repo, data, owns=lambda p: None):
        self.repo, self.record, self.owns = Path(repo), Path(data)/'update-rollback.json', owns
        self.running = self.git('rev-parse', '--short', 'HEAD', optional=True) or 'not a checkout'
        self.target = None
        self.branch = None

    def git(self, *args, optional=False):
        command = ['git', '-C', str(self.repo), *args]
        # Git runs as the checkout owner, not root. Preserve SSH credential access.
        if os.name == 'posix' and os.geteuid() == 0 and self.repo.stat().st_uid != 0:
            import pwd
            user = pwd.getpwuid(self.repo.stat().st_uid).pw_name
            command = ['runuser', '-u', user, '--', *command]
        env = dict(os.environ, GIT_TERMINAL_PROMPT='0')
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=40, env=env)
            if result.returncode:
                raise RuntimeError((result.stderr or 'Git operation failed').strip()[-600:])
            return result.stdout.strip()
        except (OSError, subprocess.TimeoutExpired, RuntimeError):
            if optional:
                return ''
            raise

    def clean(self):
        # Untracked files are left out: a fast-forward or switch refuses to overwrite one, so
        # a stray log or download in the checkout is harmless and shouldn't block updates.
        changed = self.git('status', '--porcelain', '--untracked-files=no').splitlines()
        if changed:
            first = changed[0].split(None, 1)[-1]
            more = f' and {len(changed)-1} more' if len(changed) > 1 else ''
            raise RuntimeError(f'Edited on the Pi: {first}{more}. Commit or stash it first.')

    def check(self):
        self.target = None
        self.clean()
        self.branch = self.git('symbolic-ref', '--short', 'HEAD')
        upstream = self.git('rev-parse', '--abbrev-ref', '@{upstream}')
        remote = self.git('config', '--get', f'branch.{self.branch}.remote')
        if remote == '.':
            raise RuntimeError('Choose a GitHub tracking branch in a terminal first')
        self.git('fetch', '--', remote)
        current = self.git('rev-parse', 'HEAD')
        target = self.git('rev-parse', upstream)
        if current == target:
            return 'Already up to date'
        if self.git('merge-base', current, target) != current:
            raise RuntimeError('Branches diverged; resolve in a terminal first')
        self.target = target
        return f'Available: {target[:8]} (tap Update)'

    def apply(self):
        self.clean()
        if not self.target or self.git('symbolic-ref', '--short', 'HEAD') != self.branch:
            raise RuntimeError('Check for updates again first')
        previous = self.git('rev-parse', 'HEAD')
        if self.git('merge-base', previous, self.target) != previous:
            raise RuntimeError('Checkout changed; check again')
        # Validate syntax without executing newly fetched code or writing bytecode.
        for path in self.git('ls-tree', '-r', '--name-only', self.target).splitlines():
            if path.endswith('.py'):
                ast.parse(self.git('show', self.target+':'+path), filename=path)
        atomic_json(self.record, {'previous': previous, 'installed': self.target,
                                 'branch': self.branch}, self.owns)
        self.git('merge', '--ff-only', self.target)
        self.target = None
        return 'Update installed. Restart to load it.'

    def rollback(self):
        self.clean()
        saved = json.loads(self.record.read_text())
        if self.git('rev-parse', 'HEAD') != saved['installed']:
            raise RuntimeError('Checkout changed since update; rollback left untouched')
        # Detached checkout keeps all branch history and all user data intact.
        self.git('switch', '--detach', saved['previous'])
        self.target = None
        return 'Rolled back. Restart; reselect branch in terminal for future updates.'
