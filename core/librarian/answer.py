"""Grounded answers: retrieve passages, then ask the model to answer only from them.

When retrieval finds nothing, the answer is NOT_FOUND and the model is never called.
Citations to sources that weren't given are removed; an answer citing nothing is flagged.
"""
from dataclasses import dataclass, field, replace
import os
import re

NOT_FOUND = 'Not found in the offline sources.'
# [1] or [1, 3]; anything else in brackets is left alone.
CITATION = re.compile(r'\s*\[(\d+(?:\s*,\s*\d+)*)\]')
# Room for the instructions, titles and question around the passages (estimated tokens).
PROMPT_OVERHEAD = 250
# Passage tokens the model reads before answering. On the Pi most of the wait before the
# first word is spent reading them, so this trades detail for speed.
DEFAULT_BUDGET = 400  # On the Pi: first word 28 s median, vs 45 s at 700 and 69 s at 1,400.

SYSTEM_PROMPT = (
    'You are an offline librarian. Answer the question using only the numbered sources. '
    'Cite every fact with its source number in square brackets, like [1]. '
    'Answer the question itself; do not describe or list the sources. '
    'Be brief: a few sentences. '
    f'If the sources do not contain the answer, reply exactly: {NOT_FOUND}'
)


@dataclass(frozen=True)
class Answer:
    question: str
    text: str
    found: bool
    sources: list = field(default_factory=list)  # Passages; source [n] is sources[n - 1].
    warnings: list = field(default_factory=list)
    reply: object = None  # The model's Reply, or None when the model was not called.
    cited: list = field(default_factory=list)  # Valid source numbers, in order of first use.
    dropped: list = field(default_factory=list)  # Cited numbers with no matching source.

    @property
    def uncited(self):
        """A found answer that names no source can't be traced back; show it as unverified."""
        return self.found and not self.cited


def budget_from_environment():
    """CYBERDECK_LIBRARIAN_BUDGET, or DEFAULT_BUDGET when unset."""
    raw = os.environ.get('CYBERDECK_LIBRARIAN_BUDGET', '').strip()
    if not raw:
        return DEFAULT_BUDGET
    try:
        value = int(raw)
    except ValueError:
        raise ValueError('CYBERDECK_LIBRARIAN_BUDGET must be an integer') from None
    if not 100 <= value <= 10000:
        raise ValueError('CYBERDECK_LIBRARIAN_BUDGET must be from 100 to 10000')
    return value


def passage_budget(client, budget_tokens=DEFAULT_BUDGET):
    """Fit passages, prompt and answer inside the model's context window."""
    room = client.num_ctx - client.num_predict - PROMPT_OVERHEAD
    return max(0, min(budget_tokens, room))


def by_article(passages):
    """One source per article, best article first, its paragraphs in page order: eleven
    paragraphs of one page numbered [1]-[11] led a small model to describe its sources
    instead of answering."""
    pieces = {}
    for passage in passages:
        pieces.setdefault(passage.url, []).append(passage)
    merged = []
    for group in pieces.values():
        text = '\n\n'.join(p.text for p in sorted(group, key=lambda p: p.order))
        merged.append(replace(group[0], text=text))
    return merged


def build_messages(question, passages):
    blocks = [f'[{n}] {p.title}\n{p.text}' for n, p in enumerate(passages, 1)]
    user = 'Sources:\n\n' + '\n\n'.join(blocks) + f'\n\nQuestion: {question.strip()}'
    return [{'role': 'system', 'content': SYSTEM_PROMPT},
            {'role': 'user', 'content': user}]


# Wrapping a small model puts around the not-found sentence: quotes, bold, the full stop.
DECORATION = ' \t\n.*_"\'`'


def said_not_found(text):
    """The not-found sentence, even with the [1], quotes or bold a small model adds.

    Text after it counts as an answer only if it cites a source; uncited remarks such as
    "The sources only discuss lamps." don't turn "not found" into a found answer.
    """
    phrase = NOT_FOUND.rstrip('.').casefold()
    start = text.casefold().find(phrase)
    if start < 0 or text[:start].strip(DECORATION):
        return False
    rest = text[start + len(phrase):].lstrip(DECORATION)
    # "...sources. [1]" or "[1][2]" cites the sentence, not a fact.
    while citation := CITATION.match(rest):
        rest = rest[citation.end():].lstrip(DECORATION)
    return not CITATION.search(rest)


