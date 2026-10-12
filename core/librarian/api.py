"""Librarian JSON API, plus safe plain-text local document links."""
from dataclasses import asdict
import json
from threading import Lock
from urllib.error import HTTPError

from flask import Blueprint, Response, jsonify, request

from .answer import ask_events, passage_json, to_json
from .extract import fetch_article
from .kiwix import KiwixError
from .library import LibraryError
from .ollama import OllamaClient, OllamaError
from .query import keywords
from .service import RetrievalError, Retriever


# The panel reader pages through these; a long article is cut rather than stalling it.
ARTICLE_PARAGRAPHS = 400
ARTICLE_CHARS = 120_000


def result_json(row):
    """A search result or passage, without the internal page-order sort key."""
    data = asdict(row)
    data.pop('order', None)
    return data


def create_blueprint(telemetry=None, retriever=None, client=None):
    api = Blueprint('librarian', __name__)
    lock = Lock()
    asking = Lock()  # The model uses most of the Pi; answer one question at a time.

    def service():
        return retriever if retriever is not None else Retriever.from_environment(telemetry)

    def query(search_only):
        question = request.args.get('q', '').strip()
        if not question or len(question) > 1000:
            return jsonify(error='q must contain 1-1000 characters'), 400
        try:
            budget = int(request.args.get('budget_tokens', 1400))
        except ValueError:
            return jsonify(error='budget_tokens must be an integer'), 400
        if not 0 <= budget <= 10000:
            return jsonify(error='budget_tokens must be from 0 to 10000'), 400
        if not lock.acquire(blocking=False):
            return jsonify(error='A search is already running; try again shortly'), 503
        try:
            rows, warnings = service().run(question, budget, search_only=search_only)
            return jsonify(query=question, keywords=keywords(question),
                           results=[result_json(row) for row in rows], warnings=warnings,
                           partial=bool(warnings))
        except (RetrievalError, ValueError) as exc:
            return jsonify(error=str(exc)), 503
        finally:
            lock.release()

    @api.get('/api/search')
    def search():
        return query(True)

    @api.get('/api/retrieve')
    def retrieve():
        return query(False)

    @api.get('/api/ask')
    def ask():
        question = request.args.get('q', '').strip()
        if not question or len(question) > 1000:
            return jsonify(error='q must contain 1-1000 characters'), 400
        try:
            model = client if client is not None else OllamaClient.from_environment(telemetry)
            sources = service()
        except ValueError as exc:
            return jsonify(error=str(exc)), 503
        if not asking.acquire(blocking=False):
            return jsonify(error='Another question is being answered; try again shortly'), 503
        events = ask_events(question, sources, model)
        if request.args.get('stream') not in ('1', 'true'):
            try:
                for kind, value in events:
                    if kind == 'done':
                        return jsonify(to_json(value))
            except (RetrievalError, OllamaError, ValueError) as exc:
                return jsonify(error=str(exc)), 503
            finally:
                asking.release()

        def stream():
            # One JSON object per line: passages, text pieces, then done (or error).
            try:
                for kind, value in events:
                    if kind == 'passages':
                        passages, warnings = value
                        row = {'event': 'passages', 'keywords': keywords(question),
                               'model': model.model, 'warnings': warnings,
                               'passages': [passage_json(n, p) for n, p in enumerate(passages, 1)]}
                    elif kind == 'text':
                        row = {'event': 'text', 'text': value}
                    else:
                        row = {'event': 'done', **to_json(value)}
                    yield json.dumps(row) + '\n'
            except (RetrievalError, OllamaError, ValueError) as exc:
                yield json.dumps({'event': 'error', 'error': str(exc)}) + '\n'
            finally:
                done()

        released = []

        def done():
            # Runs from the generator and from the server's close(), which also covers a client
            # that leaves before the first line; release exactly once.
            if not released:
                released.append(True)
                events.close()  # Closing the Ollama stream stops the model.
                asking.release()

        response = Response(stream(), content_type='application/x-ndjson',
                            headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})
        response.call_on_close(done)
        return response

    @api.get('/api/article')
    def article():
        """Plain-text paragraphs of one Kiwix article, for reading on the panel. Only URLs on
        the configured loopback kiwix-serve are fetched; nothing touches the model."""
        url = request.args.get('url', '').strip()
        if not url or len(url) > 2000:
            return jsonify(error='url must contain 1-2000 characters'), 400
        kiwix = service().kiwix
        if kiwix is None:
            return jsonify(error='Kiwix is not configured'), 503
        try:
            url = kiwix.offline_url(url)
        except KiwixError as exc:
            return jsonify(error=str(exc)), 400
        if not lock.acquire(blocking=False):
            return jsonify(error='A search is already running; try again shortly'), 503
        try:
            rows = fetch_article(kiwix, url)
        except KiwixError as exc:
            if isinstance(exc.__cause__, HTTPError) and exc.__cause__.code == 404:
                return jsonify(error='Kiwix has no such article'), 404
            return jsonify(error=str(exc)), 502
        finally:
            lock.release()
        kept, size = [], 0
        for row in rows[:ARTICLE_PARAGRAPHS]:
            if size + len(row) > ARTICLE_CHARS:
                break
            kept.append(row)
            size += len(row)
        return jsonify(url=url, paragraphs=kept, truncated=len(kept) < len(rows))

    @api.get('/api/library/<path:relative>')
    def document(relative):
        try:
            library = service().library
            if library is None:
                raise LibraryError('Local library is not configured')
            text = library.read_document(relative)
            return Response(text, content_type='text/plain; charset=utf-8', headers={
                'X-Content-Type-Options': 'nosniff', 'Content-Security-Policy': "default-src 'none'; sandbox"})
        except (LibraryError, ValueError) as exc:
            return jsonify(error=str(exc)), 404

    return api
