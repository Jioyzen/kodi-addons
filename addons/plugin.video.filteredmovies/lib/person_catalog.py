"""Actor identity and credits; local works first, optional bounded TMDb metadata."""
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from collections import OrderedDict
from .tv_catalog import rpc

_CACHE = OrderedDict()
_LOCK = threading.Lock()
_NETWORK_SLOTS = threading.BoundedSemaphore(2)
IMAGE_BASE = 'https://image.tmdb.org/t/p/'


def get_tmdb(path):
    import xbmcaddon
    key = 'f090bb54758cabf231fb605d3e3e0468'
    try:
        key = xbmcaddon.Addon('script.ai.movie.scanner').getSetting('tmdb_api_key') or key
    except RuntimeError:
        pass  # The wall does not require the scanner to be installed.
    with _LOCK:
        cached = _CACHE.get(path)
        if cached and time.monotonic() - cached[0] < 86400:
            _CACHE.move_to_end(path)
            return cached[1]
    url = 'https://api.tmdb.org/3/' + path
    url += ('&' if '?' in path else '?') + urllib.parse.urlencode({'api_key': key, 'language': 'zh-CN'})
    request = urllib.request.Request(url, headers={'Accept': 'application/json'})
    if not _NETWORK_SLOTS.acquire(blocking=False):
        raise RuntimeError('Actor metadata requests are busy')
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            payload = json.loads(response.read(4 * 1024 * 1024))
        if not isinstance(payload,dict):
            raise RuntimeError('Invalid actor metadata response')
    finally:
        _NETWORK_SLOTS.release()
    with _LOCK:
        _CACHE[path] = (time.monotonic(), payload)
        while len(_CACHE) > 96:
            _CACHE.popitem(last=False)
    return payload


def profile_name(url):
    value = urllib.parse.unquote(str(url or ''))
    if value.startswith('image://'):
        value = value[8:].rstrip('/')
    return os.path.basename(urllib.parse.urlsplit(value).path)


def name_key(name):
    return re.sub(r'[\W_]+', '', str(name).casefold())


def resolve_person(actor, show, get=get_tmdb):
    """Never pick the first same-name person: verify against the source show's credits."""
    show_id = (show.get('uniqueid') or {}).get('tmdb')
    if not str(show_id or '').isdigit():
        return None
    source_movie = show.get('media_type')=='movie' or bool(show.get('movieid'))
    path = 'movie/%s/credits' if source_movie else 'tv/%s/aggregate_credits'
    credits = get(path % show_id).get('cast', [])
    thumbnail = profile_name(actor.get('thumbnail'))
    exact_photo = [c for c in credits if thumbnail and profile_name(c.get('profile_path')) == thumbnail]
    if len(exact_photo) == 1:
        return exact_photo[0]['id']
    names = [c for c in credits if name_key(c.get('name')) == name_key(actor.get('name'))]
    if len(names) == 1:
        return names[0]['id']
    return None


def local_works(actor, query=rpc, cancelled=lambda: False):
    works = []
    seen = set()
    for media, method, key in (('movie', 'VideoLibrary.GetMovies', 'movies'),
                               ('tv', 'VideoLibrary.GetTVShows', 'tvshows')):
        params = {'filter': {'field': 'actor', 'operator': 'is', 'value': actor['name']},
                  'properties': ['title', 'year', 'rating', 'art', 'plot', 'uniqueid'],
                  'limits': {'start': 0, 'end': 120}, 'sort': {'method': 'year', 'order': 'descending'}}
        offset = 0
        while True:
            if cancelled():
                return works
            params['limits'] = {'start': offset, 'end': offset + 120}
            result = query(method, params)
            rows = result.get(key) or []
            added = 0
            for row in rows:
                identity=(media,row.get('movieid' if media=='movie' else 'tvshowid'))
                if identity[1] is None or identity in seen:
                    continue
                seen.add(identity);added+=1
                row = dict(row)
                row['credit_type'] = media
                row['media_type'] = 'movie' if media == 'movie' else 'tvshow'
                row['local'] = True
                works.append(row)
            offset += len(rows)
            if not rows or not added or offset >= result.get('limits', {}).get('total', offset):
                break
    return works


def merge_credits(credits, local):
    identities = {(w['credit_type'], str((w.get('uniqueid') or {}).get('tmdb'))): w for w in local
                  if (w.get('uniqueid') or {}).get('tmdb')}
    result, seen = [], set()
    for credit in credits:
        media = credit.get('media_type')
        if media not in ('movie', 'tv') or not credit.get('id'):
            continue
        identity = (media, str(credit['id']))
        if identity in seen:
            continue
        seen.add(identity)
        if identity in identities:
            row = dict(identities[identity])
        else:
            date = credit.get('release_date') or credit.get('first_air_date') or ''
            row = {'title': credit.get('title') or credit.get('name') or '',
                   'year': int(date[:4]) if date[:4].isdigit() else 0,
                   'rating': credit.get('vote_average') or 0, 'plot': credit.get('overview') or '',
                   'media_type': 'movie' if media == 'movie' else 'tvshow', 'local': False,
                   'art': {'poster': IMAGE_BASE + 'w500' + credit['poster_path'] if credit.get('poster_path') else ''}}
        result.append(row)
    # Locally verified credits remain available when TMDb omits a work or ID.
    known_local = {(w.get('media_type'), w.get('movieid') or w.get('tvshowid')) for w in result if w.get('local')}
    for row in local:
        if (row['media_type'], row.get('movieid') or row.get('tvshowid')) not in known_local:
            result.append(row)
    return sorted(result, key=lambda w: (not w.get('local'), -(w.get('year') or 0), w.get('title', '')))[:120]


def load_person(actor, show, local, get=get_tmdb, cancelled=lambda: False):
    if cancelled():
        return {'name':actor['name'], 'works':local}
    identity = resolve_person(actor, show, get)
    if identity is None or cancelled():
        return {'name': actor['name'], 'biography': '暂无可确认的人物简介。当前展示媒体库中的参演作品。', 'works': local}
    person = get('person/%d' % identity)
    original = next((name for name in person.get('also_known_as', [])
                     if name != actor['name'] and name.isascii() and any(c.isalpha() for c in name)), '')
    if not original and person.get('name') != actor['name']:
        original = person.get('name') or ''
    return {'name': actor['name'], 'originalname': original,
            'biography': person.get('biography') or '暂无人物简介',
            'portrait': IMAGE_BASE + 'h632' + person['profile_path'] if person.get('profile_path') else actor.get('thumbnail', ''),
            'birthday': person.get('birthday') or '', 'birthplace': person.get('place_of_birth') or '',
            'works': local}
