# Hardware map: pins, buses and ports

Where every part plugs into the Pi 4, so nothing is wired onto a pin something else already
uses. It covers the deck today and the parts on order. **Nothing here changes the Pi.** Each
`config.txt` line below is applied by Eli when that part arrives, not before.

To check the plan against the Pi's real `config.txt` (read only, no root needed):

```bash
cd ~/cyberdeck-repo && python3 -m core.hardware
```

It lists who uses each GPIO (with its physical pin number), I2C address, UART, PWM channel and
USB port, then any clashes, and exits with status 1 if there are any. Options:
`--installed-only` checks just what `config.txt` switches on today; `--usb-ports 7` counts a
powered hub; `--config FILE` reads another copy of `config.txt`. The plan itself is `PLAN` in
[`core/hardware/pinmap.py`](../core/hardware/pinmap.py): when a part or a wire changes, change
it there and in this page.

## Parts

| Part | Model | Connects by | Notes |
| --- | --- | --- | --- |
| 2.8" TFT and touch | ILI9341 + XPT2046 | SPI0 CE0 and CE1 | Installed. DC, RESET and touch IRQ pins come from `config.txt` |
| Backlight | - | GPIO18, PWM0 channel 0 | Installed if `setup_backlight.py` was run |
| GNSS | BZGNSS BZ-251 (u-blox M10) | UART0, GPIO14/15 | Powered from 5 V. Whether this board has a compass is not confirmed yet |
| Compass | QMC5883L, if present | I2C1 0x0D | An HMC5883L would be 0x1E |
| Radar A | HLK-LD2450 | UART3, GPIO4/5 | 256000 baud, powered from 5 V. Tracks up to 3 moving targets (x, y, speed) |
| Radar B | HLK-LD2450 | CP2102 USB-serial adapter | No spare GPIO UART left without losing PWM or the HAT ID pins |
| SDR | RTL-SDR Blog V4 | USB 2 port | Receive only |
| 7" screen | 1024x600 IPS, capacitive | HDMI0 + USB (touch and power) | |
| 5" screen | 1024x600, capacitive | HDMI1 + USB (touch and power) | Check whether it mounts on the pin header (see Open questions) |
| Keyboard | iPazzPort 2.4 GHz | USB dongle | |
| ESP32 | - | USB serial | Roadmap 5.4 |
| SSD | - | USB 3 port | Roadmap 6.2 |
| RTC | DS3231 module | I2C1 0x68 (+ EEPROM 0x57) | Roadmap 6.1, not ordered yet |
| Power sensor | INA226 or INA219 | I2C1 0x40 | Roadmap 6.3, not ordered yet |
| Temperature probes | DS18B20 | 1-wire on GPIO6 | Power & Thermal design |
| Fan | - | GPIO13, PWM0 channel 1 | Reserved for a switchable fan. The case's dual fans run from the 5 V pins, always on |

## Header pins

Physical pin numbers are in brackets. Pins marked *from config* are confirmed only once
`config.txt` has been read.

| GPIO | Pin | Use |
| --- | --- | --- |
| 0, 1 | 27, 28 | HAT ID EEPROM. Left free |
| 2, 3 | 3, 5 | I2C1 SDA/SCL: compass, RTC, power sensor |
| 4, 5 | 7, 29 | UART3 TX/RX: radar A |
| 6 | 31 | 1-wire temperature probes |
| 7, 8 | 26, 24 | SPI0 CE1 (touch), CE0 (TFT) |
| 9, 10, 11 | 21, 19, 23 | SPI0 MISO, MOSI, SCLK |
| 12 | 32 | GNSS PPS, only if the board breaks it out |
| 13 | 33 | Fan PWM (reserved) |
| 14, 15 | 8, 10 | UART0 TX/RX: GNSS |
| 17 | 11 | Touch IRQ (usual wiring, *from config*) |
| 18 | 12 | Backlight PWM |
| 24, 25 | 18, 22 | TFT DC and RESET (usual wiring, *from config*) |
| 16, 19-23, 26, 27 | | Free |

5 V is on pins 2 and 4, ground on 6, 9, 14, 20, 25, 30, 34 and 39.

