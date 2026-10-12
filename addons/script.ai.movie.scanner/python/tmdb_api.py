# -*- coding: UTF-8 -*-
"""TMDB API client - search movies, TV shows and get details.

Optimized for no-VPN usage from China:
  - Shared requests.Session with connection pooling (reuses TCP/TLS handshakes)
  - Thread-safe token-bucket rate limiter (stays under TMDB's 40 req / 10 s)
  - In-memory search & details cache (multi-version files search only once)
  - Dual-language search chain (primary language -> English) for higher recall
  - Maoyan trailer circuit-breaker + cache (never blocks a scan)
  - Direct TMDB image URLs (no third-party proxy) for previews
"""

import re
import threading
import time
import requests
from urllib.parse import quote
from requests.adapters import HTTPAdapter
from matching import merge_candidates, match_score, norm_title as _norm_title

import xbmc

ADDONID = 'script.ai.movie.scanner'

TMDB_SEARCH_URL = 'https://api.tmdb.org/3/search/movie'
TMDB_MOVIE_URL = 'https://api.tmdb.org/3/movie/{}'
TMDB_SEARCH_TV_URL = 'https://api.tmdb.org/3/search/tv'
TMDB_SEARCH_MULTI_URL = 'https://api.tmdb.org/3/search/multi'
TMDB_TV_URL = 'https://api.tmdb.org/3/tv/{}'
TMDB_IMAGE_BASE = 'https://image.tmdb.org/t/p/'
IMAGE_PROXY_PREFIX = 'https://wsrv.nl/?url='

# Public TMDB resource used to validate an API key (no personal data).
TMDB_KEYCHECK_URL = 'https://api.tmdb.org/3/movie/550'


def _log(level, msg):
    xbmc.log(msg='%s: %s' % (ADDONID, msg), level=level)


# ============================================================
# Connection pool + rate limiter + in-memory caches
# ============================================================
_session = None
_session_lock = threading.Lock()
_search_cache = {}
_details_cache = {}
_cache_lock = threading.Lock()
_cache_max = 4000

# Maoyan trailer circuit breaker: once the service fails at the network
# level, assume it is unreachable and skip it for the rest of the process.
_MAOYAN_OK = True
_maoyan_cache = {}


class _RateLimiter(object):
    """Thread-safe token bucket. Keeps total TMDB request rate under the
    free-tier limit (40 req / 10 s) even with many worker threads."""

    def __init__(self, rate, burst):
        self.rate = float(rate)
        self.burst = float(burst)
        self.tokens = burst
        self.ts = time.time()
        self.lock = threading.Lock()

    def wait(self):
        with self.lock:
            now = time.time()
            self.tokens = min(self.burst,
                              self.tokens + (now - self.ts) * self.rate)
            self.ts = now
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return
            deficit = 1.0 - self.tokens
            self.tokens = 0.0
        time.sleep(deficit / self.rate)


_LIMITER = _RateLimiter(rate=6.0, burst=12)


def _get_session():
    """Shared requests.Session with a keep-alive connection pool."""
    global _session
    if _session is None:
        with _session_lock:
            if _session is None:
                s = requests.Session()
                s.headers.update({
                    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                                   'AppleWebKit/537.36 (KHTML, like Gecko) '
                                   'Chrome/120.0.0.0 Safari/537.36'),
                    'Accept': 'application/json,text/plain,*/*',
                    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
                })
                adapter = HTTPAdapter(pool_connections=16,
                                      pool_maxsize=16,
                                      max_retries=2)
                s.mount('https://', adapter)
                s.mount('http://', adapter)
                _session = s
    return _session


def _tmdb_get(url, params, timeout=(4, 12)):
    """Rate-limited GET through the shared session.

    With more concurrent workers, HTTP 429 (rate limit) becomes the most
    likely failure mode: honor the Retry-After header with a short backoff
    instead of burning the caller's retry attempts. Stale pooled connections
    (after suspend/wake) are also retried once on a fresh connection.
    """
    _LIMITER.wait()
    for attempt in range(3):
        try:
            resp = _get_session().get(url, params=params, timeout=timeout)
        except requests.exceptions.ConnectionError:
            # Pool might have stale connections after suspend etc.; retry
            # once on a fresh connection.
            if attempt >= 2:
                raise
            time.sleep(0.5 * (attempt + 1))
            continue
        if resp.status_code == 429 and attempt < 2:
            retry_after = resp.headers.get('Retry-After')
            delay = 1.0
            if retry_after:
                try:
                    delay = max(1.0, min(float(retry_after), 10.0))
                except ValueError:
                    delay = 1.0
            _log(xbmc.LOGWARNING,
                 'TMDB rate limited (429), backing off %.1fs' % delay)
            time.sleep(delay)
            _LIMITER.wait()
            continue
        return resp
    return resp


def _cache_put(store, key, value):
    with _cache_lock:
        if key not in store and len(store) >= _cache_max:
            # Retain the rest of the scan cache instead of flushing all
            # previous identities when a large library reaches the bound.
            store.pop(next(iter(store)))
        store[key] = value


def _cache_get(store, key):
    with _cache_lock:
        return store.get(key)


# ============================================================
# Key check
# ============================================================
# Cache the last key check result in-process: scans can be started back to
# back (manual + auto), and re-validating the same key costs a network
# round-trip every time. 5-minute TTL, invalidated when the key changes.
_key_check_cache = {'key': None, 'result': None, 'ts': 0.0}


