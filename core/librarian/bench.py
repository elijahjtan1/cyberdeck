"""Librarian benchmark: retrieval hit rate, honesty, and timing on the Pi.

Each question names the article titles that should answer it, or none when the offline
sources shouldn't contain an answer. Judge the librarian by whether it finds the right
article and admits what it doesn't know, not by the prose.
"""
from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import statistics
import threading
import time

from .answer import ask_events
from .query import fold

DEFAULT_QUESTIONS = Path(__file__).with_name('benchmarks') / 'restarters.json'


@dataclass
class Result:
    question: str
    expect: list
    titles: list = field(default_factory=list)  # Distinct passage titles, best first.
    rank: int = 0  # 1-based position of the first expected title; 0 when missed.
    retrieval_s: float = 0.0
    first_word_s: float = None  # From asking to the first answer text; None without one.
    total_s: float = 0.0
    found: bool = False
    uncited: bool = False
    error: str = ''
    model: bool = True  # False for retrieval-only runs; found then means "had passages".

    @property
    def answerable(self):
        return bool(self.expect)

    @property
    def top3(self):
        return 0 < self.rank <= 3

    @property
    def honest(self):
        """Out-of-scope questions should end as "not found" (or with no passages)."""
        return not self.answerable and not self.error and not self.found


def load_questions(path=DEFAULT_QUESTIONS):
    rows = json.loads(Path(path).read_text(encoding='utf-8'))
    questions = []
    for row in rows:
        if not isinstance(row, dict) or not str(row.get('q', '')).strip():
            raise ValueError(f'{path}: each entry needs "q" and an "expect" list of titles')
        questions.append((row['q'].strip(), [str(t) for t in row.get('expect', [])]))
    return questions


def first_match(titles, expect):
    wanted = [fold(t) for t in expect]
    for rank, title in enumerate(titles, 1):
        if any(w in fold(title) for w in wanted):
            return rank
    return 0


class MemoryWatch:
    """Samples MemAvailable and Ollama's resident memory while the benchmark runs.

    The model runs in Ollama's own process, so this process's memory says little.
    """

    def __init__(self, interval=0.5, proc=Path('/proc')):
        self.interval, self.proc = interval, proc
        self.min_available_mib = None
        self.max_ollama_mib = None
        self._stop = threading.Event()
        self._thread = None

    def sample(self):
        try:
            for line in (self.proc / 'meminfo').read_text().splitlines():
                if line.startswith('MemAvailable:'):
                    mib = int(line.split()[1]) // 1024
                    if self.min_available_mib is None or mib < self.min_available_mib:
                        self.min_available_mib = mib
        except (OSError, ValueError, IndexError):
            return
        total = 0
        for entry in self.proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                if not (entry / 'comm').read_text().startswith('ollama'):
                    continue
                for line in (entry / 'status').read_text().splitlines():
                    if line.startswith('VmRSS:'):
                        total += int(line.split()[1]) // 1024
            except (OSError, ValueError, IndexError):
                continue  # The process ended between listing and reading.
        if total and (self.max_ollama_mib is None or total > self.max_ollama_mib):
            self.max_ollama_mib = total

    def _run(self):
        while not self._stop.wait(self.interval):
            self.sample()

    def __enter__(self):
        if (self.proc / 'meminfo').exists():
            self.sample()
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread:
            self._thread.join()
            self.sample()


def run_one(question, expect, retriever, client, *, use_model=True, clock=time.monotonic):
    result = Result(question, expect, model=use_model)
    start = clock()
    try:
        if not use_model:
            passages, _ = retriever.run(question, 1400)
            result.retrieval_s = clock() - start
            result.titles = list(dict.fromkeys(p.title for p in passages))
            result.found = bool(passages)
        else:
            for kind, value in ask_events(question, retriever, client):
                if kind == 'passages':
                    result.retrieval_s = clock() - start
                    result.titles = list(dict.fromkeys(p.title for p in value[0]))
                elif kind == 'text' and result.first_word_s is None:
                    result.first_word_s = clock() - start
                elif kind == 'done':
                    result.found, result.uncited = value.found, value.uncited
    except Exception as exc:  # Any failure is recorded; one bad question shouldn't end the run.
        result.error = str(exc) or type(exc).__name__
    result.total_s = clock() - start
    result.rank = first_match(result.titles, expect)
    return result


