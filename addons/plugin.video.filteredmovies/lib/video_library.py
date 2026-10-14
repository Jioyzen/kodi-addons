# -*- coding: utf-8 -*-
from .common import log, DB_PATH, TEXTURES_DB_PATH, THUMBNAILS_PATH
import xbmc
import xbmcgui
import xbmcaddon
import json
import time
import threading
import urllib.parse
from pathlib import Path

# TTL-based query result cache
_QUERY_CACHE = {}
_CACHE_TTL = 30
_QUERY_LOCK = threading.RLock()
_TEXTURE_LOCK = threading.RLock()

def _cache_key(filters, limit, offset=0):
    if filters:
        items = tuple(sorted((k, str(v)) for k, v in filters.items()))
    else:
        items = ()
    return (items, limit, offset)

def _cache_get(key):
    with _QUERY_LOCK:
        entry = _QUERY_CACHE.get(key)
        if entry is not None:
            ts, data = entry
            if time.monotonic() - ts < _CACHE_TTL:
                return data
            _QUERY_CACHE.pop(key, None)
    return None

def _cache_set(key, data):
    if len(data) > 240:
        return
    with _QUERY_LOCK:
        _QUERY_CACHE[key] = (time.monotonic(), data)
        if len(_QUERY_CACHE) > 16:
            oldest = min(_QUERY_CACHE.items(), key=lambda x: x[1][0])
            _QUERY_CACHE.pop(oldest[0], None)

_PROGRESS_CACHE = {}

# 模块级 props 常量，避免多处重复定义
_MOVIE_PROPS = [
    "title", "art", "dateadded", "rating", "year", "file", "resume", "runtime",
    "lastplayed", "playcount", "genre", "plot", "country", "premiered", "mpaa",
    "trailer", "tagline", "director", "studio", "originaltitle", "imdbnumber",
    "sorttitle", "userrating", "votes", "streamdetails",
]
_TVSHOW_PROPS = [
    "title", "art", "dateadded", "rating", "year", "episode", "watchedepisodes",
    "file", "lastplayed", "playcount", "genre", "plot", "premiered", "mpaa",
    "studio", "originaltitle", "imdbnumber", "sorttitle", "userrating", "votes",
]

_PROGRESS_CACHE_TTL = 60


def get_search_field():
    """返回用户选择的搜索索引字段名（originaltitle 或 sorttitle）。"""
    return xbmcaddon.Addon('plugin.video.filteredmovies').getSetting('search_field') or 'originaltitle'


def get_filter_val(filters, key, default=None):
    if filters and key in filters:
        return filters[key]
    return default

def has_t9_filter(filters):
    t9 = get_filter_val(filters, "filter.t9")
    if t9 is None:
        return False
    return bool(str(t9).strip())

def _t9_match_distance(field_value, search_str):
    """计算搜索串在目标字段中匹配位置到最近前一个 | 的距离，距离越小越好。"""
    if not field_value or not search_str:
        return float('inf')
    target = field_value if field_value.startswith('|') else '|' + field_value
    min_dist = float('inf')
    start = 0
    while True:
        pos = target.find(search_str, start)
        if pos < 0:
            break
        pipe_pos = target.rfind('|', 0, pos)
        dist = pos - pipe_pos - 1 if pipe_pos >= 0 else pos
        if dist < min_dist:
            min_dist = dist
            if min_dist == 0:
                break
        start = pos + 1
    return min_dist


def jsonrpc_get_items(filters=None, limit=500, offset=0):
    """
    获取筛选项 - 纯 SQLite 直读，彻底移除 JSON-RPC。
    SQLite 失败时返回空列表。
    """
    media_type = get_filter_val(filters, "filter.mediatype", "all")
    t9_val = get_filter_val(filters, "filter.t9")
    t9_active = t9_val is not None and str(t9_val).strip() != "" and not str(t9_val).strip().startswith('|')

    log(f"jsonrpc_get_items: type={media_type}, limit={limit}")

    from . import catalog_cache
    rev = catalog_cache.revision()
    disk_key = catalog_cache.key(filters, limit, offset)
    cached = catalog_cache.get(disk_key, rev)
    if cached is not None:
        return cached
    # Use cache for non-T9 queries
    if not t9_active:
        ck = (_cache_key(filters, limit, offset), rev)
        cached = _cache_get(ck)
        if cached is not None:
            log(f"jsonrpc_get_items: cache hit, returning {len(cached)} items")
            return cached

    # 纯 SQLite 直读 - 无 JSON-RPC 回退
    items = _sqlite_get_items(media_type, filters, limit, offset)

    if items is None:
        items = []

    # T9 distance sorting
    if t9_active:
        raw_t9 = str(t9_val).strip()
        _sf = get_search_field()
        def _t9_key(item):
            if item.get("media_type") == "set":
                field = item.get("plot", "")
            else:
                field = item.get(_sf, "")
            return _t9_match_distance(field, raw_t9)
        items.sort(key=_t9_key)
    else:
        _cache_set(ck, items)

    if catalog_cache.revision() == rev:
        catalog_cache.put(disk_key, rev, items)
    return items