def check_api_key(api_key):
    """Validate the TMDB API key before scanning.

    Returns one of:
      'ok'      - key accepted (HTTP 200)
      'empty'   - key is blank/not configured
      'invalid' - key rejected by TMDB (HTTP 401/403)
      'error'   - network/other failure (treat as non-blocking)
    """
    if not api_key:
        return 'empty'
    now = time.time()
    if (_key_check_cache['key'] == api_key
            and now - _key_check_cache['ts'] < 300):
        return _key_check_cache['result']
    try:
        resp = _tmdb_get(TMDB_KEYCHECK_URL,
                         params={'api_key': api_key},
                         timeout=(5, 15))
        if resp.status_code == 200:
            result = 'ok'
        elif resp.status_code in (401, 403):
            result = 'invalid'
        else:
            _log(xbmc.LOGWARNING,
                 'TMDB key check unexpected HTTP %d' % resp.status_code)
            result = 'error'
    except requests.exceptions.RequestException as e:
        _log(xbmc.LOGWARNING, 'TMDB key check network error: %s' % e)
        result = 'error'
    _key_check_cache['key'] = api_key
    _key_check_cache['result'] = result
    _key_check_cache['ts'] = now
    return result


# ============================================================
# Movie search / details
# ============================================================
def _movie_search_once(title, year, api_key, language):
    """Single cached TMDB movie search (year filter optional)."""
    params = {
        'api_key': api_key,
        'language': language,
        'query': title,
    }
    if year:
        try:
            params['year'] = int(year)
        except (ValueError, TypeError):
            pass

    cache_key = ('movie', title.strip().lower(), params.get('year'), language)
    cached = _cache_get(_search_cache, cache_key)
    if cached is not None:
        return cached

    results = None
    for attempt in (1, 2):
        if attempt > 1:
            _log(xbmc.LOGINFO, 'TMDB search retrying (%d/2)' % attempt)
        try:
            resp = _tmdb_get(TMDB_SEARCH_URL, params=params)
            if resp.status_code != 200:
                _log(xbmc.LOGERROR, 'TMDB search HTTP %d: %s'
                     % (resp.status_code, resp.text[:200]))
                if attempt == 2:
                    results = None
                continue
            data = resp.json()
            results = data.get('results', []) or []
            _log(xbmc.LOGINFO,
                 'TMDB search "%s" (%s): %d results'
                     % (title, year, len(results or [])))
            break
        except requests.exceptions.Timeout:
            if attempt == 2:
                _log(xbmc.LOGERROR, 'TMDB search timeout (retry failed): %s'
                     % title)
                results = None
        except Exception as e:
            if attempt == 2:
                _log(xbmc.LOGERROR, 'TMDB search error: %s' % type(e).__name__)
                results = None

    if results is not None:
        _cache_put(_search_cache, cache_key, results)
    return results


def search_movie(title, year, api_key, language='zh-CN'):
    """Search TMDB for a movie by title and year. Cached. Returns list or None.

    Filename years are frequently wrong (festival premieres vs wide release,
    production year), and TMDB's year filter is strict - an empty
    year-filtered result triggers one retry without the year before giving
    up; year similarity is then handled by the match scoring.
    """
    results = _movie_search_once(title, year, api_key, language)
    if not results and year:
        results = _movie_search_once(title, None, api_key, language)
        if results:
            _log(xbmc.LOGINFO,
                 'TMDB search "%s": year filter (%s) too strict, no-year '
                 'retry found %d results' % (title, year, len(results)))
    return results


def search_movie_best(title, year, english_title, api_key, language='zh-CN'):
    """Inspect both localizations; approximate matches must not stop recall."""
    candidates=[];by_id={}
    for query in dict.fromkeys((title, english_title)):
        if not query:
            continue
        for variant in _title_variants(query):
            for lang in dict.fromkeys((language, 'en-US')):
                merge_candidates(candidates, by_id, search_movie(variant, year, api_key, lang))
    return candidates


_identity_cache = {}


def find_movie_by_imdb(imdb_id, api_key, language='zh-CN'):
    key=('imdb_find', imdb_id, language)
    cached=_cache_get(_identity_cache, key)
    if cached is not None:
        return cached
    response=_tmdb_get('https://api.tmdb.org/3/find/'+imdb_id,
                       {'api_key':api_key, 'external_source':'imdb_id', 'language':language})
    if response.status_code!=200:
        raise RuntimeError('TMDb identifier lookup HTTP %d' % response.status_code)
    results=response.json().get('movie_results', [])
    _cache_put(_identity_cache, key, results)
    return results


def get_movie_identity(tmdb_id, api_key, language='zh-CN'):
    """Small identity summary; never performs trailer/image downloads."""
    key=('identity', int(tmdb_id), language)
    cached=_cache_get(_identity_cache, key)
    if cached is not None:
        return cached
    response=_tmdb_get(TMDB_MOVIE_URL.format(tmdb_id),
                       {'api_key':api_key, 'language':language,
                        'append_to_response':'credits,alternative_titles'})
    if response.status_code==404:
        return None
    if response.status_code!=200:
        raise RuntimeError('TMDb identity lookup HTTP %d' % response.status_code)
    data=response.json()
    result={k:data.get(k) for k in ('id','title','original_title','release_date',
                                  'runtime','overview','original_language','imdb_id')}
    result['runtime']=result.get('runtime') or 0
    result['genre_ids']=[g['id'] for g in data.get('genres', [])]
    credits=data.get('credits') or {}
    result['directors']=[c['name'] for c in credits.get('crew', []) if c.get('job')=='Director']
    result['cast']=[c['name'] for c in credits.get('cast', [])[:5]]
    result['_official_titles']=[t['title'] for t in (data.get('alternative_titles')or{}).get('titles', []) if t.get('title')]
    result['_identity_checked']=True
    _cache_put(_identity_cache, key, result)
    return result


