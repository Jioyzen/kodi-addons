# -*- coding: UTF-8 -*-
"""Kodi video database writer for TV shows - direct SQLite operations."""

from ai_database import current_database
import os
import re
import sqlite3
import threading
import xbmc
import xbmcvfs

ADDONID = 'script.ai.movie.scanner'

DEFAULT_DB_PATH = current_database('MyVideos')

# Same write-serialization lock as kodi_db.py: SQLite locks the whole file
# per write transaction, so serialize our own writers to avoid lock timeouts
# when 12 scan workers commit concurrently.
DB_WRITE_LOCK = threading.RLock()

_CANDIDATE_TV_SCRAPERS = [
    'metadata.tvshows.tmdb.cn.optimization',
    'metadata.tvshows.themoviedb.org.python',
    'metadata.tv.themoviedb.org',
    'metadata.tv.mtime.com.python',
    'metadata.tv.douban.com',
]

_SEASON_DIR_RE = re.compile(
    r'(?:Season|S(?:eason)?)\s*(\d+)', re.IGNORECASE)


def _log(level, msg):
    xbmc.log(msg='%s: %s' % (ADDONID, msg), level=level)


def _art_url(url, size):
    """Replace original size with specified size for art table (poster wall)."""
    return url.replace('/original/', '/%s/' % size)


def _detect_tv_scraper():
    addon_dir = xbmcvfs.translatePath('special://home/addons')
    for sid in _CANDIDATE_TV_SCRAPERS:
        if os.path.isdir(os.path.join(addon_dir, sid)):
            return sid
    return _CANDIDATE_TV_SCRAPERS[0]


def _get_or_create_actor(cur, name, art_urls=''):
    # Refresh art_urls for existing actors too (same fix as kodi_db):
    # without it, actors created earlier with an empty/old art_urls
    # never get a visible thumbnail (image.tmdb.org is blocked without
    # a VPN, so Kodi's on-demand download always fails).
    cur.execute("SELECT actor_id, art_urls FROM actor WHERE name=?", (name,))
    row = cur.fetchone()
    if row:
        if art_urls and (row[1] or '') != art_urls:
            cur.execute("UPDATE actor SET art_urls=? WHERE actor_id=?",
                        (art_urls, row[0]))
        return row[0]
    cur.execute("INSERT INTO actor (name, art_urls) VALUES (?, ?)", (name, art_urls))
    return cur.lastrowid


def _get_or_create_genre(cur, name):
    cur.execute("SELECT genre_id FROM genre WHERE name=?", (name,))
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute("INSERT INTO genre (name) VALUES (?)", (name,))
    return cur.lastrowid


def _get_or_create_studio(cur, name):
    cur.execute("SELECT studio_id FROM studio WHERE name=?", (name,))
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute("INSERT INTO studio (name) VALUES (?)", (name,))
    return cur.lastrowid


def _get_or_create_country(cur, name):
    cur.execute("SELECT country_id FROM country WHERE name=?", (name,))
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute("INSERT INTO country (name) VALUES (?)", (name,))
    return cur.lastrowid


def _xml_escape(text):
    if not text:
        return ''
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')


def _resolve_show_root(file_path):
    """Given an episode file path, derive the TV show root directory.

    If the parent directory looks like a season folder (e.g. 'Season 1',
    'S01'), the show root is one level up. Otherwise, the file's own
    directory is treated as the show root.
    """
    dirpath = os.path.dirname(file_path)
    if not dirpath.endswith('/') and not dirpath.endswith('\\'):
        dirpath += '/'
    dirname = os.path.basename(os.path.dirname(file_path))

    if _SEASON_DIR_RE.search(dirname):
        show_root = os.path.dirname(os.path.dirname(file_path))
        if not show_root.endswith('/') and not show_root.endswith('\\'):
            show_root += '/'
    else:
        show_root = dirpath

    return show_root, dirpath


