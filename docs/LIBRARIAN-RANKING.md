# Keywords and passage ranking

`core.librarian.query.keywords(question)` removes English stopwords and question
boilerplate without a model. It keeps names, numbers, hyphenated terms, C++ and C#,
deduplicates case-insensitively, and limits work to 1,000 characters / 24 keywords.
`search_query(question)` joins those terms for Kiwix. There is no synonym expansion,
stemming, translation, or semantic understanding.

`core.librarian.rank.Passage` has `text`, `title`, `url`, `source` (`kiwix` or `local`),
and `score`. `rank_passages(question, passages, budget_tokens=1400)` compares paragraphs
from all sources using BM25 (k1=1.2, b=0.75). Matching ignores case and accents, so
"cafe" matches "Café". It splits long paragraphs at sentence or
word boundaries, deduplicates text, and considers at most 1,000 chunks. It returns
passages in decreasing score order whose combined estimated token cost fits the budget.
The estimate is `ceil(len(text)/4)` per passage. Prompt text, titles and URLs need a
separate allowance in Phase 3. Very small budgets may return fragments or no results.

The default gate requires a score above 0.25 and at least half of the query keywords
in the passage. Both are configurable. No passing passage means an empty list;
Phase 3 must then say "not found in the offline sources" without calling the model.
This gate indicates lexical overlap, not factual sufficiency or calibrated confidence.
Benchmark its recall against real Pi searches before tuning the threshold.
