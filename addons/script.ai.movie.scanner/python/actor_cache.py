# -*- coding: UTF-8 -*-
"""Independent, bounded pre-cache of library actors into Kodi's texture cache."""
import ast
import json
import os
import sqlite3
import tempfile
import threading
import urllib.parse
import xml.etree.ElementTree as ET
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

import xbmc
import xbmcgui
import xbmcvfs
from ai_database import current_database
from image_cacher import (ImageCacher, ADDON, _log,
                          _get_image_metadata, _get_kodi_texture_hash)



_LOCAL_STRINGS = None


def L(string_id):
    """Read new strings locally if Kodi still holds the older addon language.

    This lets a live addon update add menus without reloading the skin or
    restarting Kodi. Normal installed-addon localization remains preferred.
    """
    localized = ADDON.getLocalizedString(string_id)
    if localized:
        return localized
    global _LOCAL_STRINGS
    if _LOCAL_STRINGS is None:
        _LOCAL_STRINGS = {}
        try:
            language = xbmc.getLanguage(xbmc.ISO_639_1).lower().replace('_', '-')
        except AttributeError:
            language = 'en'
        locale = 'en_gb'
        if language.startswith('zh'):
            locale = 'zh_tw' if any(code in language for code in ('tw', 'hk', 'hant')) else 'zh_cn'
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                            'resources', 'language', 'resource.language.' + locale, 'strings.po')
        sid = None
        collecting = False
        try:
            with open(path, encoding='utf-8') as source:
                for line in source:
                    line = line.strip()
                    if line.startswith('msgctxt '):
                        context = ast.literal_eval(line[8:])
                        sid = int(context[1:]) if context.startswith('#') else None
                        collecting = False
                    elif line.startswith('msgstr ') and sid is not None:
                        _LOCAL_STRINGS[sid] = ast.literal_eval(line[7:])
                        collecting = True
                    elif line.startswith('"') and collecting:
                        _LOCAL_STRINGS[sid] += ast.literal_eval(line)
                    else:
                        collecting = False
        except (OSError, ValueError, SyntaxError) as exc:
            _log('Actor cache language fallback failed: %s' % exc, xbmc.LOGWARNING)
    return _LOCAL_STRINGS.get(string_id, '')


def _actor_http_url(value):
    """Preserve Kodi's exact source URL; never open SMB or local media paths."""
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    candidates = [value]
    if value.startswith('<'):
        try:
            candidates = [node.text or '' for node in
                          ET.fromstring('<art>' + value + '</art>').iter('thumb')]
        except ET.ParseError:
            return None
    for url in candidates:
        url = url.strip()
        if url.startswith('image://'):
            url = urllib.parse.unquote(url[8:-1] if url.endswith('/') else url[8:])
        try:
            parts = urllib.parse.urlsplit(url)
            if parts.scheme in ('http', 'https') and parts.hostname:
                return url
        except ValueError:
            pass
    return None


class CacheCanceled(Exception):
    pass


