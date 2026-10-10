# Librarian fixtures

`kiwix-3.7.0-restarters.xml` is a real response supplied by Eli on 2026-10-10 from
kiwix-tools 3.7.0 / libkiwix 14.0.0 on the Pi, at port 8080, for:
`/search?books.name=restarters_en_all_maxi_2026-04&pattern=repair&format=xml&pageLength=2`.
Chat escaping of angle brackets/underscores and Markdown namespace links was removed;
the search response text, item order and URLs are retained. The shell prompt is excluded.
`kiwix-3.7.0-repair-cafe.html` is Eli's subsequent capture of the first result URL from
the same server/archive. Chat escapes and Markdown links were normalized back to HTML;
content, attributes and relative URLs are retained (blank whitespace lines omitted).
The content is from Restarters Wiki, whose About page specifies CC BY-SA 4.0:
https://wiki.restarters.net/Restart_wiki:About and the named source pages
`Glossary:Repair Café` and `Summary iPhone iPad and iPod repair`.

The other samples are **synthetic**, not recordings from Eli's Pi.
`kiwix-search.xml` follows the RSS item structure in
https://github.com/kiwix/libkiwix/blob/14.0.0/static/templates/search_result.xml.
The installed libkiwix 14.0.0 tag (commit 20abebd6237fe5879bf79950a0e620edd620f33a)
was inspected: its `test/server_search.cpp` verifies `books.name`, RSS channel items,
inline snippet markup and `/content/<book>/A/...` links (including URL prefixes).
`kiwix-legacy.xml` exercises a legacy article path. `sonobuoy.html` is original test
prose in Wikipedia-like markup. No Wikipedia article has been copied here.

The real Wikipedia sonobuoy acceptance check still needs a Wikipedia archive. Restarters
recordings verify installed-version compatibility; the sonobuoy sample tests the pipeline.