def get_movie_details(tmdb_id, api_key, language='zh-CN'):
    """Get full movie details from TMDB. Cached. Returns dict or None."""
    cache_key = ('movie_detail', tmdb_id, language)
    cached = _cache_get(_details_cache, cache_key)
    if cached is not None:
        return cached

    url = TMDB_MOVIE_URL.format(tmdb_id)
    params = {
        'api_key': api_key,
        'language': language,
        'append_to_response': 'credits,external_ids,images,release_dates,videos'
    }

    details = None
    for attempt in (1, 2):
        if attempt > 1:
            _log(xbmc.LOGINFO, 'TMDB details retrying (%d/2) for %s'
                 % (attempt, tmdb_id))
        try:
            resp = _tmdb_get(url, params=params, timeout=(4, 15))
            if resp.status_code != 200:
                _log(xbmc.LOGERROR, 'TMDB details HTTP %d for %s'
                     % (resp.status_code, tmdb_id))
                if attempt == 2:
                    details = None
                continue
            details = _parse_movie_data(resp.json())
            break
        except requests.exceptions.Timeout:
            if attempt == 2:
                _log(xbmc.LOGERROR,
                     'TMDB details timeout (retry failed) for %s' % tmdb_id)
                details = None
            _log(xbmc.LOGINFO, 'TMDB details timeout, will retry for %s'
                 % tmdb_id)
        except Exception as e:
            if attempt == 2:
                _log(xbmc.LOGERROR, 'TMDB details error: %s' % e)
                details = None
            _log(xbmc.LOGINFO, 'TMDB details error, will retry: %s' % e)

    if details:
        _cache_put(_details_cache, cache_key, details)
    return details


# ============================================================
# TV search / details
# ============================================================
def _tv_search_once(name, year, api_key, language):
    """Single cached TMDB TV search (year filter optional)."""
    params = {
        'api_key': api_key,
        'language': language,
        'query': name,
    }
    if year:
        try:
            params['first_air_date_year'] = int(year)
        except (ValueError, TypeError):
            pass

    cache_key = ('tv', name.strip().lower(), params.get('first_air_date_year'),
                 language)
    cached = _cache_get(_search_cache, cache_key)
    if cached is not None:
        return cached

    results = None
    for attempt in (1, 2):
        if attempt > 1:
            _log(xbmc.LOGINFO, 'TMDB TV search retrying (%d/2)' % attempt)
        try:
            resp = _tmdb_get(TMDB_SEARCH_TV_URL, params=params)
            if resp.status_code != 200:
                _log(xbmc.LOGERROR, 'TMDB TV search HTTP %d: %s'
                     % (resp.status_code, resp.text[:200]))
                if attempt == 2:
                    results = None
                continue
            data = resp.json()
            results = data.get('results', []) or None
            _log(xbmc.LOGINFO, 'TMDB TV search "%s" (%s): %d results'
                 % (name, year, len(results or [])))
            break
        except requests.exceptions.Timeout:
            if attempt == 2:
                _log(xbmc.LOGERROR,
                     'TMDB TV search timeout (retry failed): %s' % name)
                results = None
        except Exception as e:
            if attempt == 2:
                _log(xbmc.LOGERROR, 'TMDB TV search error: %s' % e)
                results = None

    if results:
        _cache_put(_search_cache, cache_key, results)
    return results


def search_tv(name, year, api_key, language='zh-CN'):
    """Search TMDB for a TV show by name and optional year. Cached.

    Same no-year retry as search_movie: filename years are often off by a
    year while TMDB's first_air_date_year filter is strict.
    """
    results = _tv_search_once(name, year, api_key, language)
    if not results and year:
        results = _tv_search_once(name, None, api_key, language)
        if results:
            _log(xbmc.LOGINFO,
                 'TMDB TV search "%s": year filter (%s) too strict, no-year '
                 'retry found %d results' % (name, year, len(results)))
    return results


def search_tv_best(name, year, english_title, api_key, language='zh-CN'):
    """Dual-language TV search chain (same strategy as search_movie_best)."""
    candidates = []
    by_id = {}

    def _add(res):
        merge_candidates(candidates, by_id, res)

    def _add_variants(t):
        for v in _title_variants(t):
            _add(search_tv(v, year, api_key, language))

    _add_variants(name)
    if not candidates and english_title and english_title != name:
        _add_variants(english_title)

    if candidates:
        best, score = _select_best_match_light(candidates, name, year)
        if score < 0.62:
            # Weak primary match -> broaden with English + english_title
            _add(search_tv(name, year, api_key, 'en-US'))
            if english_title and english_title != name:
                _add_variants(english_title)
    else:
        _add(search_tv(name, year, api_key, 'en-US'))
        if english_title and english_title != name:
            _add_variants(english_title)
    return candidates


def search_multi(query, api_key, language='zh-CN'):
    """Search TMDB for both movies and TV shows. Returns list or None."""
    params = {
        'api_key': api_key,
        'language': language,
        'query': query,
    }

    for attempt in (1, 2):
        if attempt > 1:
            _log(xbmc.LOGINFO, 'TMDB multi search retrying (%d/2)' % attempt)
        try:
            resp = _tmdb_get(TMDB_SEARCH_MULTI_URL, params=params)
            if resp.status_code != 200:
                _log(xbmc.LOGERROR, 'TMDB multi search HTTP %d: %s'
                     % (resp.status_code, resp.text[:200]))
                if attempt == 2:
                    return None
                continue
            data = resp.json()
            results = data.get('results', [])
            results = [r for r in results
                       if r.get('media_type') in ('movie', 'tv')]
            return results
        except requests.exceptions.Timeout:
            if attempt == 2:
                _log(xbmc.LOGERROR,
                     'TMDB multi search timeout (retry failed): %s' % query)
                return None
        except Exception as e:
            if attempt == 2:
                _log(xbmc.LOGERROR, 'TMDB multi search error: %s' % e)
                return None

    return None