def get_filtered_count(filters=None):
    from . import db
    return db.get_filtered_count(filters)


def _sqlite_get_items(media_type, filters, limit, offset=0):
    """纯 SQLite 查询入口 - 不涉及任何 JSON-RPC"""
    try:
        from . import db
        if media_type == "set":
            return db.sqlite_get_sets(filters, limit, offset)
        elif media_type in ("concert", "documentary"):
            return db.sqlite_get_items(filters, limit, offset)
        elif media_type == "__old_concert":
            # 演唱会: TMDB 返回 音乐/Music/音樂 三种，SQL 阶段全匹配
            cf = dict(filters) if filters else {}
            cf["filter.genre"] = "音乐"
            items = db.sqlite_get_items(cf, limit * 2)
            # 二次严格匹配：只有一个 genre 且是三种之一
            concert_genres = ('音乐', 'Music', '音樂')
            items = [x for x in items if x.get('genre') and len(x.get('genre', [])) == 1
                     and x['genre'][0] in concert_genres]
            items = items[:limit]
            return items
        elif media_type == "documentary":
            import re
            items = db.sqlite_get_items(filters, limit * 2, offset)
            doc_re = re.compile(r'纪录|记录|Documentary|紀錄', re.I)
            items = [x for x in items if any(doc_re.search(g) for g in x.get('genre',[]))]
            return items[:limit]
        else:
            return db.sqlite_get_items(filters, limit, offset)
    except Exception as e:
        log(f"_sqlite_get_items: SQLite failed ({e})", xbmc.LOGERROR)
        raise


def clear_query_cache():
    """Clear all cached query results and progress maps."""
    global _QUERY_CACHE, _PROGRESS_CACHE
    with _QUERY_LOCK:
        _QUERY_CACHE.clear()
        _PROGRESS_CACHE.clear()
    log("Query caches cleared")


def get_total_count():
    """获取所有影片的总数（电影 + 剧集 + 系列电影）- 纯 SQLite"""
    try:
        from . import db
        return db.get_total_count()
    except Exception as e:
        log(f"Error getting total count: {e}")
        return 0


def _get_inprogress_episodes_map(conn=None):
    """
    SQLite 直读：获取所有进行中剧集的进度映射。
    返回 {tvshow_id: partial_progress_float}
    """
    import sqlite3
    result = {}
    close_conn = conn is None
    try:
        if conn is None:
            from . import db
            conn = db._conn()
        cur = conn.execute(
            "SELECT idShow, "
            "SUM(CASE WHEN resumeTimeInSeconds>0 AND totalTimeInSeconds>0 "
            "    THEN CAST(resumeTimeInSeconds AS REAL)/totalTimeInSeconds ELSE 0 END) as partial, "
            "SUM(CASE WHEN playCount IS NULL OR playCount=0 THEN 1 ELSE 0 END) as unwatched "
            "FROM episode_view "
            "WHERE resumeTimeInSeconds>0 "
            "GROUP BY idShow"
        )
        for row in cur:
            result[row[0]] = float(row[1] or 0)
        if close_conn:
            conn.close()
    except Exception as e:
        log(f"Error in _get_inprogress_episodes_map: {e}", xbmc.LOGERROR)
        if close_conn and conn:
            try: conn.close()
            except: pass
    return result


