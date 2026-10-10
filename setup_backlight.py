#!/usr/bin/env python3
"""Let the panel app dim and switch off the TFT backlight with hardware PWM.

Wire the module's LED pin to a PWM-capable GPIO first (see docs/BACKLIGHT-PWM.md), then:

  sudo python3 setup_backlight.py            # GPIO18 (pin 12): adds dtoverlay=pwm, reboot
  sudo python3 setup_backlight.py --test     # after rebooting: steps the brightness
  sudo python3 setup_backlight.py --on       # light the panel without the app running
  sudo python3 setup_backlight.py --undo     # back to the always-on 3.3 V behaviour

Options: --gpio 12|13|18|19, --invert (active-low driver such as a PNP switch),
--frequency HZ (default 2000), --data DIR (default ~/cyberdeck-data).
Only lines this script added to config.txt are ever removed.
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

MARK = '# panel backlight (setup_backlight.py)'
# BCM2711 PWM0: channel 0 on GPIO12 (ALT0) / GPIO18 (ALT5); channel 1 on GPIO13 / GPIO19.
PINS = {12: (4, 0), 13: (4, 1), 18: (2, 0), 19: (2, 1)}


def config_path(root=Path('/')):
    for candidate in (root / 'boot/firmware/config.txt', root / 'boot/config.txt'):
        if candidate.is_file():
            return candidate
    raise RuntimeError('config.txt not found in /boot/firmware or /boot')


def overlay_line(gpio):
    func, _ = PINS[gpio]
    return f'dtoverlay=pwm,pin={gpio},func={func}'


def pins_in_use(text):
    """GPIO numbers named by other overlay parameters (dc/reset/led/penirq pins...)."""
    used = set()
    for line in text.splitlines():
        line = line.split('#', 1)[0].strip()
        if not line.startswith(('dtoverlay=', 'dtparam=')) or MARK in line:
            continue
        for name, number in re.findall(r'([A-Za-z_-]*(?:pin|irq|gpio|led)[A-Za-z_-]*)=(\d+)', line):
            if name not in ('func',):
                used.add(int(number))
    return used


def add_overlay(config, gpio, force=False):
    text = config.read_text()
    wanted = overlay_line(gpio)
    lines = text.splitlines()
    ours = [i for i, line in enumerate(lines) if line.strip() == MARK]
    existing = [line.strip() for line in lines
                if re.match(r'\s*dtoverlay=pwm(-2chan)?\b', line)]
    if ours:
        current = lines[ours[0]+1].strip() if ours[0]+1 < len(lines) else ''
        if current == wanted:
            return False  # Already installed.
        raise RuntimeError(f'config.txt already has {current!r} from this script; run --undo first')
    if existing:
        raise RuntimeError(f'config.txt already enables PWM ({existing[0]}); left unchanged')
    if gpio in pins_in_use(text) and not force:
        raise RuntimeError(f'GPIO{gpio} appears in another overlay line of {config}; pick '
                           'another --gpio or check your wiring (use --force if you are sure)')
    if not text.endswith('\n'):
        text += '\n'
    backup = config.with_name(config.name + '.before-backlight')
    if not backup.exists():
        backup.write_text(config.read_text())
    config.write_text(text + f'\n{MARK}\n{wanted}\n')
    return True


def remove_overlay(config):
    lines = config.read_text().splitlines(keepends=True)
    out, skip = [], False
    for line in lines:
        if line.strip() == MARK:
            skip = True
            if out and not out[-1].strip():
                out.pop()  # The blank separator line we added.
            continue
        if skip and line.strip().startswith('dtoverlay=pwm'):
            skip = False
            continue
        skip = False
        out.append(line)
    changed = len(out) != len(lines)
    if changed:
        config.write_text(''.join(out))
    return changed


def default_data():
    if os.environ.get('SUDO_UID'):
        import pwd
        return Path(pwd.getpwuid(int(os.environ['SUDO_UID'])).pw_dir) / 'cyberdeck-data'
    return Path.home() / 'cyberdeck-data'


def write_settings(data, gpio, frequency, invert, chip=0):
    data.mkdir(parents=True, exist_ok=True)
    settings = {'chip': chip, 'channel': PINS[gpio][1], 'frequency': frequency,
                'invert': invert, 'gpio': gpio}
    path = data / 'backlight.json'
    path.write_text(json.dumps(settings, indent=2) + '\n')
    if os.environ.get('SUDO_UID'):
        os.chown(path, int(os.environ['SUDO_UID']), int(os.environ['SUDO_GID']))
    return path


def open_backlight(data):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from panel_tools.backlight import load_backlight
    light = load_backlight(data)
    if not light.available:
        raise RuntimeError('PWM backlight not available yet. Reboot after setup, then retry.')
    return light


def run_test(data):
    light = open_backlight(data)
    for level in (100, 70, 40, 15, 5, 0, 100):
        print(f'Backlight {level}%', flush=True)
        light.set(level)
        time.sleep(1.5)
    print('If the screen dimmed in steps and went dark at 0%, the wiring works.')


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--gpio', type=int, choices=sorted(PINS), default=18)
    parser.add_argument('--frequency', type=int, default=2000)
    parser.add_argument('--invert', action='store_true', help='active-low driver (e.g. PNP)')
    parser.add_argument('--data', help='panel data folder (default ~/cyberdeck-data)')
    parser.add_argument('--test', action='store_true', help='step the brightness to check wiring')
    parser.add_argument('--on', action='store_true', help='set the backlight to 100%% and exit')
    parser.add_argument('--undo', action='store_true', help='remove PWM backlight control')
    parser.add_argument('--force', action='store_true', help='skip the pin-in-use check')
    args = parser.parse_args()
    if not hasattr(os, 'geteuid') or os.geteuid() != 0:
        raise RuntimeError('Run this on the Pi using sudo.')
    data = Path(args.data).expanduser() if args.data else default_data()
    if args.test:
        run_test(data)
        return
    if args.on:
        open_backlight(data).set(100)
        print('Backlight on (stays on until the app or a reboot changes it).')
        return
    config = config_path()
    if args.undo:
        changed = remove_overlay(config)
        (data / 'backlight.json').unlink(missing_ok=True)
        print('PWM backlight control removed.' + (' Reboot to release the pin.' if changed else ''))
        print('Rewire LED back to 3.3 V if you want it always on without the app.')
        return
    if not 100 <= args.frequency <= 50000:
        raise RuntimeError('--frequency must be between 100 and 50000 Hz')
    added = add_overlay(config, args.gpio, args.force)
    path = write_settings(data, args.gpio, args.frequency, args.invert)
    print(f'Backlight settings: {path}')
    if added:
        print(f'Added "{overlay_line(args.gpio)}" to {config}. Reboot, then run --test.')
    else:
        print('config.txt already set up. Run with --test to check the wiring.')
    print('Restart the panel app (or its service) to use the new settings.')


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError) as exc:
        raise SystemExit(f'Backlight setup stopped: {exc}')
