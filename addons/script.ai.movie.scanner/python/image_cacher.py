# -*- coding: UTF-8 -*-
"""Proactive image cache warmer - downloads all artwork into Kodi's Textures13.db."""

from ai_database import current_database
import os
import json
import socket
import struct
import sqlite3
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs

ADDON = xbmcaddon.Addon()
ADDONID = ADDON.getAddonInfo('id')


def L(string_id):
    return ADDON.getLocalizedString(string_id)


def _log(msg, level=xbmc.LOGINFO):
    xbmc.log('[%s] ImageCacher: %s' % (ADDONID, msg), level)


def _get_kodi_texture_hash(url):
    crc = 0xffffffff
    for val in url.lower().encode("utf-8"):
        crc ^= val << 24
        for _ in range(8):
            crc = crc << 1 if (crc & 0x80000000) == 0 else (crc << 1) ^ 0x104c11db7
    return format(crc & 0xFFFFFFFF, '08x')


def _get_image_metadata(img_data):
    height = 0
    width = 0
    size = len(img_data)
    if size < 24:
        return width, height
    try:
        if img_data.startswith(b'\x89PNG\r\n\x1a\n'):
            width, height = struct.unpack('>ii', img_data[16:24])
        elif img_data.startswith(b'GIF87a') or img_data.startswith(b'GIF89a'):
            width, height = struct.unpack('<HH', img_data[6:10])
        elif img_data.startswith(b'BM'):
            width, height = struct.unpack('<ii', img_data[18:26])
        elif img_data.startswith(b'RIFF') and img_data[8:12] == b'WEBP':
            vp8_sig = img_data[12:16]
            if vp8_sig == b'VP8 ':
                width, height = struct.unpack('<HH', img_data[26:30])
                width = width & 0x3fff
                height = height & 0x3fff
            elif vp8_sig == b'VP8L':
                b = struct.unpack('<I', img_data[21:25])[0]
                width = (b & 0x3FFF) + 1
                height = ((b >> 14) & 0x3FFF) + 1
            elif vp8_sig == b'VP8X':
                width = (img_data[24] | (img_data[25] << 8) | (img_data[26] << 16)) + 1
                height = (img_data[27] | (img_data[28] << 8) | (img_data[29] << 16)) + 1
        elif img_data.startswith(b'\xff\xd8'):
            i = 2
            while i < size - 2:
                while i < size and img_data[i] != 0xFF:
                    i += 1
                while i < size and img_data[i] == 0xFF:
                    i += 1
                if i >= size:
                    break
                marker = img_data[i]
                i += 1
                if marker == 0xD9 or marker == 0xDA:
                    break
                if i + 1 >= size:
                    break
                block_length = struct.unpack('>H', img_data[i:i + 2])[0]
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    if i + 5 < size:
                        height, width = struct.unpack('>HH', img_data[i + 3:i + 7])
                    break
                i += block_length
    except Exception:
        pass
    return width, height


def _get_kodi_texture_path(url):
    """Return (physical_path, cached_url) for a given image URL."""
    crc_hex = _get_kodi_texture_hash(url)
    folder = crc_hex[0]
    clean_url = url.split("?")[0].lower()
    if clean_url.endswith(".png"):
        ext = ".png"
    elif clean_url.endswith(".gif"):
        ext = ".gif"
    else:
        ext = ".jpg"
    cached_url = "%s/%s%s" % (folder, crc_hex, ext)
    physical_path = xbmcvfs.translatePath("special://thumbnails/%s" % cached_url)
    return physical_path, cached_url


