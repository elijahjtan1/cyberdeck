"""Panel ASK page: keyboard, streamed answers and failures, with a fake backend."""
import sys
import threading
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import panel_app as app  # noqa: E402
from core.cli import BackendMissing  # noqa: E402
from panel_tools.ask import (EXAMPLES, AskApp, answer_buttons, example_rects, field_rect,  # noqa: E402
                             keyboard_rects)

WALL = datetime(2026, 10, 11, 9, 30)
PASSAGES = {'event': 'passages', 'model': 'qwen3:0.6b', 'keywords': ['coffee', 'leak'],
            'warnings': [], 'passages': [{'n': 1, 'title': 'Coffee makers'}, {'n': 2, 'title': 'Kettle'}]}
DONE = {'event': 'done', 'answer': 'Check the seals [1].', 'found': True, 'uncited': False,
        'cited': [1], 'dropped': [7], 'warnings': [],
        'sources': [{'n': 1, 'title': 'Coffee makers', 'source': 'kiwix'},
                    {'n': 2, 'title': 'Kettle', 'source': 'kiwix'}]}


def centre(rect):
    return (rect[0]+rect[2])/2, (rect[1]+rect[3])/2


def scripted(*events, gate=None):
    """A stand-in for backend_events; `gate` holds the stream before the first event."""
    calls = []

    def events_for(question, base_url, opener=None, timeout=None):
        calls.append((question, base_url))
        try:
            if gate is not None:
                gate.wait(5)
            for event in events:
                if isinstance(event, Exception):
                    raise event
                yield event
        finally:
            calls.append('closed')
    return events_for, calls


