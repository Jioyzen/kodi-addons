"""Local Kodi library queries for the wall's show and season pages."""
import json
import re
import unicodedata
from urllib.parse import urlsplit, unquote

EPISODE_PAGE_SIZE = 80


def rpc(method, params):
    import xbmc
    response = json.loads(xbmc.executeJSONRPC(json.dumps(
        {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params})))
    if 'error' in response:
        raise RuntimeError('Kodi library query failed: ' + method)
    return response.get('result', {})


def favourite_entries(result):
    """Kodi returns favourites:null when its last favourite is removed."""
    value=result.get('favourites') if isinstance(result,dict) else None
    return [entry for entry in value if isinstance(entry,dict)] if isinstance(value,list) else []


def season_title(row):
    number = int(row.get('season', 0))
    title = row.get('title') or row.get('label') or ''
    if title and not re.fullmatch(r'(?:Season|第)\s*\d+\s*季?', title, re.I):
        return title
    return '特别篇' if number == 0 else '第 %d 季' % number


def next_episode(show_id, season=None, query=rpc):
    params = {'tvshowid': int(show_id), 'properties': ['title', 'season', 'episode', 'resume', 'playcount'],
              'sort': {'method': 'episode', 'order': 'ascending'}, 'limits': {'start': 0, 'end': 1}}
    if season is not None:
        params['season'] = int(season)
    recent = query('VideoLibrary.GetEpisodes', dict(params,
        properties=params['properties'] + ['lastplayed'],
        sort={'method': 'lastplayed', 'order': 'descending'})).get('episodes', [])
    last = recent[0] if recent and recent[0].get('lastplayed') else None
    if last:
        if (last.get('resume') or {}).get('position', 0) > 0:
            return last
        if last.get('playcount', 0):
            later = dict(params, season=int(last.get('season', 0)), filter={
                'field': 'episode', 'operator': 'greaterthan', 'value': str(last.get('episode', 0))})
            rows = query('VideoLibrary.GetEpisodes', later).get('episodes', [])
            if rows:
                return rows[0]
            if season is None:
                seasons = query('VideoLibrary.GetSeasons', {'tvshowid': int(show_id),
            'properties': ['season', 'episode']}).get('seasons') or []
                later_seasons = sorted(s['season'] for s in seasons
                    if s.get('episode', 0) and s['season'] > last.get('season', 0))
                if later_seasons:
                    rows = query('VideoLibrary.GetEpisodes', dict(params, season=later_seasons[0])).get('episodes', [])
                    if rows:
                        return rows[0]
    result = query('VideoLibrary.GetEpisodes', dict(params, filter={
        'field': 'playcount', 'operator': 'is', 'value': '0'}))
    rows = result.get('episodes', [])
    if not rows:
        rows = query('VideoLibrary.GetEpisodes', params).get('episodes', [])
    return rows[0] if rows else None


def load_show(show_id, query=rpc):
    show_id = int(show_id)
    if show_id <= 0:
        raise ValueError('A library TV show ID is required')
    details = query('VideoLibrary.GetTVShowDetails', {
        'tvshowid': show_id, 'properties': ['title', 'plot', 'year', 'rating', 'genre',
            'studio', 'mpaa', 'premiered', 'art', 'episode', 'watchedepisodes', 'cast', 'uniqueid']})['tvshowdetails']
    seasons = query('VideoLibrary.GetSeasons', {'tvshowid': show_id,
        'properties': ['title', 'season', 'episode', 'watchedepisodes', 'art']}).get('seasons') or []
    seasons = sorted((s for s in seasons if s.get('season', -1) >= 0 and s.get('episode', 0)),
                     key=lambda s: int(s['season']))
    return {'show': details, 'seasons': seasons, 'next': next_episode(show_id, query=query)}


