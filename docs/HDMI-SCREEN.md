# HDMI touch screen

The panel app can draw on an HDMI screen, such as a 5" 800x480 touch display, instead of
the 2.8" SPI panel. Every page, game and app works the same way on both.

## How it looks

The app still lays out a 320x240 page (240x320 in portrait) and shows it at the largest
whole-number scale that fits, centred, so text stays sharp:

| Screen | Scale | Page size on screen | Black border |
| --- | --- | --- | --- |
| 800x480 (most 5" screens) | 2x | 640x480 | 80 px left and right |
| 1024x600 (some 7" screens) | 2x | 640x480 | 192 px each side, 60 px top and bottom |
| 320x240 (the 2.8" panel) | 1x | full screen | none |

Buttons are twice as big on a 5" screen, so they are easier to hit. Portrait
(**OPTIONS → Screen**) works too; the page is then 480 px tall with wider borders.

## Which screen and touch device it uses

- `--display auto` (the default) uses HDMI when the Pi sees a monitor on an HDMI port, and
  the 2.8" panel otherwise. HDMI wins because the 2.8" panel's overlay in
  `/boot/firmware/config.txt` keeps its framebuffer even when the panel is unplugged.
- `--display hdmi` or `--display spi` picks one for this run.
- Touch: on HDMI any touchscreen comes before the 2.8" panel's ADS7846 controller; on the
  2.8" panel it is the other way round. `--touch /dev/input/eventN` overrides both.
- The first start on a new touch device shows the five-cross calibration, as on the first
  install. It is saved in `~/cyberdeck-data/touch.json`; **OPTIONS → Calibrate touch** redoes it.
- On HDMI the text console is switched to graphics mode while the app runs, so its cursor
  and messages don't draw over the page, and switched back when it closes.

## Running it

The app draws straight to the framebuffer, so the Pi must boot to the console, not the
desktop. If a desktop is showing on the HDMI screen, it hides the panel.

```
systemctl get-default                           # multi-user.target means console boot
cat /sys/class/graphics/fb*/name                # vc4drmfb is HDMI, fb_ili9341 the 2.8" panel
cat /sys/class/graphics/fb0/virtual_size        # for example 800,480
grep -H . /sys/class/drm/card*-HDMI-A-*/status  # connected for the HDMI screen
sudo python3 ~/cyberdeck-repo/panel_app.py
```

To boot to the console instead of the desktop: `sudo raspi-config`, then
**System Options → Boot / Auto Login → Console Autologin**, then reboot.

## Checks

- **Tests:** `tests/test_power.py` draws on fake 800x480 32-bit and 1024x600 16-bit
  framebuffers (scaling, centring, borders, changed-rows-only updates, portrait), and
  `tests/test_panel_app.py` covers choosing the screen and the touch device.
- **On the Pi:** the page fills the middle of the screen, taps land where you touch after
  calibration, and closing the app gives the console back.