class AskPageTest(unittest.TestCase):
    def tearDown(self):
        app.set_rotation(0)

    def page(self, events=None, base='http://127.0.0.1:8765'):
        events, self.calls = events or scripted(PASSAGES, DONE)
        return AskApp(app, base, events=events)

    def press(self, page, *keys):
        for key in keys:
            rect = next(r for r, k in keyboard_rects(app.W, app.H, page.symbols) if k == key)
            page.tap(*centre(rect), 0)

    def finish(self, page):
        page.thread.join(5)
        self.assertFalse(page.running)

    def test_keys_fit_without_overlap_in_both_orientations(self):
        for rotation in (0, 90):
            app.set_rotation(rotation)
            for symbols in (False, True):
                keys = keyboard_rects(app.W, app.H, symbols)
                self.assertEqual(len(keys), 10 + 10 + 9 + 4)
                box = field_rect(app.W, app.H)
                for i, (rect, key) in enumerate(keys):
                    self.assertTrue(0 <= rect[0] < rect[2] < app.W and box[3] < rect[1] < rect[3] < app.H,
                                    (rotation, key, rect))
                    for other, _ in keys[i+1:]:
                        self.assertFalse(rect[0] < other[2] and other[0] < rect[2]
                                         and rect[1] < other[3] and other[1] < rect[3], (key, rect, other))

    def test_typing_editing_and_clearing(self):
        page = self.page()
        self.press(page, 'w', 'h', 'y', 'space', 'space', 'f', 'u', 's', 'e', 'del', 'space')
        self.assertEqual(page.question, 'why fus ')
        self.press(page, 'mode', '1', '2', 'mode', '?')
        self.assertEqual(page.question, 'why fus 12?')
        box = field_rect(app.W, app.H)
        page.tap(*centre(box), 0)  # A stray tap on the box keeps the question...
        self.assertEqual(page.question, 'why fus 12?')
        page.tap(box[2] - 8, centre(box)[1], 0)  # ...the x clears it...
        self.assertEqual(page.question, '')
        page.tap(*centre(field_rect(app.W, app.H)), 0)  # ...and an empty box offers examples.
        self.assertEqual(page.view, 'examples')
        page.tap(*centre(example_rects(app.W, app.H, len(page.examples()))[0]), 0)
        self.finish(page)
        self.assertEqual((page.view, page.asked), ('answer', EXAMPLES[0]))

    def test_back_leaves_the_page(self):
        page = self.page()
        self.press(page, 'a')
        rect = next(r for r, k in keyboard_rects(app.W, app.H) if k == 'back')
        self.assertEqual(page.tap(*centre(rect), 0), 'exit')

    def test_streams_then_shows_the_checked_answer_and_its_sources(self):
        events = scripted(PASSAGES, {'event': 'text', 'text': 'Check the seals [1'},
                          {'event': 'text', 'text': '][7].'}, DONE)
        page = self.page(events)
        page.question = 'Why does my coffee maker leak?'
        self.press(page, 'ask')
        self.finish(page)
        self.assertEqual(self.calls[0], ('Why does my coffee maker leak?', 'http://127.0.0.1:8765'))
        self.assertEqual(self.calls[-1], 'closed')
        self.assertEqual((page.view, page.stage, page.text), ('answer', 'done', 'Check the seals [1].'))
        # Only the cited source is listed, as deck ask does.
        self.assertEqual(page.body(), ['Check the seals [1].', '', 'Sources:', '[1] Coffee makers (kiwix)'])
        self.assertTrue(page.status().startswith('Done in'))
        self.assertEqual(page.recent, ['Why does my coffee maker leak?'])

    def test_not_found_and_uncited_answers_are_labelled(self):
        done = dict(DONE, answer='Not found in the offline sources.', found=False, cited=[],
                    uncited=True, warnings=['Kiwix timed out'])
        page = self.page(scripted(PASSAGES, done))
        page.ask('What is the capital of Peru?')
        self.finish(page)
        body = page.body()
        self.assertEqual(body[0], 'Not found in the offline sources.')
        self.assertIn('UNVERIFIED: the answer cites no source.', body)
        self.assertIn('Warning: Kiwix timed out', body)
        self.assertIn('Searched:', body)
        page = self.page(scripted(PASSAGES, dict(DONE, copied=True)))
        page.ask('Why does my coffee maker leak?')
        self.finish(page)
        self.assertIn('QUOTED: this repeats the sources instead of answering.', page.body())

    def test_partial_answers_line_breaks_and_bad_lines(self):
        page = self.page(scripted(PASSAGES, {'event': 'text', 'text': '1. Unplug it [1].\n2. Check the seal'},
                                  {'event': 'error', 'error': 'lost the backend mid-answer'}))
        page.ask('Why does my coffee maker leak?')
        self.finish(page)
        self.assertEqual(page.body(), ['1. Unplug it [1].', '2. Check the seal', '', 'lost the backend mid-answer'])
        page.stage = 'done'
        self.assertEqual(page.body()[:2], ['1. Unplug it [1].', '2. Check the seal'])
        odd = self.page(scripted(PASSAGES, [1, 2]))
        odd.ask('anything')
        self.finish(odd)
        self.assertEqual(odd.stage, 'error')

    def test_backend_down_is_a_message_not_a_crash(self):
        page = self.page(scripted(BackendMissing('the backend is not running')))
        page.ask('Why does my kettle not switch on?')
        self.finish(page)
        self.assertEqual(page.stage, 'error')
        self.assertIn('backend is not running', page.body()[0])
        self.assertEqual(page.render(0, WALL, []).size, (app.W, app.H))

    def test_error_event_and_backend_off(self):
        page = self.page(scripted({'event': 'error', 'error': 'Another question is being answered'}))
        page.ask('anything')
        self.finish(page)
        self.assertEqual(page.body(), ['Another question is being answered'])
        off = self.page(base=None)
        off.ask('anything')
        self.assertEqual(off.stage, 'error')
        self.assertIn('--backend off', off.error)
        self.assertEqual(off.note(), 'Backend off')

    def test_stop_ends_the_stream_while_waiting_for_the_model(self):
        gate = threading.Event()
        page = self.page(scripted(PASSAGES, DONE, gate=gate))
        page.ask('Why does my coffee maker leak?')
        self.assertTrue(page.running)
        self.assertEqual(page.note(), 'Answering...')
        page.tap(*centre(answer_buttons(app.W, app.H)[0]), 0)  # STOP
        gate.set()
        self.finish(page)
        self.assertEqual(page.stage, 'stopped')
        self.assertEqual(self.calls[-1], 'closed')
        page.tap(*centre(answer_buttons(app.W, app.H)[0]), 0)  # Now EDIT, with the question kept.
        self.assertEqual((page.view, page.question), ('type', 'Why does my coffee maker leak?'))

    def test_long_answers_scroll_and_follow_the_end(self):
        long = dict(DONE, answer=' '.join(['Check the seals and the gasket [1].'] * 30))
        page = self.page(scripted(PASSAGES, long))
        page.ask('Why does my coffee maker leak?')
        self.finish(page)
        first, lines, visible, _ = page.layout()
        self.assertGreater(len(lines), visible)
        self.assertEqual(first, len(lines) - visible)
        up, down = answer_buttons(app.W, app.H)[1:3]
        page.tap(*centre(up), 0)
        self.assertLess(page.layout()[0], first)
        for _ in range(20):
            page.tap(*centre(down), 0)
        self.assertEqual(page.layout()[0], first)
        self.assertIsNone(page.scroll)  # Back at the end, it follows new text again.

    def test_every_view_renders_in_both_orientations(self):
        for rotation in (0, 90):
            app.set_rotation(rotation)
            page = self.page()
            for view in ('type', 'examples'):
                page.view = view
                self.assertEqual(page.render(0, WALL, []).size, (app.W, app.H))
            page.question = 'x' * 200
            page.view = 'type'
            page.render(0, WALL, [('warning', 'Hot')])
            page.ask('Why does my coffee maker leak?')
            self.finish(page)
            self.assertEqual(page.render(0, WALL, []).size, (app.W, app.H))

    def test_home_has_an_ask_tile(self):
        self.assertIn(('ASK', 'ask'), [(label, key) for label, key, _ in app.HOME_TILES])


if __name__ == '__main__':
    unittest.main()