class ActorImageCacher(ImageCacher):
    def __init__(self):
        super().__init__()
        self.thread_count = 4
        self.cancel = threading.Event()

    def _get_actor_urls(self):
        db = current_database('MyVideos')
        uri = 'file:' + urllib.parse.quote(db, safe='/') + '?mode=ro'
        urls = set()
        actors = unavailable = 0
        with closing(sqlite3.connect(uri, uri=True, timeout=1)) as conn:
            rows = conn.execute("""
                WITH leading_cast AS (
                    SELECT DISTINCT actor_id FROM (
                        SELECT actor_id, ROW_NUMBER() OVER (
                            PARTITION BY media_type,media_id
                            ORDER BY cast_order,actor_id) AS position
                        FROM actor_link WHERE media_type IN
                            ('movie','tvshow','episode','musicvideo'))
                    WHERE position <= 5)
                SELECT COALESCE(
                    (SELECT art.url FROM art
                     WHERE art.media_id=a.actor_id AND art.media_type='actor'
                       AND art.type='thumb' AND LENGTH(TRIM(art.url))>0
                     ORDER BY art.art_id LIMIT 1), a.art_urls, '')
                FROM actor a WHERE a.actor_id IN (SELECT actor_id FROM leading_cast)
                ORDER BY a.actor_id
            """)
            for (value,) in rows:
                actors += 1
                url = _actor_http_url(value)
                if url:
                    urls.add(url)
                else:
                    unavailable += 1
        return sorted(urls), actors, unavailable

    def _cached_urls(self, urls):
        """Single texture DB read, then local file checks; no per-actor RPC."""
        uri = 'file:' + urllib.parse.quote(self.db_path, safe='/') + '?mode=ro'
        wanted = set(urls)
        cached = set()
        root = xbmcvfs.translatePath('special://thumbnails/')
        with closing(sqlite3.connect(uri, uri=True, timeout=1)) as conn:
            for url, path in conn.execute('SELECT url,cachedurl FROM texture'):
                if url in wanted and path and os.path.isfile(os.path.join(root, path)):
                    cached.add(url)
        return cached

    def _fetch_bytes(self, url, timeout=(4, 12)):
        if self.cancel.is_set():
            raise CacheCanceled()
        result = super()._fetch_bytes(url, timeout=timeout)
        if self.cancel.is_set():
            raise CacheCanceled()
        return result

    def _write_to_db(self, kodi_url, cachedurl, width, height):
        # Preserve existing texture IDs, sizes/use counts and Kodi tracking.
        # REPLACE would delete the old texture row and leave orphan size rows.
        try:
            with self.db_lock, closing(sqlite3.connect(self.db_path, timeout=5)) as conn:
                with conn:
                    conn.execute('BEGIN IMMEDIATE')
                    old = conn.execute('SELECT id FROM texture WHERE url=?', (kodi_url,)).fetchone()
                    values = (cachedurl, _get_kodi_texture_hash(kodi_url))
                    if old:
                        texture_id = old[0]
                        conn.execute("UPDATE texture SET cachedurl=?,imagehash=?,"
                                     "lasthashcheck=datetime('now','localtime') WHERE id=?",
                                     values + (texture_id,))
                    else:
                        cur = conn.execute("INSERT INTO texture(url,cachedurl,imagehash,lasthashcheck) "
                                           "VALUES (?,?,?,datetime('now','localtime'))",
                                           (kodi_url,) + values)
                        texture_id = cur.lastrowid
                    cur = conn.execute('UPDATE sizes SET width=?,height=? WHERE idtexture=? AND size=1',
                                       (width, height, texture_id))
                    if not cur.rowcount:
                        conn.execute("INSERT INTO sizes(idtexture,size,width,height,usecount,lastusetime) "
                                     "VALUES (?,1,?,?,1,datetime('now','localtime'))",
                                     (texture_id, width, height))
            return True
        except (sqlite3.Error, OSError) as exc:
            _log('Actor cache DB write failed: %s' % exc, xbmc.LOGERROR)
            return False

    def _save_image(self, data, path, url, cached_url, route_name):
        if self.cancel.is_set():
            raise CacheCanceled()
        width, height = _get_image_metadata(data)
        if not self._valid_image(data) or width <= 0 or height <= 0:
            return False
        temp = None
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=os.path.dirname(path), prefix='.actor-cache-', delete=False) as out:
                temp = out.name
                out.write(data)
            os.replace(temp, path)
            temp = None
            return self._write_to_db(url, cached_url, width, height)
        except OSError as exc:
            _log('Actor cache image save failed: %s' % exc, xbmc.LOGERROR)
            return False
        finally:
            if temp and os.path.exists(temp):
                os.unlink(temp)

    def _download_actor(self, url):
        try:
            if self.cancel.is_set():
                return 'canceled'
            return self._download_image(url)
        except CacheCanceled:
            return 'canceled'
        except Exception as exc:
            _log('Actor image download failed: %s' % exc, xbmc.LOGWARNING)
            return 'failed'

    def _run_downloads(self, urls, progress, cached_count=0):
        """At most four live jobs, polling cancellation without blocking the UI."""
        results = {'success': 0, 'skipped': cached_count, 'failed': 0, 'canceled': False}
        iterator = iter(urls)
        monitor = xbmc.Monitor()
        total = len(urls)
        if progress.iscanceled() or monitor.abortRequested():
            self.cancel.set()
            results['canceled'] = True
            return results
        finished = 0
        with ThreadPoolExecutor(max_workers=self.thread_count) as pool:
            pending = set()

            def replenish():
                while not self.cancel.is_set() and len(pending) < self.thread_count:
                    if progress.iscanceled() or monitor.abortRequested():
                        self.cancel.set()
                        results['canceled'] = True
                        return
                    url = next(iterator, None)
                    if url is None:
                        break
                    pending.add(pool.submit(self._download_actor, url))

            replenish()
            while pending:
                if progress.iscanceled() or monitor.abortRequested():
                    self.cancel.set()
                    results['canceled'] = True
                    for future in pending:
                        future.cancel()
                    progress.update(int(finished * 100 / max(1, total)), L(32206))
                done, pending = wait(pending, timeout=.1, return_when=FIRST_COMPLETED)
                for future in done:
                    if future.cancelled():
                        continue
                    status = future.result()
                    if status in ('success', 'skipped', 'failed'):
                        results[status] += 1
                        finished += 1
                if not self.cancel.is_set():
                    if done:
                        progress.update(int(finished * 100 / max(1, total)),
                                        L(32205) % (finished, total, results['success'],
                                                    results['skipped'], results['failed']))
                    replenish()
        return results

    def start_actors(self):
        # Process-wide flock also handles separate Kodi plugin invocations.
        import fcntl
        profile = xbmcvfs.translatePath(ADDON.getAddonInfo('profile'))
        os.makedirs(profile, exist_ok=True)
        with open(os.path.join(profile, 'actor-cache.lock'), 'a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                xbmcgui.Dialog().ok(L(32201), L(32209))
                self.session.close()
                return
            progress = xbmcgui.DialogProgress()
            progress.create(L(32201), L(32202))
            try:
                urls, actors, unavailable = self._get_actor_urls()
                cached = self._cached_urls(urls) if urls else set()
                if progress.iscanceled() or xbmc.Monitor().abortRequested():
                    return
                progress.close()
                _log('Actor cache: actors=%d, unique URLs=%d, cached=%d, unavailable=%d' %
                     (actors, len(urls), len(cached), unavailable))
                if not urls:
                    xbmcgui.Dialog().ok(L(32201), L(32204))
                    return
                remaining = [url for url in urls if url not in cached]
                if not remaining:
                    xbmcgui.Dialog().ok(L(32201), L(32211) % (len(urls), unavailable))
                    return
                if not xbmcgui.Dialog().yesno(L(32201), L(32203) %
                                             (actors, len(urls), len(cached), unavailable, len(remaining))):
                    return
                progress = xbmcgui.DialogProgress()
                progress.create(L(32201), L(32202))
                results = self._run_downloads(remaining, progress, len(cached))
                report = dict(results, actors=actors, total=len(urls), unavailable=unavailable, actors_per_resource=5)
                _log('Actor cache finished: %s' % report)
                try:
                    with open(os.path.join(profile, 'actor-cache-last.json'), 'w', encoding='utf-8') as out:
                        json.dump(report, out, ensure_ascii=False, indent=2)
                except OSError as exc:
                    _log('Actor cache report save failed: %s' % exc, xbmc.LOGWARNING)
                progress.close()
                if not xbmc.Monitor().abortRequested():
                    counts = (results['success'], results['skipped'], results['failed'])
                    message = L(32208) % counts if results['canceled'] else L(32207) % (counts + (unavailable,))
                    xbmcgui.Dialog().ok(L(32201), message)
            except (OSError, sqlite3.Error, RuntimeError) as exc:
                _log('Actor cache could not read local data: %s' % exc, xbmc.LOGERROR)
                progress.close()
                xbmcgui.Dialog().ok(L(32201), L(32210))
            finally:
                progress.close()
                self.session.close()