def _get_movieset_progress_map(conn=None):
    """
    SQLite 直读：获取所有电影集的进度映射。
    返回 {setid: {total, watched, partial, rating_sum, rating_count}}
    """
    import sqlite3
    result = {}
    close_conn = conn is None
    try:
        if conn is None:
            from . import db
            conn = db._conn()
        cur = conn.execute(
            "SELECT idSet, COUNT(*) as total, "
            "SUM(CASE WHEN playCount>0 THEN 1 ELSE 0 END) as watched, "
            "SUM(CASE WHEN resumeTimeInSeconds>0 AND totalTimeInSeconds>0 "
            "    THEN CAST(resumeTimeInSeconds AS REAL)/totalTimeInSeconds ELSE 0 END) as partial, "
            "SUM(CASE WHEN CAST(rating AS REAL)>0 THEN CAST(rating AS REAL) ELSE 0 END) as rating_sum, "
            "SUM(CASE WHEN CAST(rating AS REAL)>0 THEN 1 ELSE 0 END) as rating_count "
            "FROM movie_view WHERE idSet IS NOT NULL AND idSet>0 GROUP BY idSet"
        )
        for row in cur:
            result[row[0]] = {
                "total": row[1] or 0, "watched": row[2] or 0,
                "partial": float(row[3] or 0), "rating_sum": float(row[4] or 0),
                "rating_count": row[5] or 0,
            }
        if close_conn:
            conn.close()
    except Exception as e:
        log(f"Error in _get_movieset_progress_map: {e}", xbmc.LOGERROR)
        if close_conn and conn:
            try: conn.close()
            except: pass
    return result


