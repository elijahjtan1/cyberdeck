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
| `telemetry` | E->P | Every 5 seconds, and at once when `charging` changes |
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
```

Each side sends one every **2 s**. The other side counts as **gone** after **6 s** (three
missed beats) without any message from it. The Pi sends heartbeats from `cyberdeck-core`,
so they stop when the backend stops, including during shutdown.

### telemetry

```json
{"v": 1, "type": "telemetry", "seq": 7, "simulated": true, "battery_v": 7.42, "battery_pct": 81, "battery_current_ma": -1350, "charging": false, "input_v": null, "rail_5v_v": 5.08, "temp_c": 31.5}
```

| Field | Type | Meaning |
| --- | --- | --- |
| `simulated` | boolean, **required** | `true` for invented data (the mock). The Pi labels every value from such a message **SIMULATED** wherever it shows it, and refuses telemetry without this field |
| `battery_v` | number or null | Pack voltage, volts |
| `battery_pct` | integer 0-100 or null | The ESP32's state-of-charge estimate |
| `battery_current_ma` | integer or null | Pack current, milliamps: positive while charging, negative while discharging |
| `charging` | boolean or null | The charger reports it is charging |
| `input_v` | number or null | Charger or USB-C input voltage; null when nothing is plugged in or there is no sensor |
| `rail_5v_v` | number or null | The Pi's 5 V supply rail, volts |
| `temp_c` | number or null | Battery or board temperature, degrees Celsius |

A field is `null` (or missing) when the board has no sensor for it. The Pi never fills one
in: a missing reading is shown as missing.

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
| `reason` | `battery_low`, `battery_critical`, `button`, `user` (chosen on the Pi), `temperature` |
| `id` | Request number chosen by the requester; the ack repeats it |
| `accept` | In the ack: `true`, or `false` with a `detail` string (for example "camera is recording") |

## Shutdown

Only the ESP32 can cut the Pi's power, and it does so only when **both** are true:

1. **A shutdown was acknowledged.** Either the ESP32 sent `shutdown_request` and the Pi
   answered `shutdown_ack` with `accept: true`, or the Pi sent `shutdown_request` and the
   ESP32 answered `shutdown_ack` with `accept: true`.
2. **The Pi's heartbeat has stopped:** no message from the Pi for 6 s after the ack.

It then waits a further **grace period** before cutting power, because the backend stops
early in the shutdown and the SD card may still be writing:

- If the board has the optional **power-off signal** (a Pi GPIO set by
  `dtoverlay=gpio-poweroff`, which goes active only when the Pi has finished shutting
  down), it waits for that signal, up to 60 s.
- Otherwise it waits a fixed **30 s** after the heartbeat stopped.

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

### Example: low battery

```
E->P {"v":1,"type":"telemetry","seq":120,"simulated":false,"battery_v":6.41,"battery_pct":4,"battery_current_ma":-1200,"charging":false,"input_v":null,"rail_5v_v":5.02,"temp_c":29.0}
E->P {"v":1,"type":"shutdown_request","seq":121,"reason":"battery_low","id":1}
P->E {"v":1,"type":"shutdown_ack","seq":310,"id":1,"accept":true}
P->E {"v":1,"type":"heartbeat","seq":311}
     (backend stops: no more Pi messages)
     ESP32: 6 s without a Pi message -> wait for the power-off signal (or 30 s) -> cut power
```

## Mock ESP32

For testing without the board, roadmap 5.5 adds a mock that opens a pseudo-terminal and
speaks this protocol: `hello` with `"simulated": true`, heartbeats, telemetry with
`"simulated": true` and a slowly draining battery, and on request a `shutdown_request`. It
never touches real power. The Pi side is then pointed at the pty instead of
`/dev/ttyACM0`.

## Open questions

- Battery thresholds for `battery_low` and `battery_critical` depend on the cell chemistry
  and pack, and are set in the ESP32 firmware once the battery is chosen.
- Which Pi GPIO carries the power-off signal is decided with the hardware pin map.
