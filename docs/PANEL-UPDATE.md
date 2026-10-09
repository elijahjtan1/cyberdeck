# Panel app update: touch isolation and status graphs

This version is named `panel_app.py`, reflecting its role as the secondary screen app. It keeps the existing `cyberdeck-data` directory so your photos and calibration remain available. `cyberdeck.py` is the older version, retained as a fallback.

## What changed

- CPU and RAM plots show the last 60 seconds, with fixed 0-100% scales and one sample per second. History collects on both Camera and Status pages and starts empty at launch. Calibration pauses sampling; those gaps are not connected with invented data.
- Current CPU usage, temperature, used/total RAM, disk space, uptime, and network address remain visible. A storage bar indicates the percentage of disk space used.
- Calibration drains queued events and waits for a released finger before each target and before returning to the app. It never releases the existing touch grab.
- Failed recalibration retains the old mapping and keeps the app running. This includes calibration timeouts and file-save errors. Initial setup still requires successful calibration.
- EXIT opens a confirmation screen instead of immediately closing. A short input delay after calibration/Exit prevents trailing taps from hitting new controls.
- Normal exit and handled failures draw a clear "Panel app is not running" screen, replacing stale controls before touch is released.
- A separate setup script tells both libinput-based desktops and Xorg to ignore only `ADS7846 Touchscreen`. Direct evdev access remains available to the panel app. This protects the desktop even when the panel app exits or fails.

The original code already kept its grab during calibration; without Pi logs, the exact cause of the reported desktop clicks is unconfirmed. Exiting or failing previously released touch back to the desktop. The changes cover those paths rather than assuming recalibration explicitly ungrabbed the device.

## Update

1. Stop the old app with Ctrl+C in its Pi terminal. Back up the current script:

```bash
cp ~/panel_app.py ~/panel_app.previous.py
```

2. From Windows PowerShell, copy these two files (replace the hostname with the Pi's IP if required):

```powershell
scp "C:\Users\Eli\Documents\Codex\2026-10-09\i\outputs\panel_app.py" "C:\Users\Eli\Documents\Codex\2026-10-09\i\outputs\setup_panel_touch.py" eli@cyberdeck.local:/home/eli/
```

3. In the Pi terminal, install desktop touch isolation, then reboot:

```bash
sudo python3 ~/setup_panel_touch.py
sudo reboot
```

If setup reports an error, resolve that before rebooting; it prints what prevented installation. It verifies the expected touchscreen exists and refuses to overwrite unfamiliar configuration files.

4. Reconnect through VNC and launch:

```bash
sudo python3 ~/panel_app.py
```

Existing libraries are sufficient. No camera, SPI speed, GPIO, colour, or boot-overlay changes are needed. Automatic startup is still not configured.

## Verify on the Pi

After reboot, touching the TFT with the app stopped should no longer click the desktop. Launch the app, use CAL, tap and release each cross, then switch between CAMERA and STATUS. The pointer in VNC should stay unaffected. Touch buttons should continue working in the app. Status charts fill from the right as samples arrive. EXIT now needs a separate CLOSE APP tap; CANCEL returns to your page.

If desktop clicks persist after the reboot, collect the output of these commands rather than changing GPIO wiring:

```bash
cat /etc/udev/rules.d/99-panel-touch.rules
cat /etc/X11/xorg.conf.d/99-panel-touch.conf
sudo evtest
```

Select ADS7846 Touchscreen only after stopping the app, since the running app grabs it exclusively. Report any errors printed by the app too.

## Undo desktop isolation

```bash
sudo python3 ~/setup_panel_touch.py --undo
sudo reboot
```

Only the setup script's own matching configuration files are removed. Photos/calibration are untouched. Until undone, this particular touch panel is intentionally unavailable as a desktop pointer; your regular mouse, keyboard, and VNC inputs remain available.

Configuration files managed by setup:

- `/etc/udev/rules.d/99-panel-touch.rules`
- `/etc/X11/xorg.conf.d/99-panel-touch.conf`

## Validation

Local tests cover RGB565 ordering, affine calibration, single-tap release handling, framebuffer padding, all three views, preservation of mapping/touch on recalibration failures, successful recalibration ownership, graph time gaps, idempotent/reversible installation, and refusal to overwrite other configuration. Visual preview was inspected. Actual GPIO, camera and desktop isolation must still be verified on your Pi.

The preview illustration and readings are sample data. SNAP still saves the current 320x240 camera frame. Pausing freezes the preview rather than powering the camera off. Forced termination or hardware failure can prevent the stopped message from being drawn, but desktop isolation still applies once installed and rebooted.

References: [libinput device isolation](https://wayland.freedesktop.org/libinput/doc/1.25.0/ignoring-devices.html), [Xorg InputClass Ignore](https://manpages.debian.org/stretch/xserver-xorg-core/xorg.conf.d.5.en.html), [evdev exclusive access](https://python-evdev.readthedocs.io/en/latest/tutorial.html#getting-exclusive-access-to-a-device).
