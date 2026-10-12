# ESP32 link protocol (version 1)

How the Pi and an ESP32 power and input board talk to each other. The ESP32 watches the
battery, reads buttons and switches the Pi's power; the Pi shows what it reports and shuts
down cleanly when asked. This file is the contract both sides are written against: the Pi
side lives in `core/esp32.py` (roadmap 5.5), and until the board exists a mock ESP32 sends
**SIMULATED** data.

The one rule that matters most: **the ESP32 cuts the Pi's power only after a poweroff has
been acknowledged and the Pi's heartbeat has stopped soon after**, or when the Pi has
provably halted, when the power button is held 10 s, or (as a last resort, pending Eli's
approval) when a cell is critically low (see [Shutdown](#shutdown)).

## Transport

- **Link:** the ESP32's USB serial port (USB CDC, or its USB-to-UART chip) plugged into a
  Pi USB port. It appears on the Pi as `/dev/ttyACM0` or `/dev/ttyUSB0`; a udev rule can
  give it a stable name later. USB only: the Pi's GPIO UART (GPIO 14/15) is taken by the
  GPS receiver in the wiring map.
- **Settings:** 115200 baud, 8 data bits, no parity, 1 stop bit, no flow control. On
  native USB CDC the baud rate is ignored.
- **Framing:** one JSON object per line, UTF-8, ending in `\n` (a `\r` before it is
  ignored). At most 512 bytes per line including the newline. The firmware's own log output
  goes to another UART, not this port.
- **Robustness:** a receiver skips lines that are too long, aren't valid JSON, aren't an
  object, or lack `v` or `type`, and keeps reading. After a reset or reconnect it drops any
  partial line it was holding. Boot messages from the ESP32 ROM and stray bytes after a
  reset are therefore harmless. Unknown `type`s and unknown fields are ignored, so either
  side can add fields without breaking the other.

### A reset must never cut power

The ESP32 can reset at any time: a brownout when a rail switches, its watchdog, flashing,
or the auto-reset circuit on boards with a USB-to-UART chip, which pulses DTR/RTS when the
port is opened. So:

- **Hardware:** the `pi` rail's load switch is ON by default (pulled on) while the ESP32 is
  in reset or unpowered. The ESP32 must drive it actively to turn it off.
- **Opening the port:** the Pi opens it without toggling DTR or RTS (HUPCL off), or the
  board has no auto-reset circuit.
- **State of charge:** the ESP32 keeps its charge count in NVS or RTC memory, so a reset
  doesn't lose it.

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
| `features` | What the ESP32 can do: `battery`, `buttons`, `power_switch`, `halt_signal` (the power-off signal is wired; a firmware setting, see [Shutdown](#shutdown)). The Pi doesn't expect telemetry from a board without `battery` |
| `simulated` | `true` when the sender is the mock. Everything from a simulated board is labelled **SIMULATED**. Required in the ESP32's `hello`; on other messages a missing `simulated` means the board's `hello` decides |
| `last_cut` | Optional, ESP32 only: why the ESP32 last cut the Pi's power, kept in NVS (`acked`, `halted`, `forced`, `critical`), so the Pi can log a hard cut after it powers back on |
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
temperature, or null if it can't be read) for a fan the ESP32 drives, if it drives one.
The switchable fan reserved on GPIO13 in [HARDWARE-MAP.md](HARDWARE-MAP.md) is driven by the
Pi; a given fan is run by one side only. The other side counts as **gone** after **6 s**
(three missed beats) without any message from it. The Pi sends heartbeats from `cyberdeck-core`,
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
| `shed_tier` | integer 0-2 or null | Load-shedding step in force: 0 none, 1 non-essential apps off, 2 hub and large screens off as well (see [Low battery](#low-battery)) |

A field is `null` (or missing) when the board has no sensor for it. The Pi never fills one
in: a missing reading is shown as missing. Readers ignore fields they don't know, so a board
with fewer sensors simply sends fewer.

### button

```json
{"v": 1, "type": "button", "seq": 12, "id": "power", "action": "long", "simulated": false}
```

| Field | Meaning |
| --- | --- |
| `id` | `power`, or `user1`, `user2`, ... for other buttons |
| `action` | `press` (pressed and released quickly), `long` (held 2 s), `down`, `up` |
| `simulated` | `true` when the mock invented the press |

The ESP32 decides `press` and `long`; `down` and `up` are optional extras for buttons the
Pi wants to track itself. A `long` press on `power` means "shut down": the ESP32 follows the
`button` message with a `shutdown_request` with reason `button`, so the usual ack applies.

### shutdown_request and shutdown_ack

```json
{"v": 1, "type": "shutdown_request", "seq": 30, "reason": "battery_low", "id": 3, "simulated": false}
{"v": 1, "type": "shutdown_ack", "seq": 88, "id": 3, "accept": true}
```

| Field | Meaning |
| --- | --- |
| `reason` | `battery_low`, `battery_critical`, `button`, `user` (chosen on the Pi), `temperature` (pack too hot) |
| `id` | Request number chosen by the requester; the ack repeats it |
| `simulated` | In a request from the ESP32: `true` when it comes from the mock |
| `accept` | In the ack: `true`, or `false` with a `detail` string (for example "camera is recording") |

## Shutdown

Shutdown requests and acks are for **poweroff only**. A reboot sends neither: it shows up
as missing heartbeats and then a new `hello`, and with no ack the ESP32 keeps power on.

The ESP32 cuts the Pi's power in exactly four cases.

**1. An acknowledged poweroff.** All three must hold:

1. **A shutdown was acknowledged.** Either the ESP32 sent `shutdown_request` and the Pi
   answered `shutdown_ack` with `accept: true`, or the Pi sent `shutdown_request` and the
   ESP32 answered `shutdown_ack` with `accept: true`.
2. **The heartbeat stopped soon after:** 6 s with no message from the Pi, starting within
   **60 s** of the first ack for that `id` (a re-sent ack doesn't restart it). If the Pi is still sending 60 s after the ack (poweroff failed or
   was held off), the shutdown is dropped: power stays on, and the ESP32 asks again with a
   new `id` if it still wants a shutdown. An ack never stays armed beyond that window.
3. **The wait passed** with no Pi message, because the backend stops early in the shutdown
   and the SD card may still be writing:
   - **With the power-off signal** (feature `halt_signal`), the ESP32 waits for the signal
     and then **10 s** more. There is no timed fallback: if the signal never comes, the Pi
     hasn't halted and power stays on.
   - **Without it,** the ESP32 waits a fixed **30 s** after the heartbeat stopped.

   Any Pi message during the wait (a heartbeat, or a `hello` after a reboot) cancels the
   shutdown and power stays on.

**2. A halted Pi.** With the power-off signal wired, the signal asserted and 6 s with no Pi
message prove the Pi has halted, so the ESP32 cuts power 10 s later without an ack. This
covers a Pi halted from the command line without an ack, and an ESP32 that reset
mid-shutdown and lost the ack.

**3. A forced cut.** Holding the physical `power` button for **10 s** cuts power
unconditionally and stores `last_cut: "forced"`. It is the way out for a hung Pi, or a Pi
halted without an ack on a board without the power-off signal. Being a deliberate physical
act, it is the one manual exception to the ack rule. (A `long` press at 2 s still only asks.)

**4. A critical battery** (last resort, pending Eli's approval). When any cell stays below
**2.9 V** for 10 s (the pack below 11.6 V on a board without cell readings), the ESP32 sends
`shutdown_request` with reason `battery_critical` and handles an ack as in case 1. If power
is still on **120 s** after the first request and the cell is still below 2.9 V, it cuts
power without an ack, stores `last_cut: "critical"` in NVS and reports it in its next
`hello`. This protects the pack from a hung or crashed Pi, a version mismatch, or an ESP32
reset mid-shutdown, all of which would otherwise drain it until the BMS trips at a damaging
2.0-2.5 V per cell. The 2.9 V figure has no load condition yet: cell voltage sags under the
SDR or the large screens, so it must be tuned on the real pack (for example, judged at light
load or with a longer hold time) so a load spike doesn't trip the cut.

### The power-off signal

GPIO 26 (physical pin 37) driven by `dtoverlay=gpio-poweroff,gpiopin=26`, which sets it
**high** only once the Pi has halted. It is **active-high**: don't add `active_low`. The
ESP32 input has a **pull-down**, so an unpowered or booting Pi reads low, never "halted".
Whether it is wired is a firmware setting on the ESP32, reported as the `halt_signal`
feature. The ESP32 **latches** the rising edge: on a Pi 4 with the EEPROM setting
`POWER_OFF_ON_HALT=1`, 3V3 drops after halt and the line falls again, so keep
`POWER_OFF_ON_HALT=0` (the default) and check on the Pi that the line stays high after
halt. It needs the line in `/boot/firmware/config.txt`, which Eli applies. It is the
preferred wiring: a missing heartbeat alone could be a hang, not a halt.

### Requests, acks and ids

- **Unanswered request.** The requester repeats `shutdown_request` with the same `id` every
  5 s until it gets an ack or the link drops.
- **Repeats.** The receiver answers every repeat of an `id` it has already acked with the
  same ack, and doesn't start poweroff twice.
- **Ids.** Each new request, including a re-ask after a decline or a dropped shutdown, uses
  a new `id`. An ack whose `id` doesn't match the pending request is ignored. A restart of
  either side is detected by its `hello`, not by `seq` going back to 0.
- **Declined.** After `accept: false` the ESP32 keeps power on. For `battery_low` it may ask
  again after 60 s; for `battery_critical` it asks again every 10 s, and the hard cut above
  still applies.
- **The Pi asks first.** When the user powers off from the panel or the command line, the Pi
  sends `shutdown_request` with reason `user`, waits up to 5 s for the ack, then powers off
  either way. Without the ack the ESP32 cuts power only once the power-off signal shows the
  Pi has halted (case 2); without the signal the Pi stays powered until the button is held
  10 s (case 3).
- **Charger plugged in.** An acknowledged shutdown still completes if a charger is plugged
  in during it.

### On the Pi

Accepting a real request means: send `shutdown_ack`, show "Shutting down (battery low)" or
similar on the panel, then run the normal poweroff (`systemctl poweroff`).

A request from a **simulated** board never powers the Pi off. It counts as simulated if
either the board's `hello` or the request carries `"simulated": true`. The Pi shows
"SIMULATED shutdown request (battery low)", logs it, and answers `accept: false` with
`detail` "simulated". Simulated button presses are labelled **SIMULATED** too, and a long
press on a simulated `power` button does not shut down.

### Power on

Whenever the ESP32 cuts the Pi's power it stores "rail off" and the reason in NVS, and
checks that record first thing at every boot, before releasing the rail. So a watchdog
reset, a brownout (likely at low battery) or a reflash after a cut doesn't power the Pi
back on, and a low battery can't loop through boot, request, cut and reset.

The ESP32 switches the `pi` rail on:

- at a cold power-up with no stored cut;
- on a `press` of the `power` button, unless the pack is below the `battery_low` level;
- after a `battery_low` or `battery_critical` cut, also on its own once the charge is
  above 20% and the charger is charging;
- after a `temperature` cut, only on a button press once `battery_temp_c` is below 45;
- after a `user`, `button` or `forced` cut, only on a button press.

Switching the rail on clears the stored record. Because the rail defaults ON while the
ESP32 is in reset, a reset after a cut gives the Pi a brief power pulse before the firmware
turns the rail off again. The load switch's turn-on delay must be longer than the ESP32's
reset-to-GPIO time (tens of milliseconds) so the Pi doesn't start booting in that pulse.

### Low battery

The ESP32 decides when to shed load and when to ask for a shutdown. Starting thresholds,
all set in the firmware, with hysteresis so they don't flap (proposed by the Power &
Thermal Engineer; the battery isn't chosen yet, so they will be tuned from measurements):

| Charge | Action | Undone at |
| --- | --- | --- |
| below 40% | `shed_tier` 1: the Pi turns off non-essential apps (games, camera, timelapse) and switches to its low-power profile | above 45% |
| below 25% | `shed_tier` 2: the ESP32 also switches off the `hub` and `screens` rails | above 30% |
| below 10%, or pack below 12.0 V at light load | `shutdown_request` with reason `battery_low` | See [Power on](#power-on) |
| any cell below 2.9 V for 10 s (pack below 11.6 V without cell readings) | `shutdown_request` with reason `battery_critical`, then the hard cut in [Shutdown](#shutdown) (pending Eli's approval) | See [Power on](#power-on) |
| `battery_temp_c` above 60 | `shutdown_request` with reason `temperature` | See [Power on](#power-on) |

Before switching a rail off, the ESP32 sends `telemetry` with the new `shed_tier` (and
`rails` still showing the rail on), then waits **5 s**, so the Pi can stop whatever uses it
and the panel can say what is being turned off and why.

### Example: low battery

```
E->P {"v":1,"type":"telemetry","seq":120,"simulated":false,"battery_v":12.4,"battery_pct":9,"pct_source":"coulomb","battery_current_ma":-300,"charging":false,"shed_tier":2}
E->P {"v":1,"type":"shutdown_request","seq":121,"reason":"battery_low","id":1,"simulated":false}
P->E {"v":1,"type":"shutdown_ack","seq":310,"id":1,"accept":true}
P->E {"v":1,"type":"heartbeat","seq":311}
     (backend stops: no more Pi messages)
     ESP32: 6 s without a Pi message (within 60 s of the ack) -> power-off signal + 10 s (or 30 s without it) -> cut power
```

## Mock ESP32

For testing without the board, roadmap 5.5 adds a mock that opens a pseudo-terminal and
speaks this protocol: `hello` with `"simulated": true`, heartbeats, telemetry with
`"simulated": true` and a slowly draining battery, and on request a `shutdown_request`
with `"simulated": true`. It never touches real power, and the Pi never shuts down for it
(see [Shutdown](#shutdown)). The Pi side is then pointed at the pty instead of
`/dev/ttyACM0`.

## Open questions

- The thresholds above assume a 4S LiFePO4 pack, proposed in the Power & Thermal
  Engineer's design notes (not yet in the repo) and not yet approved by Eli; they move
  with the pack.
- The critical-battery hard cut is Eli's decision. If Eli declines it, the BMS alone guards
  against deep discharge, and a hung or halted Pi can drain the pack to its cutoff.
