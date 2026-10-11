"""Check the hardware plan for pin, address and port clashes: python3 -m core.hardware

Reads config.txt (read only) for what the Pi has switched on today, adds the planned parts
from core/hardware/pinmap.py, and prints who uses what. Exits 1 when something clashes.
"""
import argparse
from pathlib import Path

from .pinmap import BASELINE, PLAN, USB_PORTS, from_config, report

CONFIGS = (Path('/boot/firmware/config.txt'), Path('/boot/config.txt'))


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python3 -m core.hardware', description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--config', help='config.txt to read (default: the Pi\'s own)')
    parser.add_argument('--installed-only', action='store_true',
                        help='check only what config.txt switches on, not the planned parts')
    parser.add_argument('--usb-ports', type=int, default=USB_PORTS,
                        help='USB ports available, counting a powered hub (default 4)')
    args = parser.parse_args(argv)
    path = Path(args.config) if args.config else next((p for p in CONFIGS if p.is_file()), None)
    if path:
        devices, warnings = from_config(path.read_text())
        source = str(path)
    else:
        devices, warnings = list(BASELINE), []
        source = 'built-in baseline (no config.txt found)'
    if not args.installed_only:
        devices += PLAN
    text, clashes = report(devices, warnings, args.usb_ports)
    print(f'Installed: {source}' + ('' if args.installed_only else '; plus planned parts'))
    print(text)
    return 1 if clashes else 0


if __name__ == '__main__':
    raise SystemExit(main())
