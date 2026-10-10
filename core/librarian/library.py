"""Incremental on-disk FTS5 index of local UTF-8 documents."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3
from urllib.parse import quote

from .extract import paragraphs
from .query import keywords
from .rank import Passage, chunks

EXTENSIONS = {'.txt', '.md', '.html', '.htm'}
SCHEMA = '''
CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, mtime_ns INTEGER, size INTEGER);
CREATE VIRTUAL TABLE IF NOT EXISTS passages USING fts5(text, title, path UNINDEXED);
'''


class LibraryError(RuntimeError):
    pass


class LocalLibrary:
    def __init__(self, root=None, db_path=None, max_bytes=2_000_000):
        self.root = Path(root or '~/cyberdeck-data/library').expanduser().resolve()
        self.db_path = Path(db_path).expanduser().resolve() if db_path else self.root.parent / 'library.sqlite3'
        self.max_bytes = max_bytes

    def document_path(self, relative):
        rel = Path(relative)
        if rel.is_absolute() or '..' in rel.parts:
            raise LibraryError('Document is outside the library')
        path = self.root / rel
        if path.suffix.lower() not in EXTENSIONS:
            raise LibraryError('Unsupported document type')
        if any(part.is_symlink() for part in [path, *path.parents]):
            raise LibraryError('Library document links are not followed')
        if not path.resolve().is_relative_to(self.root):
            raise LibraryError('Document is outside the library')
        return path

    def read_document(self, relative):
        path = self.document_path(relative)
        try:
            with path.open('rb') as source:
                data = source.read(self.max_bytes + 1)
            if len(data) > self.max_bytes:
                raise LibraryError('Document exceeds the two-megabyte limit')
            return data.decode('utf-8-sig')
        except (OSError, UnicodeError) as exc:
            raise LibraryError('Document is unavailable or is not UTF-8 text') from exc

    def reindex(self):
        """One transaction; unchanged mtimes/sizes incur no document reads or writes."""
        changed, removed, skipped = 0, 0, []
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with closing(sqlite3.connect(self.db_path, timeout=2)) as db, db:
                db.executescript(SCHEMA)
                old = {path: (mtime, size) for path, mtime, size in db.execute('SELECT * FROM files')}
                seen = set()

                def fail(exc):
                    raise exc

                for folder, directories, filenames in os.walk(self.root, followlinks=False, onerror=fail):
                    directories[:] = sorted(d for d in directories if not d.startswith('.')
                                            and not (Path(folder) / d).is_symlink())
                    for name in sorted(filenames):
                        path = Path(folder) / name
                        if name.startswith('.') or path.suffix.lower() not in EXTENSIONS:
                            continue
                        relative = path.relative_to(self.root).as_posix()
                        try:
                            path = self.document_path(relative)
                            info = path.stat()
                            signature = (info.st_mtime_ns, info.st_size)
                            if info.st_size > self.max_bytes:
                                raise LibraryError('Document exceeds the two-megabyte limit')
                            if old.get(relative) == signature:
                                seen.add(relative)
                                continue
                            text = self.read_document(relative)
                            if path.stat().st_mtime_ns != info.st_mtime_ns:
                                raise LibraryError('Document changed during indexing; retry')
                        except (LibraryError, OSError) as exc:
                            skipped.append({'path': relative, 'error': str(exc)})
                            continue
                        raw = paragraphs(text) if path.suffix.lower() in {'.html', '.htm'} else re.split(r'\n\s*\n', text)
                        title = path.stem
                        db.execute('DELETE FROM passages WHERE path=?', (relative,))
                        count = 0
                        for paragraph in raw:
                            for part in chunks(paragraph):
                                db.execute('INSERT INTO passages(text,title,path) VALUES(?,?,?)',
                                           (part, title, relative))
                                count += 1
                                if count >= 1000:
                                    break
                            if count >= 1000:
                                skipped.append({'path': relative, 'error': 'Indexed first 1000 passages only'})
                                break
                        db.execute('INSERT OR REPLACE INTO files VALUES(?,?,?)', (relative, *signature))
                        seen.add(relative)
                        changed += 1
                for relative in old.keys() - seen:
                    db.execute('DELETE FROM passages WHERE path=?', (relative,))
                    db.execute('DELETE FROM files WHERE path=?', (relative,))
                    removed += 1
        except (OSError, sqlite3.Error) as exc:
            raise LibraryError('Library reindex failed; check folder access and SQLite FTS5') from exc
        return {'changed': changed, 'removed': removed, 'skipped': skipped}

    def search(self, question, limit=80):
        terms = keywords(question)
        if not terms or limit <= 0 or not self.db_path.exists():
            return []
        match = ' OR '.join('"' + term.replace('"', '""') + '"' for term in terms)
        try:
            uri = self.db_path.resolve().as_uri() + '?mode=ro'
            with closing(sqlite3.connect(uri, uri=True, timeout=2)) as db:
                rows = db.execute('SELECT text,title,path FROM passages WHERE passages MATCH ? '
                                  'ORDER BY bm25(passages),rowid LIMIT ?', (match, min(limit, 200))).fetchall()
            result = []
            for text, title, relative in rows:
                try:
                    if not self.document_path(relative).is_file():
                        continue
                except LibraryError:
                    continue
                result.append(Passage(text, title, '/api/library/' + quote(relative, safe='/'), 'local'))
            return result
        except sqlite3.Error as exc:
            raise LibraryError('Library index is unavailable; run the reindex command') from exc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default=os.environ.get('CYBERDECK_LIBRARY_DIR'))
    parser.add_argument('--db', default=os.environ.get('CYBERDECK_LIBRARY_DB'))
    args = parser.parse_args()
    try:
        print(json.dumps(LocalLibrary(args.root, args.db).reindex(), indent=2))
    except LibraryError as exc:
        parser.exit(1, str(exc) + '\n')


if __name__ == '__main__':
    main()
