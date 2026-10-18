"""Real keyboard: key names, hot-plug and typing into READER, ASK and PLANNER, without evdev."""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import panel_app as app  # noqa: E402
from panel_tools import keyboard, planner  # noqa: E402
from panel_tools.ask import AskApp  # noqa: E402
from tests.test_ask_page import scripted, PASSAGES, DONE  # noqa: E402
from tests.test_reader import FakeBackend, make, wait  # noqa: E402

# Linux key codes used below.
A, H, I, SPACE, ENTER, BACKSPACE, ESC, SHIFT, CTRL, CAPS, ONE, SLASH = (
    30, 35, 23, 57, 28, 14, 1, 42, 29, 58, 2, 53)
RIGHT, LEFT, UP, DOWN, PAGEUP = 106, 105, 103, 108, 104


def names(*events):
    state = keyboard.KeyState()
    return [k for k in (state.feed(code, value) for code, value in events) if k]


class KeyStateTest(unittest.TestCase):
    def test_letters_shift_caps_and_repeat(self):
        self.assertEqual(names((H, 1), (H, 0), (I, 1), (I, 2), (I, 0)), ['h', 'i', 'i'])
        self.assertEqual(names((SHIFT, 1), (H, 1), (ONE, 1), (SLASH, 1), (SHIFT, 0), (I, 1)),
                         ['H', '!', '?', 'i'])
        # Caps Lock capitalises letters only, and Shift undoes it.
        self.assertEqual(names((CAPS, 1), (CAPS, 0), (H, 1), (ONE, 1), (SHIFT, 1), (I, 1)),
                         ['H', '1', 'i'])
        self.assertEqual(names((SPACE, 1), (ENTER, 1), (BACKSPACE, 2), (ESC, 1), (RIGHT, 1),
                               (PAGEUP, 1), (96, 1)),
                         ['space', 'enter', 'del', 'esc', 'right', 'pageup', 'enter'])

    def test_ctrl_c_quits_and_other_ctrl_keys_type_nothing(self):
        self.assertEqual(names((CTRL, 1), (A, 1), (46, 1), (CTRL, 0), (A, 1)), ['quit', 'a'])
        self.assertEqual(names((999, 1)), [])

    def test_only_full_keyboards_count(self):
        self.assertTrue(keyboard.is_keyboard([1, 28, 30, 44, 57]))
        self.assertFalse(keyboard.is_keyboard([272, 273]))       # Mouse buttons.
        self.assertFalse(keyboard.is_keyboard([113, 114, 115]))  # Media keys of a dongle.


class FakeDevice:
    """An evdev InputDevice backed by a pipe, so select() works on it."""
    def __init__(self, path, keys):
        self.path, self.name, self.keys = path, 'Fake ' + path, keys
        self.read_end, self.write_end = os.pipe()
        self.events, self.grabbed, self.closed, self.gone = [], False, False, False

    def capabilities(self, absinfo=True):
        return {1: self.keys}

    def fileno(self):
        return self.read_end

    def press(self, *codes):
        for code in codes:
            self.events += [SimpleNamespace(type=1, code=code, value=1),
                            SimpleNamespace(type=1, code=code, value=0)]
        os.write(self.write_end, b'x')

    def read(self):
        os.read(self.read_end, 64)
        if self.gone:
            raise OSError(19, 'No such device')
        events, self.events = self.events, []
        return iter(events)

    def grab(self):
        self.grabbed = True

    def ungrab(self):
        self.grabbed = False

    def close(self):
        if not self.closed:
            self.closed = True
            os.close(self.read_end)
            os.close(self.write_end)


class FakeEvdev:
    def __init__(self):
        self.devices = {}

    def list_devices(self):
        return list(self.devices)

    def InputDevice(self, path):
        old = self.devices[path]
        device = FakeDevice(path, old.keys)  # A fresh handle, like opening the node again.
        self.devices[path] = device
        return device


