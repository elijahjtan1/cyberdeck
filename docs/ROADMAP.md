# Cyberdeck software roadmap

Goal: make the cyberdeck a complete offline computer while it is still on the desk, using
hardware already on hand. Hardware that hasn't arrived (GPS, battery, larger screen and
keyboard) is simulated or deferred, and clearly labelled when simulated.

How to use this file (see [AGENTS.md](../AGENTS.md)):

- **Owner:** `Claude`, `Codex` or `Eli`; `-` means unclaimed. Claim an item in your branch's
  first commit before starting it.
- **Status:** `todo`, `in progress`, `review` (pull request open), `done`, `blocked` (say on what).
- Keep one roadmap item per pull request where possible, and link the pull request in Notes.

## Already in place

| Area | What exists |
| --- | --- |
| Touch panel | Home page, rotation, options, boot service, safe shutdown (`panel_app.py`) |
| Status | CPU, RAM, temperature, storage, uptime, network; health banner decoding `vcgencmd get_throttled` |
| Camera | Live preview, full-resolution stills, self-timer, timelapse, photo review and zoom |
| Power | PWM backlight with real dimming; the Black saver turns the backlight off |
| Extras | Audio controls and visualizer, photo album, GIF player, nine games, screensavers |
| GNSS | Placeholder page that deliberately shows no position |
| Repository | GitHub repo, hardware-free test suite, preview renderer |

## Architecture

```
Browser UI (Chromium on the Pi, or a PC on the network)      2.8" touch panel (panel_app.py)
                         \                                       /
                          \---- cyberdeck-core (Flask, localhost) ----/
                                 |-- librarian: Kiwix search + local documents -> Ollama
                                 |-- telemetry: sampling, history log, activity markers
                                 |-- gps: serial / gpsd / NMEA replay (simulated)
                                 |-- esp32: JSON-lines serial protocol (mock until hardware)
                                 `-- camera: later, single owner of the camera
