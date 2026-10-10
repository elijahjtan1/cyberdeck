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

The recorded XML verifies the installed search format; other compatibility fixtures are
synthetic and real article HTML is still pending. With a Wikipedia ZIM installed, search
for `passive sonobuoy`, verify that the
result URL opens offline, then check that fetched paragraphs describe hydrophones and
radio transmission. Stop kiwix-serve and confirm a clear error instead of a hang.

Unusual HTML may need additional cleanup rules. Table-only facts and reference sections
are intentionally excluded. UTF-8 articles are assumed. A socket timeout bounds stalled
I/O, not a hostile server that continuously trickles bytes; the trusted loopback service
is the only allowed HTTP destination.
