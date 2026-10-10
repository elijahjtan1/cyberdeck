# Offline librarian

Phase 2 retrieves text without calling a model. Its modules use the standard library;
the backend uses the already-approved Flask package. It never falls back to the internet.

## Kiwix setup

Eli reports kiwix-tools 3.7.0 / libkiwix 14.0.0; see [PI-BASELINE.md](PI-BASELINE.md).
That version's source confirms `books.name` and RSS output. The port, book names and
real Pi recordings are still pending. Do not assume those from the test fixtures.

`KiwixClient(base_url, opener=None, book='', book_parameter='books.name', timeout=2.0)`
uses `/search?pattern=...&format=xml&pageLength=...`. Set `book_parameter='content'`
for versions that require the legacy parameter. The base URL must be loopback HTTP
(for example `http://127.0.0.1:8080` if that is your configured port). A URL prefix
is supported. An empty book searches all books supported by that server.

Search results contain `title`, `url`, `snippet`, and `source`. Article URLs come
directly from the RSS response, preserving `/content/<book>/...` or `/<book>/A/...`
paths rather than guessing a layout. Cross-origin links and redirects are refused.
HTTP proxies are disabled. Each HTTP operation has a two-second socket timeout and
a two-megabyte response cap; search returns at most 20 results. A failed server raises
a readable `KiwixError`. Callers should display that error.

`fetch_article(client, url)` returns plain paragraphs using `html.parser`, dropping
navigation, tables, references and other common Wikipedia page furniture. The parser
does not execute scripts or load linked assets.

## Known limits and Pi check

See [LIBRARIAN-RANKING.md](LIBRARIAN-RANKING.md) for keyword rules, BM25 scoring and
the configurable relevance gate.

The compatibility fixtures are synthetic; installed-version validation is pending the
baseline and real recordings. On the Pi, search for `passive sonobuoy`, verify that the
result URL opens offline, then check that fetched paragraphs describe hydrophones and
radio transmission. Stop kiwix-serve and confirm a clear error instead of a hang.

Unusual HTML may need additional cleanup rules. Table-only facts and reference sections
are intentionally excluded. UTF-8 articles are assumed. A socket timeout bounds stalled
I/O, not a hostile server that continuously trickles bytes; the trusted loopback service
is the only allowed HTTP destination.

## Local documents

Create `~/cyberdeck-data/library/` and copy UTF-8 `.txt`, `.md`, `.html` or `.htm` files
there (subfolders are supported). PDF indexing is deferred. Then, from the repository:

```bash
python3 -m core.librarian.library
```

Run that command again after adding, changing or removing files. It compares nanosecond
mtime and size; unchanged documents are not read or rewritten. Deleted, unreadable,
oversized or unsupported documents are removed from the index. Keep timestamps accurate:
an edit preserving both size and mtime cannot be detected. The command prints changed /
removed counts and skipped-file warnings. HTML uses the same paragraph cleanup as Kiwix;
Markdown is searchable plain text, without rendering.

The SQLite FTS5 database defaults to `~/cyberdeck-data/library.sqlite3`, outside the
document folder. Indexing batches changes in one transaction. Searches use read-only,
short-lived connections, and never reindex automatically. A missing index is an empty
local library. A corrupt or locked index yields a clear error. No vector database is used.

Limits: two megabytes per file and the first 1,000 chunks per document; symlinks and hidden
files/directories are skipped. Local retrieval considers at most 80 matching chunks.
Rerun indexing after removing a large amount of data; SQLite may retain freed pages for
reuse rather than shrink the database. Library files and the index are user data: do not commit them.

## Backend configuration

Set these environment variables for the backend process (or its service environment):

| Variable | Meaning |
| --- | --- |
| `CYBERDECK_KIWIX_URL` | Actual loopback HTTP base URL, including port; unset disables Kiwix |
| `CYBERDECK_KIWIX_BOOK` | Book key from a working Kiwix article URL; empty searches all available books |
| `CYBERDECK_KIWIX_BOOK_PARAMETER` | `books.name` for installed libkiwix 14.0.0; legacy `content` is optional |
| `CYBERDECK_LIBRARY_DIR` | Override document folder (default `~/cyberdeck-data/library`) |
| `CYBERDECK_LIBRARY_DB` | Override index path (default `library.sqlite3` beside the document folder) |

The reindex command uses the same library environment variables, or `--root` and `--db`.
If the backend runs with a custom `--data` directory, set these two library variables
explicitly; the librarian does not infer that argument. Restart the backend after changing
its service environment. No Pi service settings are changed by these PRs.

## API

Both routes accept `q` (1–1,000 characters):

- `GET /api/search?q=passive%20sonobuoy` returns document hits with title, offline URL,
  snippet and source. It uses keywords, Kiwix full-text search and local FTS5.
- `GET /api/retrieve?q=How%20does%20a%20passive%20sonobuoy%20work%3F&budget_tokens=1400`
  returns ranked passages. Budget is optional, 0–10,000. Each result has `text`, `title`,
  `url`, `source` (`kiwix` or `local`) and `score`.

Successful responses contain `query`, `keywords`, `results`, `warnings` and `partial`.
No evidence returns `results: []`. If one source fails but another provides results,
the response includes warnings and `partial: true`. If no results can be returned after
a source failure, HTTP 503 carries `error`; this must not be treated as evidence that
the answer is absent. Invalid input is HTTP 400. Only one API search runs at a time;
another receives a readable 503 rather than increasing Pi memory pressure.

Local URLs use `GET /api/library/<relative-path>` and return the original document as
plain text with browser execution blocked. Paths outside the configured library and
symlinks are rejected. Kiwix URLs retain the server's exact article path and configured
loopback origin. Open links on the Pi, where those services run.

## Phase 3 interface

```python
from core.librarian import retrieve
passages = retrieve(question, budget_tokens=1400, telemetry=telemetry)
```

`retrieve` returns `list[Passage]`. `telemetry` is optional. For explicit configuration
or test injection, use `Retriever(kiwix=client, library=library, telemetry=telemetry)`
from `core.librarian.service`. Set `threshold` on that constructor to tune the gate.
Each retrieval/search is wrapped in `telemetry.activity('search', question)` when supplied.
Leave the nested Kiwix client's telemetry unset when the Retriever owns the activity.

An empty list means Phase 3 should answer "not found in the offline sources" without
calling a model. `RetrievalError` means a source failed; display the error instead.
Use `Retriever.run()` when the caller needs warnings about partial results as well.
Nothing here invokes Ollama. Context token estimates cover passage text only.
