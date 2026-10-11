from contextlib import contextmanager
import io
import json
import os
import unittest
from unittest import mock
from urllib.error import HTTPError, URLError

from core.librarian.ollama import OllamaClient, OllamaError, _ThinkFilter


def ndjson(*events):
    return b''.join(json.dumps(e).encode() + b'\n' for e in events)


def piece(text):
    return {'model': 'qwen3:0.6b', 'message': {'role': 'assistant', 'content': text}, 'done': False}


DONE = {'model': 'qwen3:0.6b', 'message': {'role': 'assistant', 'content': ''}, 'done': True,
        'done_reason': 'stop', 'prompt_eval_count': 31, 'eval_count': 7,
        'total_duration': 2_500_000_000}


class FakeOpener:
    def __init__(self, data=b'', error=None):
        self.data, self.error, self.requests = data, error, []

    def open(self, request, timeout):
        body = json.loads(request.data) if request.data else None
        self.requests.append((request.full_url, body, timeout))
        if self.error:
            raise self.error
        return io.BytesIO(self.data)


class FakeTelemetry:
    def __init__(self):
        self.events = []

    @contextmanager
    def activity(self, kind, detail=''):
        self.events.append(('start', kind, detail))
        try:
            yield
        finally:
            self.events.append(('end', kind, detail))


def client(opener, **kw):
    return OllamaClient('http://127.0.0.1:11434', opener, **kw)


class OllamaTest(unittest.TestCase):
    def test_streams_text_and_sends_settings(self):
        opener = FakeOpener(ndjson(piece('Hello'), piece(', deck.'), DONE))
        telemetry = FakeTelemetry()
        seen = []
        reply = client(opener, model='qwen3:0.6b', num_ctx=1024, telemetry=telemetry).chat(
            [{'role': 'user', 'content': 'hi'}], on_text=seen.append, detail='hi')
        self.assertEqual(seen, ['Hello', ', deck.'])
        self.assertEqual(reply.text, 'Hello, deck.')
        self.assertEqual((reply.done_reason, reply.prompt_tokens, reply.output_tokens, reply.seconds),
                         ('stop', 31, 7, 2.5))
        url, body, timeout = opener.requests[0]
        self.assertEqual(url, 'http://127.0.0.1:11434/api/chat')
        self.assertIs(body['stream'], True)
        self.assertIs(body['think'], False)
        self.assertEqual(body['model'], 'qwen3:0.6b')
        self.assertEqual(body['options']['num_ctx'], 1024)
        self.assertEqual(telemetry.events, [('start', 'ai', 'hi'), ('end', 'ai', 'hi')])

    def test_stream_yields_pieces(self):
        opener = FakeOpener(ndjson(piece('a'), piece('b'), DONE))
        self.assertEqual(list(client(opener).stream([])), ['a', 'b'])

    def test_think_blocks_removed_even_when_split(self):
        opener = FakeOpener(ndjson(piece('<thi'), piece('nk>\nhmm</th'), piece('ink>\n\nAnswer'),
                                   piece(' <'), piece('3'), DONE))
        self.assertEqual(client(opener).chat([]).text, 'Answer <3')

    def test_think_filter_edge_cases(self):
        f = _ThinkFilter()
        self.assertEqual(f.feed('a<think>x</think>b<think>y'), 'ab')
        self.assertEqual(f.flush(), '')  # Unclosed thinking is never shown.
        f = _ThinkFilter()
        self.assertEqual(f.feed('5 <'), '5 ')
        self.assertEqual(f.feed('t'), '')  # Could still become <think>.
        self.assertEqual(f.flush(), '<t')

    def test_ignores_separate_thinking_field(self):
        event = piece('ok')
        event['message']['thinking'] = 'secret reasoning'
        self.assertEqual(client(FakeOpener(ndjson(event, DONE))).chat([]).text, 'ok')

    def test_unreachable_server_is_readable(self):
        telemetry = FakeTelemetry()
        with self.assertRaisesRegex(OllamaError, 'unavailable'):
            client(FakeOpener(error=URLError('refused')), telemetry=telemetry).chat([])
        self.assertEqual(telemetry.events[-1][0], 'end')  # Busy indicator cleared.

    def test_missing_model_names_the_fix(self):
        error = HTTPError('http://127.0.0.1:11434/api/chat', 404, 'Not Found', {},
                          io.BytesIO(b'{"error":"model \'qwen3:0.6b\' not found"}'))
        with self.assertRaisesRegex(OllamaError, 'ollama pull qwen3:0.6b'):
            client(FakeOpener(error=error)).chat([])

    def test_error_in_stream(self):
        opener = FakeOpener(ndjson(piece('par'), {'error': 'out of memory'}))
        with self.assertRaisesRegex(OllamaError, 'out of memory'):
            client(opener).chat([])

    def test_truncated_stream(self):
        with self.assertRaisesRegex(OllamaError, 'before finishing'):
            client(FakeOpener(ndjson(piece('half')))).chat([])

    def test_bad_json_and_size_cap(self):
        with self.assertRaisesRegex(OllamaError, 'unexpected'):
            client(FakeOpener(b'<html>\n')).chat([])
        big = ndjson(*[piece('x' * 100)] * 50, DONE)
        with self.assertRaisesRegex(OllamaError, 'size limit'):
            client(FakeOpener(big), max_bytes=2000).chat([])

    def test_models(self):
        opener = FakeOpener(b'{"models":[{"name":"qwen3:0.6b"},{"name":"tinyllama:latest"}]}')
        self.assertEqual(client(opener).models(), ['qwen3:0.6b', 'tinyllama:latest'])
        self.assertEqual(opener.requests[0][0], 'http://127.0.0.1:11434/api/tags')

    def test_rejects_unsafe_configuration(self):
        for url in ('http://example.com:11434', 'https://127.0.0.1:11434',
                    'http://user:pw@127.0.0.1:11434', 'http://10.0.0.5:11434'):
            with self.assertRaises(ValueError):
                OllamaClient(url)
        with self.assertRaises(ValueError):
            OllamaClient(model='bad name')
        with self.assertRaises(ValueError):
            OllamaClient(num_ctx=100)
        OllamaClient('http://localhost:11434')

    def test_environment(self):
        env = {'CYBERDECK_OLLAMA_MODEL': 'tinyllama', 'CYBERDECK_OLLAMA_NUM_CTX': '1536'}
        with mock.patch.dict(os.environ, env, clear=True):
            c = OllamaClient.from_environment()
        self.assertEqual((c.base_url, c.model, c.num_ctx),
                         ('http://127.0.0.1:11434/', 'tinyllama', 1536))
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(OllamaClient.from_environment().model, 'qwen3:0.6b')
        with mock.patch.dict(os.environ, {'CYBERDECK_OLLAMA_NUM_CTX': 'big'}, clear=True):
            with self.assertRaises(ValueError):
                OllamaClient.from_environment()


if __name__ == '__main__':
    unittest.main()
