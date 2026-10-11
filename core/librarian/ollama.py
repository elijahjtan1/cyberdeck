"""Ollama /api/chat client: loopback only, streaming, thinking off.

Check it on the Pi with `python3 -m core.librarian.ollama "Say hello"`.
"""
from contextlib import nullcontext
from dataclasses import dataclass
from http.client import HTTPException
import ipaddress
import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

DEFAULT_URL = 'http://127.0.0.1:11434'
DEFAULT_MODEL = 'qwen3:0.6b'
DEFAULT_NUM_CTX = 2048
DEFAULT_NUM_PREDICT = 400
THINK_OPEN, THINK_CLOSE = '<think>', '</think>'


class OllamaError(RuntimeError):
    """A readable model failure that callers can display without a traceback."""


@dataclass(frozen=True)
class Reply:
    text: str
    model: str
    done_reason: str = ''
    prompt_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0


def _loopback(url):
    parts = urlsplit(url)
    try:
        local = parts.hostname == 'localhost' or ipaddress.ip_address(parts.hostname or '').is_loopback
    except ValueError:
        local = False
    return (parts.scheme == 'http' and local and not parts.username and not parts.password
            and not parts.query and not parts.fragment)


class _ThinkFilter:
    """Drops <think>...</think> even if Ollama is too old to honour think=false.

    Tags can be split across streamed pieces, so a possible tag prefix is held back.
    """

    def __init__(self):
        self.pending, self.thinking = '', False

    def feed(self, text):
        self.pending += text
        out = []
        while self.pending:
            tag = THINK_CLOSE if self.thinking else THINK_OPEN
            index = self.pending.find(tag)
            if index >= 0:
                if not self.thinking:
                    out.append(self.pending[:index])
                self.pending = self.pending[index + len(tag):]
                self.thinking = not self.thinking
                continue
            keep = next((n for n in range(len(tag) - 1, 0, -1)
                         if self.pending.endswith(tag[:n])), 0)
            if not self.thinking:
                out.append(self.pending[:len(self.pending) - keep])
            self.pending = self.pending[len(self.pending) - keep:]
            break
        return ''.join(out)

    def flush(self):
        rest, self.pending = ('' if self.thinking else self.pending), ''
        return rest


