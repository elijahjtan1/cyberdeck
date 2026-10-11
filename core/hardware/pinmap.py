"""Who uses which GPIO, bus address, UART, PWM channel and USB port, and where two parts clash.

The installed side comes from the Pi's config.txt (read only) or, without it, a built-in
baseline. The planned side is PLAN below: the parts on the way, wired as docs/HARDWARE-MAP.md
recommends. Part numbers aren't final, so each planned part says which part it assumes.
"""
from collections import namedtuple
import re

# A claim on one resource. Claims that carry the same share tag coexist (every device on
# I2C1 uses GPIO2/3); a claim with share=None is exclusive.
Claim = namedtuple('Claim', 'resource share')
Clash = namedtuple('Clash', 'resource owners')

# Pi 4 header pins of each peripheral (BCM numbers), per the overlay defaults.
PERIPHERAL_PINS = {
    'i2c1': (2, 3), 'i2c3': (4, 5), 'i2c4': (6, 7), 'i2c5': (10, 11), 'i2c6': (22, 23),
    'spi0': (9, 10, 11), 'spi1': (19, 20, 21),
    'uart0': (14, 15), 'uart2': (0, 1), 'uart3': (4, 5), 'uart4': (8, 9), 'uart5': (12, 13),
}
SPI_CE_DEFAULTS = {'spi0': (8, 7), 'spi1': (18, 17, 16)}
# BCM2711 PWM0 on the header: channel 0 on GPIO12 or GPIO18, channel 1 on GPIO13 or GPIO19.
PWM_CHANNEL = {12: 0, 18: 0, 13: 1, 19: 1}
# BCM number -> physical header pin, for wiring.
PHYSICAL = {2: 3, 3: 5, 4: 7, 14: 8, 15: 10, 17: 11, 18: 12, 27: 13, 22: 15, 23: 16, 24: 18,
            10: 19, 9: 21, 25: 22, 11: 23, 8: 24, 7: 26, 0: 27, 1: 28, 5: 29, 6: 31, 12: 32,
            13: 33, 19: 35, 16: 36, 26: 37, 20: 38, 21: 40}
USB_PORTS = 4  # Two USB 3 and two USB 2 on a Pi 4, 1.2 A shared between them.


class Device:
    def __init__(self, name, claims, part='', note=''):
        self.name, self.claims, self.part, self.note = name, list(claims), part, note

    def __repr__(self):
        return f'Device({self.name!r})'


def gpio(n):
    return [Claim(f'GPIO{n}', None)]


def peripheral(name):
    """The pins of an I2C, SPI or UART block, shared by everything on that block."""
    return [Claim(f'GPIO{n}', name) for n in PERIPHERAL_PINS[name]]


def i2c(*addresses, bus='i2c1'):
    return peripheral(bus) + [Claim(f'{bus} 0x{a:02X}', None) for a in addresses]


def uart(name):
    return peripheral(name) + [Claim(name, None)]


def spi(bus, ce, cs_gpio=None):
    cs = SPI_CE_DEFAULTS[bus][ce] if cs_gpio is None else cs_gpio
    return peripheral(bus) + [Claim(f'GPIO{cs}', bus), Claim(f'{bus} CE{ce}', None)]


def pwm(n):
    return gpio(n) + [Claim(f'PWM0 channel {PWM_CHANNEL[n]}', None)]


def hdmi(n):
    return [Claim(f'HDMI{n}', None)]


def usb():
    return [Claim('USB port', 'usb')]


# What is on the deck today when config.txt isn't available. The TFT's DC and RESET pins and
# the touch interrupt aren't recorded in the repo; reading config.txt fills them in.
BASELINE = [
    Device('2.8" TFT', spi('spi0', 0), 'ILI9341 (fbtft)'),
    Device('Touch', spi('spi0', 1), 'XPT2046 / ADS7846'),
    Device('Backlight PWM', pwm(18), note='Only once setup_backlight.py has run'),
]