def get_tv_details(tmdb_id, api_key, language='zh-CN'):
    """Get full TV show details from TMDB. Cached. Returns dict or None."""
    cache_key = ('tv_detail', tmdb_id, language)
    cached = _cache_get(_details_cache, cache_key)
    if cached is not None:
        return cached

    url = TMDB_TV_URL.format(tmdb_id)
    params = {
        'api_key': api_key,
        'language': language,
        'append_to_response': 'credits,external_ids,images,videos'
    }

    details = None
    for attempt in (1, 2):
        if attempt > 1:
            _log(xbmc.LOGINFO, 'TMDB TV details retrying (%d/2) for %s'
                 % (attempt, tmdb_id))
        try:
            resp = _tmdb_get(url, params=params, timeout=(4, 15))
            if resp.status_code != 200:
                _log(xbmc.LOGERROR, 'TMDB TV details HTTP %d for %s'
                     % (resp.status_code, tmdb_id))
                if attempt == 2:
                    details = None
                continue
            details = _parse_tv_data(resp.json())
            break
        except requests.exceptions.Timeout:
            if attempt == 2:
                _log(xbmc.LOGERROR,
                     'TMDB TV details timeout (retry failed) for %s'
                     % tmdb_id)
                details = None
        except Exception as e:
            if attempt == 2:
                _log(xbmc.LOGERROR, 'TMDB TV details error: %s' % e)
                details = None

    if details:
        _cache_put(_details_cache, cache_key, details)
    return details


def get_tv_episode_details(tmdb_id, season_number, episode_number, api_key,
                           language='zh-CN'):
    """Get specific episode details from TMDB. Cached. Returns dict or None."""
    cache_key = ('ep_detail', tmdb_id, season_number, episode_number, language)
    cached = _cache_get(_details_cache, cache_key)
    if cached is not None:
        return cached

    url = TMDB_TV_URL.format(tmdb_id) + '/season/%d/episode/%d' % (
        season_number, episode_number)
    params = {
        'api_key': api_key,
        'language': language,
        'append_to_response': 'credits'
    }

    details = None
    for attempt in (1, 2):
        if attempt > 1:
            _log(xbmc.LOGINFO, 'TMDB episode retrying (%d/2) for S%02dE%02d'
                 % (attempt, season_number, episode_number))
        try:
            resp = _tmdb_get(url, params=params, timeout=(4, 15))
            if resp.status_code != 200:
                _log(xbmc.LOGERROR, 'TMDB episode HTTP %d for S%02dE%02d'
                     % (resp.status_code, season_number, episode_number))
                if attempt == 2:
                    details = None
                continue
            details = _parse_episode_data(resp.json(), season_number,
                                          episode_number)
            break
        except requests.exceptions.Timeout:
            if attempt == 2:
                _log(xbmc.LOGERROR, 'TMDB episode timeout (retry failed)')
                details = None
        except Exception as e:
            if attempt == 2:
                _log(xbmc.LOGERROR, 'TMDB episode error: %s' % e)
                details = None

    if details:
        _cache_put(_details_cache, cache_key, details)
    return details


# ============================================================
# Helpers
# ============================================================
_CJK_RE = re.compile(r'[\u4e00-\u9fff]+')
_CJK_NUM_RE = re.compile(r'[\u4e00-\u9fff]+[0-9]*')
# CJK run with an optional glued leading Latin letter ("X圣治", "A计划")
_CJK_RUN_RE = re.compile(r'[A-Za-z]?[\u4e00-\u9fff]+[0-9]*')
_LATIN_RE = re.compile(r'[A-Za-z]')

# Standalone quality/source tokens that pollute TMDB queries when they
# survive filename cleaning ("Movie.4k", "Movie 2160p", "乱 Ran 4k").
# Stripped (word-boundary, case-insensitive) from every search variant.
_QUERY_JUNK_TOKENS_RE = re.compile(
    r'(?<![a-z0-9])(?:'
    r'4k|8k|uhd|sdr|hdr10|hdr|dv|dolby|vision|atmos|imax|3d|2d|'
    r'2160p|2160i|1080p|1080i|720p|720i|576p|576i|480p|480i|60fps|'
    r'blu-?ray|bdrip|bdremux|remux|web-?dl|webrip|hdtv|dvdrip|hddvd|'
    r'hevc|avc|h\.?265|x\.?265|h\.?264|x\.?264|xvid|divx|'
    r'dts-?hd|dts|ac3|aac|truehd|ddp|lpcm|pcm|flac|'
    r'diy|sgnb|repack|proper|remastered|extended|unrated|limited|'
    r'cd[1-9]|remux'
    r')(?![a-z0-9])', re.IGNORECASE)

# CJK runs dropped from search variants: a run is dropped only when it is
# EXACTLY one of these release-tag words. Real title runs ("天启" in
# "X战警：天启") never match this set and are always kept.
_CJK_TAG_RUNS = frozenset((
    '字幕', '特效字幕', '特效', '简繁', '双语', '简体', '繁体', '简英', '简德',
    '简法', '中字', '中英', '三语', '多语', '双国语', '国英',
    '国语', '国配', '粤配', '配音', '音轨', '原盘', '原盤', '内封', '外置',
    '德版', '美版', '欧版', '港版', '台版', '日版', '国版', '亚太版',
    '西班牙版', '官译', '官譯', '保留', '杜比', '视界', '全景声', '高码',
    '次世代', '次时代', '未删减', '加长版', '剧场版', '修复版', '完整版',
    '公映', '上译', '央视', '中文字幕', '双语字幕', '特效中字', '英语', '日语',
))

# CJK bracket tags that may survive into a title (AI output, manual input)
_BRACKET_TAG_RE = re.compile(r'[【［〔].*?[】］〕]|（.*?）')