def load_episodes(show_id, season, offset=0, query=rpc):
    offset = max(0, int(offset))
    result = query('VideoLibrary.GetEpisodes', {
        'tvshowid': int(show_id), 'season': int(season),
        'properties': ['title', 'plot', 'season', 'episode', 'rating', 'art', 'thumbnail',
                       'playcount', 'resume', 'runtime', 'streamdetails'],
        'sort': {'method': 'episode', 'order': 'ascending'},
        'limits': {'start': offset, 'end': offset + EPISODE_PAGE_SIZE}})
    rows = result.get('episodes') or []
    cast = None
    if offset == 0 and rows:
        cast = query('VideoLibrary.GetEpisodeDetails', {'episodeid': rows[0]['episodeid'],
            'properties': ['cast']}).get('episodedetails', {}).get('cast')
    return {'episodes': rows, 'total': result.get('limits', {}).get('total', len(rows)),
            'offset': offset, 'next': next_episode(show_id, season, query), 'cast': cast}


def play_label(episode):
    if not episode:
        return '暂无可播放单集'
    return '第 %d 集' % int(episode.get('episode', 0))


def resolution(episode):
    streams = (episode.get('streamdetails') or {}).get('video') or []
    height = max((int(s.get('height', 0) or 0) for s in streams), default=0)
    if height >= 2160:
        return '4K'
    if height >= 1080:
        return '1080P'
    if height >= 720:
        return '720P'
    return ''


def episode_summary(row, columns=38):
    """One compact paragraph, explicitly capped at three CJK-aware lines."""
    parts = [str(row.get('title') or '').strip(), str(row.get('plot') or '').strip()]
    text = '%d. %s' % (row.get('episode', 0), ' '.join(dict.fromkeys(p for p in parts if p)))
    text = re.sub(r'\s+', ' ', text.replace('[CR]', ' '))
    lines, line, width = [], '', 0
    for index, char in enumerate(text):
        units = 2 if unicodedata.east_asian_width(char) in ('W', 'F') else 1
        if width + units > columns:
            lines.append(line.rstrip())
            line, width = '', 0
            if len(lines) == 3:
                lines[-1] = lines[-1].rstrip(' .，。')[:-1] + '…'
                return '\n'.join(lines)
        line += char
        width += units
    if line:
        lines.append(line.rstrip())
    return '\n'.join(lines)


def actor_image_candidates(actor):
    """Reuse existing sizes of the same TMDb headshot without a download."""
    source = actor.get('thumbnail') or ''
    result = [source] if source else []
    decoded = unquote(source[8:].rstrip('/')) if source.startswith('image://') else source
    parsed = urlsplit(decoded)
    if parsed.hostname == 'image.tmdb.org' and re.match(r'^/t/p/[^/]+/[^/]+$', parsed.path):
        name = parsed.path.rsplit('/', 1)[-1]
        result += ['https://image.tmdb.org/t/p/%s/%s' % (size, name)
                   for size in ('w185', 'w300', 'h632', 'w500', 'original')]
    return list(dict.fromkeys(result))


def set_watched(show_id, flag, season=None, query=rpc, cancelled=lambda: False):
    """The show setter handles a whole show; seasons require episode IDs."""
    if season is None:
        query('VideoLibrary.SetTVShowDetails', {'tvshowid': int(show_id), 'playcount': int(flag)})
        return
    offset = 0
    while not cancelled():
        result = query('VideoLibrary.GetEpisodes', {'tvshowid': int(show_id), 'season': int(season),
            'limits': {'start': offset, 'end': offset + EPISODE_PAGE_SIZE},
            'sort': {'method': 'episode', 'order': 'ascending'}})
        rows = result.get('episodes', [])
        for row in rows:
            if cancelled():
                return
            query('VideoLibrary.SetEpisodeDetails', {'episodeid': int(row['episodeid']), 'playcount': int(flag)})
        offset += len(rows)
        if not rows or offset >= result.get('limits', {}).get('total', offset):
            return
