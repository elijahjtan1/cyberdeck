#!/usr/bin/env python3
"""Dedicate the ADS7846 touchscreen to the panel, leaving evdev access available.

Install: sudo python3 setup_panel_touch.py
Undo:    sudo python3 setup_panel_touch.py --undo
Reboot after either operation. No drivers, GPIO settings, or desktop devices
other than the named ADS7846 touchscreen are changed.
"""
import argparse
import os
from pathlib import Path

DEVICE = 'ADS7846 Touchscreen'
RULE = '''# Panel app touchscreen isolation (managed by setup_panel_touch.py)
SUBSYSTEM=="input", KERNEL=="event*", ATTRS{name}=="ADS7846 Touchscreen", ENV{LIBINPUT_IGNORE_DEVICE}="1"
'''
XORG = '''# Panel app touchscreen isolation (managed by setup_panel_touch.py)
Section "InputClass"
    Identifier "Panel app dedicated ADS7846 touchscreen"
    MatchProduct "ADS7846 Touchscreen"
    MatchDevicePath "/dev/input/event*"
    Option "Ignore" "true"
EndSection
'''


def settings(root=Path('/')):
    return {
        root / 'etc/udev/rules.d/99-panel-touch.rules': RULE,
        root / 'etc/X11/xorg.conf.d/99-panel-touch.conf': XORG,
    }


def apply(files, undo=False):
    # Preflight all files before making a change. Never replace someone else's config.
    for path, content in files.items():
        if path.is_symlink() or (path.exists() and path.read_text() != content):
            raise RuntimeError(f'{path} already contains different settings; left unchanged.')
    changed = []
    try:
        for path, content in files.items():
            if undo:
                if path.exists():
                    path.unlink()
            elif not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open('x') as output:
                    output.write(content)
                changed.append(path)
                path.chmod(0o644)
    except Exception:
        # Only roll back newly created files, never an existing installation.
        if not undo:
            for path in changed:
                path.unlink(missing_ok=True)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--undo', action='store_true')
    args = parser.parse_args()
    if not hasattr(os, 'geteuid') or os.geteuid() != 0:
        raise RuntimeError('Run this on the Pi using sudo.')
    if not args.undo:
        names = []
        for path in Path('/sys/class/input').glob('event*/device/name'):
            try:
                names.append(path.read_text().strip())
            except OSError:
                continue
        if DEVICE not in names:
            raise RuntimeError(f'{DEVICE} not detected. Nothing changed. Check sudo evtest.')
    apply(settings(), args.undo)
    print('Desktop touch restored.' if args.undo else
          'ADS7846 touch dedicated to panel apps; direct evdev access remains enabled.')
    print('Reboot the Pi to apply: sudo reboot')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        raise SystemExit(f'Touch setup stopped: {exc}')
