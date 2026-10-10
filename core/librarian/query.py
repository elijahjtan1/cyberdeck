"""Deterministic English keywords; no model, network or stemming dependency."""
import re
import unicodedata

TOKEN = re.compile(r"[^\W_]+(?:[.\-'’][^\W_]+)*(?:\+\+|#)?", re.UNICODE)
STOPWORDS = frozenset('''a an the and or but if then than as at by for from in into of
on onto to with without about above below between through during before after over
under is are was were be been being am do does did doing have has had having can
could shall should will would may might must i me my we our you your he him his she
her it its they them their this that these those there here what which who whom whose
where when why how please tell explain describe give find show know work works working
some any each every all both many much more most such also just really not no so
'''.split())


def tokens(text):
    return TOKEN.findall(unicodedata.normalize('NFKC', text))


def keywords(question, limit=24):
    """Preserve spelling of names, numbers, hyphens, C++ and C#; deduplicate."""
    found, seen = [], set()
    for word in tokens(question[:1000]):
        key = word.casefold()
        if key in STOPWORDS or key in seen:
            continue
        seen.add(key)
        found.append(word)
        if len(found) >= limit:
            break
    return found if limit > 0 else []


def search_query(question):
    return ' '.join(keywords(question))
