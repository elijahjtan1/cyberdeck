# PWM backlight, a real "off" screensaver, and power/performance changes

This update does three things:

- **Brightness:** the panel's brightness can come from the backlight itself (PWM) instead of darker pixels.
- **Black screensaver:** the Black screensaver can switch the backlight off.
- **Power and CPU:** the app now does less work when nothing on screen is changing.

It builds on the Tools update (`feature/panel-tools`).

## 1. Rewire the backlight to PWM

**Do this with the Pi powered off.**

Right now the module's **LED** pin is tied to 3.3 V, so it's always fully on. To control it, the LED pin has to be driven by a GPIO with hardware PWM. The recommended pin is **GPIO18 (physical pin 12)**.

### Check how much current the LED pin draws

A GPIO pin can only supply a few milliamps safely: keep it under about 8 mA. Some 2.8" ILI9341 boards switch the backlight with their own transistor, so the LED pin draws very little. Others feed the LEDs straight from that pin and draw tens of mA. I can't tell which board you have, so measure it:

1. Set a multimeter to its mA range.
2. Connect it between 3.3 V and the module's **LED** pin, with everything else wired as usual.
3. Power up and read the current.

### Option A: 8 mA or less (the board has its own transistor)

```
Pi GPIO18 (pin 12) ──[ 100 Ω ]── LED pin on the TFT module
```

The 100 Ω resistor is optional protection.

Set up with: `sudo python3 setup_backlight.py`

### Option B: more than 8 mA, or you're not sure (always safe)

A small PNP transistor switches 3.3 V to the LED pin. The GPIO only drives its base. Suitable parts: S8550, 2N3906, BC327.

```
3.3 V (pin 1) ────────────────┬──────────── emitter (E)
                              │
                          [ 10 kΩ ]  keeps it off until the app takes over
                              │
Pi GPIO18 (pin 12) ──[ 1 kΩ ]─┴──────────── base (B)

                    collector (C) ───────── LED pin on the TFT module
```

A PNP turns **on when the GPIO is low**, so tell the app the output is inverted:

```bash
sudo python3 setup_backlight.py --invert
```

### Notes for either option

- **Pin conflicts:** the setup script checks `config.txt`. It refuses if GPIO18 already appears in another overlay line, such as the TFT's `dc_pin`/`reset_pin` or the touch `penirq`. If it's taken, use `--gpio 12`, `13` or `19` and wire to that pin instead:

  | GPIO | Physical pin |
  | --- | --- |
  | 12 | 32 |
  | 13 | 33 |
  | 18 | 12 |
  | 19 | 35 |

- **Headphone jack:** on a Pi 4 it uses a different PWM block (GPIO40/41), so GPIO18 shouldn't affect analog audio. Check the headphone output still works afterwards if you use it.
- **Frequency:** the default is 2 kHz, so there's no visible flicker. If you hear a faint whine, try `--frequency 20000`.

## 2. Turn it on

1. Copy the update and stop the running app, as in [TOOLS.md](TOOLS.md).
2. Configure the backlight, choosing the command for your wiring:

   ```bash
   cd ~/cyberdeck-repo
   sudo python3 setup_backlight.py            # Option A
   sudo python3 setup_backlight.py --invert   # Option B
   sudo reboot
   ```

3. Check the wiring: `sudo python3 setup_backlight.py --test`. The backlight steps through 100 → 70 → 40 → 15 → 5 → 0 → 100%.
4. Start the app: `sudo systemctl start cyberdeck-panel`, or `sudo python3 panel_app.py`.

The script does three things:

- adds one marked line, `dtoverlay=pwm,pin=18,func=2`, to `/boot/firmware/config.txt`, after first saving `config.txt.before-backlight`;
- writes `~/cyberdeck-data/backlight.json`;
- with `--undo`, removes only its own line and that file.

Without `backlight.json`, the app behaves exactly as before: pixel dimming, with the backlight assumed to be on 3.3 V.

## What changes on screen

| Setting | Before | With the PWM backlight |
| --- | --- | --- |
| OPTIONS → Pixel level | Darker pixels; backlight unchanged | Renamed **Brightness**. Dims the backlight itself, and colours stay at full strength. The 100/70/40% steps follow a perceptual curve, so 40% looks like 40%, not like 16%. |
| Screensaver level | Darker saver pixels | Saver brightness. |
| **Black** screensaver | Black pixels; backlight still on | **Backlight at 0%** and the panel asked to blank. The camera sensor stops (see below), and the app wakes only about once a second. The first tap wakes everything. |
| Test colours, calibration | Full intensity | Full brightness. |
| App closed or crashed | Backlight on | The app sets 100% on exit, so the "not running" screen is readable. |

**During boot:** with Option A, the backlight is dark from power-on until the app starts. With Option B, the 10 kΩ resistor keeps it dark during that time. To light it without the app, run `sudo python3 setup_backlight.py --on`. After a shutdown, the backlight may stay in its last state, so go by the green LED before unplugging, as before.

