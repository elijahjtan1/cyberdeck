# HDMI touch screen

The panel app can draw on an HDMI screen, such as a 5" 800x480 touch display, instead of
the 2.8" SPI panel. Every page, game and app works the same way on both.

## How it looks

The app still lays out a 320x240 page (240x320 in portrait) and shows it as large as it
fits, centred. Whole-number scales copy pixels exactly; others are scaled up to the next
whole number and back down, so edges stay crisp with slight smoothing:

| Screen | Scale | Page size on screen | Black border |
| --- | --- | --- | --- |
| 1024x600 (the deck's 5" QDtech screen) | 2.5x | 800x600 | 112 px left and right |
| 800x480 | 2x | 640x480 | 80 px left and right |
| 320x240 (the 2.8" panel) | 1x | full screen | none |

Buttons are 2.5 times bigger on the 5" screen, so they are easier to hit. Portrait
(**OPTIONS → Screen**) works too; the page is then the full height with wider borders.

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

## Keyboard

A USB or wireless keyboard types in **READER**, **ASK** and **PLANNER** as well as the
on-screen keys. Plug it in or switch it on at any time; the panel finds it within a few
seconds. The keys are read with python3-evdev, like the touchscreen, and the layout is
US English. While the panel runs it has the keyboard to itself, so typing doesn't reach
the console behind it. **Ctrl+C** stops the panel when you started it from a terminal;
under the boot service it only opens the EXIT page, so a stray Ctrl+C can't leave the
panel off until the next reboot.

| Key | READER | ASK | PLANNER |
| --- | --- | --- | --- |
| Letters, digits, symbols | Type the search; typing on a result or article starts a new one | Type the question; typing after an answer starts a new one | Type the entry; typing on a day starts a new entry |
| **Enter** | Search, then open the first result | Ask | NEXT, then SAVE; on the month, open today |
| **1** to **9** | Open that result | | |
| **Esc** | Back one step, then HOME | STOP or EDIT, then HOME | Back one step, then HOME |
| **Left / Right** | Page through results or the article | | Previous or next month or day |
| **Up / Down**, **Page Up / Page Down** | Page, like Left / Right | Scroll the answer | Scroll the day; on the time, 15 minutes (Page Up/Down: an hour) |
| **Space** | Next page of an article | Scroll down an answer | |
| **Backspace** | Delete a letter | Delete a letter | Delete a letter; on a chosen entry, delete it |

Any key also wakes the screen from a screensaver. Other pages still need touch.

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

- **Tests:** `tests/test_power.py` draws on fake 800x480 32-bit (2x) and 1024x600 16-bit
  (2.5x, portrait) framebuffers: scaling, centring, borders and changed-rows-only updates;
  `tests/test_panel_app.py` covers choosing the screen and the touch device;
  `tests/test_keyboard.py` covers key names, finding and losing a keyboard, and typing in
  READER, ASK and PLANNER.
- **On the Pi:** the page fills the middle of the screen, taps land where you touch after
  calibration, and closing the app gives the console back. Type a search in READER on the
  wireless keyboard and press Enter; Ctrl+C stops a terminal run (under the boot service
  it opens EXIT instead).