# The parts on order (October 2026) and the roadmap's Phase 6 parts, wired the recommended
# way. "(assumed)" marks a detail not confirmed from the part itself.
PLAN = [
    Device('GNSS', uart('uart0'), 'BZGNSS BZ-251, u-blox M10',
           'GPIO14/15. Needs dtoverlay=disable-bt and no serial console'),
    Device('GNSS PPS', gpio(12), 'Only if the module breaks out PPS (assumed)',
           'Plain input; GPIO12 is then not free for PWM'),
    Device('Compass', i2c(0x0D), 'QMC5883L, if the GNSS board has one (assumed)',
           'An HMC5883L would be 0x1E instead'),
    Device('Radar A', uart('uart3'), 'HLK-LD2450, 256000 baud',
           'GPIO4/5 via dtoverlay=uart3, so 1-wire moves to GPIO6'),
    Device('Radar B', usb(), 'HLK-LD2450 on a CP2102 USB-serial adapter',
           'No spare GPIO UART without giving up PWM or the HAT ID pins'),
    Device('RTC', i2c(0x68, 0x57), 'DS3231 module (roadmap 6.1)',
           '0x57 is the AT24C32 EEPROM most DS3231 boards carry'),
    Device('Power sensor', i2c(0x40), 'INA226 or INA219 (roadmap 6.3)'),
    Device('Temperature probes', gpio(6), 'DS18B20 on 1-wire',
           'dtoverlay=w1-gpio,gpiopin=6'),
    Device('Fan PWM', pwm(13), 'Power & Thermal design',
           'PWM0 channel 1; the backlight has channel 0. Needs dtoverlay=pwm-2chan'),
    Device('7" screen', hdmi(0) + usb(), '1024x600 IPS, HDMI with USB touch'),
    Device('5" screen', hdmi(1) + usb(), '1024x600, HDMI with USB touch'),
    Device('SDR', usb(), 'RTL-SDR Blog V4', 'A USB 2 port, away from the GNSS antenna'),
    Device('SSD', usb(), 'USB 3 SSD (roadmap 6.2)'),
    Device('ESP32', usb(), 'USB serial (roadmap 5.4)'),
    Device('Keyboard', usb(), 'iPazzPort 2.4 GHz, USB dongle'),
]


def check(devices, usb_ports=USB_PORTS):
    """Every resource claimed by two devices that can't share it, plus too many USB devices."""
    owners = {}
    for device in devices:
        for claim in device.claims:
            owners.setdefault(claim.resource, {}).setdefault(device.name, claim.share)
    clashes = []
    for resource, users in owners.items():
        if resource == 'USB port':
            continue
        shares = set(users.values())
        if len(users) > 1 and (None in shares or len(shares) > 1):
            clashes.append(Clash(resource, tuple(f'{name} ({share or "exclusive"})'
                                                 for name, share in users.items())))
    usb_users = list(owners.get('USB port', {}))
    if len(usb_users) > usb_ports:
        clashes.append(Clash(f'USB ports ({len(usb_users)} devices, {usb_ports} ports)',
                             tuple(usb_users)))
    return sorted(clashes, key=lambda c: _resource_key(c.resource))


# --- config.txt ---------------------------------------------------------------------------

RTC_ADDRESSES = {'ds1307': 0x68, 'ds3231': 0x68, 'pcf8523': 0x68, 'pcf8563': 0x51,
                 'rv3028': 0x52, 'mcp7940x': 0x6F}
# Overlays with a single pin parameter: (parameter name, default GPIO).
ONE_PIN = {'w1-gpio': ('gpiopin', 4), 'pps-gpio': ('gpiopin', 18), 'gpio-fan': ('gpiopin', 12),
           'gpio-shutdown': ('gpio_pin', 3), 'gpio-poweroff': ('gpiopin', 26)}
# Overlays that use no header pins.
NO_PINS = {'vc4-kms-v3d', 'vc4-fkms-v3d', 'dwc2', 'disable-bt', 'miniuart-bt', 'disable-wifi',
           'imx219', 'imx477', 'imx708', 'ov5647', 'hifiberry-dac', 'rpivid-v4l2'}
PIN_PARAM = re.compile(r'(pin|gpio|irq)', re.I)
SECTIONS = {'all', 'pi4', 'pi400', 'board-type=0x11'}  # Filters a Pi 4 obeys.


def from_config(text):
    """(devices, warnings) for what a config.txt switches on. Doesn't touch any hardware."""
    devices, warnings, active = [], [], True
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.split('#', 1)[0].strip()
        if not line:
            continue
        if line.startswith('['):
            active = line.strip('[]').strip().lower() in SECTIONS
            continue
        if not active or '=' not in line:
            continue
        key, value = (s.strip() for s in line.split('=', 1))
        if key == 'dtparam':
            devices += _dtparams(value, f'config line {number}: dtparam')
        elif key == 'dtoverlay' and value:
            device, warning = _overlay(value, f'config line {number}')
            devices += [device] if device else []
            warnings += [warning] if warning else []
        elif key == 'enable_uart' and value == '1':
            warnings.append('enable_uart=1: if cmdline.txt has console=serial0, a login console '
                            'is on GPIO14/15 and a GNSS there would be fed console text')
            devices.append(Device(f'config line {number}: enable_uart', peripheral('uart0')))
    return devices, warnings