**Panel blanking** uses the framebuffer's `blank` control. Not every fbtft driver supports it; if yours doesn't, the backlight still goes off and nothing else changes.

## 3. Performance and power changes

I reviewed the merged code (yours, the other agent's `panel_tools`, and mine) for wasted work. These changes are in this update:

| Change | Effect |
| --- | --- |
| **Camera sensor stops when nobody is watching.** Previously the camera streamed 20 frames per second forever, on every page and under every screensaver. Now, 15 s after you leave the Camera page (or a saver starts), the sensor and image processor stop. They restart in about half a second when you return or take a photo, and exposure settles before the photo. | Usually the largest single saving outside the backlight. Timelapses with 30–60 s intervals also let the sensor rest between shots. |
| **Event-driven main loop.** The loop used to wake 50 times a second whatever was on screen. It now sleeps until the next redraw or a touch (it uses `select()` on the touchscreen). | Measured on an idle Status page: **52 → 7 wakeups a second**. About 1 a second with the backlight off. |
| **Only changed pixels go over SPI.** Identical frames are no longer sent. Other frames send only the changed rows, through a memory map, so fbtft transfers just those lines. A full refresh still happens every 10 s, as insurance. | Static menus cost no SPI traffic. A ticking clock costs a strip of rows instead of the whole 153 KB frame. Doesn't apply to the moving camera picture. |
| **Camera page: draw only what moves.** Text and buttons are cached and rebuilt at most once a second. Each frame only pastes the picture, using bilinear scaling. Each camera frame is drawn exactly once, instead of on a fixed timer that sometimes drew the same frame twice. | Measured here: **4.5 → 0.6 ms per frame**, about 8× less CPU for the live preview. Output is pixel-identical, checked in the tests. |
| **Fewer background processes.** Wi-Fi signal is read from `/proc/net/wireless` instead of running `iw` every 10 s, and clock sync is checked once a minute instead of every 10 s. | Fewer process launches in the background. |
| **Camera stream shape (bug fix).** In portrait, the sensor was asked for a 240×320 portrait-shaped stream, which can crop or stretch the image. It now always streams 320×240 in the sensor's own orientation. Rotation and Camera turn are applied afterwards. | A correct preview in portrait. |

## Review notes I did not change

Most of these were left alone to keep this update focused:

- **Photo zoom** decodes the full 12 MP image (about 36 MB) for every photo. Using Pillow's `draft()` for zoom levels of 1–2× would make opening it near-instant.
- **Tools pages** redraw 5 times a second (`ToolsUI.delay = .2`). SPI traffic is now skipped when nothing changes, but 1 Hz would be enough for the static menus. The Timelapse page also checks disk usage and recent file sizes on every redraw.
- **Network history** reads `/proc/net/dev` every second, even when the Network page is closed. It's cheap, but it could pause outside that page if the 120 s history isn't needed.
- **Unused imports in `test_tools.py`** (`queue`, `Job`). A similar one in `panel_app.py` (`camera_image`) is now removed.
- **System-level savings, if unused:**
  - Bluetooth: `dtoverlay=disable-bt` in `config.txt`.
  - HDMI, if no monitor is ever attached. Be careful with VNC on Wayland, which relies on the desktop session.
  - These are settings choices, not code changes.

## Checks

All 67 tests pass (`python3 -m unittest discover -s tests -v`), including 15 new ones in `tests/test_power.py`:

- **Backlight:** perceptual duty values, sysfs writes skipped when the level hasn't changed, the inverted (PNP) mode, and staying off unless `backlight.json` exists. It falls back to pixel dimming before the reboot.
- **Framebuffer:** only changed rows are written, identical frames are skipped, the full frame is resent after unblanking and every 10 s, and portrait frames land at the right panel pixels.
- **Camera:** the sensor stops when unwatched, is woken for a photo, and resumes for the preview.
- **Camera page:** the cached render is pixel-identical to a full redraw, with identical buttons, in both orientations.
- **Camera stream:** stays 320×240 in portrait.
- **`setup_backlight.py`:** adds its line once, refuses a GPIO already used by the TFT or touch, refuses an existing PWM overlay, picks the right PWM channel for the pin, and `--undo` restores the file exactly.
- **Real application loop with a simulated PWM backlight:**
  1. Preview Black: the backlight goes to 0% and the panel is blanked.
  2. Tap: the panel is unblanked and the backlight returns to 70%.
  3. Close: the backlight is set to 100%.

**Still needs checking on the Pi:**

- the LED-pin current and your wiring choice
- that `--test` dims smoothly
- that Black really goes dark and a tap brings it back
- that photos taken after the camera has been idle are well exposed
- whether your fbtft driver supports panel blanking (not required)