class OllamaClient:
    def __init__(self, base_url=DEFAULT_URL, opener=None, *, model=DEFAULT_MODEL,
                 num_ctx=DEFAULT_NUM_CTX, num_predict=DEFAULT_NUM_PREDICT, temperature=0.2,
                 timeout=120.0, max_bytes=1_000_000, telemetry=None):
        if not _loopback(base_url):
            raise ValueError('Ollama URL must be a loopback HTTP URL')
        if not model or any(c.isspace() for c in model):
            raise ValueError('model must be an Ollama model name such as qwen3:0.6b')
        if type(num_ctx) is not int or not 256 <= num_ctx <= 32768:
            raise ValueError('num_ctx must be an integer from 256 to 32768')
        if type(num_predict) is not int or not 1 <= num_predict <= 4096:
            raise ValueError('num_predict must be an integer from 1 to 4096')
        if not 0 < timeout <= 600 or not 0 < max_bytes <= 8_000_000:
            raise ValueError('timeout or max_bytes outside safe bounds')
        self.base_url = base_url.rstrip('/') + '/'
        self.model, self.num_ctx, self.num_predict = model, num_ctx, num_predict
        self.temperature, self.timeout, self.max_bytes = temperature, timeout, max_bytes
        self.telemetry = telemetry
        # No proxies: the model server is on this machine and nothing may go online.
        self.opener = opener or build_opener(ProxyHandler({}))

    @classmethod
    def from_environment(cls, telemetry=None):
        env = os.environ
        try:
            num_ctx = int(env.get('CYBERDECK_OLLAMA_NUM_CTX', DEFAULT_NUM_CTX))
            num_predict = int(env.get('CYBERDECK_OLLAMA_NUM_PREDICT', DEFAULT_NUM_PREDICT))
        except ValueError:
            raise ValueError('CYBERDECK_OLLAMA_NUM_CTX and _NUM_PREDICT must be integers') from None
        return cls(env.get('CYBERDECK_OLLAMA_URL') or DEFAULT_URL,
                   model=env.get('CYBERDECK_OLLAMA_MODEL') or DEFAULT_MODEL,
                   num_ctx=num_ctx, num_predict=num_predict, telemetry=telemetry)

    def _open(self, path, payload=None):
        data = None if payload is None else json.dumps(payload).encode()
        request = Request(self.base_url + path, data=data,
                          headers={'Content-Type': 'application/json'})
        try:
            return self.opener.open(request, timeout=self.timeout)
        except HTTPError as exc:
            raise OllamaError(self._http_message(exc)) from exc
        except (URLError, OSError, ValueError, HTTPException) as exc:
            raise OllamaError('Ollama is unavailable or timed out; check `systemctl status ollama`') from exc

    def _http_message(self, exc):
        try:
            detail = json.loads(exc.read(4096) or b'{}').get('error', '')
        except (ValueError, OSError, AttributeError):
            detail = ''
        if exc.code == 404 and 'not found' in detail:
            return f'Model {self.model} is not installed; run `ollama pull {self.model}`'
        return f'Ollama refused the request ({exc.code}): {detail or exc.reason}'

    def models(self):
        """Names of the installed models, from /api/tags."""
        with self._open('api/tags') as response:
            data = response.read(self.max_bytes + 1)
        try:
            rows = json.loads(data).get('models', [])
            return [row['name'] for row in rows if isinstance(row, dict) and 'name' in row]
        except (ValueError, AttributeError) as exc:
            raise OllamaError('Ollama returned an unexpected model list') from exc

    def _events(self, messages, detail):
        payload = {'model': self.model, 'messages': list(messages), 'stream': True,
                   'think': False,
                   'options': {'num_ctx': self.num_ctx, 'num_predict': self.num_predict,
                               'temperature': self.temperature}}
        activity = self.telemetry.activity('ai', detail) if self.telemetry else nullcontext()
        with activity, self._open('api/chat', payload) as response:
            think, total = _ThinkFilter(), 0
            try:
                while True:
                    line = response.readline(self.max_bytes + 1)
                    if not line:
                        raise OllamaError('Ollama stopped before finishing the answer')
                    total += len(line)
                    if total > self.max_bytes:
                        raise OllamaError('Ollama response exceeds the configured size limit')
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError as exc:
                        raise OllamaError('Ollama returned an unexpected chat stream') from exc
                    if not isinstance(event, dict):
                        raise OllamaError('Ollama returned an unexpected chat stream')
                    if event.get('error'):
                        raise OllamaError(f"Ollama error: {event['error']}")
                    message = event.get('message') or {}
                    # Only content is shown; a separate 'thinking' field is ignored.
                    text = think.feed(message.get('content') or '')
                    if text:
                        yield 'text', text
                    if event.get('done'):
                        rest = think.flush()
                        if rest:
                            yield 'text', rest
                        yield 'done', event
                        return
            except (OSError, HTTPException) as exc:
                raise OllamaError('Ollama connection dropped or timed out mid-answer') from exc

    def stream(self, messages, *, detail=''):
        """Yield answer text as it arrives. Raises OllamaError on any failure."""
        for kind, value in self._events(messages, detail):
            if kind == 'text':
                yield value

    def chat(self, messages, *, on_text=None, detail=''):
        """Run a whole answer; on_text(piece) is called as text streams in."""
        pieces, final = [], {}
        for kind, value in self._events(messages, detail):
            if kind == 'text':
                pieces.append(value)
                if on_text:
                    on_text(value)
            else:
                final = value
        return Reply(''.join(pieces).strip(), final.get('model') or self.model,
                     final.get('done_reason', ''), int(final.get('prompt_eval_count') or 0),
                     int(final.get('eval_count') or 0),
                     round((final.get('total_duration') or 0) / 1e9, 2))


def main(argv=None):
    import argparse
    import sys
    parser = argparse.ArgumentParser(description='Send one prompt to the local Ollama model.')
    parser.add_argument('prompt')
    args = parser.parse_args(argv)
    try:
        client = OllamaClient.from_environment()
        reply = client.chat([{'role': 'user', 'content': args.prompt}],
                            on_text=lambda t: print(t, end='', flush=True))
    except (OllamaError, ValueError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 1
    print(f'\n[{reply.model}: {reply.output_tokens} tokens in {reply.seconds} s, '
          f'stop: {reply.done_reason or "?"}]')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
