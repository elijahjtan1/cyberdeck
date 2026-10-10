# Cyberdeck panel

Touch app for a Raspberry Pi 4 with a 2.8" ILI9341 SPI TFT (320x240, `fbtft`), an ADS7846/XPT2046 touchscreen and a ribbon camera.

| Page | What it does |
| --- | --- |
| CAMERA | Live preview, full-resolution SNAP, 3 s/10 s self-timer, pause, FPS, and a last-photo thumbnail |
| PHOTOS | Browse, zoom/pan full-size pictures and delete saved photos |
| APPS | Audio, Games (Snake, Tetris, Pong), slideshow, GIFs, Quick controls and Tools |
| TOOLS | Timelapse, timers, network history, selected-photo sharing, presets, recovery, updates and GNSS placeholder |
| STATUS | CPU/RAM graphs, temperature, storage, uptime; DETAILS shows power, clock sync, Wi-Fi and network |
| OPTIONS | Pixel level, screensavers, colour test, touch calibration, graph length, photo size, alert banner |
| EXIT | Close the app, or shut down the Pi safely |

A health banner warns about low voltage, heat, throttling and low storage on every page.
Screensavers: Radar, Starfield, interactive Bunnies, Clock, Photo album, GIFs, Black.
The whole UI can be turned (OPTIONS -> Screen); it defaults to portrait, 90° clockwise, with
layouts rebuilt for 240x320. See [ROTATION.md](docs/ROTATION.md).

## Files

- `panel_app.py` and `panel_tools/`: the app. Keep the repository together; run `sudo python3 ~/cyberdeck-repo/panel_app.py`.
- `setup_panel_service.py`: start at boot with crash restart (`sudo python3 ~/cyberdeck-repo/setup_panel_service.py --now`).
- `setup_panel_touch.py`: keeps the TFT touch from clicking the desktop.
- `setup_backlight.py`: optional PWM backlight (real dimming, Black saver switches the backlight off). See [BACKLIGHT-PWM.md](docs/BACKLIGHT-PWM.md).
- `cyberdeck.py`: older first version, kept as a fallback.
- `docs/`: [GAMES.md](docs/GAMES.md) covers Tetris and Pong; [BACKLIGHT-PWM.md](docs/BACKLIGHT-PWM.md) (backlight wiring, power and performance changes); [TOOLS.md](docs/TOOLS.md) covers the tools update, data-folder migration and installation. Rotation notes are in [ROTATION.md](docs/ROTATION.md); apps and savers in [APPS-AND-SAVERS.md](docs/APPS-AND-SAVERS.md); boot start and shutdown in [CAMERA-PHOTOS-POWER.md](docs/CAMERA-PHOTOS-POWER.md).
- `tests/`: hardware-free tests (`python3 -m unittest discover -s tests -v`).
- `tools/render_previews.py`: regenerates the sample screen images in `docs/previews/`.
