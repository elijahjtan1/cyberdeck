# Main app

The main app is the cyberdeck's full-screen home on the 5" HDMI touch screen. It draws at the
screen's own resolution (1024x600) instead of the quick panel's 320x240 page scaled up. Like
the quick panel (`panel_app.py`), it draws straight to the framebuffer with no desktop, which
leaves the most memory for Ollama.

![Main app home](previews/main-app-preview.png)

## Running it

The Pi must boot to the console, as for the quick panel on HDMI (see [HDMI-SCREEN.md](HDMI-SCREEN.md#running-it)).

```
sudo python3 ~/cyberdeck-repo/main_app.py
```

It isn't a boot service yet. Ctrl+C on the keyboard closes it, and so does the CLOSE tile.

## Using it

| Key | Does |
| --- | --- |
| `1` to `9`, `0` | Open that tile |
| Arrows, then Enter | Pick a tile and open it |
| `Q` | Open the quick panel |
| Esc | Back to home |
| Ctrl+C | Close the main app |

Touch works on every tile. It uses the quick panel's calibration (`~/cyberdeck-data/touch.json`).
If the quick panel hasn't calibrated this touch device yet, the main app says so. Press `Q`,
let the quick panel calibrate, then come back.

## The main app and the quick panel

While both share the 5" screen, only one runs at a time:

- **QUICK PANEL** (or `Q`) closes the main app and starts the quick panel in its place.
- The quick panel's **EXIT** page then shows **MAIN APP** instead of CLOSE APP, which hands the
  screen back.
- Tiles for apps that haven't moved here yet (Ask, Wikipedia, Planner, Tools, Games, Camera,
  Photos) open the quick panel straight on that app. Leaving it through EXIT → MAIN APP returns
  here.

The quick panel takes these options for this:

- `--open PAGE` starts it on `ask`, `reader`, `planner`, `tools`, `games`, `camera` or `photos`.
- `--main-app` makes its EXIT page hand the screen back.

The plan is for the apps to move into the main app one at a time, starting with Ask and
Wikipedia at full size. Later, the 2.8" screen can run the quick panel full time beside the main
app on the 5".

## Status

![Main app status](previews/main-app-status-preview.png)

**STATUS** (`8`) shows the temperature, CPU, memory, free disk, clock speed and uptime. These
are read on the Pi, the same way the quick panel reads them. It also shows whether the backend
(`cyberdeck-core`) is running and what it is busy with. Health alerts (low voltage, heat,
throttling) show in the header on every page.

## Checks

- **Tests:** `tests/test_main_app.py` covers the tile layout at 1024x600 and 800x480, the
  arrow keys, the hand-off arguments, drawing on a fake 1024x600 16-bit screen (only changed
  rows are sent), and mapping calibrated taps.
- **On the Pi:**
  - The home fills the screen with sharp text.
  - Each tile opens with its number key and by touch, and taps land where you touch.
  - `Q` opens the quick panel, and EXIT → MAIN APP comes back.
  - The WIKIPEDIA tile opens the quick panel's READER.