def _has_query_junk(text):
    """True when the string still carries quality/source tokens or CJK
    release-tag words that would make TMDB return 0 results."""
    if _QUERY_JUNK_TOKENS_RE.search(text):
        return True
    for run in _CJK_RUN_RE.findall(text):
        core = re.sub(r'^[A-Za-z]', '', run)
        if core in _CJK_TAG_RUNS:
            return True
    return False


def _strip_query_junk(text):
    """Remove standalone quality tokens, CJK tag runs and trailing
    separator noise from a search candidate string."""
    out = _QUERY_JUNK_TOKENS_RE.sub(' ', text)
    if _CJK_RE.search(out):

        def _keep(m):
            core = re.sub(r'^[A-Za-z]', '', m.group(0))
            return ' ' if core in _CJK_TAG_RUNS else m.group(0)

        out = _CJK_RUN_RE.sub(_keep, out)
    out = re.sub(r'[\s\-–—_]+', ' ', out)
    return out.strip(' .-_：:')


def _title_variants(title):
    """Search query variants for a title.

    Mixed CJK+Latin titles ("我是传奇I Am Legend", "流浪地球2The Wandering
    Earth") rarely match TMDB's index as a whole string - the title is
    indexed either fully in Chinese or fully in the original language.

    Variant chain (deduplicated, original order first):
      1. the title itself - skipped when it still carries release junk
         ("Movie.4k-特效字幕"), which makes TMDB return 0 results;
      2. dots/underscores normalized to spaces, junk tokens and CJK tag
         runs stripped ("绿皮书 Green Book 4k- 特效字幕" -> "绿皮书 Green Book");
      3. the CJK-only part, keeping sequel digits ("流浪地球2") and a glued
         leading Latin letter ("X圣治", "A计划"), tag runs removed;
      4. the Latin-only part with leading sequel digits dropped and junk
         tokens removed ("2The Wandering Earth 4k" -> "The Wandering Earth").
    Leftover CJK bracket tags ("【中字】") are stripped everywhere.
    """
    if not title:
        return []
    variants = []
    # Original title is only useful as a query when it carries no junk
    if not _has_query_junk(title):
        variants.append(title)

    # Normalize for search: strip CJK bracket tags, dots/underscores -> space
    base = _BRACKET_TAG_RE.sub(' ', title)
    base = re.sub(r'[._\u3000]+', ' ', base)
    base = ' '.join(base.split())
    cleaned = _strip_query_junk(base)
    if cleaned and cleaned.lower() not in (
            v.strip().lower() for v in variants):
        variants.append(cleaned)

    src = cleaned or base or title
    if _CJK_RE.search(src) and _LATIN_RE.search(src):
        # CJK part: keep runs, drop pure-tag runs ("绿皮书 特效字幕" ->
        # "绿皮书"); a single-character run ("乱", "鲸") is kept when it is
        # the only CJK run - those are real (short) Chinese titles.
        runs = _CJK_RUN_RE.findall(src)
        keep = [r for r in runs
                if re.sub(r'^[A-Za-z]', '', r) not in _CJK_TAG_RUNS]
        if keep:
            cjk = ' '.join(keep)
            if (len(cjk.replace(' ', '')) >= 2 or len(keep) == 1) \
                    and cjk.strip().lower() not in (
                        v.strip().lower() for v in variants):
                variants.append(cjk)
        # Latin part: remove CJK, drop junk tokens and leading sequel digits
        latin = re.sub(r'[\u4e00-\u9fff]+', ' ', src)
        latin = re.sub(r'[^A-Za-z0-9\'\s]+', ' ', latin)
        latin = ' '.join(latin.split())
        latin = re.sub(r'^\d+\s*', '', latin)
        latin = _strip_query_junk(latin)
        if len(latin.replace(' ', '')) >= 2 and latin.lower() not in (
                v.strip().lower() for v in variants):
            variants.append(latin)
    # Fall back to the raw title when everything else was stripped away
    if not variants:
        variants.append(title)
    # Dedup while keeping order
    seen = set()
    out = []
    for v in variants:
        k = v.strip().lower()
        if k and k not in seen:
            seen.add(k)
            out.append(v)
    return out


def _select_best_match_light(results, title, year, english_title=''):
    if not results:
        return None, 0.0
    best = max(results, key=lambda r: match_score(r, title, year, english_title))
    return best, match_score(best, title, year, english_title)


def _seq_ratio(a, b):
    import difflib
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _year_of(r):
    d = r.get('release_date') or r.get('first_air_date') or ''
    if len(d) >= 4 and d[:4].isdigit():
        return int(d[:4])
    return None


def _parse_image_list(items, img_type):
    """Parse TMDB images array into our art dict format. Sorted by vote_average desc.

    Preview URLs point directly at TMDB's CDN (same size class), avoiding the
    third-party wsrv.nl proxy which is slow/unreliable from China.
    """
    results = []
    if img_type == 'poster':
        url_size = 'original'
        preview_tmpl = 'w342'
    elif img_type == 'fanart':
        url_size = 'w1920'  # 1920x1080, matches 16:9 detail view
        preview_tmpl = 'w780'
    else:
        url_size = 'original'
        preview_tmpl = 'original'

    for img in sorted(items, key=lambda x: x.get('vote_average', 0),
                      reverse=True):
        fp = img.get('file_path', '')
        if fp:
            results.append({
                'url': TMDB_IMAGE_BASE + url_size + fp,
                'preview': TMDB_IMAGE_BASE + preview_tmpl + fp,
                'width': img.get('width', 0),
                'height': img.get('height', 0),
            })
    return results


