"""Hardware backlight dimming through the kernel's sysfs PWM interface.

Enabled only by an explicit <data>/backlight.json written by setup_backlight.py, e.g.
{"chip": 0, "channel": 0, "frequency": 2000, "invert": false}
Without that file the app keeps its old behaviour (pixel dimming, backlight always on).
"""
import json
import time
from pathlib import Path

PWM_ROOT = Path('/sys/class/pwm')


def duty_fraction(percent, gamma=2.0, floor=.02):
    """Perceptual mapping: 70% looks about 70% as bright; any level above 0 stays visible."""
    percent = max(0, min(100, percent))
    if percent == 0:
        return 0.
    return max(floor, (percent/100) ** gamma)


class PwmBacklight:
    available = True

    def __init__(self, chip=0, channel=0, frequency=2000, invert=False, root=PWM_ROOT,
                 wait=10., sleep=time.sleep):
        """wait: seconds to wait for the PWM device. At boot the panel service can start
        before the kernel has loaded the PWM driver; giving up at once left the backlight
        dark (the pin idles low) for the whole run."""
        if not 100 <= int(frequency) <= 50000:
            raise ValueError(f'frequency must be 100-50000 Hz, not {frequency}')
        self.chip = Path(root) / f'pwmchip{int(chip)}'
        self.pwm = self.chip / f'pwm{int(channel)}'
        self.period = round(1e9 / int(frequency))
        self.invert = bool(invert)
        self.level = None
        self.duty = None
        for _ in range(max(1, round(wait/.2))):
            if self.chip.is_dir():
                break
            sleep(.2)
        if not self.chip.is_dir():
            raise RuntimeError(f'{self.chip} not found: is dtoverlay=pwm in config.txt and rebooted?')
        if not self.pwm.is_dir():
            try:
                (self.chip / 'export').write_text(str(int(channel)))
            except OSError:
                pass  # Already exported by an earlier run is fine.
        for _ in range(250):  # udev can take a moment (longer while booting) to add the files.
            if (self.pwm / 'period').exists():
                break
            sleep(.02)
        # Start fully lit, so the screen is readable from the first moment. Order matters:
        # the duty may never exceed the period while either changes.
        try:
            current = int((self.pwm / 'duty_cycle').read_text())
        except (OSError, ValueError):
            current = 0
        if current > self.period:
            self._write('duty_cycle', 0)
        self._write('period', self.period)
        self.duty = self.duty_for(100)
        self._write('duty_cycle', self.duty)
        self._write('enable', 1)
        self.level = 100

    def duty_for(self, percent):
        duty = round(self.period * duty_fraction(percent))
        if self.invert:  # Active-low driver (e.g. a PNP high-side switch).
            duty = self.period - duty
        return duty

    def _write(self, name, value):
        (self.pwm / name).write_text(str(int(value)))

    def set(self, percent):
        """0 = off, 100 = full. Repeated values do not touch sysfs."""
        if percent == self.level:
            return
        duty = self.duty_for(percent)
        if duty != self.duty:
            self._write('duty_cycle', duty)
            self.duty = duty
        self.level = percent

    def close(self, percent=100):
        """Leave the panel lit so 'not running' and shutdown messages stay readable."""
        try:
            self.set(percent)
        except OSError:
            pass


class NoBacklight:
    """Backlight wired to 3.3 V: nothing to control; the app dims pixels instead."""
    available = False
    level = 100

    def set(self, percent):
        self.level = percent

    def close(self, percent=100):
        pass


def load_backlight(data, root=PWM_ROOT, log=print, wait=10.):
    path = Path(data) / 'backlight.json'
    if not path.is_file():
        return NoBacklight()
    try:
        settings = json.loads(path.read_text())
        if not isinstance(settings, dict):
            raise ValueError('expected a JSON object')
        return PwmBacklight(settings.get('chip', 0), settings.get('channel', 0),
                            settings.get('frequency', 2000), settings.get('invert', False), root,
                            wait=wait)
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        log(f'PWM backlight unavailable ({exc}); using pixel dimming instead.')
        return NoBacklight()
