# -*- coding: utf-8 -*-
"""SQLite access using the current Kodi schema and batched artwork"""
import sqlite3, time, json, math
from .common import log, DB_PATH
from .regions import region_match
from .genres import genre_match, genre_values

# Kodi 22 VideoDatabaseColumns.h. c07 is unused, c05 is a rating ID,
# c22 is the base path, and bookmark totalTime is not the scraped runtime.
_MOVIE_FIELDS = [
    ('movieid', 'idMovie'), ('title', 'c00'), ('plot', 'c01'), ('tagline', 'c03'),
    ('rating', 'rating'), ('mpaa', 'c12'), ('genre', 'c14'),
    ('director', 'c15'), ('originaltitle', 'c16'), ('studio', 'c18'),
    ('trailer', 'c19'), ('country', 'c21'), ('premiered', 'premiered'), ('sorttitle', 'c10'),
    ('imdbnumber', 'uniqueid_value'), ('userrating', 'userrating'), ('votes', 'votes'),
    ('playcount', 'playCount'), ('lastplayed', 'lastPlayed'), ('dateadded', 'dateAdded'),
    ('runtime', 'c11'), ('file', 'strPath'), ('_fname', 'strFileName'),
    ('resume', 'resumeTimeInSeconds'), ('_resume_total', 'totalTimeInSeconds'),
]
_TVSHOW_FIELDS = [
    ('tvshowid', 'idShow'), ('title', 'c00'), ('plot', 'c01'),
    ('rating', 'rating'), ('genre', 'c08'), ('originaltitle', 'c09'),
    ('mpaa', 'c13'), ('studio', 'c14'), ('country', 'c18'),
    ('premiered', 'c05'), ('sorttitle', 'c15'),
    ('trailer', 'c16'), ('runtime', 'duration'), ('imdbnumber', 'uniqueid_value'),
    ('userrating', 'userrating'), ('votes', 'votes'),
    ('playcount', 'playCount'), ('lastplayed', 'lastPlayed'), ('dateadded', 'dateAdded'),
    ('episode', 'totalCount'), ('watchedepisodes', 'watchedcount'), ('file', 'strPath'),
]
_LIST_FIELDS = ['genre', 'country', 'director', 'studio']
_INT_FIELDS = ['year', 'playcount', 'runtime', 'episode', 'watchedepisodes',
               'userrating', 'votes', 'tvshowid', 'movieid']

def _conn():
    from urllib.parse import quote
    conn = sqlite3.connect("file:"+quote(DB_PATH,safe="/")+"?mode=ro", uri=True, timeout=.3)
    conn.create_function('region_match', 2, region_match)
    conn.create_function('genre_match', 2, genre_match)
    return conn
def _get_val(f, k, d=None): return f[k] if f and k in f else d

def _parse_list(val):
    if val and isinstance(val, str):
        return [v.strip() for v in val.split('/') if v.strip()]
    return val if isinstance(val, list) else ([] if not val else [val])

def _format_item(item):
    for k in _INT_FIELDS:
        if k in item:
            try: item[k] = int(item.get(k,0) or 0)
            except (ValueError, TypeError, OverflowError): item[k] = 0
            item[k] = max(0,min(item[k],9999 if k=='year' else 10 if k=='userrating' else 2147483647))
    try: item['rating'] = float(item.get('rating',0) or 0)
    except (ValueError, TypeError, OverflowError): item['rating'] = 0.0
    item['rating'] = max(0.0,min(item['rating'],10.0)) if math.isfinite(item['rating']) else 0.0
    for k in _LIST_FIELDS:
        if k in item:
            item[k] = _parse_list(item.get(k))
    try: rv = float(item.get('resume',0) or 0)
    except (ValueError, TypeError, OverflowError): rv = 0.0
    try: total = float(item.pop('_resume_total',0) or 0)
    except (ValueError, TypeError, OverflowError): total = 0.0
    rv=max(0.0,rv) if math.isfinite(rv) else 0.0
    total=max(0.0,total) if math.isfinite(total) else 0.0
    item['resume'] = {'position': min(rv,total) if total else rv, 'total': total}
    for k in ('title','plot','tagline','mpaa','originaltitle','trailer','premiered',
              'sorttitle','imdbnumber','lastplayed','dateadded'):
        if k in item: item[k] = str(item[k] or '')
    # Combine strPath + strFileName for full file path
    fname = item.pop('_fname', '') or ''
    fpath = item.get('file', '') or ''
    if fname and fpath:
        item['file'] = fpath.rstrip('/') + '/' + fname
    # Art loaded separately