class ImageCacher:
    def __init__(self):
        # Proxy downloads are slower than direct CDN hits, so a bit more
        # parallelism compensates. Still low enough to avoid starving the
        # Kodi texture thread or hammering Textures13.db.
        self.thread_count = 8
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko)"
        })
        self.db_path = current_database('Textures')
        self.db_lock = threading.Lock()

    def _get_video_items(self):
        movies = []
        tvshows = []
        episodes = []
        movie_sets = []
        try:
            req = {"jsonrpc": "2.0", "method": "VideoLibrary.GetMovies",
                   "params": {"properties": ["art"]}, "id": 1}
            res = json.loads(xbmc.executeJSONRPC(json.dumps(req)))
            movies = res.get("result", {}).get("movies", [])

            req = {"jsonrpc": "2.0", "method": "VideoLibrary.GetTVShows",
                   "params": {"properties": ["art"]}, "id": 2}
            res = json.loads(xbmc.executeJSONRPC(json.dumps(req)))
            tvshows = res.get("result", {}).get("tvshows", [])

            req = {"jsonrpc": "2.0", "method": "VideoLibrary.GetEpisodes",
                   "params": {"properties": ["art"]}, "id": 3}
            res = json.loads(xbmc.executeJSONRPC(json.dumps(req)))
            episodes = res.get("result", {}).get("episodes", [])

            req = {"jsonrpc": "2.0", "method": "VideoLibrary.GetMovieSets",
                   "params": {"properties": ["art"]}, "id": 4}
            res = json.loads(xbmc.executeJSONRPC(json.dumps(req)))
            movie_sets = res.get("result", {}).get("sets", [])
        except Exception:
            pass
        return movies + tvshows + episodes + movie_sets

    def _check_exists(self, kodi_url):
        try:
            with self.db_lock:
                conn = sqlite3.connect(self.db_path, timeout=5.0)
                c = conn.cursor()
                c.execute("SELECT cachedurl FROM texture WHERE url=?", (kodi_url,))
                row = c.fetchone()
                conn.close()
                if row:
                    physical_path = xbmcvfs.translatePath(
                        "special://thumbnails/%s" % row[0])
                    if os.path.exists(physical_path):
                        return True
        except Exception as e:
            _log("DB check failed for %s: %s" % (kodi_url, e), xbmc.LOGERROR)
        return False

    def _write_to_db(self, kodi_url, cachedurl, width, height):
        try:
            # Compute URL-based hash so Kodi doesn't treat image as "unverified"
            img_hash = _get_kodi_texture_hash(kodi_url)
            with self.db_lock:
                conn = sqlite3.connect(self.db_path, timeout=10.0)
                c = conn.cursor()
                # INSERT OR REPLACE avoids breaking Kodi's existing texture tracking
                c.execute(
                    "INSERT OR REPLACE INTO texture (url, cachedurl, imagehash, "
                    "lasthashcheck) VALUES (?, ?, ?, datetime('now', 'localtime'))",
                    (kodi_url, cachedurl, img_hash))
                texture_id = c.lastrowid
                # Only insert sizes if texture_id is new (not a replace)
                c.execute(
                    "SELECT idtexture FROM sizes WHERE idtexture=? AND size=1",
                    (texture_id,))
                if not c.fetchone():
                    c.execute(
                        "INSERT INTO sizes (idtexture, size, width, height, "
                        "usecount, lastusetime) "
                        "VALUES (?, 1, ?, ?, 1, datetime('now', 'localtime'))",
                        (texture_id, width, height))
                conn.commit()
                conn.close()
            return True
        except Exception as e:
            _log("DB write failed for %s: %s" % (kodi_url, e), xbmc.LOGERROR)
            return False

    # ------------------------------------------------------------------
    # Download routes for GFW-hostile CDNs (image.tmdb.org etc.).
    #
    # Which route survives depends ENTIRELY on the ISP - measured on one
    # network (2026-08): only wsrv.nl works; on other networks (other
    # provinces / other ISPs) different routes are reachable. So instead
    # of a fixed order we RACE all routes in parallel on first use and
    # stick with the fastest winner. If the winner later fails repeatedly
    # (or 15 minutes pass), we re-race and switch automatically.
    #
    # Templates: {url} = full https URL, {path} = URL path only,
    #            {strip} = URL without scheme (URL-quoted).
    # ------------------------------------------------------------------
    _ROUTES = (
        ('direct', '{url}'),
        ('tmdb-media', 'https://media.themoviedb.org{path}'),
        ('wsrv.nl', 'https://wsrv.nl/?url={strip}'),
        ('weserv', 'https://images.weserv.nl/?url={strip}'),
        ('wp-i0', 'https://i0.wp.com/{strip}'),
        ('wp-i1', 'https://i1.wp.com/{strip}'),
        ('wp-i2', 'https://i2.wp.com/{strip}'),
        ('pimg.tw', 'https://imageproxy.pimg.tw/?url=https://{strip}'),
        ('hive.blog', 'https://images.hive.blog/0x0/https://{strip}'),
    )
    # Re-race the routes after this many seconds to adapt to changing
    # network conditions (e.g. ISP-level throttling kicks in mid-scan).
    _ROUTE_RECHECK_SECS = 900
    # Consecutive failures of the preferred route before re-racing.
    _ROUTE_FAIL_LIMIT = 3

    # --- Route preference state (class-level, shared across threads) ---
    _PREFERRED_ROUTE = {}    # host -> route name
    _ROUTE_FAILS = {}        # host -> consecutive failures of preferred
    _ROUTE_PROBED_AT = {}    # host -> epoch of last race
    _SOURCE_STATS = {}       # route name -> success counter (for the log)
    _STATE_LOCK = threading.Lock()
    _RACE_LOCK = threading.Lock()

    def _build_route_url(self, route_name, template, url):
        stripped = url.split('://', 1)[1]
        qstripped = urllib.parse.quote(stripped, safe='/:?&=%')
        try:
            path = urllib.parse.urlparse(url).path
        except Exception:
            path = '/'
        return (template
                .replace('{url}', url)
                .replace('{path}', path)
                .replace('{strip}', qstripped))

    def _fetch_bytes(self, url, timeout=(4, 12)):
        """Single HTTP GET into memory. Returns (ok, status_or_error, data)."""
        try:
            r = self.session.get(url, timeout=timeout, verify=False)
            if r.status_code != 200:
                return False, 'HTTP %d' % r.status_code, b''
            data = r.content
            if not data:
                return False, 'empty response', b''
            return True, '200', data
        except Exception as e:
            return False, str(e)[:80], b''

    @staticmethod
    def _valid_image(data):
        """Guard against proxy error pages (JSON/HTML) masquerading as 200."""
        if not data:
            return False
        return (data[:3] == b'\xff\xd8\xff' or          # JPEG
                data[:3] == b'GIF' or                    # GIF87a/89a
                data.startswith(b'\x89PNG') or           # PNG
                (data[:4] == b'RIFF' and data[8:12] == b'WEBP') or
                data[:2] == b'BM')                       # BMP

    def _race_routes(self, url):
        """Fire ALL routes in parallel; first valid image wins.
        Returns (img_data, winner_route_name) or (None, None)."""
        jobs = []
        for name, template in self._ROUTES:
            try:
                jobs.append((name, self._build_route_url(name, template, url)))
            except Exception:
                continue

        def _fetch(job):
            name, rurl = job
            ok, status, data = self._fetch_bytes(rurl)
            if ok and self._valid_image(data):
                return name, data
            return None, '%s: %s' % (name, status)

        winner = None
        errors = []
        with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
            futures = [ex.submit(_fetch, j) for j in jobs]
            for fut in as_completed(futures):
                name, result = fut.result()
                if name:
                    winner = (result, name)
                    for f in futures:
                        f.cancel()
                    break
                errors.append(result)
        if winner:
            _log('Route race winner: %s (%d bytes, %d routes raced, '
                 'errors: %s)'
                 % (winner[1], len(winner[0]), len(jobs),
                    '; '.join(errors)[:200]))
        else:
            _log('Route race: ALL %d routes failed: %s'
                 % (len(jobs), '; '.join(errors)[:300]), xbmc.LOGERROR)
        return winner or (None, None)

    def _select_route(self, host, url):
        """Return the preferred route for this host, racing all routes if
        no preference exists yet (or the old one went stale/bad).
        Returns (route_name, img_data_or_None)."""
        with self._RACE_LOCK:
            # Double-check: another thread may have finished a race while
            # we were waiting for the lock.
            with self._STATE_LOCK:
                tpl = self._PREFERRED_ROUTE.get(host)
            if tpl is not None:
                return tpl, None
            data, name = self._race_routes(url)
            if name:
                with self._STATE_LOCK:
                    self._PREFERRED_ROUTE[host] = name
                    self._ROUTE_FAILS.pop(host, None)
                    self._ROUTE_PROBED_AT[host] = time.time()
                    self._SOURCE_STATS['race'] = \
                        self._SOURCE_STATS.get('race', 0) + 1
            return name, data

    def _note_route_result(self, host, route_name, ok):
        with self._STATE_LOCK:
            if ok:
                self._ROUTE_FAILS.pop(host, None)
                self._SOURCE_STATS[route_name] = \
                    self._SOURCE_STATS.get(route_name, 0) + 1
            else:
                fails = self._ROUTE_FAILS.get(host, 0) + 1
                self._ROUTE_FAILS[host] = fails
                if fails >= self._ROUTE_FAIL_LIMIT:
                    # Preferred route has gone bad - drop it so the next
                    # download re-races and picks a working one.
                    self._PREFERRED_ROUTE.pop(host, None)
                    self._ROUTE_PROBED_AT.pop(host, None)
                    self._ROUTE_FAILS.pop(host, None)

    def _save_image(self, img_data, physical_path, unwrapped_url, cached_url,
                    route_name):
        out_dir = os.path.dirname(physical_path)
        if not os.path.exists(out_dir):
            os.makedirs(out_dir, exist_ok=True)
        with open(physical_path, "wb") as f:
            f.write(img_data)
        width, height = _get_image_metadata(img_data)
        if self._write_to_db(unwrapped_url, cached_url, width, height):
            _log("Download OK via %s %s (%dx%d, %d bytes)"
                 % (route_name, unwrapped_url, width, height, len(img_data)))
            return True
        _log("Download OK but DB write failed for %s" % unwrapped_url,
             xbmc.LOGWARNING)
        return False

    def _download_image(self, kodi_url):
        if not kodi_url:
            return "skipped"

        unwrapped_url = kodi_url
        if kodi_url.startswith("image://"):
            try:
                unwrapped_url = kodi_url[8:]
                if unwrapped_url.endswith("/"):
                    unwrapped_url = unwrapped_url[:-1]
                unwrapped_url = urllib.parse.unquote(unwrapped_url)
            except Exception:
                pass

        if not (unwrapped_url.startswith("http") or
                unwrapped_url.startswith("webdav") or
                unwrapped_url.startswith("dav")):
            return "skipped"

        if self._check_exists(unwrapped_url):
            return "skipped"

        physical_path, cached_url = _get_kodi_texture_path(unwrapped_url)

        # Normalize scheme for download
        download_url = unwrapped_url
        if download_url.startswith("webdavs://"):
            download_url = download_url.replace("webdavs://", "https://", 1)
        elif download_url.startswith("webdav://"):
            download_url = download_url.replace("webdav://", "http://", 1)
        elif download_url.startswith("davs://"):
            download_url = download_url.replace("davs://", "https://", 1)
        elif download_url.startswith("dav://"):
            download_url = download_url.replace("dav://", "http://", 1)

        # Log resolved IP for network diagnostics
        host = urllib.parse.urlparse(download_url).hostname
        resolved_ip = "unknown"
        try:
            resolved_ip = socket.gethostbyname(host)
        except Exception:
            pass

        # Non-http(s) URLs (webdav/dav) cannot be proxied - direct only.
        if not download_url.startswith('http'):
            ok, status, img_data = self._fetch_bytes(download_url,
                                                     timeout=(5, 20))
            if ok and self._valid_image(img_data) and \
                    self._save_image(img_data, physical_path, unwrapped_url,
                                     cached_url, 'direct'):
                return "success"
            _log("Download failed for %s: %s"
                 % (unwrapped_url, status), xbmc.LOGERROR)
            return "failed"

        # Periodically re-race so a route that goes bad mid-session (e.g.
        # ISP throttling kicks in) is replaced automatically.
        with self._STATE_LOCK:
            stale = (time.time() - self._ROUTE_PROBED_AT.get(host, 0)
                     > self._ROUTE_RECHECK_SECS)
            if stale and host in self._PREFERRED_ROUTE:
                self._PREFERRED_ROUTE.pop(host, None)

        # First image (or after route went bad/stale): race everything.
        preferred = None
        with self._STATE_LOCK:
            preferred = self._PREFERRED_ROUTE.get(host)
        if preferred is None:
            name, img_data = self._select_route(host, download_url)
            if name and img_data:
                # The race already downloaded this image - reuse the bytes.
                if self._save_image(img_data, physical_path, unwrapped_url,
                                    cached_url, name):
                    return "success"
                return "failed"
            if name is None:
                _log("All download routes failed for %s (resolved %s -> %s)"
                     % (unwrapped_url, host, resolved_ip), xbmc.LOGERROR)
                return "failed"
            preferred = name

        # We have a preferred route: try it first, then the others in order.
        ordered = [preferred] + [n for n, _ in self._ROUTES if n != preferred]
        templates = dict(self._ROUTES)
        _log("Downloading %s via preferred route %s (resolved %s -> %s)"
             % (unwrapped_url, preferred, host, resolved_ip))

        last_error = 'no attempt'
        for name in ordered:
            url = self._build_route_url(name, templates[name], download_url)
            attempts = 1 if name == 'direct' else 2
            for attempt in range(1, attempts + 1):
                ok, status, img_data = self._fetch_bytes(url)
                if ok and self._valid_image(img_data):
                    self._note_route_result(host, name, True)
                    if self._save_image(img_data, physical_path,
                                        unwrapped_url, cached_url, name):
                        return "success"
                    return "failed"
                last_error = status
                _log("Download via %s failed (attempt %d/%d): %s - %s"
                     % (name, attempt, attempts, url, status),
                     xbmc.LOGWARNING)
                self._note_route_result(host, name, False)
                if name == 'direct':
                    break
                if attempt < attempts:
                    # Rate-limited / server error: back off longer so the
                    # proxy can recover - hammering it makes things worse.
                    if str(status).startswith(('HTTP 429', 'HTTP 5')):
                        time.sleep(5)
                    else:
                        time.sleep(1)
            # If the preferred route just tripped the fail limit, its
            # preference was dropped - continue with the remaining routes.

        _log("All download methods failed for %s (resolved %s -> %s, "
             "last error: %s)"
             % (unwrapped_url, host, resolved_ip, last_error),
             xbmc.LOGERROR)
        return "failed"

    def prefetch_art(self, art_entries):
        """Non-blocking background pre-fetch of art URLs into Kodi's texture
        cache. `art_entries` is a list of `available_art` dicts as returned
        by tmdb_api (poster/fanart/clearlogo keys). Resized to the same
        variants kodi_db writes to the art table (w500/w1280/w500), so the
        poster wall opens instantly after a scan. Posters are queued first
        (wall visibility) before fanart (the heavy files). Reuses the
        existing download routine (texture DB insert included), skipping
        already cached images.
        """
        # Global priority ordering (NOT per-movie interleaved): ALL posters
        # first so the wall fills in within seconds, then clearlogos, then
        # fanart (heavy), and finally actor thumbnails (detail-page only).
        # The proxy throughput is ~1-2 images/sec, so with per-movie
        # interleaving the poster of movie #50 sat behind ~650 URLs.
        buckets = ([], [], [], [])   # posters, clearlogos, fanart, cast
        for art in art_entries:
            if not art:
                continue
            for idx, key, size in ((0, 'poster', 'w500'),
                                   (0, 'set.poster', 'w500'),
                                   (1, 'clearlogo', 'w500'),
                                   (2, 'fanart', 'w1280'),
                                   (2, 'set.fanart', 'w1280')):
                for item in art.get(key, [])[:1]:
                    u = item.get('url')
                    if u:
                        u = u.replace('/original/', '/%s/' % size)
                        if u.startswith('http'):
                            buckets[idx].append(u)
            for item in art.get('cast', [])[:20]:
                u = item.get('url')
                if u and u.startswith('http'):
                    buckets[3].append(u)
        seen = set()
        urls = []
        for bucket in buckets:
            for u in bucket:
                if u not in seen:
                    seen.add(u)
                    urls.append(u)
        if not urls:
            _log('prefetch_art: no HTTP URLs to cache')
            return
        _log('Background prefetch of %d art URLs (threads=%d)'
             % (len(urls), self.thread_count), xbmc.LOGINFO)

        def _run():
            ok = skip = fail = 0
            with ThreadPoolExecutor(max_workers=self.thread_count) as executor:
                for res in executor.map(self._download_image, urls):
                    if res == "success":
                        ok += 1
                    elif res == "skipped":
                        skip += 1
                    else:
                        fail += 1
            with self._STATE_LOCK:
                stats = dict(self._SOURCE_STATS)
            _log('Prefetch done: %d OK, %d skipped, %d failed (total %d, '
                 'by source: %s)'
                 % (ok, skip, fail, len(urls),
                    ', '.join('%s=%d' % kv for kv in sorted(stats.items()))
                    or 'none'),
                 xbmc.LOGINFO if fail == 0 else xbmc.LOGWARNING)

        t = threading.Thread(target=_run, name='ImagePrefetch', daemon=True)
        t.start()

    def start(self):
        dp = xbmcgui.DialogProgress()
        dp.create(L(32120), L(32121))

        items = self._get_video_items()
        if not items:
            dp.close()
            xbmcgui.Dialog().ok(L(32122), L(32123))
            return

        urls_to_cache = set()
        for m in items:
            if dp.iscanceled():
                dp.close()
                return
            art = m.get("art", {})
            for k in ["poster", "fanart", "clearlogo", "set.poster", "set.fanart"]:
                v = art.get(k)
                if v and isinstance(v, str) and any(
                        proto in v for proto in
                        ("http", "image://", "webdav", "dav")):
                    urls_to_cache.add(v)

        urls_to_cache = list(urls_to_cache)
        total = len(urls_to_cache)
        if total == 0:
            dp.close()
            xbmcgui.Dialog().ok(L(32122), L(32124))
            return

        dp.update(0, L(32125) % total)
        _log("Starting cache of %d images with %d threads" %
             (total, self.thread_count))

        success_count = 0
        skip_count = 0
        fail_count = 0
        canceled = False

        with ThreadPoolExecutor(max_workers=self.thread_count) as executor:
            future_to_url = {
                executor.submit(self._download_image, url): url
                for url in urls_to_cache}
            for i, future in enumerate(as_completed(future_to_url)):
                if dp.iscanceled():
                    _log("Canceled by user")
                    for f in future_to_url:
                        f.cancel()
                    canceled = True
                    break
                try:
                    res = future.result()
                    if res == "success":
                        success_count += 1
                    elif res == "skipped":
                        skip_count += 1
                    else:
                        fail_count += 1
                except Exception as exc:
                    _log("Thread error: %s" % exc, xbmc.LOGERROR)
                    fail_count += 1

                pct = int((i + 1) * 100 / total)
                msg = L(32126) % (
                    total, success_count, skip_count, fail_count)
                dp.update(pct, L(32127) + msg)

        dp.close()
        if canceled:
            xbmcgui.Dialog().ok(L(32128),
                                L(32129) %
                                (success_count, skip_count, fail_count))
        else:
            xbmcgui.Dialog().ok(L(32130),
                                L(32131) %
                                (total, success_count, skip_count, fail_count))
