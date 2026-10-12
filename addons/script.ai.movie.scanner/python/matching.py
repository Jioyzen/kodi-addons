"""Title scoring shared by TMDb retrieval and scan selection.

Retain translated search titles by TMDb ID; original titles may use any script.
"""
import difflib
import re
import unicodedata


def norm_title(value):
    value = unicodedata.normalize('NFKC', str(value or '')).casefold()
    value = ''.join(c if c.isalnum() else ' ' for c in value)
    value = ' '.join(value.split())
    return re.sub(r'^(the|a|an)\s+', '', value)


def year_of(candidate):
    date = candidate.get('release_date') or candidate.get('first_air_date') or ''
    return int(date[:4]) if len(date) >= 4 and date[:4].isdigit() else None


def candidate_titles(candidate):
    return [candidate.get(key, '') for key in
            ('title', 'name', 'original_title', 'original_name')] + list(candidate.get('_match_titles') or [])


def match_score(candidate, title, year, english_title=''):
    queries = [norm_title(title), norm_title(english_title)]
    if re.search(r'[\u4e00-\u9fff]', title) and re.search(r'[A-Za-z]', title):
        cjk = norm_title(' '.join(re.findall(r'[\u4e00-\u9fff]+[0-9]*', title)))
        latin = norm_title(re.sub(r'[\u4e00-\u9fff]+', ' ', title))
        if len(re.sub(r'[^a-z0-9]', '', latin)) >= 4:
            queries.append(latin)
        if cjk:
            queries.append(cjk)
    names = [norm_title(t) for t in candidate_titles(candidate) if t]
    score = max((difflib.SequenceMatcher(None, q, name).ratio()
                 for q in queries for name in names if q and name), default=0.0)
    cy = year_of(candidate)
    if year and cy:
        score += 0.15 if cy == year else (-0.25 if abs(cy-year) > 1 else 0.05)
    return score


def ambiguous_matches(candidates, title, year, english_title=''):
    """Do not silently choose between different IDs with identical title/year."""
    if not year:
        return []
    queries = {norm_title(t) for t in (title, english_title) if t}
    seen = set()
    exact = []
    for candidate in candidates or []:
        identity = candidate.get('id')
        names = {norm_title(t) for t in candidate_titles(candidate) if t}
        if identity not in seen and year_of(candidate) == year and queries & names:
            seen.add(identity)
            exact.append(candidate)
    return exact if len(exact) > 1 else []


def merge_candidates(candidates, index, incoming):
    """Copy cached responses; keep localized fields and collect aliases by ID."""
    for source in incoming or []:
        identity = source.get('id')
        if identity is None:
            continue
        names = [name for name in candidate_titles(source) if name]
        if identity not in index:
            item = dict(source)
            item['_match_titles'] = list(dict.fromkeys(names))
            index[identity] = item
            candidates.append(item)
        else:
            item = index[identity]
            item['_match_titles'] = list(dict.fromkeys(t for t in candidate_titles(item) + names if t))