def _load_art(conn, items, media_type):
    """Batch-load art for a list of items from the art table"""
    if not items: return
    id_key = 'movieid' if media_type == 'movie' else 'tvshowid'
    ids = [str(item.get(id_key, 0)) for item in items if item.get(id_key)]
    if not ids: return

    mtype = media_type if media_type != 'tvshow' else 'tvshow'
    # Kodi art table: art_id, media_id, media_type, type, url
    sql = f"SELECT media_id, type, url FROM art WHERE media_id IN ({','.join(ids)}) AND media_type='{mtype}'"
    cur = conn.execute(sql)
    art_map = {}
    for media_id, art_type, url in cur:
        media_id = str(media_id)
        if media_id not in art_map:
            art_map[media_id] = {}
        art_map[media_id][art_type] = url

    for item in items:
        mid = str(item.get(id_key, 0))
        item['art'] = art_map.get(mid, {})

def _load_cast(conn, items, media_type):
    """Batch-load cast (actors + roles + headshots) for a list of items"""
    if not items: return
    id_key = 'movieid' if media_type == 'movie' else 'tvshowid'
    ids = [str(item.get(id_key, 0)) for item in items if item.get(id_key)]
    if not ids: return

    mtype = media_type if media_type != 'tvshow' else 'tvshow'
    # Kodi actor_link: actor_id, media_id, media_type, role, cast_order
    # actor: actor_id, name, art_urls (Kodi 19+ 头像存此处 JSON 数组；早期版本存 art 表)
    sql = (
        f"SELECT al.media_id, a.name, al.role, al.cast_order, a.actor_id, a.art_urls "
        f"FROM actor_link al "
        f"JOIN actor a ON a.actor_id = al.actor_id "
        f"WHERE al.media_id IN ({','.join(ids)}) AND al.media_type='{mtype}' "
        f"ORDER BY al.media_id, al.cast_order"
    )
    cast_map = {}
    actor_ids = set()
    cur = conn.execute(sql)
    for media_id, name, role, order, actor_id, art_urls in cur:
        mid = str(media_id)
        cast_map.setdefault(mid, []).append({
            'name': name or '',
            'role': role or '',
            'order': int(order or 0),
            'actor_id': actor_id,
            'art_urls': art_urls or '',
        })
        actor_ids.add(str(actor_id))

    # 1) 优先从 art 表取演员头像（Kodi 18 及更早）
    thumb_map = {}
    if actor_ids:
        aids = ','.join(actor_ids)
        try:
            acur = conn.execute(
                f"SELECT media_id, url FROM art WHERE media_id IN ({aids}) AND media_type='actor' AND type='thumb'"
            )
            for actor_id, url in acur:
                thumb_map[str(actor_id)] = url
        except Exception:
            pass

    for item in items:
        mid = str(item.get(id_key, 0))
        cast_list = cast_map.get(mid, [])
        for c in cast_list:
            # 先 art 表，再 art_urls（Kodi 19+），否则空
            thumb = thumb_map.get(str(c.get('actor_id', '')), '')
            if not thumb:
                au = c.get('art_urls', '')
                if au:
                    try:
                        arr = json.loads(au)
                        if isinstance(arr, list) and arr:
                            thumb = arr[0]
                        elif isinstance(arr, str):
                            thumb = arr
                    except Exception:
                        thumb = au  # 当作单个 URL
            c['thumbnail'] = thumb
            c.pop('actor_id', None)
            c.pop('art_urls', None)
        item['cast'] = cast_list