def summarize(results, memory=None):
    answerable = [r for r in results if r.answerable]
    unanswerable = [r for r in results if not r.answerable]
    answered = [r for r in answerable if r.found]
    firsts = [r.first_word_s for r in results if r.first_word_s is not None]
    totals = [r.total_s for r in results if not r.error]

    used_model = any(r.model for r in results)

    def rate(hits, rows, needs_model=False):
        return round(hits / len(rows), 3) if rows and (used_model or not needs_model) else None

    def spread(values):
        return {'median': round(statistics.median(values), 2), 'max': round(max(values), 2)} if values else None

    return {
        'questions': len(results),
        'errors': sum(bool(r.error) for r in results),
        'top3_hit_rate': rate(sum(r.top3 for r in answerable), answerable),
        'answered_rate': rate(len(answered), answerable, True),
        'uncited_rate': rate(sum(r.uncited for r in answered), answered, True),
        'honest_not_found_rate': rate(sum(r.honest for r in unanswerable), unanswerable, True),
        'retrieval_s': spread([r.retrieval_s for r in results if not r.error]),
        'first_word_s': spread(firsts),
        'total_s': spread(totals),
        'min_mem_available_mib': memory.min_available_mib if memory else None,
        'peak_ollama_rss_mib': memory.max_ollama_mib if memory else None,
    }


def show_result(n, r, out):
    if r.error:
        verdict = f'ERROR {r.error}'
    elif r.answerable:
        verdict = (f'top-3 hit (#{r.rank})' if r.top3 else f'hit #{r.rank}' if r.rank else 'MISS')
        if r.model and r.found and r.uncited:
            verdict += ', UNVERIFIED'
        elif r.model and not r.found:
            verdict += ', said not found'
    elif not r.model:
        verdict = 'no passages' if not r.found else 'passages found (model would decide)'
    else:
        verdict = 'honest: not found' if r.honest else 'ANSWERED an out-of-scope question'
    first = f'{r.first_word_s:.1f}s' if r.first_word_s is not None else '-'
    print(f'{n:2}. {verdict} | first word {first}, total {r.total_s:.1f}s | {r.question}', file=out)
    if r.answerable and not r.top3 and r.titles:
        print(f'    got: {", ".join(r.titles[:3])}', file=out)


def show_summary(s, out):
    def pct(value):
        return '-' if value is None else f'{value * 100:.0f}%'

    def secs(value):
        return '-' if value is None else f"{value['median']}s median, {value['max']}s max"

    print('', file=out)
    print(f"Top-3 hit rate:        {pct(s['top3_hit_rate'])}", file=out)
    print(f"Answered (in scope):   {pct(s['answered_rate'])}, of which uncited {pct(s['uncited_rate'])}", file=out)
    print(f"Honest 'not found':    {pct(s['honest_not_found_rate'])}", file=out)
    print(f"Retrieval:             {secs(s['retrieval_s'])}", file=out)
    print(f"Time to first word:    {secs(s['first_word_s'])}", file=out)
    print(f"Total per question:    {secs(s['total_s'])}", file=out)
    if s['min_mem_available_mib'] is not None:
        ollama = s['peak_ollama_rss_mib']
        print(f"Memory:                lowest available {s['min_mem_available_mib']} MiB"
              + (f', Ollama peak {ollama} MiB' if ollama else ''), file=out)
    if s['errors']:
        print(f"Errors:                {s['errors']}", file=out)


def run(questions, retriever, client, *, use_model=True, out=None, json_path=None):
    import sys
    out = out or sys.stdout
    results = []
    with MemoryWatch() as memory:
        for n, (question, expect) in enumerate(questions, 1):
            result = run_one(question, expect, retriever, client, use_model=use_model)
            results.append(result)
            show_result(n, result, out)
    summary = summarize(results, memory)
    show_summary(summary, out)
    if json_path:
        data = {'summary': summary, 'model': getattr(client, 'model', None) if use_model else None,
                'results': [asdict(r) for r in results]}
        Path(json_path).expanduser().write_text(json.dumps(data, indent=1), encoding='utf-8')
        print(f'Saved {json_path}', file=out)
    return summary


def main(argv=None):
    import argparse
    import sys
    from ..cli import use_service_kiwix
    from .ollama import OllamaClient
    from .service import Retriever
    parser = argparse.ArgumentParser(prog='deck bench', description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('questions', nargs='?', default=str(DEFAULT_QUESTIONS),
                        help='JSON list of {"q": ..., "expect": [titles]} (default: Restarters set)')
    parser.add_argument('--no-model', action='store_true', help='measure retrieval only; fast')
    parser.add_argument('--limit', type=int, help='run only the first N questions')
    parser.add_argument('--json', help='also save the results to this file')
    args = parser.parse_args(argv)
    try:
        questions = load_questions(args.questions)[:args.limit]
    except (OSError, ValueError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 1
    use_service_kiwix(sys.stderr)
    if not os.environ.get('CYBERDECK_KIWIX_URL'):
        print('warning: without Kiwix most questions will miss.', file=sys.stderr)
    try:
        retriever, client = Retriever.from_environment(), OllamaClient.from_environment()
        run(questions, retriever, client, use_model=not args.no_model, json_path=args.json)
    except ValueError as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('\nStopped.', file=sys.stderr)
        return 130
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
