"""Plan, download and verify the deck's offline library (ZIM files from Kiwix).

Run by hand as user eli; it is the only thing besides the panel updater that uses the
network, and only when asked. Nothing is deleted: a newer ZIM downloads beside the old
one, and the old file is listed for the owner to remove after checking the new one.

  python3 tools/content.py plan 2 --dest /mnt/deck-ssd/zims    # sizes and free space only
  python3 tools/content.py fetch 2 --dest /mnt/deck-ssd/zims   # download missing or newer
  python3 tools/content.py verify --dest /mnt/deck-ssd/zims    # re-hash installed files
"""
import argparse
from datetime import datetime, timezone
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import sys
from urllib.request import Request, build_opener
from xml.etree import ElementTree

BASE = 'https://download.kiwix.org/zim/'
SOURCES = Path(__file__).with_name('content_sources.json')
FALLBACK_DIRS = ('other', 'zimit')  # Kiwix moves some books between these folders.
HEADROOM = 0.10  # Share of the disk left free after a fetch, for updates and photos.
CHUNK = 1 << 20
TIMEOUT = 30
METALINK = '{urn:ietf:params:xml:ns:metalink}'


class ContentError(Exception):
    pass


def load_sources(path=SOURCES):
    return json.loads(Path(path).read_text())


def tier_sources(config, tier, only=()):
    picked = [s for s in config['sources'] if tier in s['tiers']]
    if only:
        picked = [s for s in picked if s['prefix'] in only]
        missing = set(only) - {s['prefix'] for s in picked}
        if missing:
            raise ContentError('Not in tier %d: %s' % (tier, ', '.join(sorted(missing))))
    return picked


def human(size):
    for unit in ('B', 'KB', 'MB', 'GB'):
        if size < 1000:
            return '%.0f %s' % (size, unit) if unit == 'B' else '%.1f %s' % (size, unit)
        size /= 1000
    return '%.1f TB' % size


class Kiwix:
    """Reads the download.kiwix.org folder listings and each file's metalink."""

    def __init__(self, opener=None, base=BASE):
        self.opener = opener or build_opener()
        self.base = base
        self.listings = {}

    def open(self, url, headers=None):
        try:
            return self.opener.open(Request(url, headers=headers or {}), timeout=TIMEOUT)
        except OSError as error:
            raise ContentError('Could not reach %s: %s' % (url, error)) from error

    def read(self, url):
        with self.open(url) as response:
            return response.read()

    def listing(self, folder):
        if folder not in self.listings:
            self.listings[folder] = self.read(self.base + folder + '/').decode('utf-8', 'replace')
        return self.listings[folder]

    def latest(self, source):
        """Newest dated file for a prefix: {'file', 'url', 'size', 'sha256'}."""
        pattern = re.compile(r'href="(%s_(\d{4}-\d{2})\.zim)"' % re.escape(source['prefix']))
        for folder in (source['dir'],) + tuple(d for d in FALLBACK_DIRS if d != source['dir']):
            found = pattern.findall(self.listing(folder))
            if found:
                name = max(found, key=lambda item: item[1])[0]
                url = self.base + folder + '/' + name
                size, sha256 = self.metalink(url)
                return {'file': name, 'url': url, 'size': size, 'sha256': sha256}
        raise ContentError('%s not found on %s' % (source['prefix'], self.base))

    def metalink(self, url):
        root = ElementTree.fromstring(self.read(url + '.meta4'))
        size = root.findtext('.//%ssize' % METALINK)
        sha256 = ''
        for item in root.iter(METALINK + 'hash'):
            if item.get('type') == 'sha-256':
                sha256 = (item.text or '').strip().lower()
        if not sha256:  # Older metalinks carry only md5 and sha-1.
            sha256 = self.read(url + '.sha256').decode().split()[0].lower()
        if not size or not re.fullmatch(r'[0-9a-f]{64}', sha256):
            raise ContentError('No size or sha256 published for %s' % url)
        return int(size), sha256


class Manifest:
    """manifest.json beside the ZIMs: file name -> prefix, size, sha256, fetched."""

    def __init__(self, dest):
        self.path = Path(dest) / 'manifest.json'
        self.files = json.loads(self.path.read_text())['files'] if self.path.exists() else {}

    def installed(self, prefix):
        return sorted(name for name, entry in self.files.items() if entry['prefix'] == prefix)

    def add(self, name, prefix, size, sha256):
        self.files[name] = {'prefix': prefix, 'size': size, 'sha256': sha256,
                            'fetched': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps({'files': self.files}, indent=1, sort_keys=True) + '\n')
        os.replace(temporary, self.path)


def plan(kiwix, sources, dest):
    """One row per source: what is installed, what is newest, and what to download."""
    manifest = Manifest(dest)
    rows = []
    for source in sources:
        latest = kiwix.latest(source)
        installed = manifest.installed(source['prefix'])
        if latest['file'] in installed:
            state = 'installed'
        else:
            state = 'newer' if installed else 'missing'
        part = Path(dest) / (latest['file'] + '.part')
        have = part.stat().st_size if state != 'installed' and part.exists() else 0
        rows.append(dict(latest, prefix=source['prefix'], state=state, installed=installed,
                         need=0 if state == 'installed' else max(latest['size'] - have, 0)))
    return rows


