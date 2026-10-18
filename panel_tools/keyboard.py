"""A USB or wireless keyboard for typing on the panel.

Keys are read straight from the input devices (evdev), the same way as the touchscreen,
because the panel draws on the framebuffer with no window system. Keyboards are grabbed so
typing doesn't also land in the console or the shell behind the panel. Ctrl+C still stops
the panel. A keyboard plugged in or switched on later is picked up within a few seconds.
Without python3-evdev or a keyboard this does nothing.

Keys are named the way the on-screen keyboards name theirs: a typed character, or
'space', 'del', 'enter', 'esc', 'up', 'down', 'left', 'right', 'pageup', 'pagedown'
and 'quit' (Ctrl+C). The layout is US English."""
import select
import time

# Linux input event codes (linux/input-event-codes.h); fixed by the kernel ABI.
EV_KEY = 1
KEY_A, KEY_Z, KEY_SPACE, KEY_ENTER = 30, 44, 57, 28
SHIFTS, CTRLS, CAPSLOCK = (42, 54), (29, 97), 58
KEY_C = 46
# code: (plain, shifted)
CHARACTERS = {
    2: ('1', '!'), 3: ('2', '@'), 4: ('3', '#'), 5: ('4', '$'), 6: ('5', '%'),
    7: ('6', '^'), 8: ('7', '&'), 9: ('8', '*'), 10: ('9', '('), 11: ('0', ')'),
    12: ('-', '_'), 13: ('=', '+'), 26: ('[', '{'), 27: (']', '}'), 39: (';', ':'),
    40: ("'", '"'), 41: ('`', '~'), 43: ('\\', '|'), 51: (',', '<'), 52: ('.', '>'),
    53: ('/', '?'),
}
for _code, _row in ((16, 'qwertyuiop'), (30, 'asdfghjkl'), (44, 'zxcvbnm')):
    for _i, _letter in enumerate(_row):
        CHARACTERS[_code + _i] = (_letter, _letter.upper())
NAMED = {1: 'esc', 14: 'del', 28: 'enter', 96: 'enter', 57: 'space', 103: 'up', 108: 'down',
         105: 'left', 106: 'right', 104: 'pageup', 109: 'pagedown'}
RESCAN = 3.  # Seconds between looks for a keyboard while none is connected.


class KeyState:
    """Turns key events into key names, tracking Shift, Ctrl and Caps Lock."""
    def __init__(self):
        self.shift, self.ctrl, self.caps = set(), set(), False

    def feed(self, code, value):
        """value is 1 for a press, 2 for auto-repeat and 0 for a release."""
        if code in SHIFTS or code in CTRLS:
            held = self.shift if code in SHIFTS else self.ctrl
            (held.add if value else held.discard)(code)
            return None
        if value == 0:
            return None
        if code == CAPSLOCK:
            if value == 1:
                self.caps = not self.caps
            return None
        if self.ctrl:
            return 'quit' if code == KEY_C and value == 1 else None
        if code in NAMED:
            return NAMED[code]
        if code in CHARACTERS:
            plain, shifted = CHARACTERS[code]
            upper = bool(self.shift) != (self.caps and plain.isalpha())
            return shifted if upper else plain
        return None


def is_keyboard(keys):
    """True for a device whose key list has letters, space and Enter (not a mouse,
    touchscreen or the media-key half of a wireless dongle)."""
    return all(k in keys for k in (KEY_A, KEY_Z, KEY_SPACE, KEY_ENTER))


class Keyboard:
    def __init__(self, skip=()):
        try:
            import evdev
        except ImportError:
            evdev = None
        self.evdev, self.skip = evdev, set(skip)
        self.devices, self.state = [], KeyState()
        self.next_scan = 0.
        self.scan()

    def scan(self):
        self.next_scan = time.monotonic() + RESCAN
        if self.evdev is None:
            return
        try:
            paths = self.evdev.list_devices()
        except OSError:
            return
        known = {d.path for d in self.devices}
        for path in paths:
            if path in known or path in self.skip:
                continue
            try:
                device = self.evdev.InputDevice(path)
            except OSError:
                continue
            if is_keyboard(device.capabilities(absinfo=False).get(EV_KEY, [])):
                try:
                    device.grab()
                except OSError:
                    pass  # Still readable; keys may also reach the console.
                self.devices.append(device)
                print('Keyboard:', path, device.name, flush=True)
            else:
                device.close()

    def poll(self):
        """Key names typed since the last call."""
        if not self.devices and time.monotonic() >= self.next_scan:
            self.scan()
        if not self.devices:
            return []
        keys = []
        for device in select.select(self.devices, [], [], 0)[0]:
            try:
                events = list(device.read())
            except BlockingIOError:
                continue
            except OSError:  # Unplugged or switched off.
                self.drop(device)
                continue
            for event in events:
                if event.type == EV_KEY:
                    key = self.state.feed(event.code, event.value)
                    if key:
                        keys.append(key)
        return keys

    def drop(self, device):
        self.devices.remove(device)
        self.state = KeyState()
        try:
            device.close()
        except OSError:
            pass

    def close(self):
        for device in list(self.devices):
            try:
                device.ungrab()
            except OSError:
                pass
            self.drop(device)