def _build_props(media_type):
    cols_cache = getattr(_build_props, '_cache', {})
    if media_type not in cols_cache:
        table = 'movie_view' if media_type in ('movie','concert','documentary') else 'tvshow_view'
        from contextlib import closing
        with closing(_conn()) as conn:
            cur = conn.execute(f'SELECT * FROM {table} LIMIT 0')
            all_cols = [d[0] for d in cur.description]
        fields = _MOVIE_FIELDS if media_type in ('movie','concert','documentary') else _TVSHOW_FIELDS
        props = {}
        for py_name, db_name in fields:
            for c in (py_name, db_name):
                if c in all_cols:
                    props[py_name] = c
                    break
        if media_type in ('movie','concert','documentary') and 'isDefaultVersion' in all_cols:
            props['_default_version']='isDefaultVersion'
        premiered = props.get('premiered')
        if premiered:
            props['year'] = f"CAST(substr({premiered}, 1, 4) AS INTEGER)"
        cols_cache[media_type] = props
        _build_props._cache = cols_cache
    return cols_cache[media_type]

def _where(media_type, filters, props):
    filters = filters or {}
    clauses, args = [], []
    if props.get("_default_version"):
        clauses.append(props["_default_version"]+"=1")
    def match(column, value, prefix=False):
        if not column:
            clauses.append('0')
            return
        clauses.append(column + " LIKE ? ESCAPE '\\'")
        value = str(value).replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        args.append(value+'%' if prefix else '%'+value+'%')
    if '_collection_id' in filters:
        # The child page is a movie grid scoped to one collection. Invalid
        # IDs must never fall through to the complete library.
        try:
            collection_id = int(filters['_collection_id'])
        except (ValueError, TypeError):
            collection_id = 0
        if props.get('movieid') and collection_id > 0:
            clauses.append('idSet = ?')
            args.append(collection_id)
        else:
            clauses.append('0')
    genre = filters.get('filter.genre')
    if genre and genre!='类型':
        column = props.get('genre')
        clauses.append('genre_match('+column+', ?) = 1' if column else '0')
        if column:
            args.append(str(genre))
    if media_type == 'concert':
        clauses.append("trim(%s, ' /') IN (?,?,?)" % props['genre'])
        args.extend(('音乐', 'Music', '音樂'))
    elif media_type == 'documentary':
        clauses.append('('+' OR '.join(props['genre']+' LIKE ?' for _ in range(4))+')')
        args.extend(('%纪录%', '%记录%', '%Documentary%', '%紀錄%'))
    region = filters.get('filter.region')
    if region:
        column = props.get('country')
        clauses.append('region_match('+column+', ?) = 1' if column else '0')
        if column: args.append(str(region))
    year = filters.get('filter.year')
    if year and year != '年份':
        column = props.get('year')
        if not column:
            clauses.append('0')
        elif year == 'this_year':
            clauses.append(column+" = CAST(strftime('%Y','now') AS INTEGER)")
        elif year == 'older':
            clauses.append(column+' < 1960')
        elif str(year) in ('1960s','1970s','1980s','1990s','2000s','2010s','2020s'):
            low = int(str(year)[:4]);clauses.append(column+' BETWEEN ? AND ?');args.extend((low,low+9))
    ratings = []
    column = props.get('rating')
    for key, value in filters.items():
        if not key.startswith('filter.rating.') or not value:
            continue
        if key.endswith('10-9'): ratings.append(column+' >= 9')
        elif key.endswith('9-8'): ratings.append(column+' BETWEEN 8 AND 9')
        elif key.endswith('8-7'): ratings.append(column+' BETWEEN 7 AND 8')
        elif key.endswith('7-6'): ratings.append(column+' BETWEEN 6 AND 7')
        elif key.endswith('6分以下'): ratings.append(column+' < 6')
    if ratings: clauses.append('('+' OR '.join(ratings)+')')
    text = filters.get('filter.t9')
    if text:
        import xbmcaddon
        field = xbmcaddon.Addon('plugin.video.filteredmovies').getSetting('search_field') or 'originaltitle'
        match(props.get(field if field in ('originaltitle','sorttitle') else 'originaltitle'), str(text).strip())
    letter = filters.get('filter.letter')
    if letter: match(props.get('title'), letter, True)
    return (' WHERE '+' AND '.join(clauses) if clauses else ''), args