def create_list_item(m):
    li = xbmcgui.ListItem(label=m.get("title", ""), offscreen=True)
    li.setContentLookup(False)
    if m.get('media_type') == 'home_more':
        li.setProperty('MFG.ViewAll', m.get('view_all', ''))
        li.setProperty('IsPlayable', 'false')
        li.setIsFolder(True)
        return li, '', True
    # Poster panels must never request image://video@ thumbnails: that can
    # start expensive video extraction on SMB UHD files just by browsing.
    art = m.get("art") or {}
    media_type = m.get("media_type", "movie")
    poster = art.get("poster") or art.get("keyart") or ""
    art_dict = {}
    art_dict['poster'] = _resolve_artwork(
        [art.get('poster'), art.get('keyart')] + list(m.get('poster_fallbacks') or []))
    # Compatibility with older panel XML and native item info dialogs.
    art_dict["thumb"] = art_dict.get("poster", "")
    art_dict['fanart'] = _resolve_artwork(
        [art.get('fanart')] + list(m.get('fanart_fallbacks') or []))
    # Home continue-watching cards prefer dedicated landscape artwork.
    # Never fall back to extracted video thumbnails on high bitrate SMB files.
    for image in [art.get("landscape"), art.get("fanart"), poster] + list(m.get("fanart_fallbacks") or []) + list(m.get("poster_fallbacks") or []):
        if image and not _is_video_thumbnail(image):
            resolved = _get_cached_texture_path(image)
            if resolved:
                art_dict["landscape"] = resolved
                break
    li.setArt(art_dict)

    # ID 处理
    url = ""
    is_folder = False

    if media_type in ("movie", "documentary", "concert"):
        item_id = m.get("movieid")
        if "file" in m:
            url = m["file"]
        else:
            url = f"videodb://movies/titles/{item_id}"
        is_folder = False
    elif media_type == "episode":
        item_id = m.get("episodeid")
        url = m.get('file') or 'videodb://tvshows/titles/%s/%s/%s' % (m.get('tvshowid',0),m.get('season',0),item_id)
        is_folder = False
    elif media_type == "tvshow":
        item_id = m.get("tvshowid")
        url = f"videodb://tvshows/titles/{item_id}/"
        is_folder = True
    elif media_type == "set":
        item_id = m.get("setid")
        url = f"videodb://movies/sets/{item_id}/"
        is_folder = True
    else:
        item_id = m.get("movieid")
        url = f"videodb://movies/titles/{item_id}"
        is_folder = False

    if not item_id:
        return None, None, False

    # 设置 IsPlayable
    if not is_folder:
        li.setProperty("IsPlayable", "true")
        li.setIsFolder(False)
    else:
        li.setProperty("IsPlayable", "false")
        li.setIsFolder(True)

    info_tag = li.getVideoInfoTag()
    info_tag.setTitle(m["title"])
    if media_type == 'episode':
        info_tag.setSeason(int(m.get('season',0)))
        info_tag.setEpisode(int(m.get('episode',0)))
        info_tag.setTvShowTitle(m.get('showtitle',''))
        li.setProperty('MFG.TvshowId',str(m.get('tvshowid',0)))
        li.setProperty('MFG.Season',str(m.get('season',0)))
        li.setProperty('MFG.ShowTitle',m.get('showtitle',''))
        li.setLabel('%s · S%02dE%02d · %s' % (m.get('showtitle') or '电视剧',
                    int(m.get('season',0)),int(m.get('episode',0)),m['title']))
    info_tag.setYear(m.get("year", 0))
    info_tag.setRating(m.get("rating", 0.0))
    if "dateadded" in m:
        info_tag.setDateAdded(m["dateadded"])
    info_tag.setDuration(m.get("runtime", 0))
    if "genre" in m:
        info_tag.setGenres(m["genre"])
    if "country" in m:
        info_tag.setCountries(m["country"])
    if "premiered" in m:
        info_tag.setPremiered(m["premiered"])
    if "mpaa" in m:
        info_tag.setMpaa(m["mpaa"])
    if "tagline" in m:
        info_tag.setTagLine(m["tagline"])
    if "director" in m and m["director"]:
        val = m["director"]
        info_tag.setDirectors(val if isinstance(val, list) else [val])
    if "studio" in m and m["studio"]:
        val = m["studio"]
        info_tag.setStudios(val if isinstance(val, list) else [val])
    if "originaltitle" in m:
        info_tag.setOriginalTitle(m["originaltitle"])
    if "imdbnumber" in m:
        info_tag.setIMDBNumber(m["imdbnumber"])
    if "userrating" in m:
        info_tag.setUserRating(int(m.get("userrating", 0) or 0))
    if "votes" in m:
        info_tag.setVotes(int(m.get("votes", 0) or 0))
    if "trailer" in m:
        info_tag.setTrailer(m["trailer"])
    if "plot" in m:
        info_tag.setPlot(m["plot"])

    # Cast: 让 Kodi 信息对话框能显示演员与头像
    # 注意: Kodi 21(Omega) 的 setCast 只接受 List[xbmc.Actor]，不接受 dict
    cast = m.get("cast")
    if cast:
        try:
            from xbmc import Actor
            use_actor_obj = True
        except Exception:
            use_actor_obj = False
        cast_list = []
        for c in cast:
            thumb = c.get("thumbnail", "")
            if not isinstance(thumb,str):thumb=""
            # 演员头像同样优先走本地纹理缓存
            if thumb:
                thumb = _get_cached_texture_path(thumb)
            if use_actor_obj:
                cast_list.append(Actor(
                    name=c.get("name", ""),
                    role=c.get("role", ""),
                    thumbnail=thumb,
                    order=int(c.get("order", 0) or 0),
                ))
            else:
                # 旧版 Kodi(<=18) 回退：dict 列表
                cast_list.append({
                    "name": c.get("name", ""),
                    "role": c.get("role", ""),
                    "thumbnail": thumb,
                    "order": int(c.get("order", 0) or 0),
                })
        if cast_list:
            try:
                info_tag.setCast(cast_list)
            except Exception as e:
                log(f"setCast failed: {e}", xbmc.LOGWARNING)

    if "playcount" in m:
        info_tag.setPlaycount(m.get("playcount", 0))

    if "file" in m:
        info_tag.setFilenameAndPath(m["file"])
        if media_type in ("movie", "documentary", "concert"):
            info_tag.setPath(f"/@movies/{item_id}")
        else:
            info_tag.setPath(m["file"])

    # streamdetails
    sd = m.get("streamdetails")
    if sd:
        for vs in sd.get("video", [])[:1]:
            li.addStreamInfo("video", {"duration": vs.get("duration", m.get("runtime", 0)), "width": vs.get("width", 0), "height": vs.get("height", 0), "aspect": vs.get("aspect", 0.0), "codec": vs.get("codec", ""), "stereomode": vs.get("stereomode", "")})
        for astr in sd.get("audio", []):
            li.addStreamInfo("audio", {"codec": astr.get("codec", ""), "language": astr.get("language", ""), "channels": astr.get("channels", 0)})
        for sstr in sd.get("subtitle", []):
            li.addStreamInfo("subtitle", {"language": sstr.get("language", "")})

    # 设置断点续播点
    resume = m.get("resume", {})

    if media_type == "tvshow":
        total_episodes = m.get("episode", 0)
        watched_episodes = m.get("watchedepisodes", 0)
        last_played = m.get("lastplayed", "")

        if total_episodes > 0:
            partial = m.get("partial_progress", 0.0)
            val = float(watched_episodes) + partial

            pct = int((val / total_episodes) * 100)
            if pct > 100: pct = 100
            if (partial > 0 or (watched_episodes == 0 and last_played)) and pct == 0:
                pct = 1

            if pct == 100: pct = 0

            li.setProperty("SkinPercentPlayed", str(pct))

    elif media_type == "set":
        total_movies = m.get("total", 0)
        watched_movies = m.get("watched", 0)

        if total_movies > 0:
            partial = m.get("partial_progress", 0.0)
            val = float(watched_movies) + partial

            pct = int((val / total_movies) * 100)
            if pct > 100: pct = 100

            if (partial > 0 or watched_movies > 0) and pct == 0:
                pct = 1

            if pct == 100: pct = 0

            li.setProperty("SkinPercentPlayed", str(pct))

    elif resume and "position" in resume and resume["position"] > 0:
        total = resume.get("total", 0)
        if total == 0:
            total = m.get("runtime", 0)

        if total > 0:
            info_tag.setResumePoint(resume["position"], total)
            pct = int((resume["position"] / total) * 100)
            li.setProperty("SkinPercentPlayed", str(pct))
        else:
            info_tag.setResumePoint(resume["position"])
            pass

    info_tag.setMediaType(media_type if media_type not in ["documentary", "concert"] else "movie")
    info_tag.setDbId(int(item_id))

    li.setPath(url)
    dbtype_map = {"movie": "movie", "tvshow": "tvshow", "set": "set", "concert": "movie", "documentary": "movie", "episode": "episode"}
    li.setProperty("DBTYPE", dbtype_map.get(media_type, "movie"))
    li.setProperty("targetwindow", "videos")
    li.setProperty("FilteredMovies", "true")

    return li, url, is_folder


