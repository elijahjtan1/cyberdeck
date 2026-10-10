"""Librarian JSON API, plus safe plain-text local document links."""
from dataclasses import asdict
from threading import Lock

from flask import Blueprint, Response, jsonify, request

from .library import LibraryError
from .query import keywords
from .service import RetrievalError, Retriever


def create_blueprint(telemetry=None, retriever=None):
    api = Blueprint('librarian', __name__)
    lock = Lock()

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
                           results=[asdict(row) for row in rows], warnings=warnings,
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
