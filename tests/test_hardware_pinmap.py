import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.hardware import pinmap  # noqa: E402
from core.hardware.__main__ import main  # noqa: E402
from core.hardware.pinmap import (  # noqa: E402
    Device, check, combine, from_config, gpio, i2c, pwm, uart)

# A typical cyberdeck config.txt: TFT on spi0 CE0, touch on CE1, backlight PWM on GPIO18.
DECK_CONFIG = '''
# comments and blank lines are ignored
dtparam=audio=on
dtparam=spi=on
dtparam=i2c_arm=on
dtoverlay=vc4-kms-v3d
dtoverlay=fbtft,spi0-0,ili9341,dc_pin=24,reset_pin=25,rotate=90,speed=32000000
dtoverlay=ads7846,cs=1,penirq=17,speed=50000,swapxy=1
dtoverlay=pwm,pin=18,func=2  # panel backlight (setup_backlight.py)
[pi5]
dtoverlay=uart4
[all]
'''


def resources(devices):
    return {c.resource for d in devices for c in d.claims}


class CheckTest(unittest.TestCase):
    def test_devices_on_one_bus_share_its_pins(self):
        self.assertEqual(check([Device('RTC', i2c(0x68)), Device('INA', i2c(0x40))]), [])

    def test_same_i2c_address_clashes(self):
        clashes = check([Device('RTC', i2c(0x68)), Device('IMU', i2c(0x68))])
        self.assertEqual([c.resource for c in clashes], ['i2c1 0x68'])

    def test_uart_pins_clash_with_other_functions(self):
        # uart4 sits on GPIO8/9, which the TFT needs for SPI0.
        devices = from_config(DECK_CONFIG)[0] + [Device('Radar', uart('uart4'))]
        self.assertEqual([c.resource for c in check(devices)], ['GPIO8', 'GPIO9'])

    def test_pwm_channel_is_shared_by_two_pins(self):
        # GPIO12 and GPIO18 are the same PWM channel, so the fan can't go on GPIO12.
        clashes = check([Device('Backlight', pwm(18)), Device('Fan', pwm(12))])
        self.assertEqual([c.resource for c in clashes], ['PWM0 channel 0'])
        self.assertEqual(check([Device('Backlight', pwm(18)), Device('Fan', pwm(13))]), [])

    def test_usb_ports_are_counted(self):
        devices = [Device(f'D{n}', pinmap.usb()) for n in range(5)]
        self.assertEqual(len(check(devices)), 1)
        self.assertEqual(check(devices, usb_ports=7), [])

    def test_plan_fits_the_deck_apart_from_usb_ports(self):
        devices = from_config(DECK_CONFIG)[0] + pinmap.PLAN
        self.assertEqual([c.resource for c in check(devices)],
                         ['USB ports (7 devices, 4 ports)'])
        self.assertEqual(check(devices, usb_ports=7), [])
        self.assertEqual(check(pinmap.BASELINE + pinmap.PLAN, usb_ports=7), [])


class ConfigTest(unittest.TestCase):
    def test_reads_the_panel_pins(self):
        devices, warnings = from_config(DECK_CONFIG)
        found = resources(devices)
        for name in ('GPIO24', 'GPIO25', 'GPIO17', 'GPIO18', 'PWM0 channel 0', 'spi0 CE0',
                     'spi0 CE1', 'GPIO2', 'GPIO7'):
            self.assertIn(name, found)
        self.assertNotIn('uart4', found)  # Only under [pi5].
        self.assertEqual(warnings, [])
        self.assertEqual(check(devices), [])

    def test_overlay_defaults(self):
        found = resources(from_config('dtoverlay=w1-gpio\ndtoverlay=pps-gpio\n')[0])
        self.assertEqual(found, {'GPIO4', 'GPIO18'})
        devices = from_config('dtoverlay=spi1-2cs,cs0_pin=16\n')[0]
        self.assertTrue({'GPIO16', 'GPIO17', 'GPIO19', 'spi1 CE1'} <= resources(devices))
        devices = from_config('dtoverlay=i2c-rtc,ds3231\n')[0]
        self.assertIn('i2c1 0x68', resources(devices))

    def test_gpio_shutdown_default_takes_i2c_clock(self):
        devices = from_config('dtparam=i2c_arm=on\ndtoverlay=gpio-shutdown\n')[0]
        self.assertEqual([c.resource for c in check(devices)], ['GPIO3'])

    def test_w1_default_clashes_with_radar_uart(self):
        devices = from_config('dtoverlay=w1-gpio\n')[0] + [Device('Radar', uart('uart3'))]
        self.assertEqual([c.resource for c in check(devices)], ['GPIO4'])
        devices = from_config('dtoverlay=w1-gpio,gpiopin=6\n')[0]
        self.assertEqual(check(devices + [Device('Radar', uart('uart3'))]), [])

    def test_unknown_overlay_and_console_are_warned_about(self):
        devices, warnings = from_config('enable_uart=1\ndtoverlay=mystery\ndtoverlay=other,dc=3\n')
        self.assertEqual(len(warnings), 3)
        self.assertIn('console', warnings[0])
        self.assertIn('mystery', warnings[1])

    def test_two_overlays_of_the_same_name_are_separate_owners(self):
        devices = from_config('dtoverlay=w1-gpio,gpiopin=5\ndtoverlay=w1-gpio,gpiopin=5\n')[0]
        self.assertEqual([c.resource for c in check(devices)], ['GPIO5'])

    def test_unusable_values_are_skipped(self):
        devices, _ = from_config('dtoverlay=fbtft,spi0-0,dc_pin=x,led_pin=40\n')
        self.assertEqual(resources(devices) & {f'GPIO{n}' for n in range(28)},
                         {'GPIO8', 'GPIO9', 'GPIO10', 'GPIO11'})


