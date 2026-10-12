# Panel app: Options, clock, and health indicators

## Install this update

Update the whole checkout with Git, as in [INSTALL-AND-UPDATE.md](INSTALL-AND-UPDATE.md); don't copy `panel_app.py` on its own, because it needs `panel_tools/` beside it.

No reboot, dependency change, GPIO change, or reinstallation of the touchscreen-isolation setup is needed for this update. Previously saved photos and calibration remain in `~/cyberdeck-data/`.

## Options menu

OPTIONS replaces the old CAL tab. Calibration now requires **OPTIONS -> Calibrate touch -> START**. Cancel returns to Options. The command-line `--calibrate` override remains available if touch accuracy becomes too poor to navigate menus.

Available options:

| Control | Behaviour |
| --- | --- |
| Pixel level | Cycles 100%, 70%, 40%. Reduces RGB pixel intensity in software; it does not change backlight brightness or saved photos. |
| Idle blank | Off, 30 seconds, 60 seconds, or 300 seconds since the last accepted touch. Default is off. |
| Blank image now | Draws black immediately. The first subsequent tap wakes the image and is consumed, so it cannot click a hidden control. |
| Test colours | Full-intensity primary/secondary colour bars and a grey ramp. Tap to return. Ignores pixel dimming so it remains a useful diagnostic. |
| Calibrate touch | Opens confirmation before the five-target calibration. Calibration uses full intensity. |
| Graph | Cycles 30, 60, and 120 seconds of visible history. The vertical scale stays fixed at 0-100%. |

Pixel level, idle timeout, and graph duration are saved to `~/cyberdeck-data/options.json`. If saving fails, the changed option reverts and an error is shown. Invalid saved values revert to defaults on startup.

Your LED/backlight is connected directly to 3.3 V. Dimming pixels or drawing black **does not turn off the backlight or provide a true screen power-off mode**. The camera remains active while the image is blank. Actual backlight dimming would require identifying the board's LED circuit and changing the wiring/driver configuration; this update does not do that.

## Clock and status

- A 24-hour clock appears in the header, including the camera view.
- Date is shown as `YYYY/MM/DD` on Status and its Details page.
- Time is the Pi's system time and timezone, not an assumed location or GNSS time.
- STATUS retains CPU/RAM graphs, current readings, storage bar, and uptime.
- Tap DETAILS for timezone, system clock-synchronisation state, Wi-Fi signal strength when available, undervoltage warnings, active clock limits/throttling, prior alerts since boot, CPU frequency, and the first global IPv4 address/interface.
- Tap STATUS to return to the graphs.

Health commands run on a background worker about every ten seconds. Fast CPU/RAM data is sampled approximately once per second. No connectivity probes are sent, and an IP address is not labelled as proof of Internet access. Wi-Fi signal is reported in dBm, not an invented percentage. Missing commands, unsupported readings, or unavailable status are displayed as unknown/unavailable.

Firmware power information distinguishes active alerts from historical bits: "No undervoltage" with "Yes, since boot" means an alert occurred earlier, not that undervoltage is occurring now. "Clock limits" can report throttling, frequency capping, or the soft temperature limit. These are not battery-charge measurements. CPU frequency is the kernel-reported current frequency and is not a precision measurement.

Clock synchronisation comes from the system's `NTPSynchronized` property. If the Pi is offline or its time has not been set, do not assume the displayed time is correct merely because the clock is ticking. This app does not change system time or timezone.

## GNSS recommendation

It is useful to plan a future GNSS page around receiver connection, fix type, satellite counts, latitude/longitude, altitude, speed, and fix age. A mock interface can be built with clearly labelled sample data. Live integration should wait for the module so its connection, output protocol, update rate and real no-fix behaviour can be tested. No GPS location is inferred from Wi-Fi or IP address, and no fake GNSS data has been added in this release. A map or navigation view can follow once reliable fixes are available.

## Checks

Fourteen local tests passed, including the real application event loop with simulated touch: opening Options, calibration confirmation/cancellation, blanking, consuming the wake-up tap, and confirmed exit. Tests also cover settings validation/persistence, dimming without altering snapshot pixels, distinction between current/historical firmware flags, graphics, calibration, touch handling, and desktop isolation setup. Layouts were rendered and visually inspected. Camera/GPIO and desktop behaviour still need checking on the actual Pi.

The preview contains illustrative camera scenery and sample readings. The working framebuffer, colour order, 48 MHz SPI configuration, touchscreen isolation and 20 FPS camera target are preserved.

References: [Raspberry Pi firmware status flags](https://www.raspberrypi.com/documentation/computers/os.html#get_throttled), [desktop touchscreen isolation](https://wayland.freedesktop.org/libinput/doc/1.25.0/ignoring-devices.html).