def ask_events(question, retriever, client, *, budget_tokens=None):
    """Yield ('passages', (passages, warnings)), then ('text', piece)..., then ('done', Answer).

    Raises RetrievalError if every source failed, OllamaError if the model failed, and
    ValueError for bad settings. None of them means the answer is absent.
    budget_tokens defaults to CYBERDECK_LIBRARIAN_BUDGET.
    """
    if budget_tokens is None:
        budget_tokens = budget_from_environment()
    budget = passage_budget(client, budget_tokens)
    if not budget:
        raise ValueError('The model context is too small for any passages; raise num_ctx')
    passages, warnings = retriever.run(question, budget)
    passages = by_article(passages)
    yield 'passages', (passages, warnings)
    if not passages:
        yield 'done', Answer(question, NOT_FOUND, False, [], warnings)
        return
    for item in client.iter_chat(build_messages(question, passages), detail=question):
        if isinstance(item, str):
            yield 'text', item
            continue
        if not item.text or said_not_found(item.text):
            yield 'done', Answer(question, NOT_FOUND, False, passages, warnings, item)
        else:
            text, cited, dropped = check_citations(item.text, len(passages))
            yield 'done', Answer(question, text, True, passages, warnings, item, cited, dropped)


def answer(question, retriever, client, *, budget_tokens=None, on_text=None, on_passages=None):
    """Run ask_events to the end; the callbacks see passages and streamed text."""
    for kind, value in ask_events(question, retriever, client, budget_tokens=budget_tokens):
        if kind == 'passages' and on_passages:
            on_passages(value[0])
        elif kind == 'text' and on_text:
            on_text(value)
        elif kind == 'done':
            return value


def to_json(result):
    """Plain dict for the API; sources keep their [n] numbers."""
    reply = result.reply
    return {
        'question': result.question, 'answer': result.text, 'found': result.found,
        'uncited': result.uncited, 'cited': result.cited, 'dropped': result.dropped,
        'sources': [passage_json(n, p) for n, p in enumerate(result.sources, 1)],
        'warnings': result.warnings, 'partial': bool(result.warnings),
        'model': reply.model if reply else None,
        'seconds': reply.seconds if reply else 0.0,
        'output_tokens': reply.output_tokens if reply else 0,
    }


def passage_json(n, passage):
    return {'n': n, 'title': passage.title, 'url': passage.url, 'source': passage.source,
            'score': round(passage.score, 3), 'text': passage.text}


def keep_citation(match, count, cited, dropped):
    """Rewrite one CITATION match to the numbers 1..count, or '' if none are; records both."""
    good = []
    for number in (int(n) for n in match.group(1).split(',')):
        if 1 <= number <= count:
            good.append(number)
            if number not in cited:
                cited.append(number)
        elif number not in dropped:
            dropped.append(number)
    if not good:
        return ''
    space = match.group(0)[:len(match.group(0)) - len(match.group(0).lstrip())]
    return space + '[' + ', '.join(str(n) for n in dict.fromkeys(good)) + ']'


def check_citations(text, count):
    """Keep [n] citations for sources 1..count; remove the rest.

    Returns (text, cited, dropped): cited and dropped list numbers in order of first use.
    """
    cited, dropped = [], []
    text = CITATION.sub(lambda match: keep_citation(match, count, cited, dropped), text)
    return re.sub(r'[ \t]+([.,;:!?])', r'\1', text).strip(), cited, dropped


SERVICE_UNIT = '/etc/systemd/system/cyberdeck-core.service'


def service_kiwix_settings(path=SERVICE_UNIT):
    """Kiwix settings the boot service was installed with (setup_core_service.py --kiwix-*)."""
    try:
        with open(path, encoding='utf-8') as unit:
            lines = unit.read().splitlines()
    except OSError:
        return {}
    settings = {}
    for line in lines:
        key, _, value = line.removeprefix('Environment=').partition('=')
        if line.startswith('Environment=') and key.startswith('CYBERDECK_KIWIX_'):
            settings[key] = value
    return settings


def explain(question, retriever, out):
    """Show each retrieval step, to tell missing data from bad keywords or a strict gate."""
    from .query import search_query
    query = search_query(question)
    print(f'keywords: {query or "(none: only stopwords)"}', file=out)
    if retriever.kiwix is None:
        print('kiwix: off', file=out)
    elif query:
        try:
            hits = retriever.kiwix.search(query, limit=5)
            print(f'kiwix: {len(hits)} article(s) for "{query}"', file=out)
            for hit in hits:
                print(f'  - {hit.title}', file=out)
        except Exception as exc:  # Diagnostics only; the real run reports errors itself.
            print(f'kiwix: error: {exc}', file=out)


def main(argv=None):
    """Answer in this process, always; `deck ask` prefers the running backend."""
    import argparse
    from ..cli import ask_local
    parser = argparse.ArgumentParser(description='Answer a question from the offline sources.')
    parser.add_argument('question')
    parser.add_argument('--explain', action='store_true',
                        help='show keywords, Kiwix articles and passage scores')
    args = parser.parse_args(argv)
    return ask_local(args.question, explain=args.explain)


if __name__ == '__main__':
    raise SystemExit(main())
