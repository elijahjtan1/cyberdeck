# Cyberdeck camera and status monitor

Built for your Pi 4, ILI9341 on `/dev/fb0`, 320x240 RGB565, ADS7846-compatible touch, and ribbon camera working with `rpicam-hello`.

## Install on the Pi

1. Copy `cyberdeck.py` into `/home/eli/`. Keep the original `camera_screen.py` as a fallback.
2. Stop the old camera script with Ctrl+C. Close `rpicam-hello` and `evtest` too.
3. Run in your Pi terminal:

```bash
sudo apt install python3-picamera2 python3-numpy python3-pil python3-evdev fonts-dejavu-core
sudo python3 /home/eli/cyberdeck.py
```

No changes to your working SPI speed, RGB/BGR setting, touch overlay, or boot configuration are needed. This app does not start automatically on boot.

## First launch

Tap and release each of the five calibration crosses. Use a plastic stylus or a careful fingertip press. Calibration handles swapped axes, inversion, and the existing screen rotation. It is saved in `/home/eli/cyberdeck-data/touch.json`.

If you miss a target or touch coordinates are inaccurate, tap CAL or restart with:

```bash
sudo python3 /home/eli/cyberdeck.py --calibrate
```

## Controls

- CAMERA: live view, letterboxed to preserve the complete camera frame.
- SNAP: saves the current 320x240 frame as a JPG without the interface. If paused, it saves the frozen frame. Photos go in `/home/eli/cyberdeck-data/photos/`, owned by the user who ran sudo.
- PAUSE / RESUME: freezes or resumes the displayed preview. The camera remains active; this is not a camera power-off control.
- FPS: cycles camera target rate through 10, 15, and 20 FPS. It starts at 20 unless `--fps 10` or `--fps 15` is supplied. This number is not measured panel refresh rate. Performance may be lower after drawing the interface and processing touch.
- STATUS: CPU utilisation/temperature, used/total RAM, free root-filesystem space, uptime, and the first global IPv4 address/interface. System readings update every second; address discovery every ten seconds. No network probes are sent.
- CAL: repeat touchscreen calibration.
- EXIT: quit and release camera and touch. Ctrl+C in the launching terminal also exits. The last frame can remain on the TFT afterward.

The application takes exclusive access to the ADS7846/XPT2046 input device while running so its taps do not click the desktop. It does not grab your keyboard or mouse, open a desktop preview, or switch virtual consoles. Desktop auto-keyboard settings remain separate; keep the on-screen keyboard disabled as discussed.

## Troubleshooting

- **Camera busy:** stop the previous camera app or `rpicam-hello` first. This app falls back to Status if camera startup fails; the terminal prints the reason. Restart after resolving it.
- **Touch busy:** exit `evtest` or another app using exclusive touch access.
- **No touchscreen found:** run `sudo evtest` and verify the ADS7846 device. To explicitly choose a device, use `--touch /dev/input/eventN` with the actual event number.
- **Only need system status:** run `sudo python3 /home/eli/cyberdeck.py --status-only`.
- **Colours:** uses the same BGR888 camera capture and RGB565 conversion as your working script. Keep your existing working `bgr` overlay setting.
- **Wrong framebuffer geometry:** app deliberately stops rather than writing into an unexpected display. Your existing 320x240, 16-bit `/dev/fb0` configuration is required.
- **Battery:** no battery sensor is configured, so the app does not invent a charge percentage.

## Verification and limitations

RGB565 byte ordering, rotated/inverted calibration, tap release handling, framebuffer row padding, and screen rendering were tested locally with simulated inputs. The UI was visually checked. Live camera, GPIO touch, and framebuffer operation still need to be verified on your Pi; this development machine has no connection to that hardware.

The included preview uses an illustration and sample system readings, not your camera or live Pi telemetry. Photo capture is preview resolution only in this version. There is no video recording, automatic startup, battery telemetry, or tear-free refresh guarantee.