def _dtparams(value, owner):
    out = []
    for item in value.split(','):
        name, _, setting = item.partition('=')
        if setting.strip().lower() not in ('on', 'true', 'yes', '1', ''):
            continue
        name = name.strip()
        if name in ('i2c_arm', 'i2c', 'i2c1'):
            out.append(Device(f'{owner} {name}', peripheral('i2c1')))
        elif name == 'spi':
            out.append(Device(f'{owner} {name}', peripheral('spi0') +
                              [Claim(f'GPIO{n}', 'spi0') for n in SPI_CE_DEFAULTS['spi0']]))
    return out


def _overlay(value, where):
    name, *params = [p.strip() for p in value.split(',')]
    kv = dict(p.split('=', 1) for p in params if '=' in p)
    flags = {p for p in params if '=' not in p}
    owner = f'{where}: {name}'
    claims, handled = [], set()

    def number(key, default=None):
        handled.add(key)
        try:
            return int(kv[key], 0) if key in kv else default
        except ValueError:
            return default

    if name in NO_PINS:
        return None, None
    if name in PERIPHERAL_PINS:  # uart2-5, i2c3-6
        claims += uart(name) if name.startswith('uart') else peripheral(name)
    elif name in ONE_PIN:
        claims += gpio(number(*ONE_PIN[name]))
    elif name in ('pwm', 'pwm-2chan'):
        claims += pwm(number('pin', 18))
        if name == 'pwm-2chan':
            claims += pwm(number('pin2', 19))
        handled.add('func')
    elif name == 'i2c-rtc':
        chips = [RTC_ADDRESSES[f] for f in flags if f in RTC_ADDRESSES]
        claims += i2c(*chips) if not flags & {'i2c0', 'i2c_csi_dsi'} else []
    elif name.startswith('spi1-') and name[5:6].isdigit():
        for ce in range(int(name[5])):
            claims += spi('spi1', ce, number(f'cs{ce}_pin', SPI_CE_DEFAULTS['spi1'][ce]))
    elif name == 'piscreen':
        claims += spi('spi0', 0) + spi('spi0', 1) + gpio(24) + gpio(25) + gpio(17)
    elif name == 'pitft28-resistive':
        claims += spi('spi0', 0) + spi('spi0', 1) + gpio(25) + gpio(24)
    elif name == 'ads7846':
        claims += spi('spi0', number('cs', 1))
    elif name == 'fbtft':
        for flag in flags:
            match = re.fullmatch(r'(spi[01])-([0-2])', flag)
            if match:
                claims += spi(match[1], int(match[2]))
    # Any remaining pin-like parameter (dc_pin, reset_pin, led_pin, penirq...) names a GPIO.
    for key in kv:
        if key not in handled and PIN_PARAM.search(key):
            n = number(key)
            if n is not None and 0 <= n <= 27:
                claims += gpio(n)
    if not claims:
        return None, f'{owner}: overlay not known to the checker, pins not checked'
    return Device(owner, claims), None


# --- report -------------------------------------------------------------------------------

def _resource_key(resource):
    match = re.match(r'GPIO(\d+)$', resource)
    return (0, int(match[1]), '') if match else (1, 0, resource)


def report(devices, warnings=(), usb_ports=USB_PORTS):
    """A plain-text table of every claim, then the clashes and warnings."""
    users = {}
    for device in devices:
        for claim in device.claims:
            if claim.resource != 'USB port':
                users.setdefault(claim.resource, []).append(device.name)
    lines = ['Resource       Pin  Used by']
    for resource in sorted(users, key=_resource_key):
        match = re.match(r'GPIO(\d+)$', resource)
        pin = str(PHYSICAL.get(int(match[1]), '')) if match else ''
        names = ', '.join(dict.fromkeys(users[resource]))
        lines.append(f'{resource:<14} {pin:>3}  {names}')
    usb_users = [d.name for d in devices if any(c.resource == 'USB port' for c in d.claims)]
    lines.append(f'USB ({len(usb_users)}/{usb_ports})       {", ".join(usb_users) or "-"}')
    clashes = check(devices, usb_ports)
    lines.append('')
    lines.append(f'{len(clashes)} clash(es)' if clashes else 'No clashes.')
    lines += [f'  CLASH {c.resource}: ' + ' vs '.join(c.owners) for c in clashes]
    lines += [f'  NOTE {w}' for w in warnings]
    return '\n'.join(lines), clashes