def _parse_movie_data(data):
    """Parse TMDB movie response into Kodi-compatible dict."""
    info = {
        'title': data.get('title', ''),
        'originaltitle': data.get('original_title', ''),
        'plot': data.get('overview', ''),
        'tagline': data.get('tagline', ''),
        'premiered': data.get('release_date', ''),
        'year': data.get('release_date', '')[:4] if data.get('release_date') else '',
        'duration': data.get('runtime', 0) or 0,
        'mpaa': '',
        'genre': [g['name'] for g in data.get('genres', [])],
        'studio': [c['name'] for c in data.get('production_companies', [])],
        'country': [c['iso_3166_1'] for c in data.get('production_countries', [])],
        'director': [],
        'credits': [],
    }

    # Release dates for MPAA rating
    release_dates = data.get('release_dates', {}).get('results', [])
    for rd in release_dates:
        if rd.get('iso_3166_1') in ('US', 'CN'):
            for d in rd.get('release_dates', []):
                cert = d.get('certification', '').strip()
                if cert:
                    info['mpaa'] = cert
                    break
        if info['mpaa']:
            break

    # Extract credits
    credits = data.get('credits', {})
    # Cast
    cast = []
    for actor in credits.get('cast', [])[:50]:
        cast.append({
            'name': actor.get('name', ''),
            'role': actor.get('character', ''),
            'thumbnail': (TMDB_IMAGE_BASE + 'w185' + actor['profile_path'])
            if actor.get('profile_path') else '',
            'order': actor.get('order', 0),
        })
    # Crew
    for crew in credits.get('crew', []):
        job = crew.get('job', '').lower()
        name = crew.get('name', '')
        if job == 'director':
            info['director'].append(name)
        if job in ('writer', 'screenplay'):
            info['credits'].append(name)

    # External IDs
    uniqueids = {}
    if data.get('id'):
        uniqueids['tmdb'] = str(data['id'])
    ext_ids = data.get('external_ids', {})
    for id_type in ('imdb', 'tvdb'):
        val = ext_ids.get(id_type, '')
        if val:
            uniqueids[id_type] = str(val)
    if uniqueids.get('imdb'):
        uniqueids['imdb'] = uniqueids['imdb']

    # Ratings
    ratings = {}
    vote_avg = data.get('vote_average', 0) or 0
    vote_count = data.get('vote_count', 0) or 0
    if vote_avg and vote_count:
        ratings['tmdb'] = {'rating': float(vote_avg), 'votes': int(vote_count)}

    # Images - use full TMDB images arrays (sorted by vote_average)
    images = data.get('images', {})
    posters = _parse_image_list(images.get('posters', []), 'poster')
    fanarts = _parse_image_list(images.get('backdrops', []), 'fanart')
    logos = _parse_image_list(images.get('logos', []), 'logo')

    # Fallback to top-level paths if images array empty
    if not posters and data.get('poster_path'):
        posters.append({
            'url': TMDB_IMAGE_BASE + 'original' + data['poster_path'],
            'preview': TMDB_IMAGE_BASE + 'w342' + data['poster_path'],
        })
    if not fanarts and data.get('backdrop_path'):
        fanarts.append({
            'url': TMDB_IMAGE_BASE + 'w1920' + data['backdrop_path'],
            'preview': TMDB_IMAGE_BASE + 'w780' + data['backdrop_path'],
        })

    available_art = {'poster': posters, 'fanart': fanarts, 'clearlogo': logos}

    # Collection / Movie Set
    collection = data.get('belongs_to_collection', {}) or {}
    if collection.get('name'):
        info['set'] = collection.get('name', '')
        info['setoverview'] = collection.get('overview', '')
        if collection.get('poster_path'):
            available_art.setdefault('set.poster', []).append({
                'url': TMDB_IMAGE_BASE + 'original' + collection['poster_path'],
                'preview': TMDB_IMAGE_BASE + 'w342' + collection['poster_path'],
            })
        if collection.get('backdrop_path'):
            available_art.setdefault('set.fanart', []).append({
                'url': TMDB_IMAGE_BASE + 'original' + collection['backdrop_path'],
                'preview': TMDB_IMAGE_BASE + 'w780' + collection['backdrop_path'],
            })

    # Trailer: prefer TMDB videos (free, comes with the details request),
    # fall back to Maoyan (China-friendly MP4, circuit-broken + cached).
    info['trailer'] = ''
    yt_key = None
    for v in (data.get('videos', {}) or {}).get('results', []):
        if (v.get('site') == 'YouTube' and v.get('type') == 'Trailer'
                and v.get('key')):
            yt_key = v['key']
            break
    if yt_key:
        info['trailer'] = ('plugin://plugin.video.youtube/play/?video_id=%s'
                           % yt_key)
        _log(xbmc.LOGINFO, 'TMDB trailer OK: %s' % info['title'])
    else:
        trailer = _fetch_maoyan_trailer(info['title'],
                                        info.get('premiered', ''))
        if trailer:
            info['trailer'] = trailer
            _log(xbmc.LOGINFO, 'Maoyan trailer OK: %s -> %s'
                 % (info['title'], trailer[:60]))
        else:
            _log(xbmc.LOGINFO, 'Trailer MISS: %s (%s)'
                 % (info['title'], info.get('premiered', '')))

    return {
        'info': info,
        'uniqueids': uniqueids,
        'ratings': ratings,
        'available_art': available_art,
        'cast': cast,
    }