def space_problem(rows, dest):
    """A message when the download would leave less than HEADROOM of the disk free."""
    usage = shutil.disk_usage(dest)
    need = sum(row['need'] for row in rows)
    if usage.free - need < usage.total * HEADROOM:
        return 'Needs %s but only %s is free (keeping %d%% of %s spare).' % (
            human(need), human(usage.free), HEADROOM * 100, human(usage.total))
    return ''


def print_plan(rows, dest, out=sys.stdout):
    for row in rows:
        out.write('%-10s %10s  %s\n' % (row['state'], human(row['size']), row['file']))
    usage = shutil.disk_usage(dest)
    out.write('Total %s, to download %s, free %s of %s on %s\n' % (
        human(sum(row['size'] for row in rows)), human(sum(row['need'] for row in rows)),
        human(usage.free), human(usage.total), dest))


def download(kiwix, row, dest, out=sys.stdout):
    """Fetch into <file>.part, resuming if possible; rename only after the hash matches."""
    part = Path(dest) / (row['file'] + '.part')
    digest = hashlib.sha256()
    have = part.stat().st_size if part.exists() else 0
    if have > row['size']:
        part.unlink()
        have = 0
    if have < row['size']:  # A complete .part (interrupted before the rename) only needs checking.
        response = kiwix.open(row['url'], {'Range': 'bytes=%d-' % have} if have else {})
        try:
            with response:
                if have and getattr(response, 'status', 200) != 206:
                    have = 0  # The server ignored the range, so start again.
                with part.open('ab' if have else 'wb') as target:
                    for block in iter(lambda: response.read(CHUNK), b''):
                        target.write(block)
        except (OSError, http.client.HTTPException) as error:
            raise ContentError('%s stopped part way (%s); the partial file was kept, run fetch '
                               'again to resume.' % (row['file'], str(error) or type(error).__name__)) from error
    size = part.stat().st_size
    if size < row['size']:
        raise ContentError('%s ended early at %s of %s; the partial file was kept, run fetch '
                           'again to resume.' % (row['file'], human(size), human(row['size'])))
    with part.open('rb') as written:  # Hash what is on disk, not what was sent.
        for block in iter(lambda: written.read(CHUNK), b''):
            digest.update(block)
    if size != row['size'] or digest.hexdigest() != row['sha256']:
        part.unlink()
        raise ContentError('%s failed its size or sha256 check and was removed; run fetch again.'
                           % row['file'])
    os.replace(part, Path(dest) / row['file'])
    Manifest(dest).add(row['file'], row['prefix'], row['size'], row['sha256'])
    out.write('ok         %10s  %s\n' % (human(row['size']), row['file']))


def fetch(kiwix, sources, dest, out=sys.stdout):
    rows = plan(kiwix, sources, dest)
    print_plan(rows, dest, out)
    problem = space_problem(rows, dest)
    if problem:
        raise ContentError(problem)
    failed = []
    old = []
    for row in rows:
        if row['state'] != 'installed':
            try:
                download(kiwix, row, dest, out)
            except ContentError as error:
                failed.append(str(error))
                out.write('FAILED     %s\n' % error)
                continue
        # Only once the newest file is in place is an older one safe to remove.
        old.extend(name for name in row['installed'] if name != row['file'])
    if old:
        out.write('Superseded (delete after checking the new file in kiwix-serve):\n')
        for name in old:
            out.write('  %s\n' % (Path(dest) / name))
    if failed:
        raise ContentError('%d file(s) failed.' % len(failed))


def verify(dest, out=sys.stdout):
    """Re-hash every manifest entry. Returns the names that are missing or changed."""
    manifest = Manifest(dest)
    if not manifest.path.exists():
        out.write('No manifest.json in %s; nothing fetched there yet.\n' % dest)
        return ['manifest.json']
    bad = []
    for name, entry in sorted(manifest.files.items()):
        path = Path(dest) / name
        if not path.exists():
            state = 'MISSING'
        else:
            digest = hashlib.sha256()
            with path.open('rb') as handle:
                for block in iter(lambda: handle.read(CHUNK), b''):
                    digest.update(block)
            state = 'ok' if digest.hexdigest() == entry['sha256'] else 'CHANGED'
        if state != 'ok':
            bad.append(name)
        out.write('%-10s %s\n' % (state, name))
    return bad


def main(argv=None, opener=None, out=sys.stdout):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('command', choices=('plan', 'fetch', 'verify'))
    parser.add_argument('tier', nargs='?', type=int, help='0-4, see tools/content_sources.json')
    parser.add_argument('--dest', required=True, help='Folder holding the ZIMs')
    parser.add_argument('--only', action='append', default=[], metavar='PREFIX',
                        help='Limit to one source, for example mdwiki_en_all_maxi')
    args = parser.parse_args(argv)
    if not Path(args.dest).is_dir():
        out.write('%s is not a folder. Is the SSD mounted?\n' % args.dest)
        return 2
    try:
        if args.command == 'verify':
            return 1 if verify(args.dest, out) else 0
        if args.tier is None:
            parser.error('plan and fetch need a tier')
        sources = tier_sources(load_sources(), args.tier, args.only)
        kiwix = Kiwix(opener)
        if args.command == 'plan':
            rows = plan(kiwix, sources, args.dest)
            print_plan(rows, args.dest, out)
            problem = space_problem(rows, args.dest)
            if problem:
                out.write(problem + '\n')
                return 1
            return 0
        fetch(kiwix, sources, args.dest, out)
        return 0
    except ContentError as error:
        out.write('%s\n' % error)
        return 1


if __name__ == '__main__':
    sys.exit(main())
