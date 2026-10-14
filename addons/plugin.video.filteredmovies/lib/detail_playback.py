"""Small, bounded read-only snapshots for the first frame of detail pages.

Only bookmarks/episode identities are needed here. Artwork, cast and full
metadata remain on the existing asynchronous loader. A busy or incompatible
database falls back to that loader without guessing a playback target.
"""
from contextlib import closing
import sqlite3
import time
from urllib.parse import quote


def choose_episode(rows, season=None):
    rows = sorted((r for r in rows if r['season'] >= 0 and
                   (season is None or r['season'] == season)),
                  key=lambda r: (r['season'], r['episode'], r['episodeid']))
    if not rows:
        return None
    recent = max(rows, key=lambda r: r.get('lastplayed') or '')
    if recent.get('lastplayed'):
        if recent['resume']['position'] > 0:
            return recent
        if recent['playcount']:
            later = [r for r in rows if r['season'] == recent['season'] and
                     r['episode'] > recent['episode']]
            if later:
                return later[0]
            if season is None:
                later = [r for r in rows if r['season'] > recent['season']]
                if later:
                    return later[0]
    return next((r for r in rows if not r['playcount']), rows[0])


def read_playback(kind, media_id, season=None, database=None):
    """Return (known, target); known/None means a genuinely empty season."""
    if database is None:
        from .common import DB_PATH
        database = DB_PATH
    try:
        deadline = time.monotonic() + .03
        with closing(sqlite3.connect('file:' + quote(str(database), safe='/') + '?mode=ro',
                                     uri=True, timeout=.02)) as conn:
            conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 200)
            if kind == 'movie':
                # Use the primary file, excluding alternate versions/extras
                # exposed as additional rows by Kodi 22's movie_view.
                row = conn.execute('SELECT m.idMovie,f.playCount,m.c11,b.timeInSeconds,'
                    'b.totalTimeInSeconds FROM movie m JOIN files f ON f.idFile=m.idFile '
                    'LEFT JOIN bookmark b ON b.idFile=m.idFile AND b.type=1 '
                    'WHERE m.idMovie=?', (int(media_id),)).fetchone()
                if row is None:
                    return False, None
                return True, {'movieid': row[0], 'playcount': row[1] or 0,
                    'runtime': float(row[2] or 0),
                    'resume': {'position': row[3] or 0, 'total': row[4] or 0}}
            params = [int(media_id)]
            where = 'e.idShow=?'
            if season is not None:
                where += ' AND CAST(e.c12 AS INTEGER)=?'
                params.append(int(season))
            records = conn.execute('SELECT e.idEpisode,CAST(e.c12 AS INTEGER),CAST(e.c13 AS INTEGER),'
                'f.playCount,f.lastPlayed,b.timeInSeconds,b.totalTimeInSeconds '
                'FROM episode e JOIN files f ON f.idFile=e.idFile '
                'LEFT JOIN bookmark b ON b.idFile=e.idFile AND b.type=1 '
                'WHERE ' + where + ' LIMIT 10001', params).fetchall()
            if len(records) > 10000 or time.monotonic() > deadline:
                return False, None
            rows = [{'episodeid': r[0], 'season': r[1] or 0, 'episode': r[2] or 0,
                     'playcount': r[3] or 0, 'lastplayed': r[4] or '',
                     'resume': {'position': r[5] or 0, 'total': r[6] or 0}} for r in records]
            return True, choose_episode(rows, season)
    except (sqlite3.Error, ValueError, TypeError, OverflowError, OSError):
        return False, None
