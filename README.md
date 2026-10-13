# Cyberdeck panel

Touch app for a Raspberry Pi 4 with a 2.8" ILI9341 SPI TFT (320x240, `fbtft`), an ADS7846/XPT2046 touchscreen and a ribbon camera.

| Page | What it does |
| --- | --- |
| HOME | Start page: clock, status and tiles for Reader, Planner, Ask, Tools, Quick controls, Audio, Games (Snake, Tetris, Pong, Brick Breaker, Flappy, Space Invaders, Minesweeper, Solitaire, Sudoku), SOS beacon and torch, and More |
| ASK (via Home) | Type a question on an on-screen keyboard and read the offline librarian's answer with its sources (needs cyberdeck-core) |
| CAMERA | Live preview, full-resolution SNAP, 3 s/10 s self-timer, pause, FPS, and a last-photo thumbnail |
| PHOTOS | Browse, zoom/pan full-size pictures and delete saved photos |
| MORE (via Home) | Photo album slideshow and GIF player |
| TOOLS | Timelapse, timers, network history, selected-photo sharing, presets, recovery, updates, GNSS placeholder, and Sun & moon (sunrise, sunset and moon phase, computed offline) |
| STATUS | CPU/RAM graphs, temperature, storage, uptime; HISTORY shows up to 7 days of temperature, CPU and undervoltage (needs cyberdeck-core); DETAILS shows power, clock sync, Wi-Fi and network |
| OPTIONS | Pixel level, screensavers, colour test, touch calibration, graph length, photo size, alert banner |
| EXIT | Close the app, or shut down the Pi safely |

A health banner warns about low voltage, heat, throttling and low storage on every page.
Screensavers: Radar, Starfield, interactive Bunnies, Clock, Photo album, GIFs, Black.
The whole UI can be turned (OPTIONS -> Screen); it defaults to portrait, 90° clockwise, with
layouts rebuilt for 240x320. See [ROTATION.md](docs/ROTATION.md).

## Getting started

Install, update and run options: [INSTALL-AND-UPDATE.md](docs/INSTALL-AND-UPDATE.md).

## Files

- `panel_app.py` and `panel_tools/`: the app. Keep the repository together; run `sudo python3 ~/cyberdeck-repo/panel_app.py`.
- `core/` and `setup_core_service.py`: the local backend (telemetry log, offline retrieval and AI answers, GPS position). See [CORE.md](docs/CORE.md) and [LIBRARIAN.md](docs/LIBRARIAN.md).
- `python3 -m core.hardware`: checks the wiring plan for the incoming hardware against `config.txt` (pin, I2C address, UART, PWM and USB clashes). See [HARDWARE-MAP.md](docs/HARDWARE-MAP.md).
- `deck`: terminal commands; `deck ask "..."` answers from the offline sources and `deck bench` measures the librarian. See [LIBRARIAN.md](docs/LIBRARIAN.md#asking-questions).
- `setup_panel_service.py`: start at boot with crash restart (`sudo python3 ~/cyberdeck-repo/setup_panel_service.py --now`), or use OPTIONS → Start at boot. See [HOME-AND-STARTUP.md](docs/HOME-AND-STARTUP.md).
- `setup_panel_touch.py`: keeps the TFT touch from clicking the desktop.
- `setup_backlight.py`: optional PWM backlight (real dimming, Black saver switches the backlight off). See [BACKLIGHT-PWM.md](docs/BACKLIGHT-PWM.md).
- `cyberdeck.py`: older first version, kept as a fallback ([SETUP.md](docs/SETUP.md)).
- `AGENTS.md`: rules for the coding agents working on this repo; [ROADMAP.md](docs/ROADMAP.md) is the shared task list.
- `tests/`: hardware-free tests (`python3 -m unittest discover -s tests -v`).
- `tools/render_previews.py` and `tools/render_tools.py`: regenerate the sample screen images in `docs/previews/`.
- `tools/content.py`: plans, downloads and verifies the offline library ZIMs by tier. See [CONTENT.md](docs/CONTENT.md).

## Guides

| Guide | Covers |
| --- | --- |
| [INSTALL-AND-UPDATE.md](docs/INSTALL-AND-UPDATE.md) | First install, updating with Git, running by hand, rolling back |
| [HOME-AND-STARTUP.md](docs/HOME-AND-STARTUP.md) | Home page and starting at boot |
| [CAMERA-PHOTOS-POWER.md](docs/CAMERA-PHOTOS-POWER.md) | Camera, photo review, self-timer, health banner, boot service, safe shutdown |
| [PANEL-OPTIONS.md](docs/PANEL-OPTIONS.md) | The Options menu, clock and health indicators |
| [ROTATION.md](docs/ROTATION.md) | Turning the UI (portrait) and the camera |
| [APPS-AND-SAVERS.md](docs/APPS-AND-SAVERS.md) | Apps tab, audio, Snake, photo album, GIFs, bunny and clock savers |
| [SCREENSAVERS.md](docs/SCREENSAVERS.md) | Screensaver styles and settings |
| [GAMES.md](docs/GAMES.md) | The games and their records |
| [TOOLS.md](docs/TOOLS.md) | Tools, data folder, photo sharing, explicit updates |
| [BACKLIGHT-PWM.md](docs/BACKLIGHT-PWM.md) | Wiring and setting up real backlight dimming |
| [PANEL-UPDATE.md](docs/PANEL-UPDATE.md) | Touch isolation and the status graphs |
| [CORE.md](docs/CORE.md) | The backend: telemetry, history page, API |
| [LIBRARIAN.md](docs/LIBRARIAN.md) | Offline search and AI answers, Kiwix setup, `deck ask` and `deck bench` |
| [CONTENT.md](docs/CONTENT.md) | Choosing, downloading and checking the offline library ZIMs |
| [LIBRARIAN-RANKING.md](docs/LIBRARIAN-RANKING.md) | Model-free keywords and passage ranking |
| [HARDWARE-MAP.md](docs/HARDWARE-MAP.md) | Which pins, buses and ports each part uses, and the wiring checker |
| [ESP32-PROTOCOL.md](docs/ESP32-PROTOCOL.md) | The planned serial protocol between the Pi and an ESP32 power and button board |
| [PI-BASELINE.md](docs/PI-BASELINE.md) | Recorded facts about the Pi this runs on |
| [PANEL-DEVELOPMENT.md](docs/PANEL-DEVELOPMENT.md) | How the panel code fits together, for contributors |
| [ROADMAP.md](docs/ROADMAP.md) | The shared task list |
| [SETUP.md](docs/SETUP.md) | The legacy `cyberdeck.py` |