# Every line from the "config.txt lines, per part" table in docs/HARDWARE-MAP.md.
DOC_LINES = '''
dtoverlay=disable-bt
enable_uart=1
dtoverlay=uart3
dtparam=i2c_arm=on
dtoverlay=i2c-rtc,ds3231
dtoverlay=w1-gpio,gpiopin=6
dtoverlay=pps-gpio,gpiopin=12
dtoverlay=gpio-poweroff,gpiopin=26,active_low
dtoverlay=pwm-2chan,pin=18,func=2,pin2=13,func2=4
'''


class ReviewFindingsTest(unittest.TestCase):
    """Reviewer 1's post-merge findings on #34."""

    def test_documented_lines_provide_the_planned_parts(self):
        installed = from_config(DECK_CONFIG.replace('dtoverlay=pwm,pin=18,func=2', ''))[0]
        installed += from_config(DOC_LINES)[0]
        devices = combine(installed, pinmap.PLAN)
        self.assertEqual(check(devices, usb_ports=7), [])
        names = [d.name for d in devices]
        for provided in ('Temperature probes', 'GNSS PPS', 'Fan PWM', 'Radar A',
                         'Power-off signal'):
            self.assertNotIn(provided, names)
        rtc = [d for d in devices if d.name == 'RTC']  # The EEPROM is still the board's.
        self.assertEqual({c.resource for c in rtc[0].claims if c.share is None}, {'i2c1 0x57'})

    def test_same_overlay_on_other_pins_still_clashes(self):
        installed = from_config('dtoverlay=w1-gpio\n')[0]  # Default GPIO4, not GPIO6.
        devices = combine(installed, pinmap.PLAN)
        self.assertIn('Temperature probes', [d.name for d in devices])
        self.assertIn('GPIO4', [c.resource for c in check(devices, usb_ports=7)])

    def test_pull_settings_are_not_pins(self):
        devices = from_config('dtparam=i2c_arm=on\n'
                              'dtoverlay=ads7846,cs=1,penirq=17,penirq_pull=2\n')[0]
        self.assertEqual(check(devices), [])
        self.assertIn('GPIO17', resources(devices))

    def test_i2c_overlay_pins(self):
        self.assertEqual(resources(from_config('dtoverlay=i2c4\n')[0]),
                         {'GPIO8', 'GPIO9', 'i2c4'})
        self.assertEqual(resources(from_config('dtoverlay=i2c5\n')[0]),
                         {'GPIO12', 'GPIO13', 'i2c5'})
        devices = from_config('dtparam=i2c_arm=on\ndtoverlay=i2c3,pins_2_3\n')[0]
        self.assertEqual([c.resource for c in check(devices)], ['GPIO2', 'GPIO3'])

    def test_i2s_board_takes_the_backlight_pin(self):
        devices = from_config('dtoverlay=pwm,pin=18,func=2\ndtoverlay=hifiberry-dac\n')[0]
        self.assertEqual([c.resource for c in check(devices)], ['GPIO18'])

    def test_uart_flow_control_pins(self):
        devices = from_config('dtoverlay=uart3,ctsrts\ndtoverlay=w1-gpio,gpiopin=6\n')[0]
        self.assertEqual([c.resource for c in check(devices)], ['GPIO6'])

    def test_dtparam_after_an_overlay_sets_its_parameters(self):
        devices = from_config('dtoverlay=w1-gpio\ndtparam=gpiopin=6\n'
                              'dtoverlay=\ndtparam=i2c_arm=on\n')[0]
        self.assertEqual(resources(devices), {'GPIO6', 'GPIO2', 'GPIO3'})

    def test_rtc_address_override(self):
        devices = from_config('dtoverlay=i2c-rtc,ds3231,addr=0x69\n')[0]
        self.assertIn('i2c1 0x69', resources(devices))
        self.assertNotIn('i2c1 0x68', resources(devices))

    def test_report_shows_parts_and_assumptions(self):
        text, _ = pinmap.report(pinmap.BASELINE + pinmap.PLAN)
        self.assertIn('LED pin is on 3.3 V', text)
        self.assertIn('(assumed)', text)


class CommandTest(unittest.TestCase):
    def run_main(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(list(args))
        return code, out.getvalue()

    def test_report_and_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / 'config.txt'
            config.write_text(DECK_CONFIG)
            code, text = self.run_main('--config', str(config), '--installed-only')
            self.assertEqual(code, 0)
            self.assertIn('No clashes.', text)
            self.assertRegex(text, r'GPIO18\s+12\s')  # BCM 18 is physical pin 12.
            code, text = self.run_main('--config', str(config))
            self.assertEqual(code, 1)
            self.assertIn('CLASH USB ports', text)
            code, _ = self.run_main('--config', str(config), '--usb-ports', '7')
            self.assertEqual(code, 0)

    def test_every_planned_pin_is_on_the_header(self):
        for device in pinmap.PLAN + pinmap.BASELINE:
            for claim in device.claims:
                if claim.resource.startswith('GPIO'):
                    self.assertIn(int(claim.resource[4:]), pinmap.PHYSICAL, device.name)

    def test_gpio_helper(self):
        self.assertEqual(gpio(6), [pinmap.Claim('GPIO6', None)])


if __name__ == '__main__':
    unittest.main()
