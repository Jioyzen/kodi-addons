"""TV identities from directory/filename strings, without reading media files.

Show names belong to the enclosing show directory. Episode titles and release
numbers are never movie search terms. Numeric-only episodes require independent
TV-directory evidence so numbered movie collections retain their old workflow.
"""
import os
import re
import urllib.parse
from collections import defaultdict
from functools import lru_cache
from media_identity import _extract_name, title_key

_TV_ROOTS = {'国产剧', '电视剧', '電視劇', '剧集', '劇集', '美剧', '英剧', '日剧',
             '韩剧', '港台剧', '欧美剧', '日韩剧', '海外剧', '动漫', 'tv', 'tvshows', 'tv shows', 'series', 'shows'}
_GENERIC = _TV_ROOTS | {'movies', 'movie', 'media', 'video', 'videos', '电影',
                      '電影', '4k电影', '杜比视界', '纪录片', 'storage', 'episodes'}
_CN = r'[零〇一二两三四五六七八九十百千\d]+'
_SEASON_ONLY = re.compile(r'^(?:s(?:eason)?[ ._-]*(\d{1,3})|第('+_CN+r')季|specials)$', re.I)
_SE = re.compile(r's(\d{1,3})[ ._-]*e(\d{1,4})(?!\d)', re.I)
_XE = re.compile(r'(?<!\d)(\d{1,2})x(\d{1,4})(?!\d)', re.I)
_EP = re.compile(r'(?<![a-z0-9])(?:ep?|episode)[ ._-]*(\d{1,4})(?!\d)', re.I)
_CH_EP = re.compile(r'第('+_CN+r')[集话話回]')
_BARE = re.compile(r'^\s*(\d{1,3})(?:\s*[.、_-]\s*|\s+)(?=\S)')
_NUMBER_ONLY = re.compile(r'^\s*(\d{1,3})\s*$')
_RANGE_TAIL = re.compile(r'^\s*[-~–至][ ._-]*(?:e(?:p)?[ ._-]*)?(\d{1,4})(?!\d)', re.I)
_DOUBLE_TAIL = re.compile(r'^[ ._-]*e(\d{1,4})(?!\d)', re.I)
_EXTRA = re.compile(r'(?:^|[ ._\-\[\]])(?:bonus|extras?|featurettes?|sample|trailer|花絮|特辑|特輯|幕后|幕後)(?:$|[ ._\-\[\]])', re.I)
_EXTRA_DIRS = {'extras','extra','bonus','featurettes','花絮','幕后花絮','幕後花絮','番外','番外篇','访谈','訪談','删减片段','刪減片段','解说','解說','评论','評論','commentary','commentaries'}
_EDITED = re.compile(r'精编版|精編版|剪辑版|剪輯版|edited[ ._-]*version|recut', re.I)
_NOISE = re.compile(r'\((?:\d+[-~–]\d+|[^)]*(?:精编版|精編版|无广告|無廣告|剪辑版|剪輯版))\)|\[\d+[-~–]\d+\]|全\d+集|(?:^|[ ._-])s\d{1,3}(?=$|[ ._-])|(?:^|[ ._-])e\d+(?:-e?\d+)?(?=$|[ ._-])|(?:^|[ ._-])(?:complete|全集)(?=$|[ ._-])', re.I)

# Season tags can directly follow a Chinese title; a leading whitespace
# requirement incorrectly leaves S01 in bilingual release names.
_SEASON_TAG = re.compile(r'(?<![A-Za-z0-9])(?:s(?:eason)?[ ._-]*(\d{1,3})(?:[ ._-]*[-~–][ ._-]*s?(\d{1,3}))?|第('+_CN+r')季)(?![A-Za-z0-9])', re.I)
_SEASON_RANGE = re.compile(r'(?<![A-Za-z0-9])s\d{1,3}[ ._-]*[-~–][ ._-]*s?\d{1,3}', re.I)


@lru_cache(maxsize=4096)
def directory_season(name):
    match = _SEASON_TAG.search(urllib.parse.unquote(name))
    if not match or match[2]:
        return None
    return chinese_number(match[1] or match[3])


def chinese_number(value):
    if str(value).isdigit():
        return int(value)
    digits = dict(zip('零〇一二两三四五六七八九', (0,0,1,2,2,3,4,5,6,7,8,9)))
    total, digit = 0, 0
    for char in value:
        if char in digits:
            digit = digits[char]
        elif char in '十百千':
            total += (digit or 1) * {'十':10, '百':100, '千':1000}[char]
            digit = 0
        else:
            return None
    return total + digit


