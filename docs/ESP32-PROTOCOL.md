# ESP32 link protocol (version 1)

How the Pi and an ESP32 power and input board talk to each other. The ESP32 watches the
battery, reads buttons and switches the Pi's power; the Pi shows what it reports and shuts
down cleanly when asked. This file is the contract both sides are written against: the Pi
side lives in `core/esp32.py` (roadmap 5.5), and until the board exists a mock ESP32 sends
**SIMULATED** data.

The one rule that matters most: **the ESP32 cuts the Pi's power only after a shutdown has
been acknowledged and the Pi's heartbeat has stopped** (see [Shutdown](#shutdown)).

## Transport

- **Link:** the ESP32's USB serial port (USB CDC, or its USB-to-UART chip) plugged into a
  Pi USB port. It appears on the Pi as `/dev/ttyACM0` or `/dev/ttyUSB0`; a udev rule can
  give it a stable name later. The Pi's GPIO UART (`/dev/serial0`, GPIO 14/15) works the
  same way as a fallback.
- **Settings:** 115200 baud, 8 data bits, no parity, 1 stop bit, no flow control.
- **Framing:** one JSON object per line, UTF-8, ending in `\n` (a `\r` before it is
  ignored). At most 512 bytes per line including the newline.
- **Robustness:** a receiver skips lines that are too long, aren't valid JSON, aren't an
  object, or lack `v` or `type`, and keeps reading. Boot messages from the ESP32 ROM and
  stray bytes after a reset are therefore harmless. Unknown `type`s and unknown fields are
  ignored, so either side can add fields without breaking the other.

## Common fields

Every message has these fields:

| Field | Type | Meaning |
| --- | --- | --- |
| `v` | integer | Protocol version: `1` for this document |
| `type` | string | One of the message types below |
| `seq` | integer | Sender's message counter, starting at 0 after boot or reconnect and wrapping after 2^31 - 1. Lets the receiver spot gaps; replies don't need to match it |

Senders may add `t_ms` (milliseconds since the sender started) for debugging.

**Versions:** a change that existing readers can ignore (a new optional field or message)
keeps `v` at 1. A change that would make an old reader misbehave raises `v`. A side that
receives a `v` it doesn't support ignores everything except `hello` and `heartbeat`, and
the Pi shows "ESP32 protocol vN not supported" instead of any readings. Heartbeats keep
working across versions so that the shutdown rule always holds.

## Messages

Direction: **E->P** is ESP32 to Pi, **P->E** Pi to ESP32.

| Type | Direction | When |
| --- | --- | --- |
| `hello` | both | On connect, after a reset, and in reply to the other side's `hello` |
| `heartbeat` | both | Every 2 seconds |
| `telemetry` | E->P | Every 5 seconds, and at once when `charging`, `rails` or `shed_tier` changes |
| `button` | E->P | When a button is pressed, held or released |
| `shutdown_request` | both | When one side wants the Pi powered off |
| `shutdown_ack` | both | Reply to `shutdown_request` |

### hello

```json
{"v": 1, "type": "hello", "seq": 0, "role": "esp32", "fw": "0.1.0", "board": "esp32-s3", "features": ["battery", "buttons", "power_switch"], "simulated": false}
{"v": 1, "type": "hello", "seq": 0, "role": "pi", "sw": "cyberdeck-core 0.1"}
```

| Field | Meaning |
| --- | --- |
| `role` | `esp32` or `pi` |
| `fw` / `sw` | Firmware or software version, free text |
| `board` | ESP32 board, free text (optional) |
| `features` | What the ESP32 can do: `battery`, `buttons`, `power_switch`. The Pi doesn't expect telemetry from a board without `battery` |
| `simulated` | `true` when the sender is the mock. Everything from a simulated board is labelled **SIMULATED** |
| `reply` | `true` when this `hello` answers the other side's |

A side that receives `hello` answers with its own `hello` carrying `"reply": true`; a
`hello` with `"reply": true` is never answered, so the two can't ping-pong. Any `hello`
resets the receiver's view of the other side: sequence tracking, a pending shutdown, and the
last telemetry.

### heartbeat

```json
{"v": 1, "type": "heartbeat", "seq": 41}
{"v": 1, "type": "heartbeat", "seq": 207, "cpu_temp_c": 58.4}
```

Each side sends one every **2 s**. The Pi's heartbeat carries `cpu_temp_c` (the SoC
temperature, or null if it can't be read), because the ESP32 runs the fan curve.
 The other side counts as **gone** after **6 s** (three
missed beats) without any message from it. The Pi sends heartbeats from `cyberdeck-core`,
so they stop when the backend stops, including during shutdown.