class TvShowDatabase:
    def __init__(self, db_path=None):
        self.db_path = db_path or DEFAULT_DB_PATH
        self.conn = None

    def connect(self):
        try:
            self.conn = sqlite3.connect(self.db_path, timeout=30.0)
            self.conn.row_factory = sqlite3.Row
            _log(xbmc.LOGINFO, 'Connected to DB: %s' % self.db_path)
        except Exception as e:
            _log(xbmc.LOGERROR, 'TV DB connect error: %s' % e)
            raise

    def close(self):
        if self.conn:
            self.conn.commit()
            self.conn.close()
            self.conn = None

    def commit(self):
        if self.conn:
            self.conn.commit()

    def find_tvshow_by_uniqueid(self, id_type, id_value):
        if not self.conn:
            return None
        try:
            cur = self.conn.cursor()
            cur.execute(
                "SELECT media_id FROM uniqueid WHERE media_type='tvshow' AND type=? AND value=?",
                (id_type, str(id_value)))
            row = cur.fetchone()
            return row[0] if row else None
        except Exception:
            return None

    def save_tvshow(self, tv_details, ep_details, season_number,
                    episode_number, file_path):
        """Write TV show + episode metadata to Kodi database.

        Returns (show_id, episode_id) tuple or (None, None) on failure.
        """
        with DB_WRITE_LOCK:
            return self._save_tvshow_unlocked(
                tv_details, ep_details, season_number, episode_number,
                file_path)

    def _save_tvshow_unlocked(self, tv_details, ep_details, season_number,
                              episode_number, file_path):
        """Unlocked core of save_tvshow - call under DB_WRITE_LOCK."""
        if not self.conn:
            return None, None

        cur = self.conn.cursor()
        tv_info = tv_details.get('info', {})
        tv_uniqueids = tv_details.get('uniqueids', {})
        tv_ratings = tv_details.get('ratings', {})
        tv_art = tv_details.get('available_art', {})
        tv_cast = tv_details.get('cast', [])

        ep_info = ep_details.get('info', {}) if ep_details else {}
        ep_uniqueids = ep_details.get('uniqueids', {}) if ep_details else {}
        ep_ratings = ep_details.get('ratings', {}) if ep_details else {}
        ep_art = ep_details.get('available_art', {}) if ep_details else {}
        ep_cast = ep_details.get('cast', []) if ep_details else {}

        scraper_id = _detect_tv_scraper()

        show_root, ep_dir = _resolve_show_root(file_path)
        file_name = os.path.basename(file_path)

        try:
            # 1. Get or create season directory path (where episode file lives)
            cur.execute("SELECT idPath FROM path WHERE strPath=?", (ep_dir,))
            row = cur.fetchone()
            if row:
                id_path_ep = row[0]
            else:
                cur.execute(
                    "INSERT INTO path (strPath, strContent, strScraper) VALUES (?, '', '')",
                    (ep_dir,))
                id_path_ep = cur.lastrowid

            # 2. Get or create show root path (set content + scraper)
            cur.execute("SELECT idPath, strContent, strScraper FROM path WHERE strPath=?",
                        (show_root,))
            row = cur.fetchone()
            if row:
                id_path_show = row[0]
                if not row[1] or not row[2]:
                    cur.execute(
                        "UPDATE path SET strContent='tvshows', strScraper=? "
                        "WHERE idPath=?", (scraper_id, id_path_show))
            else:
                cur.execute(
                    "INSERT INTO path (strPath, strContent, strScraper) "
                    "VALUES (?, 'tvshows', ?)", (show_root, scraper_id))
                id_path_show = cur.lastrowid

            # 3. Get or create file entry for the episode
            cur.execute("SELECT idFile FROM files WHERE strFilename=? AND idPath=?",
                        (file_name, id_path_ep))
            row = cur.fetchone()
            if row:
                id_file = row[0]
                cur.execute("UPDATE files SET dateAdded=datetime('now') WHERE idFile=?",
                            (id_file,))
            else:
                cur.execute(
                    "INSERT INTO files (strFilename, idPath, dateAdded) VALUES (?, ?, datetime('now'))",
                    (file_name, id_path_ep))
                id_file = cur.lastrowid

            # 4. Find or create TV show by tmdb uniqueid
            tv_show_id = None
            tmdb_id_val = tv_uniqueids.get('tmdb', '')
            if tmdb_id_val:
                tv_show_id = self.find_tvshow_by_uniqueid('tmdb', str(tmdb_id_val))

            is_new_show = tv_show_id is None
            if is_new_show:
                cur.execute("INSERT INTO tvshow (c05) VALUES ('')")
                tv_show_id = cur.lastrowid
                _log(xbmc.LOGINFO, 'Created new tvshow id=%d' % tv_show_id)

            # 5. tvshowlinkpath (link show to show root path)
            cur.execute(
                "INSERT OR IGNORE INTO tvshowlinkpath (idShow, idPath) VALUES (?, ?)",
                (tv_show_id, id_path_show))

            # 6. Find or create season entry
            cur.execute(
                "SELECT idSeason FROM seasons WHERE idShow=? AND season=?",
                (tv_show_id, season_number))
            row = cur.fetchone()
            if row:
                id_season = row[0]
            else:
                season_name = 'Season %d' % season_number if season_number > 0 else ('Specials' if season_number == 0 else '')
                cur.execute(
                    "INSERT INTO seasons (idShow, season, name) VALUES (?, ?, ?)",
                    (tv_show_id, season_number, season_name))
                id_season = cur.lastrowid

            # 7. Find or create episode
            cur.execute(
                "SELECT idEpisode FROM episode WHERE idShow=? AND c12=? AND c13=?",
                (tv_show_id, str(season_number), str(episode_number)))
            row = cur.fetchone()
            if row:
                id_episode = row[0]
            else:
                cur.execute(
                    "INSERT INTO episode (idFile, idShow, idSeason, c12, c13) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (id_file, tv_show_id, id_season,
                     str(season_number), str(episode_number)))
                id_episode = cur.lastrowid
            _log(xbmc.LOGINFO, 'Episode id=%d S%02dE%02d' %
                 (id_episode, season_number, episode_number))

            # 8. Update episode metadata
            ep_c00 = ep_info.get('title', '')
            ep_c01 = ep_info.get('plot', '')
            ep_c03 = 0
            ep_c04 = ' / '.join(ep_info.get('credits', []))
            ep_c05 = ep_info.get('aired', '')
            ep_c09 = ep_info.get('duration', 0) or 0
            ep_c10 = ' / '.join(ep_info.get('director', []))
            ep_c11 = ep_info.get('productioncode', '')
            ep_c14 = ''
            ep_c18 = ep_dir
            ep_c19 = str(id_path_ep)
            ep_c20 = 0

            cur.execute("""
                UPDATE episode SET
                    c00=?, c01=?, c03=?, c04=?, c05=?,
                    c09=?, c10=?, c11=?, c14=?,
                    c18=?, c19=?, c20=?, idFile=?
                WHERE idEpisode=?
            """, (ep_c00, ep_c01, ep_c03, ep_c04, ep_c05,
                  ep_c09, ep_c10, ep_c11, ep_c14,
                  ep_c18, ep_c19, ep_c20, id_file, id_episode))

            # 9. Episode still image as art
            cur.execute("DELETE FROM art WHERE media_id=? AND media_type='episode'",
                        (id_episode,))
            for p in ep_art.get('poster', [])[:1]:
                if p.get('url'):
                    cur.execute(
                        "INSERT INTO art (media_id, media_type, type, url) "
                        "VALUES (?, 'episode', 'thumb', ?)",
                        (id_episode, _art_url(p['url'], 'w500')))

            # 10. Episode unique IDs
            for id_type, id_val in ep_uniqueids.items():
                cur.execute("""
                    INSERT OR IGNORE INTO uniqueid (media_id, media_type, type, value)
                    VALUES (?, 'episode', ?, ?)
                """, (id_episode, id_type, str(id_val)))
                if id_type == 'tmdb':
                    ep_ident_id = cur.lastrowid
                    if ep_ident_id:
                        cur.execute("UPDATE episode SET c20=? WHERE idEpisode=?",
                                    (ep_ident_id, id_episode))

            # 11. Episode director links
            cur.execute("DELETE FROM director_link WHERE media_id=? AND media_type='episode'",
                        (id_episode,))
            for name in ep_info.get('director', []):
                if name:
                    id_actor = _get_or_create_actor(cur, name, '')
                    cur.execute("""
                        INSERT OR IGNORE INTO director_link
                        (actor_id, media_id, media_type)
                        VALUES (?, ?, 'episode')
                    """, (id_actor, id_episode))

            # 12. Episode writer links
            cur.execute("DELETE FROM writer_link WHERE media_id=? AND media_type='episode'",
                        (id_episode,))
            for name in ep_info.get('credits', []):
                if name:
                    id_actor = _get_or_create_actor(cur, name, '')
                    cur.execute("""
                        INSERT OR IGNORE INTO writer_link
                        (actor_id, media_id, media_type)
                        VALUES (?, ?, 'episode')
                    """, (id_actor, id_episode))

            # 13. Episode guest star cast links
            cur.execute("DELETE FROM actor_link WHERE media_id=? AND media_type='episode'",
                        (id_episode,))
            for actor in ep_cast:
                name = actor.get('name', '')
                if name:
                    id_actor = _get_or_create_actor(cur, name, actor.get('thumbnail', ''))
                    cur.execute("""
                        INSERT OR IGNORE INTO actor_link
                        (actor_id, media_id, media_type, role, cast_order)
                        VALUES (?, ?, 'episode', ?, ?)
                    """, (id_actor, id_episode, actor.get('role', ''), actor.get('order', 0)))

            # ========================================
            # TV Show level metadata
            # ========================================

            # 14. Update tvshow columns
            tv_c00 = tv_info.get('title', '')
            tv_c01 = tv_info.get('plot', '')
            tv_c02 = tv_info.get('status', '')
            tv_c03 = ''
            tv_c04 = 0
            tv_c05 = tv_info.get('premiered', '')
            tv_c06 = ''
            tv_c07 = ''
            tv_c08 = ' / '.join(tv_info.get('genre', []))
            tv_c09 = tv_info.get('originaltitle', '')
            tv_c10 = ''
            tv_c11 = ''
            tv_c12 = 0
            tv_c13 = tv_info.get('mpaa', '')
            tv_c14 = ' / '.join(tv_info.get('studio', []))
            tv_c15 = ''
            tv_c16 = ''
            tv_c17 = ''
            tv_c18 = ' / '.join(tv_info.get('country', []))
            tv_c19 = tv_info.get('year', '') or (tv_info.get('premiered', '')[:4] if tv_info.get('premiered') else '')
            tv_c20 = ''
            tv_c21 = ''
            tv_c22 = ''
            tv_c23 = ''
            tv_duration = tv_info.get('duration', 0) or 0

            cur.execute("""
                UPDATE tvshow SET
                    c00=?, c01=?, c02=?, c03=?, c04=?, c05=?, c06=?, c07=?,
                    c08=?, c09=?, c10=?, c11=?, c12=?, c13=?, c14=?, c15=?,
                    c16=?, c17=?, c18=?, c19=?, c20=?, c21=?, c22=?, c23=?,
                    duration=?
                WHERE idShow=?
            """, (tv_c00, tv_c01, tv_c02, tv_c03, tv_c04, tv_c05, tv_c06, tv_c07,
                  tv_c08, tv_c09, tv_c10, tv_c11, tv_c12, tv_c13, tv_c14, tv_c15,
                  tv_c16, tv_c17, tv_c18, tv_c19, tv_c20, tv_c21, tv_c22, tv_c23,
                  tv_duration, tv_show_id))

            # 15. TV show unique IDs
            for id_type, id_val in tv_uniqueids.items():
                cur.execute("""
                    INSERT OR IGNORE INTO uniqueid (media_id, media_type, type, value)
                    VALUES (?, 'tvshow', ?, ?)
                """, (tv_show_id, id_type, str(id_val)))
                if id_type == 'tmdb':
                    ident_id = cur.lastrowid
                    if ident_id:
                        cur.execute("UPDATE tvshow SET c12=? WHERE idShow=?",
                                    (ident_id, tv_show_id))

            # 16. TV show ratings
            cur.execute("DELETE FROM rating WHERE media_id=? AND media_type='tvshow'",
                        (tv_show_id,))
            for rating_type, rating_data in tv_ratings.items():
                rating_val = rating_data.get('rating', 0)
                votes = rating_data.get('votes', 0)
                is_default = rating_data.get('default', False)
                cur.execute("""
                    INSERT INTO rating (media_id, media_type, rating_type, rating, votes)
                    VALUES (?, 'tvshow', ?, ?, ?)
                """, (tv_show_id, rating_type, rating_val, votes))
                if is_default:
                    cur.execute("UPDATE tvshow SET c04=? WHERE idShow=?",
                                (cur.lastrowid, tv_show_id))

            # 17. TV show art table
            cur.execute("DELETE FROM art WHERE media_id=? AND media_type='tvshow'",
                        (tv_show_id,))
            for p in tv_art.get('poster', [])[:1]:
                if p.get('url'):
                    cur.execute(
                        "INSERT INTO art (media_id, media_type, type, url) "
                        "VALUES (?, 'tvshow', 'poster', ?)",
                        (tv_show_id, _art_url(p['url'], 'w780')))
            for fa in tv_art.get('fanart', [])[:1]:
                if fa.get('url'):
                    cur.execute(
                        "INSERT INTO art (media_id, media_type, type, url) "
                        "VALUES (?, 'tvshow', 'fanart', ?)",
                        (tv_show_id, _art_url(fa['url'], 'w1920')))
            for logo in tv_art.get('clearlogo', [])[:1]:
                if logo.get('url'):
                    cur.execute(
                        "INSERT INTO art (media_id, media_type, type, url) "
                        "VALUES (?, 'tvshow', 'clearlogo', ?)",
                        (tv_show_id, _art_url(logo['url'], 'w500')))

            # 18. TV show link tables
            for tbl, mediatype in [('actor_link', 'tvshow'), ('director_link', 'tvshow'),
                                    ('writer_link', 'tvshow'), ('genre_link', 'tvshow'),
                                    ('studio_link', 'tvshow'), ('country_link', 'tvshow')]:
                cur.execute("DELETE FROM %s WHERE media_id=? AND media_type='%s'" % (tbl, mediatype),
                            (tv_show_id,))

            for actor in tv_cast:
                name = actor.get('name', '')
                if name:
                    id_actor = _get_or_create_actor(cur, name, actor.get('thumbnail', ''))
                    cur.execute("""
                        INSERT OR IGNORE INTO actor_link
                        (actor_id, media_id, media_type, role, cast_order)
                        VALUES (?, ?, 'tvshow', ?, ?)
                    """, (id_actor, tv_show_id, actor.get('role', ''), actor.get('order', 0)))

            for name in tv_info.get('director', []):
                if name:
                    id_actor = _get_or_create_actor(cur, name, '')
                    cur.execute("""
                        INSERT OR IGNORE INTO director_link
                        (actor_id, media_id, media_type)
                        VALUES (?, ?, 'tvshow')
                    """, (id_actor, tv_show_id))

            for name in tv_info.get('credits', []):
                if name:
                    id_actor = _get_or_create_actor(cur, name, '')
                    cur.execute("""
                        INSERT OR IGNORE INTO writer_link
                        (actor_id, media_id, media_type)
                        VALUES (?, ?, 'tvshow')
                    """, (id_actor, tv_show_id))

            for name in tv_info.get('genre', []):
                if name:
                    id_genre = _get_or_create_genre(cur, name)
                    cur.execute("""
                        INSERT OR IGNORE INTO genre_link
                        (genre_id, media_id, media_type)
                        VALUES (?, ?, 'tvshow')
                    """, (id_genre, tv_show_id))

            for name in tv_info.get('studio', []):
                if name:
                    id_studio = _get_or_create_studio(cur, name)
                    cur.execute("""
                        INSERT OR IGNORE INTO studio_link
                        (studio_id, media_id, media_type)
                        VALUES (?, ?, 'tvshow')
                    """, (id_studio, tv_show_id))

            for name in tv_info.get('country', []):
                if name:
                    id_country = _get_or_create_country(cur, name)
                    cur.execute("""
                        INSERT OR IGNORE INTO country_link
                        (country_id, media_id, media_type)
                        VALUES (?, ?, 'tvshow')
                    """, (id_country, tv_show_id))

            self.conn.commit()
            _log(xbmc.LOGINFO, 'Saved tvshow id=%d episode id=%d: %s S%02dE%02d' %
                 (tv_show_id, id_episode, tv_c00, season_number, episode_number))
            return tv_show_id, id_episode

        except Exception as e:
            _log(xbmc.LOGERROR, 'save_tvshow error: %s' % e)
            self.conn.rollback()
            return None, None


def _build_tv_thumb_xml(art):
    """Build tvshow c06 thumb XML (poster)."""
    poster = art.get('poster', [])
    if not poster:
        return ''
    parts = ['<thumb spoof="" cache="" aspect="poster" preview="%s">%s</thumb>' %
             (_xml_escape(p.get('preview', '')), _xml_escape(p.get('url', '')))
             for p in poster[:5]]
    return ''.join(parts)


def _build_tv_fanart_xml(art):
    """Build tvshow c11 fanart XML."""
    fanart = art.get('fanart', [])
    if not fanart:
        return ''
    parts = ['<thumb colors="" preview="%s">%s</thumb>' %
             (_xml_escape(f.get('preview', '')), _xml_escape(f.get('url', '')))
             for f in fanart[:5]]
    return '<fanart>%s</fanart>' % ''.join(parts)
