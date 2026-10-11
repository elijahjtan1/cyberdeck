"""`deck` command line: deck ask "question" [--explain] [--local]

Asks the running cyberdeck-core backend, so the panel's AI badge and the one-question-at-a-time
limit apply. If the backend isn't running, it answers in this process instead.
"""
import argparse
from contextlib import closing
import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, build_opener

from . import DEFAULT_PORT

CORE_URL = f'http://127.0.0.1:{DEFAULT_PORT}'


class BackendMissing(Exception):
    """The backend isn't running, or is too old to answer; fall back to a local answer."""


def backend_events(question, base_url=None, opener=None, timeout=180):
    """Yield the NDJSON events of /api/ask?stream=1."""
    url = (base_url or os.environ.get('CYBERDECK_CORE_URL') or CORE_URL).rstrip('/')
    opener = opener or build_opener(ProxyHandler({}))
    try:
        response = opener.open(f'{url}/api/ask?' + urlencode({'q': question, 'stream': 1}),
                               timeout=timeout)
    except HTTPError as exc:
        if exc.code == 404:
            raise BackendMissing('the backend has no /api/ask yet') from exc
        try:
            message = json.loads(exc.read(4096)).get('error', '')
        except (ValueError, OSError, AttributeError):
            message = ''
        yield {'event': 'error', 'error': message or f'backend refused the question ({exc.code})'}
        return
    except (URLError, OSError) as exc:
        raise BackendMissing('the backend is not running') from exc
    with closing(response):
        try:
            for line in response:
                if line.strip():
                    yield json.loads(line)
        except (ValueError, OSError) as exc:
            yield {'event': 'error', 'error': f'lost the backend mid-answer ({exc})'}


def local_events(question, retriever, client):
    """The same events as the backend stream, computed in this process."""
    from .librarian.answer import ask_events, passage_json, to_json
    from .librarian.ollama import OllamaError
    from .librarian.query import keywords
    from .librarian.service import RetrievalError
    try:
        for kind, value in ask_events(question, retriever, client):
            if kind == 'passages':
                passages, warnings = value
                yield {'event': 'passages', 'keywords': keywords(question), 'model': client.model,
                       'passages': [passage_json(n, p) for n, p in enumerate(passages, 1)],
                       'warnings': warnings}
            elif kind == 'text':
                yield {'event': 'text', 'text': value}
            else:
                yield {'event': 'done', **to_json(value)}
    except (RetrievalError, OllamaError, ValueError) as exc:
        yield {'event': 'error', 'error': str(exc)}


def show(events, explain=False, out=None, err=None):
    """Print an answer as it streams. Returns the exit status."""
    from .librarian.answer import NOT_FOUND
    out, err = out or sys.stdout, err or sys.stderr
    print('Searching...', file=err, flush=True)
    streamed = False
    try:
        for event in events:
            kind = event.get('event')
            if kind == 'passages':
                passages = event.get('passages', [])
                if explain:
                    print(f"keywords: {' '.join(event.get('keywords', [])) or '(none)'}", file=err)
                    print(f'passages: {len(passages)} passed the relevance gate', file=err)
                    for p in passages:
                        print(f"  [{p['n']}] score {p['score']:.2f} {p['title']}", file=err)
                if passages:
                    # The model reads every passage before its first word; on the Pi that's a wait.
                    print(f"Reading {len(passages)} passage(s) with {event.get('model') or 'the model'}...",
                          file=err, flush=True)
            elif kind == 'text':
                streamed = True
                print(event['text'], end='', file=out, flush=True)
            elif kind == 'error':
                print(('\n' if streamed else '') + f"error: {event.get('error')}", file=err)
                return 1
            elif kind == 'done':
                print('' if streamed else NOT_FOUND, file=out)
                if event.get('dropped'):
                    print('Removed citations to sources that were not given: '
                          + ', '.join(f'[{n}]' for n in event['dropped']), file=out)
                if event.get('uncited'):
                    print('UNVERIFIED: the answer cites no source.', file=out)
                cited = event.get('cited') or []
                sources = [s for s in event.get('sources', []) if s['n'] in cited or not cited]
                if sources:
                    print('Sources:' if cited else 'Searched:', file=out)
                for s in sources:
                    print(f"[{s['n']}] {s['title']} ({s['source']}) {s['url']}", file=out)
                for warning in event.get('warnings', []):
                    print(f'warning: {warning}', file=err)
                return 0
        print(('\n' if streamed else '') + 'error: the answer ended early', file=err)
        return 1
    except KeyboardInterrupt:
        print('\nStopped.', file=err)
        return 130
    finally:
        close = getattr(events, 'close', None)
        if close:
            close()  # Stops the model too: Ollama cancels when its stream is dropped.


def use_service_kiwix(err):
    """A terminal doesn't see the boot service's settings; borrow them, or say Kiwix is off."""
    from .librarian.answer import service_kiwix_settings
    if os.environ.get('CYBERDECK_KIWIX_URL'):
        return
    saved = service_kiwix_settings()
    if saved.get('CYBERDECK_KIWIX_URL'):
        os.environ.update(saved)
        print(f"Using the cyberdeck-core service's Kiwix settings: {saved['CYBERDECK_KIWIX_URL']} "
              f"book {saved.get('CYBERDECK_KIWIX_BOOK') or '(all)'}", file=err)
    else:
        print('warning: Kiwix is off. Set CYBERDECK_KIWIX_URL (see docs/LIBRARIAN.md); '
              'only local documents are searched.', file=err)


def ask_local(question, explain=False):
    from .librarian.answer import explain as explain_kiwix
    from .librarian.ollama import OllamaClient
    from .librarian.service import Retriever
    use_service_kiwix(sys.stderr)
    try:
        retriever, client = Retriever.from_environment(), OllamaClient.from_environment()
        if explain:
            explain_kiwix(question, retriever, sys.stderr)
    except ValueError as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('\nStopped.', file=sys.stderr)
        return 130
    return show(local_events(question, retriever, client), explain)


def ask(question, explain=False, local=False):
    if not local:
        events = backend_events(question)
        try:
            first = next(events)
        except BackendMissing as exc:
            print(f'Answering here because {exc}.', file=sys.stderr)
        except StopIteration:
            print('error: the backend sent no answer', file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            print('\nStopped.', file=sys.stderr)
            return 130
        else:
            return show(_prepend(first, events), explain)
    return ask_local(question, explain)


def _prepend(first, rest):
    yield first
    try:
        yield from rest
    finally:
        rest.close()


def main(argv=None):
    parser = argparse.ArgumentParser(prog='deck', description='Cyberdeck commands.')
    commands = parser.add_subparsers(dest='command', required=True)
    ask_parser = commands.add_parser('ask', help='answer a question from the offline sources')
    ask_parser.add_argument('question', nargs='+')
    ask_parser.add_argument('--explain', action='store_true',
                            help='show keywords and passage scores (with --local, Kiwix articles too)')
    ask_parser.add_argument('--local', action='store_true',
                            help="answer in this process, even if the backend is running")
    args = parser.parse_args(argv)
    question = ' '.join(args.question).strip()
    if not question or len(question) > 1000:
        parser.error('the question must be 1-1000 characters')
    return ask(question, args.explain, args.local)


if __name__ == '__main__':
    raise SystemExit(main())