def _parse_tv_data(data):
    """Parse TMDB TV show response into Kodi-compatible dict."""
    info = {
        'title': data.get('name', ''),
        'originaltitle': data.get('original_name', ''),
        'plot': data.get('overview', ''),
        'tagline': data.get('tagline', ''),
        'premiered': data.get('first_air_date', ''),
        'duration': 0,
        'mpaa': '',
        'genre': [g['name'] for g in data.get('genres', [])],
        'studio': [c['name'] for c in data.get('production_companies', [])],
        'country': [c['iso_3166_1'] for c in data.get('production_countries', [])],
        'director': [],
        'credits': [],
        'year': data.get('first_air_date', '')[:4] if data.get('first_air_date') else '',
        'status': data.get('status', ''),
    }

    # Credits
    credits = data.get('credits', {})
    cast = []
    for actor in credits.get('cast', [])[:50]:
        cast.append({
            'name': actor.get('name', ''),
            'role': actor.get('character', ''),
            'thumbnail': (TMDB_IMAGE_BASE + 'w185' + actor['profile_path'])
            if actor.get('profile_path') else '',
            'order': actor.get('order', 0),
        })
    for creator in credits.get('created_by', []):
        info['director'].append(creator.get('name', ''))

    # External IDs
    uniqueids = {}
    if data.get('id'):
        uniqueids['tmdb'] = str(data['id'])
    ext_ids = data.get('external_ids', {})
    for id_type in ('imdb', 'tvdb'):
        val = ext_ids.get(id_type, '')
        if val:
            uniqueids[id_type] = str(val)

    # Ratings
    ratings = {}
    vote_avg = data.get('vote_average', 0) or 0
    vote_count = data.get('vote_count', 0) or 0
    if vote_avg and vote_count:
        ratings['tmdb'] = {'rating': float(vote_avg), 'votes': int(vote_count)}

    # Images - use full TMDB images arrays
    images = data.get('images', {})
    posters = _parse_image_list(images.get('posters', []), 'poster')
    fanarts = _parse_image_list(images.get('backdrops', []), 'fanart')
    logos = _parse_image_list(images.get('logos', []), 'logo')

    if not posters and data.get('poster_path'):
        posters.append({
            'url': TMDB_IMAGE_BASE + 'original' + data['poster_path'],
            'preview': TMDB_IMAGE_BASE + 'w342' + data['poster_path'],
        })
    if not fanarts and data.get('backdrop_path'):
        fanarts.append({
            'url': TMDB_IMAGE_BASE + 'w1920' + data['backdrop_path'],
            'preview': TMDB_IMAGE_BASE + 'w780' + data['backdrop_path'],
        })

    available_art = {'poster': posters, 'fanart': fanarts, 'clearlogo': logos}

    # Seasons (for manual match UI)
    seasons = []
    for s in (data.get('seasons') or []):
        sn = s.get('season_number', 0)
        if sn > 0:
            seasons.append({
                'season_number': sn,
                'name': s.get('name', 'Season %d' % sn),
                'episode_count': s.get('episode_count', 0),
            })

    return {
        'info': info,
        'uniqueids': uniqueids,
        'ratings': ratings,
        'available_art': available_art,
        'cast': cast,
        'seasons': seasons,
    }


def get_tv_season_episodes(tmdb_id, season_number, api_key, language='zh-CN'):
    """Get episode list for a specific season of a TV show. Returns list or None."""
    url = TMDB_TV_URL.format(tmdb_id) + '/season/%d' % season_number
    params = {
        'api_key': api_key,
        'language': language,
    }

    for attempt in (1, 2):
        if attempt > 1:
            _log(xbmc.LOGINFO,
                 'TMDB season episodes retrying (%d/2) for S%02d'
                 % (attempt, season_number))
        try:
            resp = _tmdb_get(url, params=params, timeout=(4, 15))
            if resp.status_code != 200:
                _log(xbmc.LOGERROR, 'TMDB season episodes HTTP %d for S%02d'
                     % (resp.status_code, season_number))
                if attempt == 2:
                    return None
                continue
            data = resp.json()
            episodes = []
            for ep in data.get('episodes', []):
                episodes.append({
                    'episode_number': ep.get('episode_number', 0),
                    'name': ep.get('name', '第 %d 集' % ep.get('episode_number', 0)),
                    'air_date': ep.get('air_date', ''),
                    'overview': ep.get('overview', ''),
                })
            _log(xbmc.LOGINFO, 'TMDB season %d episodes: %d found'
                 % (season_number, len(episodes)))
            return episodes
        except requests.exceptions.Timeout:
            if attempt == 2:
                _log(xbmc.LOGERROR,
                     'TMDB season episodes timeout (retry failed) for S%02d'
                     % season_number)
                return None
        except Exception as e:
            if attempt == 2:
                _log(xbmc.LOGERROR, 'TMDB season episodes error: %s' % e)
                return None

    return None


def _parse_episode_data(data, season_number, episode_number):
    """Parse TMDB episode response into Kodi-compatible dict."""
    info = {
        'title': data.get('name', ''),
        'plot': data.get('overview', ''),
        'aired': data.get('air_date', ''),
        'duration': data.get('runtime', 0) or 0,
        'director': [],
        'credits': [],
        'productioncode': data.get('production_code', ''),
    }

    # Credits from episode
    credits = data.get('credits', {})
    cast = []
    for actor in credits.get('cast', [])[:30]:
        cast.append({
            'name': actor.get('name', ''),
            'role': actor.get('character', ''),
            'thumbnail': (TMDB_IMAGE_BASE + 'w185' + actor['profile_path'])
            if actor.get('profile_path') else '',
            'order': actor.get('order', 0),
        })
    for crew in credits.get('crew', []):
        job = crew.get('job', '').lower()
        name = crew.get('name', '')
        if job == 'director':
            info['director'].append(name)
        if job in ('writer', 'screenplay'):
            info['credits'].append(name)

    # Guest stars
    guest_stars = data.get('guest_stars', [])
    for gs in guest_stars:
        cast.append({
            'name': gs.get('name', ''),
            'role': gs.get('character', ''),
            'thumbnail': (TMDB_IMAGE_BASE + 'w185' + gs['profile_path'])
            if gs.get('profile_path') else '',
            'order': gs.get('order', 99),
        })

    # External IDs from episode
    uniqueids = {}
    ext_ids = data.get('external_ids', {})
    for id_type in ('imdb', 'tmdb', 'tvdb'):
        val = ext_ids.get(id_type, '')
        if val:
            uniqueids[id_type] = str(val)

    # Ratings
    ratings = {}
    vote_avg = data.get('vote_average', 0) or 0
    vote_count = data.get('vote_count', 0) or 0
    if vote_avg and vote_count:
        ratings['tmdb'] = {'rating': float(vote_avg), 'votes': int(vote_count)}

    # Episode still image
    posters = []
    if data.get('still_path'):
        posters.append({
            'url': TMDB_IMAGE_BASE + 'original' + data['still_path'],
            'preview': TMDB_IMAGE_BASE + 'w300' + data['still_path'],
        })

    available_art = {'poster': posters, 'fanart': [], 'clearlogo': []}

    return {
        'info': info,
        'uniqueids': uniqueids,
        'ratings': ratings,
        'available_art': available_art,
        'cast': cast,
    }


