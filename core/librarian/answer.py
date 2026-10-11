"""Grounded answers: retrieve passages, then ask the model to answer only from them.

When retrieval finds nothing, the answer is NOT_FOUND and the model is never called.
Citations to sources that weren't given are removed; an answer citing nothing is flagged.
"""
from dataclasses import dataclass, field
import re

NOT_FOUND = 'Not found in the offline sources.'
# [1] or [1, 3]; anything else in brackets is left alone.
CITATION = re.compile(r'\s*\[(\d+(?:\s*,\s*\d+)*)\]')
# Room for the instructions, titles and question around the passages (estimated tokens).
PROMPT_OVERHEAD = 250

SYSTEM_PROMPT = (
    'You are an offline librarian. Answer the question using only the numbered sources. '
    'Cite every fact with its source number in square brackets, like [1]. '
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


def passage_budget(client, budget_tokens=1400):
    """Fit passages, prompt and answer inside the model's context window."""
    room = client.num_ctx - client.num_predict - PROMPT_OVERHEAD
    return max(0, min(budget_tokens, room))


def build_messages(question, passages):
    blocks = [f'[{n}] {p.title}\n{p.text}' for n, p in enumerate(passages, 1)]
    user = 'Sources:\n\n' + '\n\n'.join(blocks) + f'\n\nQuestion: {question.strip()}'
    return [{'role': 'system', 'content': SYSTEM_PROMPT},
            {'role': 'user', 'content': user}]


def said_not_found(text):
    return text.strip().rstrip('.').casefold() == NOT_FOUND.rstrip('.').casefold()


def answer(question, retriever, client, *, budget_tokens=1400, on_text=None, on_passages=None):
    """Raises RetrievalError if every source failed, OllamaError if the model failed.

    Neither error means the answer is absent; callers show the error instead.
    """
    budget = passage_budget(client, budget_tokens)
    if not budget:
        raise ValueError('The model context is too small for any passages; raise num_ctx')
    passages, warnings = retriever.run(question, budget)
    if on_passages:
        on_passages(passages)
    if not passages:
        return Answer(question, NOT_FOUND, False, [], warnings)
    reply = client.chat(build_messages(question, passages), on_text=on_text, detail=question)
    if not reply.text or said_not_found(reply.text):
        return Answer(question, NOT_FOUND, False, passages, warnings, reply)
    text, cited, dropped = check_citations(reply.text, len(passages))
    return Answer(question, text, True, passages, warnings, reply, cited, dropped)


def check_citations(text, count):
    """Keep [n] citations for sources 1..count; remove the rest.

    Returns (text, cited, dropped): cited and dropped list numbers in order of first use.
    """
    cited, dropped = [], []

    def keep(match):
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

    text = CITATION.sub(keep, text)
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
    import argparse
    import os
    import sys
    from .ollama import OllamaClient, OllamaError
    from .service import RetrievalError, Retriever
    parser = argparse.ArgumentParser(description='Answer a question from the offline sources.')
    parser.add_argument('question')
    parser.add_argument('--explain', action='store_true',
                        help='show keywords, Kiwix articles and passage scores')
    args = parser.parse_args(argv)
    if not os.environ.get('CYBERDECK_KIWIX_URL'):
        # A terminal doesn't see the boot service's settings; borrow them rather than
        # silently searching without Kiwix.
        saved = service_kiwix_settings()
        if saved.get('CYBERDECK_KIWIX_URL'):
            os.environ.update(saved)
            print(f"Using the cyberdeck-core service's Kiwix settings: {saved['CYBERDECK_KIWIX_URL']} "
                  f"book {saved.get('CYBERDECK_KIWIX_BOOK') or '(all)'}", file=sys.stderr)
        else:
            print('warning: Kiwix is off. Set CYBERDECK_KIWIX_URL (see docs/LIBRARIAN.md); '
                  'only local documents are searched.', file=sys.stderr)
    try:
        retriever, client = Retriever.from_environment(), OllamaClient.from_environment()
        if args.explain:
            explain(args.question, retriever, sys.stderr)

        def show(passages):
            print(f'passages: {len(passages)} passed the relevance gate', file=sys.stderr)
            for n, passage in enumerate(passages, 1):
                print(f'  [{n}] score {passage.score:.2f} {passage.title}', file=sys.stderr)

        result = answer(args.question, retriever, client,
                        on_text=lambda t: print(t, end='', flush=True),
                        on_passages=show if args.explain else None)
    except (RetrievalError, OllamaError, ValueError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 1
    # The model's words were already streamed; only a skipped or empty reply needs the message.
    print(NOT_FOUND if result.reply is None or not result.reply.text else '')
    if result.dropped:
        print('Removed citations to sources that were not given: '
              + ', '.join(f'[{n}]' for n in result.dropped))
    if result.uncited:
        print('UNVERIFIED: the answer cites no source.')
    if result.sources:
        print('Sources:' if result.cited else 'Searched:')
    for n, passage in enumerate(result.sources, 1):
        if n in result.cited or not result.cited:
            print(f'[{n}] {passage.title} ({passage.source}) {passage.url}')
    for warning in result.warnings:
        print(f'warning: {warning}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
