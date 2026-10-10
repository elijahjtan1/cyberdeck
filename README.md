# Cyberdeck panel

Touch app for a Raspberry Pi 4 with a 2.8" ILI9341 SPI TFT (320x240, `fbtft`), an ADS7846/XPT2046 touchscreen and a ribbon camera.

| Page | What it does |
| --- | --- |
| CAMERA | Live preview, full-resolution SNAP, 3 s/10 s self-timer, pause, FPS, and a last-photo thumbnail |
| PHOTOS | Browse, view full-screen and delete saved photos |
| APPS | Audio controls and visualizer, Snake, photo album slideshow, GIF player |
| STATUS | CPU/RAM graphs, temperature, storage, uptime; DETAILS shows power, clock sync, Wi-Fi and network |
| OPTIONS | Pixel level, screensavers, colour test, touch calibration, graph length, photo size, alert banner |
| EXIT | Close the app, or shut down the Pi safely |

A health banner warns about low voltage, heat, throttling and low storage on every page.
Screensavers: Radar, Starfield, interactive Bunnies, Clock, Photo album, GIFs, Black.
The whole UI can be turned (OPTIONS -> Screen); it defaults to portrait, 90° clockwise, with
layouts rebuilt for 240x320. See [ROTATION.md](docs/ROTATION.md).

## Files

- `panel_app.py`: the app. Run with `sudo python3 ~/panel_app.py`.
- `setup_panel_service.py`: start at boot with crash restart (`sudo python3 ~/setup_panel_service.py --now`).
- `setup_panel_touch.py`: keeps the TFT touch from clicking the desktop.
- `cyberdeck.py`: older first version, kept as a fallback.
- `docs/`: install notes for each update. The newest is [ROTATION.md](docs/ROTATION.md); apps and savers are in [APPS-AND-SAVERS.md](docs/APPS-AND-SAVERS.md); boot start and shutdown are in [CAMERA-PHOTOS-POWER.md](docs/CAMERA-PHOTOS-POWER.md).
- `tests/`: hardware-free tests (`python3 -m unittest discover -s tests -v`).
- `tools/render_previews.py`: regenerates the sample screen images in `docs/previews/`.
