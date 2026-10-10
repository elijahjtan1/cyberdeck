# Librarian fixtures

These are **synthetic**, small compatibility samples, not recordings from Eli's Pi.
`kiwix-search.xml` follows the RSS item structure in
https://github.com/kiwix/libkiwix/blob/14.0.0/static/templates/search_result.xml.
The installed libkiwix 14.0.0 tag (commit 20abebd6237fe5879bf79950a0e620edd620f33a)
was inspected: its `test/server_search.cpp` verifies `books.name`, RSS channel items,
inline snippet markup and `/content/<book>/A/...` links (including URL prefixes).
`kiwix-legacy.xml` exercises a legacy article path. `sonobuoy.html` is original test
prose in Wikipedia-like markup. No Wikipedia article has been copied here.

Before declaring item 2.1 validated on the installed version, record the Pi version,
port and book names in `docs/PI-BASELINE.md`, and add a real XML/HTML sample with its
version and provenance. The current samples cannot establish installed-version compatibility.
