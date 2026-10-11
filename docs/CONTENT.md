# Offline library content

The librarian and kiwix-serve can only answer from the ZIM files on the deck.
`tools/content.py` plans, downloads and checks those files. It is the only part of the
repository besides the panel updater that uses the internet, and only when you run it.

## Tiers

`tools/content_sources.json` lists every source and the tiers it belongs to. Sizes are
approximate; `plan` prints the real ones.

| Tier | For | Contents | About |
| --- | --- | --- | --- |
| 0 | SD card | Restarters, WikiMed, WikEM, post-disaster, water, food preparation, knots | 3 GB |
| 1 | 128 GB SSD | Tier 0, iFixit, MedlinePlus, Appropedia, Energypedia, Stack Exchange (DIY, gardening, outdoors, amateur radio, electronics, Raspberry Pi), Wikivoyage, Wikibooks, Wikipedia intros only | 33 GB |
| 2 | 256 GB SSD | Tier 1 with full-text Wikipedia (no pictures) instead of intros | 71 GB |
| 3 | 512 GB SSD | Tier 2 with Wikipedia pictures, plus Wiktionary | 140 GB |
| 4 | 1 TB SSD | Tier 3 plus Project Gutenberg | 210 GB |

To change what a tier contains, edit the JSON file. Book names are the file names on
https://download.kiwix.org/zim/ without the date.

## Use

Run as user `eli`, not root, from the repository:

```
cd ~/cyberdeck-repo
python3 tools/content.py plan 2 --dest /mnt/deck-ssd/zims
python3 tools/content.py fetch 2 --dest /mnt/deck-ssd/zims
python3 tools/content.py verify --dest /mnt/deck-ssd/zims
```

- `plan` shows, for each source, whether it is `installed`, `missing` or has a `newer`
  release, with its size and the free space left. It downloads nothing and exits with an
  error if the tier would leave less than 10% of the disk free.
- `fetch` downloads what is missing or newer into `<name>.zim.part`. Run it again after an
  interruption and it resumes. Each file is checked against the size and sha256 Kiwix
  publishes before it is renamed into place and recorded in `manifest.json`. A newer
  release downloads beside the old one; the old file is listed as superseded and is never
  deleted by the script.
- `verify` re-reads every file in `manifest.json` and reports `ok`, `CHANGED` or
  `MISSING`. It only reads, so it is safe to run any time, but hashing a full Wikipedia
  takes a while on the Pi.
- `--only <name>` limits `plan` or `fetch` to one source, for example
  `--only mdwiki_en_all_maxi`.

After a fetch, add new files to kiwix-serve (see [LIBRARIAN.md](LIBRARIAN.md)) and point the
backend at the book you want searched.

Medical sources (WikiMed, WikEM, MedlinePlus) are marked `"medical": true` in the JSON,
so the librarian can show its "verify before acting" warning for them.