def episode_marker(filename, allow_bare=False):
    stem = os.path.splitext(urllib.parse.unquote(filename))[0]
    for pattern, kind in ((_SE,'season'), (_XE,'season'), (_EP,'episode'), (_CH_EP,'chinese')):
        match = pattern.search(stem)
        if not match:
            continue
        season = int(match[1]) if kind == 'season' else None
        first = int(match[2]) if kind == 'season' else chinese_number(match[1])
        numbers = [first]
        tail = stem[match.end():]
        end = _RANGE_TAIL.match(tail)
        if end:
            last = int(end[1])
            if not first <= last <= first + 49:
                return {'error':'invalid_episode_range'}
            numbers = list(range(first, last+1))
        else:
            while True:
                extra = _DOUBLE_TAIL.match(tail)
                if not extra:
                    break
                number = int(extra[1])
                if number not in numbers:
                    numbers.append(number)
                tail = tail[extra.end():]
        if not first or max(numbers) > 9999:
            return {'error':'invalid_episode_number'}
        return {'season':season, 'episodes':numbers, 'explicit':True,
                'prefix':stem[:match.start()].strip(' ._-'), 'episode_title':''}
    if allow_bare:
        match = _BARE.match(stem) or _NUMBER_ONLY.match(stem)
        if match and 0 < int(match[1]) < 1000:
            return {'season':None, 'episodes':[int(match[1])], 'explicit':False,
                    'prefix':'', 'episode_title':stem[match.end():].strip(' ._-')}
    return None


def _show_directory(filepath):
    return _directory_context(os.path.dirname(filepath).rstrip('/'))


@lru_cache(maxsize=4096)
def _directory_context(directory):
    season = None
    for _ in range(8):
        name = urllib.parse.unquote(os.path.basename(directory))
        match = _SEASON_ONLY.fullmatch(name)
        names, _ = _titles(name)
        structural = not names and directory_season(name) is not None
        if match or structural:
            if season is None:
                season = 0 if name.casefold() == 'specials' else directory_season(name)
            directory = os.path.dirname(directory)
        elif name.casefold() == 'episodes' or name.casefold() in _EXTRA_DIRS:
            directory = os.path.dirname(directory)
        else:
            named_season = directory_season(name)
            if season is None:
                season = named_season
            parent = os.path.dirname(directory)
            parent_name = urllib.parse.unquote(os.path.basename(parent))
            parent_titles, _ = _titles(parent_name)
            # A named season in an explicit multi-season collection belongs
            # to that collection, even when its Chinese subtitle differs.
            # Standalone companion films keep their independent directory.
            related = {title_key(t, False) for t in names} & {title_key(t, False) for t in parent_titles}
            if named_season is not None and parent_titles and (_SEASON_RANGE.search(parent_name) or related):
                directory = parent
            break
    return directory+'/', season


@lru_cache(maxsize=4096)
def _titles(raw):
    raw = urllib.parse.unquote(raw).replace('（','(').replace('）',')')
    raw = raw.split('￡',1)[0]
    clean = _NOISE.sub(' ', _SEASON_TAG.sub(' ', raw))
    clean = re.sub(r'(?:^|[ ._-])(?:\+?SP)(?=$|[ ._-])', ' ', clean, flags=re.I)
    range_year = re.search(r'(?<!\d)((?:19|20)\d{2})[-~–]((?:19|20)\d{2})(?!\d)',clean)
    if range_year:
        clean = clean[:range_year.start()] + range_year[1] + clean[range_year.end():]
    names, year = _extract_name(clean)
    # Mixed-language release strings contain multiple representations of one
    # show; use complete CJK and English titles, never their concatenation.
    cjk, english, other = [], [], []
    for name in names:
        name = name.strip(' ._-()[]【】')
        if not name or name.casefold() in _GENERIC or not re.search(r'[^\W\d_]',name,re.UNICODE):
            continue
        has_cjk = bool(re.search(r'[\u4e00-\u9fff]',name))
        has_latin = bool(re.search(r'[A-Za-z\u00c0-\u024f]',name))
        if has_cjk and has_latin:
            continue
        (cjk if has_cjk else english if has_latin else other).append(name)
    output, seen = [], set()
    for name in cjk+english+other:
        key = title_key(name,False)
        if key and key not in seen:
            seen.add(key);output.append(name)
    return output, year


