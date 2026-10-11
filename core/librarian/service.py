"""Combine offline sources; source failures are distinct from no evidence."""
from contextlib import nullcontext
import os
import re

from .extract import fetch_article
from .kiwix import KiwixClient, KiwixError, SearchResult
from .library import LibraryError, LocalLibrary
from .query import search_query
from .rank import Passage, rank_passages, chunks


class RetrievalError(RuntimeError):
    pass


# Wiki translation subpages such as "Table lamps/fr" take result slots from other articles.
LANGUAGE_PAGE = re.compile(r'/([a-z]{2}(?:-[a-z]+)?)$')


def english_title(title):
    """The title to dedupe on: "Table lamps/en" is "Table lamps"; other languages are None."""
    match = LANGUAGE_PAGE.search(title)
    if not match:
        return title
    return title[:match.start()] if match.group(1) == 'en' else None


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
                    found = self.kiwix_search(query)
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

    def kiwix_search(self, query, limit=5):
        """kiwix-serve only returns articles containing every word (and reads "OR" as just
        another word), so one word the wiki doesn't use finds nothing: "table lamp flickers
        check" misses Table lamps. Fall back to adjacent word pairs, then single words, until
        there are enough articles; passage ranking then drops the ones that don't fit."""
        terms = query.split()
        pairs = [' '.join(terms[i:i + 2]) for i in range(len(terms) - 1)] if len(terms) > 2 else []
        found, seen = [], set()
        for pattern in dict.fromkeys([query, *pairs, *(terms if len(terms) > 1 else [])]):
            try:
                # Extra rows because translated copies of a page are skipped below.
                rows = self.kiwix.search(pattern, limit=min(limit * 3, 20))
            except KiwixError:
                if found:
                    return found  # Keep what earlier searches found.
                raise  # Kiwix is down; don't wait out a timeout per fallback.
            for row in rows:
                key = english_title(row.title)
                if key and key not in seen:
                    seen.add(key)
                    found.append(row)
                if len(found) >= limit:
                    return found
        return found

    def retrieve(self, question, budget_tokens=1400):
        return self.run(question, budget_tokens)[0]

    def search(self, question):
        return self.run(question, search_only=True)[0]
