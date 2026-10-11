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
| 2.1 | Kiwix client: full-text search through kiwix-serve, returning titles and offline URLs | Codex | done | Merged in [#5](https://github.com/elijahjtan1/cyberdeck/pull/5); recorded Pi 3.7.0/14.0.0 XML + HTML pass; Wikipedia archive still needed for the sonobuoy check |
| 2.2 | Fetch an article and turn its HTML into clean paragraphs | Codex | done | Merged in [#5](https://github.com/elijahjtan1/cyberdeck/pull/5); parser and failure tests |
| 2.3 | Local documents: `~/cyberdeck-data/library/` (txt, md, html; pdf later) indexed with SQLite FTS5 | Codex | done | Merged in [#7](https://github.com/elijahjtan1/cyberdeck/pull/7), with `/api/search` and `/api/retrieve`. No vector database |
| 2.4 | Question to keywords without the model (drop stopwords, keep names and terms) | Codex | done | Merged in [#3](https://github.com/elijahjtan1/cyberdeck/pull/3); no model calls |
| 2.5 | Passage ranking: BM25 over paragraphs, keep the best ~1,200-1,500 tokens | Codex | done | Merged in [#3](https://github.com/elijahjtan1/cyberdeck/pull/3); budget and empty-result tests |
| 2.6 | Backend service passes Kiwix settings to the librarian (`setup_core_service.py --kiwix-*`) | Claude | done | Merged in [#8](https://github.com/elijahjtan1/cyberdeck/pull/8) |

## Phase 3: AI answers (librarian, part 2)

| # | Item | Owner | Status | Notes |
| --- | --- | --- | --- | --- |
| 3.1 | Ollama client for `/api/chat`: streaming, thinking off, model and context size configurable | Claude | done | `core/librarian/ollama.py`; merged in [#9](https://github.com/elijahjtan1/cyberdeck/pull/9). First word in about 17.6 s on the Pi |
| 3.2 | Grounded prompt with numbered passages; "not found in offline sources" without calling the model when nothing scores well | Claude | done | `core/librarian/answer.py`; merged in [#10](https://github.com/elijahjtan1/cyberdeck/pull/10) |
| 3.3 | Citation check: drop citations that match no passage; flag answers with none | Claude | review | `check_citations` in `core/librarian/answer.py` |
| 3.4 | `deck ask "..."` command and `/api/ask` endpoint | - | todo | |
| 3.5 | Benchmark: ~20 questions with expected articles; measure top-3 hit rate, time to first word, total time, peak memory | - | todo | Judge by retrieval and honesty, not prose |

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
| 5.1 | GPS: one NMEA parser with serial, gpsd and log-replay sources; replay labelled SIMULATED everywhere | - | todo | Replaces the GNSS placeholder only when a source is configured |
| 5.2 | Offline maps: a region extract in a local map file shown with a vendored map library | - | todo | Don't bulk-download openstreetmap.org tiles |
| 5.3 | Waypoints in SQLite, with distance and bearing to a chosen one | - | todo | No routing yet |
| 5.4 | ESP32 protocol spec: JSON lines over USB serial with `hello`, `telemetry`, `button`, `heartbeat`, `shutdown_request`/`shutdown_ack` and a version field | - | todo | `docs/ESP32-PROTOCOL.md` |
| 5.5 | Pi side of the protocol, plus a mock ESP32 on a virtual serial port sending simulated battery data | - | todo | Power is cut only after the shutdown ack and the heartbeat stops |

## Deferred until hardware is chosen

- Battery-specific software and charge estimates (needs a real sensor)
- ESP32 firmware and PCB-specific code
- RF features
- Elaborate desktop customisation for the larger screen