class KeyboardTest(unittest.TestCase):
    def test_finds_grabs_reads_and_survives_unplugging(self):
        fake = FakeEvdev()
        fake.devices = {'/dev/input/event0': FakeDevice('/dev/input/event0', [330]),  # Touch.
                        '/dev/input/event1': FakeDevice('/dev/input/event1', [113, 114])}
        with mock.patch.dict(sys.modules, {'evdev': fake}):
            board = keyboard.Keyboard(skip=['/dev/input/event0'])
        try:
            self.assertEqual(board.devices, [])
            self.assertEqual(board.poll(), [])
            # The keyboard is switched on later and found on the next scan.
            fake.devices['/dev/input/event2'] = FakeDevice('/dev/input/event2', [1, 28, 30, 44, 57])
            board.next_scan = 0
            self.assertEqual(board.poll(), [])
            self.assertEqual([d.path for d in board.devices], ['/dev/input/event2'])
            device = board.devices[0]
            self.assertTrue(device.grabbed)
            device.press(H, I)
            self.assertEqual(board.poll(), ['h', 'i'])
            device.gone = True
            device.press(H)
            self.assertEqual(board.poll(), [])
            self.assertEqual(board.devices, [])
            self.assertTrue(device.closed)
        finally:
            board.close()
            for device in fake.devices.values():
                device.close()

    def test_without_evdev_it_does_nothing(self):
        with mock.patch.dict(sys.modules, {'evdev': None}):
            board = keyboard.Keyboard()
        self.assertEqual((board.devices, board.poll()), ([], []))
        board.close()


def send(view, *keys):
    results = [view.key(key, 0) for key in keys]
    return results[-1]


class ReaderKeysTest(unittest.TestCase):
    def test_search_open_page_and_go_back_with_keys(self):
        backend = FakeBackend()
        view = make(backend)
        send(view, 'R', 'a', 'd', 'x', 'del', 'space', 'space', 'i', 'o', 'enter')
        wait(view)
        self.assertEqual((view.query, view.view), ('Rad io', 'results'))
        send(view, '2')
        wait(view)
        self.assertEqual((view.view, view.title), ('article', 'Antenna notes'))
        send(view, 'esc')
        self.assertEqual(view.view, 'results')
        send(view, 'enter')
        wait(view)
        self.assertEqual(view.title, 'Amateur radio')
        self.assertGreater(view.pages(), 2)
        send(view, 'right', 'space', 'pagedown')
        self.assertEqual(view.page, 3)
        send(view, 'left')
        self.assertEqual(view.page, 2)
        send(view, 'w')  # Typing in an article starts a new search.
        self.assertEqual((view.view, view.query), ('type', 'w'))
        self.assertEqual(send(view, 'esc'), 'exit')


class AskKeysTest(unittest.TestCase):
    def test_type_ask_scroll_and_start_again(self):
        events, _ = scripted(PASSAGES, DONE)
        page = AskApp(app, 'http://127.0.0.1:8765', events=events)
        page.view = 'examples'
        send(page, 'W', 'h', 'y', '?')
        self.assertEqual((page.view, page.question), ('type', 'Why?'))
        send(page, 'enter')
        page.thread.join(5)
        self.assertEqual((page.view, page.asked), ('answer', 'Why?'))
        page.render(0, datetime(2026, 10, 11, 9, 30), ())
        send(page, 'down', 'up')
        send(page, 'n')  # Typing after the answer starts a new question.
        self.assertEqual((page.view, page.question), ('type', 'n'))
        send(page, 'enter')
        page.thread.join(5)
        send(page, 'esc')  # Back to edit the question.
        self.assertEqual((page.view, page.question), ('type', 'n'))
        self.assertEqual(send(page, 'esc'), 'exit')


class PlannerKeysTest(unittest.TestCase):
    NOW = datetime(2026, 10, 12, 14, 20, tzinfo=timezone.utc)

    def test_add_and_delete_an_entry_with_keys(self):
        with tempfile.TemporaryDirectory() as folder:
            ui = planner.PlannerApp(app, Path(folder), lambda: 'Synchronized')
            ui.wall = self.NOW
            send(ui, 'right')
            self.assertEqual(ui.shown, planner.date(2026, 11, 1))
            send(ui, 'enter')  # Today.
            self.assertEqual((ui.view, ui.day), ('day', self.NOW.date()))
            send(ui, 'C', 'a', 'l', 'l')  # Typing on a day starts an entry.
            self.assertEqual((ui.view, ui.text), ('type', 'Call'))
            send(ui, 'enter')
            self.assertEqual((ui.view, ui.time), ('time', '15:00'))
            send(ui, 'up', 'pageup', 'down')
            self.assertEqual(ui.time, '16:00')
            send(ui, 'enter')
            self.assertEqual(ui.planner.entries(self.NOW.date()), [{'time': '16:00', 'text': 'Call'}])
            ui.render(0, self.NOW, ())
            ui.selected = 0
            send(ui, 'del')
            self.assertEqual(ui.planner.entries(self.NOW.date()), [])
            send(ui, 'left')
            self.assertEqual(ui.day, planner.date(2026, 10, 11))
            send(ui, 'esc')
            self.assertEqual(ui.view, 'month')
            self.assertEqual(send(ui, 'esc'), 'exit')


if __name__ == '__main__':
    unittest.main()