def _sort(filters, props):
    value = (filters or {}).get('filter.sort','dateadded')
    if value == 'random':
        identity=props.get('movieid',props.get('tvshowid'))
        seed=int((filters or {}).get('_random_seed',1)) & 0x7fffffff
        return '((CAST(%s AS INTEGER)*1103515245+%d)&2147483647)' % (identity,seed)
    key = {'latest':'year', 'hot':'playcount'}.get(value,value)
    if key == 'title':
        return props.get('title', 'c00') + ' COLLATE NOCASE'
    return props.get(key, props.get('dateadded','dateAdded'))


def _sort_order(filters):
    # Never interpolate arbitrary filter values into ORDER BY.
    return 'ASC' if (filters or {}).get('filter.sortorder') == 'ascending' else 'DESC'


def _build_sql(media_type, filters, limit, props, offset=0):
    table = 'movie_view' if media_type in ('movie','concert','documentary') else 'tvshow_view'
    where, args = _where(media_type,filters,props)
    identity = 'idMovie' if table == 'movie_view' else 'idShow'
    sql = 'SELECT '+', '.join(column+' AS '+key for key,column in props.items())+' FROM '+table+where
    sql += ' ORDER BY '+_sort(filters,props)+' '+_sort_order(filters)+', '+identity+' ASC LIMIT ? OFFSET ?'
    return sql, args+[max(0,int(limit)),max(0,int(offset))]


def _rows(conn, sql, args, media_type):
    cursor = conn.execute(sql,args)
    columns = [d[0] for d in cursor.description]
    rows = []
    for row in cursor:
        item = dict(zip(columns,row));item['media_type']=media_type
        _format_item(item);rows.append(item)
    return rows


def _hydrate(conn, items, media_type):
    _load_art(conn,items,media_type)
    _load_cast(conn,items,media_type)


def sqlite_get_items(filters=None, limit=500, offset=0):
    from contextlib import closing
    mt = (filters or {}).get('filter.mediatype','all')
    with closing(_conn()) as conn:
        if mt not in ('all','',None):
            props = _build_props(mt)
            sql,args = _build_sql(mt,filters,limit,props,offset)
            rows = _rows(conn,sql,args,mt)
            _hydrate(conn,rows,'tvshow' if mt=='tvshow' else 'movie')
            return rows
        # Select mixed media IDs globally BEFORE OFFSET/LIMIT, then hydrate only
        # this page. Independent per-type offsets would skip/duplicate entries.
        pieces,args = [],[]
        for media_type,table,identity in [('movie','movie_view','idMovie'),('tvshow','tvshow_view','idShow')]:
            props = _build_props(media_type);where,params=_where(media_type,filters,props)
            sort = _sort(filters,props)
            pieces.append("SELECT %s AS item_id, '%s' AS media_type, %s AS sort_value FROM %s%s" %
                          (identity,media_type,sort,table,where));args.extend(params)
        selected = conn.execute('SELECT item_id,media_type FROM ('+' UNION ALL '.join(pieces)+
                                ') ORDER BY sort_value '+_sort_order(filters)+',media_type ASC,item_id ASC LIMIT ? OFFSET ?',
                                args+[max(0,int(limit)),max(0,int(offset))]).fetchall()
        indexed = {}
        for media_type,table,identity,id_key in [('movie','movie_view','idMovie','movieid'),('tvshow','tvshow_view','idShow','tvshowid')]:
            ids = [row[0] for row in selected if row[1]==media_type]
            if not ids:continue
            props = _build_props(media_type)
            sql='SELECT '+', '.join(column+' AS '+key for key,column in props.items())+' FROM '+table
            batch=[]
            for start in range(0,len(ids),200):
                part=ids[start:start+200]
                default_only = ' AND '+props['_default_version']+'=1' if props.get('_default_version') else ''
                batch.extend(_rows(conn,sql+' WHERE '+identity+' IN ('+','.join('?' for _ in part)+')'+default_only,part,media_type))
            _hydrate(conn,batch,media_type)
            indexed.update({(item[id_key],media_type):item for item in batch})
        return [indexed[(identity,media_type)] for identity,media_type in selected if (identity,media_type) in indexed]



