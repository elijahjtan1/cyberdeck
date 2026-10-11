# Offline librarian

Phase 2 retrieves text without calling a model. Its modules use the standard library;
the backend uses the already-approved Flask package. It never falls back to the internet.

## Kiwix setup

Eli reports kiwix-tools 3.7.0 / libkiwix 14.0.0; see [PI-BASELINE.md](PI-BASELINE.md).
Its real recorded XML confirms `books.name`, RSS output, port 8080 and book key
`restarters_en_all_maxi_2026-04` for the foreground test. No Wikipedia archive is
confirmed yet. Use these settings for Restarters; change the book key when adding Wikipedia.

Start the confirmed archive in a terminal on the Pi (leave it open):

```bash
kiwix-serve --address=127.0.0.1 --port=8080 /home/eli/kiwix/zims/restarters_en_all_maxi_2026-04.zim
```

This is a foreground process, not a boot service. If port 8080 is already occupied,
use the existing Kiwix instance or choose a free port and update the client base URL.

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

Recorded XML and article HTML verify the installed search format and extraction; other
compatibility fixtures are synthetic.

To check the running service live, start kiwix-serve as above, install the service with
the `--kiwix-*` options from **Backend configuration**, then:

```bash
curl 'http://127.0.0.1:8765/api/search?q=repair'
```

Results with `"source": "kiwix"` and `/content/restarters_en_all_maxi_2026-04/...` URLs mean
the service reached Kiwix. With a Wikipedia ZIM installed (and `--kiwix-book` set to its
key), search for `passive sonobuoy`, verify that the result URL opens offline, then check that fetched paragraphs describe hydrophones and
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

With Kiwix running as above, start the backend in a second terminal from the repository:

```bash
export CYBERDECK_KIWIX_URL=http://127.0.0.1:8080
export CYBERDECK_KIWIX_BOOK=restarters_en_all_maxi_2026-04
python3 -m core
```

Exports in your terminal do not reach the `cyberdeck-core` boot service. Give the service
the Kiwix settings through its installer instead, which writes them into the unit and
restarts it:

```bash
sudo python3 setup_core_service.py --now \
  --kiwix-url http://127.0.0.1:8080 --kiwix-book restarters_en_all_maxi_2026-04
```

The installer refuses a non-loopback URL or values with spaces or quotes. Re-running it
without the `--kiwix-*` options (for example after `git pull`) keeps the saved settings;
`--no-kiwix` removes them. Add `--kiwix-book-parameter content` only for a legacy Kiwix.
Check what the service has with `systemctl show cyberdeck-core -p Environment`.

The backend reads these environment variables (the installer sets the Kiwix ones):

| Variable | Meaning |
| --- | --- |
| `CYBERDECK_KIWIX_URL` | Actual loopback HTTP base URL, including port; unset disables Kiwix |
| `CYBERDECK_KIWIX_BOOK` | Book key from a working Kiwix article URL; empty searches all available books |
| `CYBERDECK_KIWIX_BOOK_PARAMETER` | `books.name` for installed libkiwix 14.0.0; legacy `content` is optional |
| `CYBERDECK_LIBRARY_DIR` | Override document folder (default `~/cyberdeck-data/library`) |
| `CYBERDECK_LIBRARY_DB` | Override index path (default `library.sqlite3` beside the document folder) |

The reindex command uses the same library environment variables, or `--root` and `--db`.
If the backend runs with a custom `--data` directory, set these two library variables
explicitly; the librarian does not infer that argument and the installer does not set them.

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

## Ollama client

`core/librarian/ollama.py` talks to the local Ollama server's `/api/chat`. It is the
model step for Phase 3; nothing calls it automatically yet. Check it on the Pi from the
repository (Ollama must be running and `qwen3:0.6b` pulled):

```bash
python3 -m core.librarian.ollama "Say hello in five words"
```

The answer streams to the terminal, then a line with the model, token count, time and
stop reason. Without Ollama it prints a readable error instead of a traceback; a missing
model names the `ollama pull` command to run.

```python
from core.librarian.ollama import OllamaClient, OllamaError
client = OllamaClient.from_environment(telemetry)
for piece in client.stream([{'role': 'user', 'content': prompt}]):
    ...                      # or: reply = client.chat(messages, on_text=print)
```

Requests use `stream: true` and `think: false`. Any `<think>...</think>` text an older
Ollama still produces is removed before it is shown, and a separate `thinking` field is
ignored. Every request runs inside `telemetry.activity('ai', detail)` when telemetry is
given, so the panel's AI badge lights while the model works. The URL must be loopback
HTTP, and proxies are disabled. Each socket read may wait up to 120 s (the first word on
the Pi can be slow while the model loads), and a reply is capped at one megabyte.
Failures raise `OllamaError`: server down, timeout, missing model, a dropped or truncated
stream, or an error reported mid-answer.

| Variable | Default | Meaning |
| --- | --- | --- |
| `CYBERDECK_OLLAMA_URL` | `http://127.0.0.1:11434` | Loopback HTTP address of Ollama |
| `CYBERDECK_OLLAMA_MODEL` | `qwen3:0.6b` | Model name from `ollama list` |
| `CYBERDECK_OLLAMA_NUM_CTX` | `2048` | Context size in tokens (256-32768); larger is slower and uses more RAM |
| `CYBERDECK_OLLAMA_NUM_PREDICT` | `400` | Maximum answer length in tokens (1-4096) |

The defaults match the Pi, so the boot service needs no extra settings.