# 纹理缓存映射（模块级，避免重复查询 Textures13.db）
_TEXTURE_PATH_CACHE = {}
_TEXTURE_CHECKED_AT = {}
_TEXTURE_CACHE_LOADED = False
_CACHED_ARTWORK_ONLY = xbmcaddon.Addon('plugin.video.filteredmovies').getSetting('cached_artwork_only') == 'true'

def _is_video_thumbnail(url):
    value = urllib.parse.unquote(str(url)).lower()
    return value.startswith(("image://video@", "video@"))

def prime_textures(items, force=False):
    """Read only artwork needed by this page, with small bound-variable batches."""
    import sqlite3
    import os
    from urllib.parse import quote
    urls = set()
    for item in items:
        urls.update(url for url in (item.get('art') or {}).values() if isinstance(url,str))
        for key in ('poster_fallbacks', 'fanart_fallbacks'):
            urls.update(url for url in item.get(key, []) if isinstance(url, str))
        urls.update(c.get('thumbnail','') for c in (item.get('cast') or []) if isinstance(c,dict) and isinstance(c.get('thumbnail',''),str))
    sources={}
    for url in urls:
        if url.startswith(('http','image://')):
            decoded=urllib.parse.unquote(url[len('image://'):].rstrip('/')) if url.startswith('image://') else url
            sources[url]=decoded
    now = time.monotonic()
    # Downloads may complete while a window is still alive. Do not retain
    # negative lookups for the entire interpreter lifetime.
    with _TEXTURE_LOCK:
        missing = {url for url in sources
                   if force or url not in _TEXTURE_PATH_CACHE or
                   now - _TEXTURE_CHECKED_AT.get(url, 0) >=
                   (5 if _TEXTURE_PATH_CACHE[url] == url else 60)}
    if not missing:return
    conn=None
    try:
        conn=sqlite3.connect('file:'+quote(TEXTURES_DB_PATH,safe='/')+'?mode=ro',uri=True,timeout=.1)
        candidates=list(missing | {sources[url] for url in missing})
        found={}
        for start in range(0,len(candidates),200):
            part=candidates[start:start+200]
            for source,cached in conn.execute('SELECT url,cachedurl FROM texture WHERE url IN ('+','.join('?' for _ in part)+')',part):
                found[source]=cached
        updates = {}
        for url in missing:
            cached=found.get(url) or found.get(sources[url])
            path=os.path.join(THUMBNAILS_PATH,cached) if cached else ''
            updates[url]=path if path and os.path.isfile(path) else url
        # Page-scoped cache, not an ever-growing mirror of Textures.db.
        with _TEXTURE_LOCK:
            _TEXTURE_PATH_CACHE.update(updates)
            _TEXTURE_CHECKED_AT.update({url: now for url in updates})
            if len(_TEXTURE_PATH_CACHE)>4096:
                # Prefer this page's covers before actor thumbnails, then
                # retain recently checked entries up to a strict upper bound.
                covers = {url for item in items for url in (item.get('art') or {}).values()
                          if isinstance(url,str)}
                keys = sorted(_TEXTURE_PATH_CACHE, key=lambda url:
                    (url in covers, _TEXTURE_CHECKED_AT.get(url,0)), reverse=True)[:4096]
                keep={url:_TEXTURE_PATH_CACHE[url] for url in keys}
                stamps={url:_TEXTURE_CHECKED_AT.get(url,0) for url in keys}
                _TEXTURE_PATH_CACHE.clear();_TEXTURE_PATH_CACHE.update(keep)
                _TEXTURE_CHECKED_AT.clear();_TEXTURE_CHECKED_AT.update(stamps)
    except (sqlite3.Error,OSError):
        # Let Kodi handle uncached URLs; a texture DB lock must not blank a wall.
        pass
    finally:
        if conn is not None:conn.close()