def load_set_art(conn, items):
    """Load collection art in batches; missing posters use a member's poster.

    Display-only fallback: do not write to Kodi's library or read media files.
    A stable movie ID order avoids changing covers when the wall is refreshed.
    """
    indexed = {}
    for item in items:
        if item.get('setid'):
            indexed.setdefault(item['setid'], []).append(item)
    ids = list(indexed)
    default_only = ' AND m.isDefaultVersion=1' if _build_props('movie').get('_default_version') else ''
    for start in range(0, len(ids), 200):
        part = ids[start:start + 200]
        marks = ','.join('?' for _ in part)
        art = {}
        for sid, kind, url in conn.execute(
                "SELECT media_id,type,url FROM art WHERE media_type='set' AND media_id IN (" + marks + ')', part):
            art.setdefault(sid, {})[kind] = url
        # Bound candidates per set/type and retain them separately from art.
        # Resolution later can distinguish a URL from an available cached image.
        fallback = {}
        sql = ("SELECT idSet,type,url FROM (SELECT m.idSet,a.type,a.url, "
               "ROW_NUMBER() OVER (PARTITION BY m.idSet,a.type ORDER BY m.idMovie,a.art_id) AS rn "
               "FROM movie_view m JOIN art a ON a.media_id=m.idMovie "
               "AND a.media_type='movie' AND a.type IN ('poster','keyart','fanart') "
               "WHERE m.idSet IN (" + marks + ") AND LENGTH(TRIM(a.url))>0" + default_only +
               ") WHERE rn<=8 ORDER BY idSet,type,rn")
        for sid, kind, url in conn.execute(sql, part):
            group = 'fanart_fallbacks' if kind == 'fanart' else 'poster_fallbacks'
            urls = fallback.setdefault(sid, {}).setdefault(group, [])
            if url not in urls:
                urls.append(url)
        for sid in part:
            covers = fallback.get(sid, {})
            primary = dict(art.get(sid, {}))
            if not primary.get('poster') and not primary.get('keyart') and covers.get('poster_fallbacks'):
                primary['poster'] = covers['poster_fallbacks'][0]
            for item in indexed[sid]:
                item['art'] = dict(primary)
                item.update({key: list(urls) for key, urls in covers.items()})


def sqlite_get_sets(filters=None, limit=500, offset=0):
    from contextlib import closing
    with closing(_conn()) as conn:
        clauses,args=[],[]
        text=(filters or {}).get('filter.t9')
        if text:
            clauses.append('(s.strSet LIKE ? OR s.strOverview LIKE ?)');args.extend(('%'+str(text)+'%',)*2)
        where=' WHERE '+' AND '.join(clauses) if clauses else ''
        default_only = ' AND m.isDefaultVersion=1' if _build_props('movie').get('_default_version') else ''
        sort = {
            'title': 's.strSet COLLATE NOCASE',
            'rating': 'AVG(NULLIF(m.rating,0))',
            'premiered': 'MAX(m.premiered)',
            'lastplayed': 'MAX(m.lastPlayed)',
        }.get((filters or {}).get('filter.sort'), 'MAX(m.dateAdded)')
        # Sort the whole collection list before applying bounded pagination.
        ids=[row[0] for row in conn.execute(
            'SELECT s.idSet FROM sets s LEFT JOIN movie_view m ON s.idSet=m.idSet'+default_only+
            where+' GROUP BY s.idSet ORDER BY '+sort+' '+_sort_order(filters)+',s.idSet ASC LIMIT ? OFFSET ?',
            args+[max(0,int(limit)),max(0,int(offset))])]
        if not ids:return []
        results=[]
        for start in range(0,len(ids),200):
            part=ids[start:start+200];marks=','.join('?' for _ in part)
            sql='''SELECT s.idSet,s.strSet,s.strOverview,COUNT(m.idMovie),
                SUM(CASE WHEN m.playCount>0 THEN 1 ELSE 0 END),
                SUM(CASE WHEN m.resumeTimeInSeconds>0 AND m.totalTimeInSeconds>0
                    THEN CAST(m.resumeTimeInSeconds AS REAL)/m.totalTimeInSeconds ELSE 0 END),
                AVG(CASE WHEN m.rating>0 THEN m.rating ELSE NULL END),MAX(m.dateAdded),MAX(m.lastPlayed)
                FROM sets s LEFT JOIN movie_view m ON s.idSet=m.idSet'''+default_only+'''
                WHERE s.idSet IN ('''+marks+') GROUP BY s.idSet'
            for sid,title,plot,total,watched,partial,rating,dateadded,lastplayed in conn.execute(sql,part):
                results.append({'setid':sid,'title':title or '', 'plot':plot or '', 'media_type':'set',
                                'total':total or 0,'watched':watched or 0,'partial_progress':partial or 0,
                                'rating':round(rating or 0,1),'dateadded':dateadded or '', 'lastplayed':lastplayed or ''})
        load_set_art(conn, results)
        lookup={item['setid']:item for item in results}
        return [lookup[sid] for sid in ids]