## Why it's wired this way

- **GNSS on UART0 (GPIO14/15).** On a Pi 4 the good UART (PL011) drives Bluetooth by default.
  `dtoverlay=disable-bt` gives it back to the header. The serial login console must also be
  off (`sudo raspi-config` → Interface → Serial: login shell *No*, hardware *Yes*), or the
  GNSS would receive console text.
- **Radar A on UART3 (GPIO4/5).** The other extra UARTs all collide with something: UART2 is
  on the HAT ID pins, UART4 on GPIO8/9 (the TFT's SPI), and UART5 on GPIO12/13 (PWM). UART3's
  GPIO4 is also the default 1-wire pin, so the temperature probes move to GPIO6.
- **Radar B over USB.** The CP2102 adapters handle 256000 baud, and nothing in `config.txt`
  changes. They're also the easiest way to record real radar and GNSS data from a laptop for
  the tests.
- **PWM.** GPIO12 and GPIO18 are the same PWM channel, and so are GPIO13 and GPIO19. The
  backlight has channel 0, so a PWM fan has to use GPIO13 or GPIO19, through
  `dtoverlay=pwm-2chan` (which replaces the backlight's single `pwm` line).
- **I2C addresses.** 0x0D compass, 0x40 power sensor, 0x57 RTC EEPROM, 0x68 RTC: no overlaps.
  An MPU6050 or MPU9250 motion sensor would collide with the RTC at 0x68 (it can move to 0x69).
- **Watch out for:** `dtoverlay=gpio-shutdown` defaults to GPIO3 (I2C clock) and
  `dtoverlay=pps-gpio` defaults to GPIO18 (the backlight). Always give them a pin. The checker
  flags both.

## USB, power and placement

- **A powered USB hub is needed.** The plan has 7 USB devices for 4 ports, and the Pi's ports
  share 1.2 A in total. Two screens powered over USB alone can need more than that.
- **5 V pins.** The header has two 5 V pins, and the case fans usually take them. The radar and
  GNSS also need 5 V, so they'll need a shared 5 V lead or the battery system's 5 V bus. This
  goes to the Power & Thermal design.
- **Logic levels.** The Pi's pins are 3.3 V only. The LD2450 uses 3.3 V serial. Measure the
  BZ-251's TX pin before connecting it: it should read about 3.3 V, not 5 V.
- **RF placement.** Keep the GNSS antenna, the compass and the SDR antenna away from the USB 3
  ports, the SSD and the battery wiring. USB 3 is noisy around 2.4 GHz and near the GNSS bands,
  and currents nearby skew a compass. Put the SDR on a USB 2 port; the 0.5 m USB extension
  helps move it away from the Pi.
- **Antennas.** The SMA needle antennas look like 2.4 GHz Wi-Fi antennas. The SDR needs an
  antenna cut for the band it listens to (for example a telescopic dipole).

## config.txt lines, per part

Applied by Eli when the part is wired, never by an installer without asking. Reboot after each.

| Part | Line |
| --- | --- |
| GNSS on UART0 | `dtoverlay=disable-bt` and `enable_uart=1`, serial console off |
| Radar A on UART3 | `dtoverlay=uart3` (appears as `/dev/ttyAMA3` or similar) |
| Compass, RTC, power sensor | `dtparam=i2c_arm=on` |
| RTC | `dtoverlay=i2c-rtc,ds3231` |
| Temperature probes | `dtoverlay=w1-gpio,gpiopin=6` |
| GNSS PPS | `dtoverlay=pps-gpio,gpiopin=12` |
| Fan PWM | `dtoverlay=pwm-2chan,pin=18,func=2,pin2=13,func2=4` in place of the backlight's `pwm` line |

## Open questions

- The DC, RESET and touch IRQ pins of the 2.8" panel: read from `config.txt`.
- Whether the BZ-251 has a compass, and whether it breaks out PPS.
- Whether the 5" screen mounts on the pin header. If it does, it covers the header and the
  radar and GNSS wiring has to go through a stacking header or a ribbon.
- The battery system and its sensors (Power & Thermal design).