def _get_cached_texture_path(url):
    if not url:return url
    with _TEXTURE_LOCK:
        resolved = _TEXTURE_PATH_CACHE.get(url, url)
    if _CACHED_ARTWORK_ONLY:
        # Never ask Kodi texture workers to download remote art while browsing.
        scheme = urllib.parse.urlsplit(str(resolved)).scheme.lower()
        if scheme in ('http', 'https', 'image', 'smb', 'dav', 'davs', 'webdav', 'webdavs'):
            return ''
    return resolved


def _resolve_artwork(candidates):
    """Prefer a local cached cover, even if the primary remote URL exists."""
    remote = ''
    for url in candidates:
        if not url or _is_video_thumbnail(url):
            continue
        path = _get_cached_texture_path(url)
        if not path:
            continue
        if not urllib.parse.urlsplit(str(path)).scheme or str(path).startswith('special://'):
            return path
        if not remote:
            remote = path
    return remote


def _load_art_batch(conn, media_ids, media_type, items, id_key):
    """Batch load art for a list of items"""
    if not media_ids:
        return
    mtype = media_type if media_type != 'tvshow' else 'tvshow'
    try:
        sql = f"SELECT media_id, type, url FROM art WHERE media_id IN ({','.join(media_ids)}) AND media_type='{mtype}'"
        cur = conn.execute(sql)
        art_map = {}
        for media_id, art_type, url in cur:
            mid = str(media_id)
            if mid not in art_map:
                art_map[mid] = {}
            art_map[mid][art_type] = url
        for item in items:
            mid = str(item.get(id_key, 0))
            item['art'] = art_map.get(mid, {})
    except Exception:
        pass


