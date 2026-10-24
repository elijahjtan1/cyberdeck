"""BM25 over bounded candidate passages from all sources."""
from collections import Counter
from dataclasses import dataclass, replace
import math
import re

from .query import keywords, match_key, tokens


@dataclass(frozen=True)
class Passage:
    text: str
    title: str
    url: str
    source: str
    score: float = 0.0
    order: int = 0  # Position in its article, so pieces of one page can be read in page order.


def chunks(text, max_chars=1600):
    """Keep sentence boundaries where possible, but bound even unbroken text."""
    text = ' '.join(text.split())
    while len(text) > max_chars:
        prefix = text[:max_chars + 1]
        ends = [m.end() for m in re.finditer(r'[.!?](?=\s)', prefix)]
        cut = ends[-1] if ends and ends[-1] >= max_chars // 2 else prefix.rfind(' ', 0, max_chars + 1)
        if cut <= 0:
            cut = max_chars
        yield text[:cut].strip()
        text = text[cut:].lstrip()
    if text:
        yield text


TITLE_WEIGHT = 3  # A title word counts like this many mentions in the text.
# BM25 gives a word in every passage almost no weight. When every fetched article is on
# the topic ("Morse code", "American Morse code", ...), every passage has the question's
# words, so the best ones scored near zero and missed the threshold. Floor each word's
# weight at that of a word found in half the passages.
MIN_IDF = math.log(2)


def rank_passages(question, passages, budget_tokens=1400, threshold=0.25,
                  min_coverage=0.5):
    """Return positive evidence only; score is relevance, not answer confidence.

    Cost is ceil(len(text)/4) per passage. Titles/URLs and prompt overhead are
    intentionally excluded; the model caller must reserve those separately.
    """
    if type(budget_tokens) is not int or budget_tokens < 0:
        raise ValueError('budget_tokens must be a nonnegative integer')
    if not math.isfinite(threshold) or threshold < 0 or not 0 <= min_coverage <= 1:
        raise ValueError('invalid relevance threshold or coverage')
    terms = {match_key(word) for word in keywords(question)}
    if not terms or not budget_tokens:
        return []
    candidates, counts, lengths, seen = [], [], [], set()
    for passage in passages:
        for part, text in enumerate(chunks(passage.text, min(1600, budget_tokens * 4))):
            key = text.casefold()
            if key in seen:
                continue
            seen.add(key)
            candidates.append(replace(passage, text=text, order=passage.order * 1000 + part))
            # The title counts toward every chunk, and more than a passing mention: a "Table
            # lamps" paragraph rarely says "table lamp", and "How to use a multimeter" should
            # beat a Toasters paragraph that mentions a multimeter once.
            text_count = Counter(match_key(word) for word in tokens(text))
            lengths.append(sum(text_count.values()))
            for word in set(match_key(word) for word in tokens(passage.title)):
                text_count[word] += TITLE_WEIGHT
            counts.append(text_count)
            if len(candidates) >= 1000:
                break
        if len(candidates) >= 1000:
            break
    if not candidates:
        return []
    average = sum(lengths) / len(lengths) or 1
    frequencies = {term: sum(term in count for count in counts) for term in terms}
    scored = []
    for passage, count, length in zip(candidates, counts, lengths):
        if sum(term in count for term in terms) / len(terms) < min_coverage:
            continue
        score = 0.0
        for term in terms:
            tf = count[term]
            df = frequencies[term]
            idf = max(MIN_IDF, math.log(1 + (len(counts) - df + 0.5) / (df + 0.5)))
            score += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * length / average))
        if score > threshold:
            scored.append(replace(passage, score=score))
    scored.sort(key=lambda passage: (-passage.score, passage.url, passage.text))
    best, remaining = [], budget_tokens
    for passage in scored:
        cost = (len(passage.text) + 3) // 4
        if cost <= remaining:
            best.append(passage)
            remaining -= cost
    return best