def get_filtered_count(filters=None):
    from contextlib import closing
    mt=(filters or {}).get('filter.mediatype','all')
    with closing(_conn()) as conn:
        if mt=='set':
            text=(filters or {}).get('filter.t9')
            if text:
                return conn.execute('SELECT COUNT(*) FROM sets WHERE strSet LIKE ? OR strOverview LIKE ?',('%'+str(text)+'%',)*2).fetchone()[0]
            return conn.execute('SELECT COUNT(*) FROM sets').fetchone()[0]
        total=0
        for media_type in (('movie','tvshow') if mt in ('all','',None) else (mt,)):
            table='tvshow_view' if media_type=='tvshow' else 'movie_view'
            where,args=_where(media_type,filters,_build_props(media_type))
            total+=conn.execute('SELECT COUNT(*) FROM '+table+where,args).fetchone()[0]
        return total


_FACET_CACHE = {}


def get_filter_facets(filters=None):
    """Available metadata in this category, independent of active facet choices.

    Narrow SQL rows only; never hydrate artwork/cast or inspect media paths.
    Cache by scope and library revision so sorting/filter clicks stay cheap.
    """
    from contextlib import closing
    from .catalog_cache import revision
    selected = filters or {}
    scope = {'filter.mediatype': selected.get('filter.mediatype', 'all')}
    if '_collection_id' in selected:
        scope['_collection_id'] = selected['_collection_id']
    cache_key = (scope['filter.mediatype'], str(scope.get('_collection_id', '')), revision())
    if cache_key in _FACET_CACHE:
        return _FACET_CACHE[cache_key]
    facets = {name: set() for name in ('genre', 'region', 'year', 'rating')}
    mt = scope['filter.mediatype']
    if mt != 'set':
        from .regions import region_code
        with closing(_conn()) as conn:
            for media in (('movie', 'tvshow') if mt in ('all', '', None) else (mt,)):
                props = _build_props(media)
                table = 'tvshow_view' if media == 'tvshow' else 'movie_view'
                where, args = _where(media, scope, props)
                columns = [props.get(field, 'NULL') for field in ('genre', 'country', 'year', 'rating')]
                for genre, country, year, rating in conn.execute('SELECT DISTINCT '+','.join(columns)+' FROM '+table+where, args):
                    facets['genre'].update(name for value in _parse_list(genre) for name in genre_values(value))
                    facets['region'].update(region_code(value) for value in _parse_list(country))
                    try:
                        if year and 1000 <= int(year) <= 9999:
                            facets['year'].add(int(year))
                    except (ValueError, TypeError, OverflowError):
                        pass
                    try:
                        if rating is not None and 0 < float(rating) <= 10:
                            facets['rating'].add(float(rating))
                    except (ValueError, TypeError, OverflowError):
                        pass
    if len(_FACET_CACHE) >= 8:
        _FACET_CACHE.clear()
    _FACET_CACHE[cache_key] = facets
    return facets


def get_total_count():
    # Base tables avoid materialising Kodi's metadata joins for a simple count.
    from contextlib import closing
    with closing(_conn()) as conn:
        return conn.execute('SELECT (SELECT COUNT(*) FROM movie)+(SELECT COUNT(*) FROM tvshow)').fetchone()[0]