def _read_continue_watching_items(limit=20):
    """
    读取具体电影和单集的续播书签，不汇总电视剧或电影合集。
    混合显示，按最近播放时间排序。
    """
    import sqlite3
    from . import db
    all_items = []
    db_path = DB_PATH
    conn = None

    try:
        conn = db._conn()

        # ==================== 0. 预加载进度映射（共用连接） ====================

        # ==================== 1. 进行中的电影 (SQLite) ====================
        try:
            props = db._build_props('movie')
            selects = ', '.join(f'{column} AS {key}' for key,column in props.items())
            cur = conn.execute(
                'SELECT '+selects+' FROM movie_view '
                'WHERE resumeTimeInSeconds>0 AND (playCount IS NULL OR playCount=0) '
                'ORDER BY lastPlayed DESC LIMIT ?', (limit,)
            )
            cols = [d[0] for d in cur.description]
            movie_ids = []
            for row in cur:
                m = dict(zip(cols, row))
                db._format_item(m)
                m["media_type"] = "movie"
                m["sort_time"] = m.get("lastplayed", "")
                all_items.append(m)
                movie_ids.append(str(m.get('movieid', 0)))
            # Batch load art / cast for movies
            if movie_ids:
                movie_items = all_items[-len(movie_ids):]
                _load_art_batch(conn, movie_ids, 'movie', movie_items, 'movieid')
                db._load_cast(conn, movie_items, 'movie')
            log(f"Found {len(movie_ids)} in-progress movies (SQLite)")
        except Exception as e:
            log(f"Error fetching in-progress movies (SQLite): {e}", xbmc.LOGERROR)

        # Concrete episodes keep their own title, numbering and resume point.
        try:
            cur=conn.execute(
                'SELECT idEpisode AS episodeid,idShow AS tvshowid,c00 AS title,c01 AS plot,'
                'c05 AS premiered,CAST(substr(c05,1,4) AS INTEGER) AS year,'
                'c09 AS runtime,CAST(c12 AS INTEGER) AS season,CAST(c13 AS INTEGER) AS episode,'
                'strTitle AS showtitle,genre,studio,mpaa,rating,playCount AS playcount,'
                'lastPlayed AS lastplayed,dateAdded AS dateadded,strPath AS file,strFileName AS _fname,'
                'resumeTimeInSeconds AS resume,totalTimeInSeconds AS _resume_total '
                'FROM episode_view WHERE resumeTimeInSeconds>0 AND (playCount IS NULL OR playCount=0) '
                'ORDER BY lastPlayed DESC,idEpisode DESC LIMIT ?', (limit,))
            names=[column[0] for column in cur.description]
            episodes=[dict(zip(names,row)) for row in cur]
            for episode in episodes:
                db._format_item(episode)
                episode['media_type']='episode'
                episode['sort_time']=episode.get('lastplayed','')
            _load_art_batch(conn,[str(e['episodeid']) for e in episodes],'episode',episodes,'episodeid')
            shows=[{'tvshowid':sid} for sid in dict.fromkeys(e['tvshowid'] for e in episodes)]
            _load_art_batch(conn,[str(show['tvshowid']) for show in shows],'tvshow',shows,'tvshowid')
            show_art={show['tvshowid']:show.get('art',{}) for show in shows}
            for episode in episodes:
                own=episode.get('art',{});parent=show_art.get(episode['tvshowid'],{})
                episode['art']=dict(parent,**own)
                episode['art']['poster']=parent.get('poster') or own.get('poster','')
                episode['art']['fanart']=parent.get('fanart') or own.get('fanart','')
                episode['art']['landscape']=own.get('thumb') or parent.get('landscape') or parent.get('fanart','')
            all_items.extend(episodes)
        except Exception as e:
            log(f"Error fetching in-progress episodes (SQLite): {e}",xbmc.LOGERROR)

        conn.close()

    except Exception as e:
        log(f"Error in get_continue_watching_items (SQLite): {e}", xbmc.LOGERROR)
        raise
    finally:
        if conn is not None:conn.close()

    # ==================== 4. 按最后播放时间排序 ====================
    def sort_key(item):
        sort_time = item.get("sort_time", "")
        return sort_time if sort_time else "0000-00-00 00:00:00"

    all_items.sort(key=sort_key, reverse=True)

    # ==================== 5. 限制数量 ====================
    result=[];seen=set()
    for item in all_items:
        identity=(item['media_type'],item.get('episodeid') if item['media_type']=='episode' else item.get('movieid'))
        if identity in seen:continue
        seen.add(identity);result.append(item)
        if len(result)>=limit:break
    log('Continue watching: returning %d movies/episodes'%len(result))

    return result




def get_continue_watching_items(limit=20):
    from . import catalog_cache
    try:limit=max(0,min(30,int(limit)))
    except (TypeError,ValueError):limit=20
    if not limit:return []
    rev=catalog_cache.revision();key=catalog_cache.key({'widget':'continue-movie-episode-v2'},limit,0)
    cached=catalog_cache.get(key,rev)
    if cached is not None:return cached
    items=_read_continue_watching_items(limit)
    if catalog_cache.revision()==rev:catalog_cache.put(key,rev,items)
    return items
