# Cyberdeck panel

Touch app for a Raspberry Pi 4 with a 2.8" ILI9341 SPI TFT (320x240, `fbtft`), an ADS7846/XPT2046 touchscreen and a ribbon camera.

| Page | What it does |
| --- | --- |
| HOME | Start page: clock, status and tiles for Camera, Photos, Games, Audio, Album, GIFs, Tools, Quick controls and Ask |
| ASK (via Home) | Type a question on an on-screen keyboard and read the offline librarian's answer with its sources (needs cyberdeck-core) |
| CAMERA | Live preview, full-resolution SNAP, 3 s/10 s self-timer, pause, FPS, and a last-photo thumbnail |
| PHOTOS | Browse, zoom/pan full-size pictures and delete saved photos |
| APPS (via Home) | Audio, Games (Snake, Tetris, Pong, Brick Breaker, Flappy, Space Invaders, Minesweeper, Solitaire, Sudoku), slideshow, GIFs, Quick controls and Tools |
| TOOLS | Timelapse, timers, network history, selected-photo sharing, presets, recovery, updates and GNSS placeholder |
| STATUS | CPU/RAM graphs, temperature, storage, uptime; HISTORY shows up to 7 days of temperature, CPU and undervoltage (needs cyberdeck-core); DETAILS shows power, clock sync, Wi-Fi and network |
| OPTIONS | Pixel level, screensavers, colour test, touch calibration, graph length, photo size, alert banner |
| EXIT | Close the app, or shut down the Pi safely |

A health banner warns about low voltage, heat, throttling and low storage on every page.
Screensavers: Radar, Starfield, interactive Bunnies, Clock, Photo album, GIFs, Black.
The whole UI can be turned (OPTIONS -> Screen); it defaults to portrait, 90° clockwise, with
layouts rebuilt for 240x320. See [ROTATION.md](docs/ROTATION.md).

## Files

- [LIBRARIAN.md](docs/LIBRARIAN.md): offline source retrieval and Kiwix configuration.
- `panel_app.py` and `panel_tools/`: the app. Keep the repository together; run `sudo python3 ~/cyberdeck-repo/panel_app.py`.
- `core/` and `setup_core_service.py`: the local backend (telemetry log, offline retrieval and AI answers, GPS position). See [CORE.md](docs/CORE.md) and [LIBRARIAN.md](docs/LIBRARIAN.md).
- `python3 -m core.hardware`: checks the wiring plan for the incoming hardware against `config.txt` (pin, I2C address, UART, PWM and USB clashes). See [HARDWARE-MAP.md](docs/HARDWARE-MAP.md).
- `deck`: terminal commands; `deck ask "..."` answers from the offline sources and `deck bench` measures the librarian. See [LIBRARIAN.md](docs/LIBRARIAN.md#asking-questions).
- `setup_panel_service.py`: start at boot with crash restart (`sudo python3 ~/cyberdeck-repo/setup_panel_service.py --now`), or use OPTIONS → Start at boot. See [HOME-AND-STARTUP.md](docs/HOME-AND-STARTUP.md).
- `setup_panel_touch.py`: keeps the TFT touch from clicking the desktop.
- `setup_backlight.py`: optional PWM backlight (real dimming, Black saver switches the backlight off). See [BACKLIGHT-PWM.md](docs/BACKLIGHT-PWM.md).
- `cyberdeck.py`: older first version, kept as a fallback.
- `docs/`: [GAMES.md](docs/GAMES.md) covers the games; [BACKLIGHT-PWM.md](docs/BACKLIGHT-PWM.md) (backlight wiring, power and performance changes); [TOOLS.md](docs/TOOLS.md) covers the tools update, data-folder migration and installation. Rotation notes are in [ROTATION.md](docs/ROTATION.md); apps and savers in [APPS-AND-SAVERS.md](docs/APPS-AND-SAVERS.md); boot start and shutdown in [CAMERA-PHOTOS-POWER.md](docs/CAMERA-PHOTOS-POWER.md).
- `AGENTS.md`: rules for the coding agents working on this repo; [ROADMAP.md](docs/ROADMAP.md) is the shared task list.
- `tests/`: hardware-free tests (`python3 -m unittest discover -s tests -v`).
- [LIBRARIAN-RANKING.md](docs/LIBRARIAN-RANKING.md): model-free keywords and passage ranking.
- [ESP32-PROTOCOL.md](docs/ESP32-PROTOCOL.md): the planned serial protocol between the Pi and an ESP32 power and button board.
- `tools/render_previews.py`: regenerates the sample screen images in `docs/previews/`.
- `tools/content.py`: plans, downloads and verifies the offline library ZIMs by tier. See [CONTENT.md](docs/CONTENT.md).
