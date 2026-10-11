"""Grounded answers: retrieve passages, then ask the model to answer only from them.

When retrieval finds nothing, the answer is NOT_FOUND and the model is never called.
"""
from dataclasses import dataclass, field

NOT_FOUND = 'Not found in the offline sources.'
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


def answer(question, retriever, client, *, budget_tokens=1400, on_text=None):
    """Raises RetrievalError if every source failed, OllamaError if the model failed.

    Neither error means the answer is absent; callers show the error instead.
    """
    budget = passage_budget(client, budget_tokens)
    if not budget:
        raise ValueError('The model context is too small for any passages; raise num_ctx')
    passages, warnings = retriever.run(question, budget)
    if not passages:
        return Answer(question, NOT_FOUND, False, [], warnings)
    reply = client.chat(build_messages(question, passages), on_text=on_text, detail=question)
    if not reply.text or said_not_found(reply.text):
        return Answer(question, NOT_FOUND, False, passages, warnings, reply)
    return Answer(question, reply.text, True, passages, warnings, reply)


def main(argv=None):
    import argparse
    import sys
    from .ollama import OllamaClient, OllamaError
    from .service import RetrievalError, Retriever
    parser = argparse.ArgumentParser(description='Answer a question from the offline sources.')
    parser.add_argument('question')
    args = parser.parse_args(argv)
    try:
        result = answer(args.question, Retriever.from_environment(), OllamaClient.from_environment(),
                        on_text=lambda t: print(t, end='', flush=True))
    except (RetrievalError, OllamaError, ValueError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 1
    # The model's words were already streamed; only a skipped or empty reply needs the message.
    print(NOT_FOUND if result.reply is None or not result.reply.text else '')
    if result.sources:
        print('Sources:' if result.found else 'Searched:')
    for n, passage in enumerate(result.sources, 1):
        print(f'[{n}] {passage.title} ({passage.source}) {passage.url}')
    for warning in result.warnings:
        print(f'warning: {warning}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