def build_tv_contexts(filepaths, expected_type='auto'):
    """Build a bounded, reusable show context for every known TV file.

    Callers supply the existing inventory. No exists/stat/listdir/open of any
    video share occurs here, including the manual-match path.
    """
    if expected_type == 'movie':
        return {}
    groups = defaultdict(list)
    for filepath in filepaths:
        root, season = _show_directory(filepath)
        groups[root].append((filepath,season))
    output = {}
    for root, members in groups.items():
        folder = urllib.parse.unquote(os.path.basename(root.rstrip('/')))
        folder_titles, folder_year = _titles(folder)
        collection = bool(_SEASON_RANGE.search(folder))
        parts = urllib.parse.unquote(urllib.parse.urlsplit(root).path).split('/')
        explicit_tv_root = any(part.casefold() in _TV_ROOTS for part in parts)
        declared = bool(re.search(r'\(\d+[-~–]\d+\)|全\d+集',folder))
        markers = {path:episode_marker(os.path.basename(path)) for path,_ in members}
        has_markers = any(marker for marker in markers.values())
        bare = [episode_marker(os.path.basename(path),allow_bare=True) for path,_ in members]
        # A year-bearing show folder containing multiple numbered videos is
        # TV evidence when those files do not identify separate movie years.
        numbered = [m for m in bare if m and not m.get('error')]
        sequential = bool(folder_year and len(numbered)>=2 and
                          len({n for m in numbered for n in m['episodes']})>=2 and
                          not any(re.search(r'(?<!\d)(?:19|20)\d{2}(?!\d)',os.path.basename(path)) for path,_ in members))
        tv_directory = expected_type == 'tvshow' or explicit_tv_root or declared or sequential or any(season is not None for _,season in members)
        explicit_numbers = {(markers[path] or {}).get('season') for path,_ in members if (markers[path] or {}).get('season') is not None}
        for path, directory_season in members:
            filename = os.path.basename(path)
            marker = markers[path] or episode_marker(filename, allow_bare=tv_directory or bool(has_markers and folder_titles))
            if not marker and not (tv_directory or (has_markers and folder_titles)):
                continue
            names, filename_year = _titles((marker or {}).get('prefix',''))
            # Parent-folder identity wins over episode titles. Root-level
            # episodes can use their explicit show-name prefix.
            titles = list(folder_titles or names)
            if not titles:
                if expected_type != 'tvshow':
                    continue
                titles = ['']
            season = (marker or {}).get('season')
            explicit_season = season is not None or directory_season is not None
            if season is None:
                season = directory_season if directory_season is not None else (next(iter(explicit_numbers)) if len(explicit_numbers)==1 else 1)
                explicit_season = explicit_season or len(explicit_numbers)==1
            kind = 'episode' if marker and not marker.get('error') else 'review'
            if not titles[0]:
                kind = 'review'
            parts = [urllib.parse.unquote(part).casefold() for part in urllib.parse.urlsplit(os.path.dirname(path)).path.split('/')]
            extra = bool(_EXTRA.search(filename) or re.search(r'番外|花絮|访谈|訪談|删减|刪減|幕后|幕後|解说|解說|commentary',filename) or any(part in _EXTRA_DIRS for part in parts))
            if extra and not (season==0 and marker and not marker.get('error')):
                kind = 'extra'
            output[path] = {
                'kind':kind, 'show_root':root, 'titles':titles, 'directory_titles':folder_titles, 'title':titles[0],
                'english_title':next((n for n in titles if re.search(r'[A-Za-z]',n)),''),
                'year':folder_year or filename_year,
                'season_year':_titles(urllib.parse.unquote(os.path.basename(os.path.dirname(path))))[1] if directory_season is not None else None,
                'collection':collection,
                'directory_identity':bool(folder_titles),
                'season_number':season, 'season_explicit':explicit_season,
                'episodes':(marker or {}).get('episodes',[]),
                'episode_title':(marker or {}).get('episode_title',''),
                'edition':'edited' if _EDITED.search(folder) else '',
                'reason':('show_title_missing' if not titles[0] else (marker or {}).get('error','episode_number_missing')) if kind == 'review' else '',
            }
        # Precompute once per source identity/season; large inventories do not
        # rebuild the same coverage set for every individual episode.
        coverage = defaultdict(set)
        for path, _ in members:
            context = output.get(path)
            if context and context['kind']=='episode':
                coverage[(tuple(context['titles']),context['season_number'])].update(context['episodes'])
        coverage = {key:sorted(numbers) for key,numbers in coverage.items()}
        for path, _ in members:
            context = output.get(path)
            if context is not None:
                context['source_episodes'] = coverage.get((tuple(context['titles']),context['season_number']),[])
    return output