```

- `cyberdeck-core` is a new `core/` package in this repo, run as its own systemd service.
  Each module has a real implementation and a fake for tests.
- The panel stays a separate process. It reads from the backend when it is running and
  falls back to its own readings when it isn't.
- **The camera can only be opened by one process.** The panel owns it today. Live camera
  in the browser waits until camera ownership moves into the backend (item 4.4).

## Phase 0: baseline from the Pi

| # | Item | Owner | Status | Notes |
| --- | --- | --- | --- | --- |
| 0.1 | Record system facts: `uname -m`, `free -h`, `df -h`, `vcgencmd measure_temp`, `vcgencmd get_throttled`, `lsblk`, `kiwix-serve --version`, ZIM file names and port, `ollama --version`, `ollama list` | Eli | in progress | Kiwix, memory and Ollama recorded in `docs/PI-BASELINE.md`; `uname -m`, `df -h`, `vcgencmd`, `lsblk` still to paste |
| 0.2 | Photo or video of the camera tearing artifact on the TFT | Eli | todo | Needed before 4.3 |
| 0.3 | Decide on `sudo apt install python3-flask` | Eli | done | Approved |

## Phase 1: telemetry and backend skeleton

| # | Item | Owner | Status | Notes |
| --- | --- | --- | --- | --- |
| 1.1 | Move status reading and `get_throttled` decoding out of `panel_app.py` into a shared module | Claude | done | `core/sysinfo.py`, used by the panel. Merged in #1 |
| 1.2 | `core/` package and Flask app on localhost, with a systemd installer like `setup_panel_service.py` | Claude | done | `python3 -m core`, `setup_core_service.py`; see [CORE.md](CORE.md). Merged in #1 |
| 1.3 | Telemetry logger: sample every 5 s, write to SQLite once a minute, keep 7 days | Claude | done | `core/telemetry.py`; `~/cyberdeck-data/telemetry.sqlite3`. Merged in #1 |
| 1.4 | Activity markers: record Kiwix searches, AI answers and camera captures in the log | Claude | done | Photos marked now; `telemetry.activity()` and `/api/activity/*` ready for Phases 2-3. Merged in #1 |
| 1.5 | Panel: history chart on STATUS -> DETAILS and an "AI busy" indicator, read from the backend | Claude | done | STATUS -> HISTORY page (1H-7D) and AI/SEARCH header badge. Merged in #1 |

Note: firmware flags say whether undervoltage happened, not the voltage. If the log ties
undervoltage to load, the fix is the power supply (official 5.1 V 3 A, short cable), not code.

## Phase 2: offline search (librarian, part 1)

| # | Item | Owner | Status | Notes |
| --- | --- | --- | --- | --- |
| 2.1 | Kiwix client: full-text search through kiwix-serve, returning titles and offline URLs | Claude | done | Merged in [#5](https://github.com/elijahjtan1/cyberdeck/pull/5); recorded Pi 3.7.0/14.0.0 XML + HTML pass; Restarters archive checked on the Pi with `deck bench`; a Wikipedia archive is still needed for the sonobuoy check |
| 2.2 | Fetch an article and turn its HTML into clean paragraphs | Claude | done | Merged in [#5](https://github.com/elijahjtan1/cyberdeck/pull/5); parser and failure tests |
| 2.3 | Local documents: `~/cyberdeck-data/library/` (txt, md, html; pdf later) indexed with SQLite FTS5 | Claude | done | Merged in [#7](https://github.com/elijahjtan1/cyberdeck/pull/7), with `/api/search` and `/api/retrieve`. No vector database |
| 2.4 | Question to keywords without the model (drop stopwords, keep names and terms) | Claude | done | Merged in [#3](https://github.com/elijahjtan1/cyberdeck/pull/3); no model calls |
| 2.5 | Passage ranking: BM25 over paragraphs, keep the best ~1,200-1,500 tokens | Claude | done | Merged in [#3](https://github.com/elijahjtan1/cyberdeck/pull/3); budget and empty-result tests. Recall fixes in [#14](https://github.com/elijahjtan1/cyberdeck/pull/14)-[#16](https://github.com/elijahjtan1/cyberdeck/pull/16) and [#19](https://github.com/elijahjtan1/cyberdeck/pull/19); the model now reads 700 tokens by default (3.6) |
| 2.6 | Backend service passes Kiwix settings to the librarian (`setup_core_service.py --kiwix-*`) | Claude | done | Merged in [#8](https://github.com/elijahjtan1/cyberdeck/pull/8) |

## Phase 3: AI answers (librarian, part 2)

| # | Item | Owner | Status | Notes |
| --- | --- | --- | --- | --- |
| 3.1 | Ollama client for `/api/chat`: streaming, thinking off, model and context size configurable | Claude | done | `core/librarian/ollama.py`; merged in [#9](https://github.com/elijahjtan1/cyberdeck/pull/9). First word in about 17.6 s on the Pi |
| 3.2 | Grounded prompt with numbered passages; "not found in offline sources" without calling the model when nothing scores well | Claude | done | `core/librarian/answer.py`; merged in [#10](https://github.com/elijahjtan1/cyberdeck/pull/10) |
| 3.3 | Citation check: drop citations that match no passage; flag answers with none | Claude | done | `check_citations` in `core/librarian/answer.py`; merged in [#11](https://github.com/elijahjtan1/cyberdeck/pull/11), checked on the Pi with Restarters |
| 3.4 | `deck ask "..."` command and `/api/ask` endpoint | Claude | done | `deck`, `core/cli.py`, `/api/ask` (JSON or streamed); merged in [#12](https://github.com/elijahjtan1/cyberdeck/pull/12), checked on the Pi |
| 3.5 | Benchmark: ~20 questions with expected articles; measure top-3 hit rate, time to first word, total time, peak memory | Claude | done | `deck bench` merged in [#13](https://github.com/elijahjtan1/cyberdeck/pull/13); recall fixes in [#14](https://github.com/elijahjtan1/cyberdeck/pull/14)-[#16](https://github.com/elijahjtan1/cyberdeck/pull/16) (6/16 to 12/16 top-3); expected articles match whole words since [#29](https://github.com/elijahjtan1/cyberdeck/pull/29). Recall fixes (short paragraphs joined, titles weighted, hub pages skipped; three expectations widened) and own-words not-found detection merged in [#42](https://github.com/elijahjtan1/cyberdeck/pull/42), which carried [#41](https://github.com/elijahjtan1/cyberdeck/pull/41); a citation standing in for the value counts as not found since [#45](https://github.com/elijahjtan1/cyberdeck/pull/45); sources labelled `Source 1:` and answers mostly copied from them marked QUOTED since [#48](https://github.com/elijahjtan1/cyberdeck/pull/48); Pi bench after them still to run. Baseline on the Pi with Restarters and `qwen3:0.6b`, see [Benchmark baseline](LIBRARIAN.md#baseline) |
| 3.6 | Faster first word: make the passage budget configurable (CYBERDECK_LIBRARIAN_BUDGET, deck bench --budget), measure on the Pi, then pick the default | Claude | done | Setting in [#18](https://github.com/elijahjtan1/cyberdeck/pull/18); default 700 in [#23](https://github.com/elijahjtan1/cyberdeck/pull/23), then 400 with one source per article in [#28](https://github.com/elijahjtan1/cyberdeck/pull/28). On the Pi, first word 28 s median at 400, vs 45 s at 700 and 69 s at 1,400. Eli's run after #28: first word 22.5 s and total 35 s median, 75% top-3 hits |

## Phase 4: interfaces and camera

| # | Item | Owner | Status | Notes |
| --- | --- | --- | --- | --- |
| 4.1 | Browser home page: Ask, Wikipedia, Files, Photos, Status, Maps | - | todo | All assets vendored; works offline |
| 4.2 | Camera: video recording (H.264 to .mp4) with a length cap and low-storage stop; optional timestamp on stills | - | todo | RECORD on the panel |
| 4.3 | Investigate tearing: compare partial vs whole-frame framebuffer writes | - | blocked | Waiting on 0.2 |
| 4.4 | Camera service in the backend as the single owner; panel and browser as clients | - | todo | Only when the browser needs live video |

## Phase 5: simulated hardware

| # | Item | Owner | Status | Notes |
| --- | --- | --- | --- | --- |
| 5.1 | GPS: one NMEA parser with serial, gpsd and log-replay sources; replay labelled SIMULATED everywhere | Claude | done | Main Programmer 2: `core/gps.py` and `/api/gps` merged in [#33](https://github.com/elijahjtan1/cyberdeck/pull/33). Replaces the GNSS placeholder only when a source is configured |
| 5.2 | Offline maps: a region extract in a local map file shown with a vendored map library | - | todo | Don't bulk-download openstreetmap.org tiles |
| 5.3 | Waypoints in SQLite, with distance and bearing to a chosen one | - | todo | No routing yet |
| 5.4 | ESP32 protocol spec: JSON lines over USB serial with `hello`, `telemetry`, `button`, `heartbeat`, `shutdown_request`/`shutdown_ack` and a version field | Claude | done | Main Programmer 2: `docs/ESP32-PROTOCOL.md` merged in [#39](https://github.com/elijahjtan1/cyberdeck/pull/39). The critical-battery hard cut awaits Eli's decision |
| 5.5 | Pi side of the protocol, plus a mock ESP32 on a virtual serial port sending simulated battery data | Claude | todo | Main Programmer 2, after 5.4. Power is cut only as the spec's Shutdown section allows (ack plus stopped heartbeat, halt signal, 10 s button hold, or the critical-battery cut if approved) |

## Phase 6: new hardware for resilience

Hardware Eli has approved adding. Each module gets a real driver and a fake for tests, in
`core/`, and shows nothing (not a made-up value) until the hardware is detected.

| # | Item | Owner | Status | Notes |
| --- | --- | --- | --- | --- |
| 6.1 | Real-time clock: DS3231 on I2C, kept in sync from NTP (and GPS once 5.1 lands); STATUS shows the clock source (network, RTC, GPS or unsynced) | - | todo | About $5. Needs `dtoverlay=i2c-rtc,ds3231` in `/boot/firmware/config.txt`, so Eli applies that step |
| 6.2 | Content pack on a USB 3 SSD: mount at a fixed path, move ZIMs and `~/cyberdeck-data/library/` there, point kiwix-serve and the librarian at it; candidates WikiMed, iFixit, Wikipedia nopic, Wikivoyage, Wiktionary, Stack Exchange (DIY, gardening, outdoors, amateur radio, electronics), Gutenberg, Appropedia | Claude | in progress | Content Curator: content tool (`tools/content.py`) merged in [#31](https://github.com/elijahjtan1/cyberdeck/pull/31); tiers and SSD size await Eli. Mount and kiwix-serve switch still to do. Eli picks the SSD and the ZIM list; the librarian benchmark (3.5) gets questions for each new source. Panel falls back cleanly when the SSD is unplugged. Medical answers (WikiMed) show the passages first with a "verify before acting" warning |
| 6.3 | Voltage and current sensor: INA219 or INA226 on the 5 V rail, sampled by telemetry, shown on STATUS and HISTORY next to the undervoltage flag | - | todo | About $5. Gives real volts and amps; groundwork for battery estimates and power profiles (7.6) |
| 6.4 | Hardware map: pins, I2C addresses, UARTs, PWM and USB for the incoming radar, GNSS, SDR and screens, with a clash checker | Claude | done | Hardware Integrator: `docs/HARDWARE-MAP.md` and `python3 -m core.hardware`, merged in [#34](https://github.com/elijahjtan1/cyberdeck/pull/34) |
| 6.5 | Radar: LD2450 driver with serial, record and replay; replay labelled SIMULATED | Claude | done | Hardware Integrator: `core/hardware/ld2450.py` merged in [#36](https://github.com/elijahjtan1/cyberdeck/pull/36). Read-only, off unless a source is given |

## Phase 7: software for an offline world

Software-only features that need no new hardware. Unclaimed; Eli chooses which to start.

| # | Item | Owner | Status | Notes |
| --- | --- | --- | --- | --- |
| 7.1 | Library manifest: sha256 of every ZIM and library file, checked weekly at low priority, result on STATUS | - | todo | Detects silent SD or SSD corruption |
| 7.2 | Quick cards: short plain-text sheets (first aid, water purification, fire, knots, radio frequencies) readable on the panel with no model | - | todo | Sources must be cited on each card; content reviewed by Eli |
| 7.3 | Panel ASK page: on-screen keyboard that calls `/api/ask` and streams the answer with citations | Claude | done | Main Programmer 1: `panel_tools/ask.py`, merged in [#40](https://github.com/elijahjtan1/cyberdeck/pull/40). The prompt now asks for answers in the model's own words; Pi bench with it still to run. Shows "backend not running" instead of failing. Preset questions or a full-screen answer view, since the keyboard leaves little room on 320x240. First word takes about a minute on the Pi, so show progress and the passages event from `/api/ask?stream=1` while waiting |
| 7.4 | Deck hotspot: the Pi as a Wi-Fi access point so phones nearby reach Kiwix, the librarian and the browser UI (4.1) | - | todo | Changes network config; Eli approves before it is installed |
| 7.5 | Hardware watchdog for the panel and core services | - | todo | `bcm2835_wdt`; systemd `WatchdogSec`, which needs the panel and core to send `sd_notify` keep-alives |
| 7.6 | Survival power profile: Wi-Fi, Bluetooth and HDMI off, powersave governor, Ollama unloaded when idle, backlight low; one switch on the panel | - | todo | Measure the saving with 6.3 |
| 7.7 | Read-only root with overlayfs; user data on its own writable partition | - | todo | Survives sudden power loss. Changes boot config; Eli applies it |
| 7.8 | Backup to a USB stick or second SD from the panel, with verify | - | todo | |
| 7.9 | Self-rebuild kit: cached apt packages, a repo bundle and a restore guide on the SSD | - | todo | After 6.2 |
| 7.10 | Night mode: red-on-black palette on every page | - | todo | Shared colours are named since [#44](https://github.com/elijahjtan1/cyberdeck/pull/44), which makes the swap smaller. Night previews for a few key pages only |
| 7.11 | Sun and moon page: sunrise, sunset, twilight and moon phase computed offline | Claude | done | Merged in [#26](https://github.com/elijahjtan1/cyberdeck/pull/26). Uses GPS when present, otherwise a saved location; warns when the clock isn't synced |
| 7.12 | SOS and signal page: Morse beacon on the backlight, strobe, Morse trainer | Claude | done | Merged in [#38](https://github.com/elijahjtan1/cyberdeck/pull/38): slow SOS (at most 2 flashes per second) plus a torch screen; Morse trainer left for later. Backlight beacon needs the PWM backlight ([BACKLIGHT-PWM.md](BACKLIGHT-PWM.md)); otherwise it flashes the whole screen white and black |
| 7.13 | Field journal: timestamped notes with optional photo and position | - | todo | |
| 7.14 | Supplies tracker: food, water and medicine with quantities, expiry dates and days remaining | - | todo | |
| 7.15 | Survival calculators: water disinfection doses, calorie and water needs, unit conversions | - | todo | Formulas cited from the offline sources |
| 7.16 | OCR from the camera (`tesseract-ocr` from apt): photograph a label or page, then search or save the text | - | todo | Releases the camera when done |
| 7.17 | Offline dictionaries and phrasebooks from FreeDict (`dict-freedict-*` from apt) | - | todo | |
| 7.18 | Camera motion watch: low-rate frame differencing that saves snapshots | - | todo | Low CPU; holds the camera only while armed |
| 7.19 | Offline place-name search from a GeoNames extract, for maps and waypoints (5.2, 5.3) | - | todo | |
| 7.20 | Survival dashboard, a page of its own opened from HOME (HOME is already full): time and clock source, power, last journal entry | - | todo | After 6.1, 6.3 and 7.13. Shows only fields with a real source; weather or messages need new hardware first |

## Deferred until hardware is chosen

- Battery-specific software and charge estimates (needs a real sensor)
- ESP32 firmware and PCB-specific code
- RF features
- Elaborate desktop customisation for the larger screen