def _build_tv_thumb_xml(art):
    """Build c06 thumb XML for tvshow update."""
    parts = []
    for p in art.get('poster', [])[:5]:
        preview = p.get('preview', '')
        url = p.get('url', '')
        if url:
            parts.append(
                '<thumb spoof="" cache="" aspect="poster" '
                'preview="%s">%s</thumb>' % (preview, url))
    return ''.join(parts)


def _build_tv_fanart_xml(art):
    """Build c11 fanart XML for tvshow update."""
    parts = []
    for f in art.get('fanart', [])[:5]:
        preview = f.get('preview', '')
        url = f.get('url', '')
        if url:
            parts.append(
                '<thumb spoof="" cache="" aspect="fanart" '
                'preview="%s">%s</thumb>' % (preview, url))
    return ''.join(parts)


# ==========================================
# Maoyan Trailer API (circuit-broken + cached)
# ==========================================
_MAOYAN_HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                   'AppleWebKit/537.36 (KHTML, like Gecko) '
                   'Chrome/120.0.0.0 Safari/537.36'),
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
}


def _maoyan_get(url):
    """Rate-limited Maoyan GET with circuit breaker."""
    global _MAOYAN_OK
    if not _MAOYAN_OK:
        return None
    _LIMITER.wait()
    try:
        return _get_session().get(url, timeout=(2, 5),
                                   headers=_MAOYAN_HEADERS)
    except requests.exceptions.RequestException as e:
        # First network-level failure -> assume Maoyan unreachable for this
        # session (e.g. blocked); do not let it stall the scan.
        _MAOYAN_OK = False
        _log(xbmc.LOGWARNING, 'Maoyan unreachable, disabling for this scan: %s'
             % e)
        return None


def _search_maoyan_movie_id(keyword, year):
    """Search Maoyan for a movie by keyword and year. Returns movie_id or None."""
    try:
        resp = _maoyan_get(
            'https://apis.netstart.cn/maoyan/search/movies?keyword=%s&ci=1'
            % quote(keyword))
        if resp is None or resp.status_code != 200:
            return None
        data = resp.json()
        if not data or not isinstance(data, list) or len(data) == 0:
            return None
        if len(data) == 1:
            return data[0].get('id')

        # Filter by year
        if year:
            year_str = str(year)[:4]
            by_year = [item for item in data
                       if (item.get('release') or '').startswith(year_str)]
            if len(by_year) == 1:
                return by_year[0].get('id')

            # Try "title (year)" format
            keyword2 = '%s (%s)' % (keyword, year_str)
            resp2 = _maoyan_get(
                'https://apis.netstart.cn/maoyan/search/movies?keyword=%s&ci=1'
                % quote(keyword2))
            if resp2 is not None and resp2.status_code == 200:
                data2 = resp2.json()
                if data2 and isinstance(data2, list) and len(data2) > 0:
                    return data2[0].get('id')
            return data[0].get('id')

        return data[0].get('id')
    except Exception as e:
        _log(xbmc.LOGDEBUG, 'Maoyan search error: %s' % e)
        return None


def _fetch_maoyan_trailer(title, release_date):
    """Fetch a trailer URL from Maoyan for the given movie.
    Cached per title; circuit-broken on network failure.
    Returns direct MP4 URL or None.
    """
    if not title:
        return None
    if title in _maoyan_cache:
        return _maoyan_cache[title]

    year = ''
    if release_date:
        year = str(release_date)[:4]

    try:
        movie_id = None
        result = None

        # Try title before colon/dash (e.g. "变形金刚5：最后的骑士" -> "变形金刚5")
        prefix = re.split(r'[：\-:]', title)[0].strip()
        if prefix != title:
            movie_id = _search_maoyan_movie_id(prefix, year)
            if movie_id:
                detail_url = ('https://apis.netstart.cn/maoyan/movie/detail?'
                              'movieId=%s' % movie_id)
                resp = _maoyan_get(detail_url)
                if resp is not None and resp.status_code == 200:
                    matches = re.findall(
                        r'https?://vod\.pipi\.cn/[^\s"\']+\.mp4', resp.text)
                    if matches:
                        result = matches[0]

        # Try full title (only if prefix attempt found nothing)
        if not result:
            movie_id = _search_maoyan_movie_id(title, year)
            if movie_id:
                detail_url = ('https://apis.netstart.cn/maoyan/movie/detail?'
                              'movieId=%s' % movie_id)
                resp = _maoyan_get(detail_url)
                if resp is not None and resp.status_code == 200:
                    matches = re.findall(
                        r'https?://vod\.pipi\.cn/[^\s"\']+\.mp4', resp.text)
                    if matches:
                        result = matches[0]

        _maoyan_cache[title] = result
        return result
    except Exception as e:
        _log(xbmc.LOGDEBUG, 'Maoyan trailer failed for "%s" (%s): %s'
             % (title, year, e))
        _maoyan_cache[title] = None
        return None
