"""Combine offline sources; source failures are distinct from no evidence."""
from contextlib import nullcontext
import os

from .extract import fetch_article
from .kiwix import KiwixClient, KiwixError, SearchResult
from .library import LibraryError, LocalLibrary
from .query import search_query
from .rank import Passage, rank_passages, chunks


class RetrievalError(RuntimeError):
    pass


class Retriever:
    def __init__(self, kiwix=None, library=None, telemetry=None, threshold=0.25):
        self.kiwix, self.library, self.telemetry = kiwix, library, telemetry
        self.threshold = threshold

    @classmethod
    def from_environment(cls, telemetry=None):
        base_url = os.environ.get('CYBERDECK_KIWIX_URL', '')
        kiwix = KiwixClient(base_url, book=os.environ.get('CYBERDECK_KIWIX_BOOK', ''),
                            book_parameter=os.environ.get('CYBERDECK_KIWIX_BOOK_PARAMETER', 'books.name')) if base_url else None
        library = LocalLibrary(os.environ.get('CYBERDECK_LIBRARY_DIR'),
                               os.environ.get('CYBERDECK_LIBRARY_DB'))
        return cls(kiwix, library, telemetry)

    def run(self, question, budget_tokens=1400, *, search_only=False):
        if not isinstance(question, str) or len(question) > 1000:
            raise ValueError('q must be text of at most 1000 characters')
        if type(budget_tokens) is not int or not 0 <= budget_tokens <= 10000:
            raise ValueError('budget_tokens must be an integer from 0 to 10000')
        query = search_query(question)
        activity = self.telemetry.activity('search', question) if self.telemetry else nullcontext()
        with activity:
            if not query or (not search_only and budget_tokens == 0):
                return [], []
            candidates, results, errors = [], [], []
            if self.library:
                try:
                    candidates = self.library.search(question)
                except LibraryError as exc:
                    errors.append(str(exc))
            if search_only:
                seen = set()
                for row in candidates:
                    if row.url not in seen:
                        seen.add(row.url)
                        results.append(SearchResult(row.title, row.url, row.text[:400], 'local'))
                    if len(results) == 10:
                        break
            if self.kiwix:
                try:
                    found = self.kiwix.search(query, limit=5)
                    if search_only:
                        results.extend(found)
                    else:
                        for result in found:
                            try:
                                rows = fetch_article(self.kiwix, result.url)
                                count = 0
                                for row in rows:
                                    for text in chunks(row):
                                        candidates.append(Passage(text, result.title, result.url, 'kiwix'))
                                        count += 1
                                        if count >= 150:
                                            break
                                    if count >= 150:
                                        break
                            except KiwixError as exc:
                                errors.append(str(exc))
                except KiwixError as exc:
                    errors.append(str(exc))
            if not search_only:
                results = rank_passages(question, candidates, budget_tokens, threshold=self.threshold)
            if errors and not results:
                raise RetrievalError('; '.join(dict.fromkeys(errors)))
            return results, list(dict.fromkeys(errors))

    def retrieve(self, question, budget_tokens=1400):
        return self.run(question, budget_tokens)[0]

    def search(self, question):
        return self.run(question, search_only=True)[0]
