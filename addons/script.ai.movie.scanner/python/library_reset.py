"""Reset video-library entries and their artwork using Kodi's own APIs.

No database replacement, external helper add-on, system service or media-file
access is needed. SQLite is used only to read existing artwork references.
"""
import json
import os
import sqlite3
import xml.etree.ElementTree as ET
from contextlib import closing
from urllib.parse import quote, unquote

PENDING = 'reset-pending.json'
COLLECTIONS = (
    ('GetMovies', 'movies', 'RemoveMovie', 'movieid'),
    ('GetTVShows', 'tvshows', 'RemoveTVShow', 'tvshowid'),
    ('GetMusicVideos', 'musicvideos', 'RemoveMusicVideo', 'musicvideoid'),
)


def canonical_url(url):
    value = str(url or '')
    for _ in range(4):
        if not value.startswith('image://'):
            break
        value = unquote(value[8:].split('/transform?', 1)[0].rstrip('/'))
    return value


def artwork_urls(database_path):
    uri = 'file:' + quote(database_path, safe='/:') + '?mode=ro'
    urls = set()
    with closing(sqlite3.connect(uri, uri=True, timeout=5)) as database:
        for (url,) in database.execute('SELECT url FROM art WHERE url IS NOT NULL'):
            if url:
                urls.add(canonical_url(url))
        for (value,) in database.execute('SELECT art_urls FROM actor WHERE art_urls IS NOT NULL'):
            if not value:
                continue
            try:
                thumbs = ET.fromstring('<root>' + value + '</root>').iter('thumb')
                urls.update(canonical_url(t.text) for t in thumbs if t.text)
            except ET.ParseError:
                if value.startswith(('http://', 'https://', 'image://')):
                    urls.add(canonical_url(value))
    return urls


def save_pending(profile, plan):
    os.makedirs(profile, exist_ok=True)
    target = os.path.join(profile, PENDING)
    temporary = target + '.tmp'
    with open(temporary, 'w', encoding='utf-8') as output:
        os.chmod(temporary, 0o600)
        json.dump(plan, output, ensure_ascii=False)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, target)


def prepare(profile, video_database, rpc):
    target = os.path.join(profile, PENDING)
    old = None
    if os.path.exists(target):
        with open(target, encoding='utf-8') as source:
            old = json.load(source)
        if old.get('version') != 1:
            raise RuntimeError('Unsupported pending reset format')
    urls = artwork_urls(video_database)
    urls.update(canonical_url(t.get('url')) for t in (old or {}).get('textures', []))
    textures = rpc('Textures.GetTextures', {'properties': ['url', 'cachedurl']}).get('textures', [])
    selected = {str(t['textureid']): t for t in textures
                if canonical_url(t.get('url')) in urls
                or canonical_url(t.get('url')).startswith('video@')}
    # Preserve cache references across an interrupted reset: the art rows may
    # already be gone by the next invocation. Never remove a reused texture ID.
    for item in (old or {}).get('textures', []):
        selected.setdefault(str(item['textureid']), item)
    items = []
    for getter, key, remover, id_key in COLLECTIONS:
        for item in rpc('VideoLibrary.' + getter, {}).get(key, []):
            items.append({'method': 'VideoLibrary.' + remover,
                          'params': {id_key: item[id_key]}})
    plan = {'version': 1, 'items': items, 'textures': list(selected.values())}
    save_pending(profile, plan)
    return plan


def remove_cached_file(thumbnails, cached_url):
    # cachedurl is normally a relative hash path. Never delete original artwork,
    # a mounted source, symlink targets or files outside Kodi's local cache.
    if not cached_url or os.path.isabs(cached_url):
        return
    root = os.path.realpath(thumbnails)
    candidate = os.path.abspath(os.path.join(root, cached_url))
    if os.path.commonpath([root, candidate]) != root or candidate == root:
        raise ValueError('Invalid cached artwork path')
    if os.path.realpath(candidate) != candidate:
        raise ValueError('Cached artwork path traverses a symlink')
    if os.path.isfile(candidate):
        os.unlink(candidate)


def execute(profile, thumbnails, plan, rpc, progress=None, aborted=lambda: False):
    total = len(plan['items']) + len(plan['textures'])
    done = 0
    report = {'media_removed': 0, 'textures_removed': 0}
    for item in plan['items']:
        if aborted():
            raise InterruptedError('Reset interrupted; it can be resumed')
        rpc(item['method'], item['params'])
        report['media_removed'] += 1
        done += 1
        if progress and (done % 25 == 0 or done == total):
            progress(done, total)
    # Once every native library removal succeeded, drop only this add-on's
    # scan records. Keep settings.xml (API credentials and preferences).
    for name in ('scan_history.json', 'scan_history.json.tmp', '.scanning',
                 '.refreshing', '.stop_requested', 'actor-cache-last.json'):
        path = os.path.join(profile, name)
        if os.path.isfile(path):
            os.unlink(path)
    plan['items'] = []
    save_pending(profile, plan)
    live = {t['textureid']: t for t in
            rpc('Textures.GetTextures', {'properties': ['url', 'cachedurl']}).get('textures', [])}
    # The native database is the progress journal. Keep the artwork snapshot
    # immutable until completion, so a crash can resume without rewriting a
    # multi-megabyte JSON file hundreds of times on flash storage.
    for item in plan['textures']:
        if aborted():
            raise InterruptedError('Reset interrupted; it can be resumed')
        current = live.get(item['textureid'])
        if current is not None and current.get('url') == item.get('url'):
            rpc('Textures.RemoveTexture', {'textureid': item['textureid']})
            remove_cached_file(thumbnails, item.get('cachedurl'))
            report['textures_removed'] += 1
        elif current is None:
            remove_cached_file(thumbnails, item.get('cachedurl'))
        done += 1
        if progress and (done % 25 == 0 or done == total):
            progress(done, total)
    # A new native read, not an unconditional success notification.
    for getter, key, _, _ in COLLECTIONS:
        if rpc('VideoLibrary.' + getter, {}).get(key):
            raise RuntimeError('Video library was repopulated during reset')
    os.unlink(os.path.join(profile, PENDING))
    return report
