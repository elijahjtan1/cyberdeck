# Panel screensavers

Open **OPTIONS -> Screensavers**.

- **RADAR:** a green rotating sweep, fading trail, scope rings, and softly glowing decorative dots. Normal speed is one revolution every 12 seconds. No radio, GNSS, object detection, or other live tracking is involved.
- **STARFIELD:** an old-school drifting starfield, with points moving outward from the centre.
- **BLACK:** the original black-image idle mode.
- **OFF:** no automatic screensaver.

Recommended starting point: **Radar / after 1 minute / normal speed / 70% pixel level**.

## Settings

Tap a style to select it; the selected button is highlighted. Settings are saved automatically.

- **After:** cycles 30 seconds, 1 minute, and 5 minutes without a touchscreen tap. VNC mouse/keyboard activity does not reset this timer.
- **Speed:** slow, normal, fast. It changes animation speed, not SPI clock speed or camera FPS.
- **Pixel level:** 100%, 70%, or 40% pixel intensity. The screensaver never exceeds the main app's pixel-intensity setting.
- **Preview:** starts the selected saver immediately. When Off is selected, Preview demonstrates Radar without enabling automatic activation. OPTIONS also has a Preview saver shortcut.

The first tap wakes the panel and returns to the previous page. It cannot activate a hidden button. The idle timer restarts after waking. The screensaver does not activate automatically while choosing its settings, testing colours, confirming exit, or confirming calibration. Calibration itself runs outside the idle loop.

The clock on animated screensavers follows the Pi's local system time. Black mode has no text or clock. Animations are capped at eight updates per second; actual rate depends on SPI transfer and Pi performance. Black mode is written once rather than continuously redrawn. CPU/RAM monitoring continues behind the saver.

This is visual flavour, not a system sleep mode. The camera remains active, and your 3.3 V-wired backlight stays on. Pixel dimming does not save a dimmed camera photo or alter the colour-test pattern.

## Existing settings

Your old enabled idle-blank timeout becomes Black with the same timeout. If idle blanking was off, automatic screensaving stays off until you choose a style. Existing pixel level, graph length, photos, and touch calibration are preserved. New settings use the existing `~/cyberdeck-data/options.json` file.

## Update

Stop the app with Ctrl+C in the Pi terminal and make a backup:

```bash
cp ~/panel_app.py ~/panel_app.previous.py
```

From Windows PowerShell:

```powershell
scp "C:\Users\Eli\Documents\Codex\2026-10-09\i\outputs\panel_app.py" eli@cyberdeck.local:/home/eli/
```

Then in the Pi terminal:

```bash
sudo python3 ~/panel_app.py
```

No reboot or additional packages are required. Your prior desktop-touch isolation remains in effect.

## Verification

Eighteen local tests passed, including legacy-settings migration, animation changes over time and speed, black-frame output, idle timing and exclusions, menu hitboxes, option persistence, and the application's simulated touch/wake/confirmation flow. Preview images were rendered from the same application drawing functions and inspected. Actual touch, animation smoothness, and SPI display output still need verification on the Pi.

Suggested hardware check: choose Radar, press Preview, tap once to wake, then choose a 30-second delay and wait on Status. Confirm the saver starts and a tap returns to Status without changing pages. Try Black and Off as well.