### telemetry

```json
{"v": 1, "type": "telemetry", "seq": 7, "simulated": true, "battery_v": 13.12, "cell_v": [3.28, 3.28, 3.27, 3.29], "battery_pct": 81, "pct_source": "coulomb", "full_calibrated_at": 1791670000, "battery_current_ma": -650, "charging": false, "charge_blocked": null, "input_v": null, "rail_5v_v": 5.08, "battery_temp_c": 24.5, "board_temp_c": 31.0, "fan_pct": 0, "rails": {"pi": true, "hub": true, "screens": true}, "shed_tier": 0}
```

| Field | Type | Meaning |
| --- | --- | --- |
| `simulated` | boolean, **required** | `true` for invented data (the mock). The Pi labels every value from such a message **SIMULATED** wherever it shows it, and refuses telemetry without this field |
| `battery_v` | number or null | Pack voltage, volts |
| `cell_v` | array of numbers or null | Each cell's voltage, volts, from a BMS that reports them. On a LiFePO4 pack the total hides one weak cell |
| `battery_pct` | integer 0-100 or null | The ESP32's state-of-charge estimate. The ESP32 keeps it, because it stays powered while the Pi is off. The Pi shows it as an **estimate**, with the measured watts beside it |
| `pct_source` | `"coulomb"`, `"voltage"` or null | How `battery_pct` was worked out. LiFePO4 voltage is nearly flat, so the Pi labels a `voltage` estimate as rough |
| `full_calibrated_at` | integer or null | Unix time the charge count was last reset at a full charge; null if never. Without a clock the ESP32 sends null |
| `battery_current_ma` | integer or null | Pack current, milliamps: positive while charging, negative while discharging |
| `charging` | boolean or null | The charger reports it is charging |
| `charge_blocked` | string or null | Why charging is held off although power is present: `cold`, `hot` or `bms`; null when it isn't |
| `input_v` | number or null | Charger or solar input voltage; null when nothing is plugged in or there is no sensor |
| `rail_5v_v` | number or null | The Pi's 5 V supply rail, volts |
| `battery_temp_c` | number or null | Pack temperature from its sensor, degrees Celsius. It sets the charge limits, so it is never mixed with the board temperature |
| `board_temp_c` | number or null | ESP32 board or enclosure temperature, degrees Celsius |
| `fan_pct` | integer 0-100 or null | Fan speed the ESP32 is driving; null without a fan it controls |
| `rails` | object or null | Load switches, each true (on), false (off) or null (no switch): `pi`, `hub` (powered USB hub: SDR, SSD), `screens` (large screens) |
| `shed_tier` | integer 0-3 or null | Load-shedding step in force: 0 none, 3 non-essential apps off, 2 hub and large screens off as well (see [Low battery](#low-battery)) |

A field is `null` (or missing) when the board has no sensor for it. The Pi never fills one
in: a missing reading is shown as missing. Readers ignore fields they don't know, so a board
with fewer sensors simply sends fewer.

### button

```json
{"v": 1, "type": "button", "seq": 12, "id": "power", "action": "long"}
```

| Field | Meaning |
| --- | --- |
| `id` | `power`, or `user1`, `user2`, ... for other buttons |
| `action` | `press` (pressed and released quickly), `long` (held 2 s), `down`, `up` |

The ESP32 decides `press` and `long`; `down` and `up` are optional extras for buttons the
Pi wants to track itself. A `long` press on `power` means "shut down": the ESP32 follows the
`button` message with a `shutdown_request` with reason `button`, so the usual ack applies.

### shutdown_request and shutdown_ack

```json
{"v": 1, "type": "shutdown_request", "seq": 30, "reason": "battery_low", "id": 3}
{"v": 1, "type": "shutdown_ack", "seq": 88, "id": 3, "accept": true}
```

| Field | Meaning |
| --- | --- |
| `reason` | `battery_low`, `battery_critical`, `button`, `user` (chosen on the Pi), `temperature` (pack too hot) |
| `id` | Request number chosen by the requester; the ack repeats it |
| `accept` | In the ack: `true`, or `false` with a `detail` string (for example "camera is recording") |

## Shutdown

Only the ESP32 can cut the Pi's power, and it does so only when **both** are true:

1. **A shutdown was acknowledged.** Either the ESP32 sent `shutdown_request` and the Pi
   answered `shutdown_ack` with `accept: true`, or the Pi sent `shutdown_request` and the
   ESP32 answered `shutdown_ack` with `accept: true`.
2. **The Pi's heartbeat has stopped:** no message from the Pi for 6 s after the ack.

It then waits before cutting power, because the backend stops early in the shutdown and
the SD card may still be writing:

- If the board has the **power-off signal** (a Pi GPIO set by `dtoverlay=gpio-poweroff`,
  which goes active only once the Pi has halted), it waits for that signal and then
  **10 s** more. If the signal hasn't come 60 s after the heartbeat stopped, it falls back
  to the rule below. This is the preferred wiring: a missing heartbeat alone could be a
  hang, not a halt. It needs a line in `/boot/firmware/config.txt`, which Eli applies.
- Without the signal it waits a fixed **30 s** after the heartbeat stopped.

Everything else follows from those two conditions:

- **No ack, no cut.** A Pi whose heartbeat stops without an ack has crashed, hung or is
  rebooting; the ESP32 keeps power on. A reboot shows up as heartbeats and a new `hello`
  coming back.
- **Heartbeats came back.** If Pi messages resume during the wait (a `hello` after a
  reboot, or a heartbeat), the shutdown is cancelled and power stays on.
- **Declined.** After `accept: false` the ESP32 keeps power on. For `battery_low` it may ask
  again after 60 s; for `battery_critical` it asks again every 10 s.
- **Unanswered request.** The requester repeats `shutdown_request` (same `id`) every 5 s
  until it gets an ack or the link drops.
- **The Pi asks first.** When the user shuts down from the panel or the command line, the
  Pi sends `shutdown_request` with reason `user`, waits up to 5 s for the ack, then shuts
  down either way. Without the ack the ESP32 won't cut power, so the Pi simply halts and
  stays powered, which is safe.
- **Last resort.** Protecting a cell from deep discharge is the battery protection
  circuit's job, not this protocol's. The ESP32 never cuts power early to save the battery.

On the Pi, accepting a request means: send `shutdown_ack`, show "Shutting down (battery
low)" or similar on the panel, then run the normal shutdown (`systemctl poweroff`).

### Low battery

The ESP32 decides when to shed load and when to ask for a shutdown. Starting thresholds,
all set in the firmware, with hysteresis so they don't flap (from the power and thermal
draft; the battery isn't chosen yet, so they will be tuned from measurements):

| Charge | Action | Undone at |
| --- | --- | --- |
| below 40% | `shed_tier` 3: the Pi turns off non-essential apps (games, camera, timelapse) and switches to its low-power profile | above 45% |
| below 25% | `shed_tier` 2: the ESP32 also switches off the `hub` and `screens` rails | above 30% |
| below 10%, or pack below 12.0 V at light load | `shutdown_request` with reason `battery_low` | Power back on above 20% and charging |
| `battery_temp_c` above 60 | `shutdown_request` with reason `temperature` | |

Before switching a rail off, the ESP32 sends `telemetry` with the new `shed_tier` (and
`rails` still showing the rail on), then waits **5 s**, so the Pi can stop whatever uses it
and the panel can say what is being turned off and why.

### Example: low battery

```
E->P {"v":1,"type":"telemetry","seq":120,"simulated":false,"battery_v":12.4,"battery_pct":9,"pct_source":"coulomb","battery_current_ma":-300,"charging":false,"shed_tier":2}
E->P {"v":1,"type":"shutdown_request","seq":121,"reason":"battery_low","id":1}
P->E {"v":1,"type":"shutdown_ack","seq":310,"id":1,"accept":true}
P->E {"v":1,"type":"heartbeat","seq":311}
     (backend stops: no more Pi messages)
     ESP32: 6 s without a Pi message -> power-off signal + 10 s (or 30 s without it) -> cut power
```

## Mock ESP32

For testing without the board, roadmap 5.5 adds a mock that opens a pseudo-terminal and
speaks this protocol: `hello` with `"simulated": true`, heartbeats, telemetry with
`"simulated": true` and a slowly draining battery, and on request a `shutdown_request`. It
never touches real power. The Pi side is then pointed at the pty instead of
`/dev/ttyACM0`.

## Open questions

- The thresholds above assume the 4S LiFePO4 pack in the power and thermal draft, which
  Eli hasn't approved yet; they move with the pack. `battery_critical` is kept for a
  pack that falls fast under load.
- Which Pi GPIO carries the power-off signal is decided with the hardware pin map.
