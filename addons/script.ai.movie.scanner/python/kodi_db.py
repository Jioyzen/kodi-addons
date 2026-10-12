# -*- coding: UTF-8 -*-
"""Kodi video database writer - direct SQLite operations."""

from ai_database import current_database
import os
import sqlite3
import threading
import xbmc
import xbmcvfs

ADDONID = 'script.ai.movie.scanner'

# Default DB path for Kodi
DEFAULT_DB_PATH = current_database('MyVideos')

# SQLite serializes writers at the file level. With 12 scan workers running
# in parallel, serialize our own writes through one lock so short bursts of
# concurrent commits never hit "database is locked" timeouts.
DB_WRITE_LOCK = threading.RLock()

def _log(level, msg):
    xbmc.log(msg='%s: %s' % (ADDONID, msg), level=level)


def _art_url(url, size):
    """Replace original size with specified size for art table (poster wall)."""
    return url.replace('/original/', '/%s/' % size)


def _xml_escape(text):
    if not text:
        return ''
    return (text.replace('&', '&amp;').replace('<', '&lt;')
            .replace('>', '&gt;').replace('"', '&quot;'))




class KodiDatabase:
    def __init__(self, db_path=None):
        self.db_path = db_path or DEFAULT_DB_PATH
        self.conn = None

    def connect(self):
        try:
            self.conn = sqlite3.connect(self.db_path, timeout=30.0)
            self.conn.row_factory = sqlite3.Row
            _log(xbmc.LOGINFO, 'Connected to DB: %s' % self.db_path)
        except Exception as e:
            _log(xbmc.LOGERROR, 'DB connect error: %s' % e)
            raise

    def close(self):
        if self.conn:
            self.conn.commit()
            self.conn.close()
            self.conn = None

    def commit(self):
        if self.conn:
            self.conn.commit()

    def file_exists(self, file_path):
        """Check if a file is already in the database."""
        if not self.conn:
            return False
        try:
            cur = self.conn.cursor()
            cur.execute("""
                SELECT f.idFile FROM files f
                JOIN path p ON f.idPath = p.idPath
                WHERE p.strPath || f.strFilename = ?
            """, (file_path,))
            return cur.fetchone() is not None
        except Exception:
            return False

    def find_movie_by_uniqueid(self, id_type, id_value):
        """Find movie ID by uniqueid (e.g. tmdb, imdb). Returns movie_id or None."""
        if not self.conn:
            return None
        try:
            cur = self.conn.cursor()
            cur.execute(
                "SELECT media_id FROM uniqueid WHERE media_type='movie' AND type=? AND value=?",
                (id_type, str(id_value)))
            row = cur.fetchone()
            return row[0] if row else None
        except Exception:
            return None

    def save_movie(self, details, file_path, merge_into_movie_id=None, commit=True):
        """Write movie metadata to Kodi database. Returns movie ID or None."""
        with DB_WRITE_LOCK:
            return self._save_movie_unlocked(details, file_path,
                                             merge_into_movie_id, commit)

    def _save_movie_unlocked(self, details, file_path, merge_into_movie_id=None, commit=True):
        """Unlocked core of save_movie - call under DB_WRITE_LOCK."""
        if not self.conn:
            return None

        cur = self.conn.cursor()
        info = details.get('info', {})
        uniqueids = details.get('uniqueids', {})
        ratings = details.get('ratings', {})
        available_art = details.get('available_art', {})
        cast = details.get('cast', [])

        try:
            # Acquire SQLite's writer lock before looking up identity/versions.
            # The whole reidentification must succeed or roll back together.
            if not self.conn.in_transaction:
                cur.execute('BEGIN IMMEDIATE')
            # 1. Get or create path (BDMV folders store INDEX.BDMV as file entry)
            # URL paths are strings, never filesystem probes of SMB sources.
            if '://' not in file_path and os.path.isdir(file_path) and os.path.isdir(os.path.join(file_path, 'BDMV')):
                dir_name = file_path.rstrip('/') + '/BDMV/'
                file_name = 'INDEX.BDMV'
            else:
                dir_name = os.path.dirname(file_path) + '/'
                file_name = os.path.basename(file_path)

            cur.execute("SELECT idPath FROM path WHERE strPath = ?", (dir_name,))
            row = cur.fetchone()
            if row:
                id_path = row[0]
            else:
                cur.execute(
                    "INSERT INTO path (strPath) VALUES (?)", (dir_name,))
                id_path = cur.lastrowid

            # 2. Get or create file
            cur.execute("SELECT idFile FROM files WHERE strFilename = ? AND idPath = ?",
                        (file_name, id_path))
            row = cur.fetchone()
            if row:
                id_file = row[0]
            else:
                cur.execute("INSERT INTO files (strFilename, idPath, dateAdded) VALUES (?, ?, datetime('now'))",
                            (file_name, id_path))
                id_file = cur.lastrowid

            # 3. Resolve identities inside the same transaction. A wrongly
            # merged file must be detached without changing its other versions.
            cur.execute("SELECT idMedia FROM videoversion WHERE idFile=? AND media_type='movie'", (id_file,))
            old_rows = cur.fetchall()
            if len(old_rows) > 1:
                raise ValueError('Multiple movie associations for one file')
            old_movie = old_rows[0][0] if old_rows else None
            if old_movie is None:
                row = cur.execute('SELECT idMovie FROM movie WHERE idFile=?', (id_file,)).fetchone()
                old_movie = row[0] if row else None
            target = None
            for provider in ('tmdb', 'imdb'):
                value = uniqueids.get(provider)
                if not value:
                    continue
                rows = cur.execute("SELECT DISTINCT u.media_id FROM uniqueid u JOIN movie m ON m.idMovie=u.media_id WHERE u.media_type='movie' AND u.type=? AND u.value=?", (provider, str(value))).fetchall()
                if len(rows) > 1:
                    raise ValueError('Duplicate movie identity requires review')
                if rows:
                    if target is not None and target != rows[0][0]:
                        raise ValueError('TMDb and IMDb identities disagree')
                    target = rows[0][0]
            if merge_into_movie_id:
                if target is not None and target != merge_into_movie_id:
                    raise ValueError('Merge target does not match identity')
                if not cur.execute('SELECT 1 FROM movie WHERE idMovie=?', (merge_into_movie_id,)).fetchone():
                    raise ValueError('Missing merge target')
                merge_ids = dict(cur.execute("SELECT type,value FROM uniqueid WHERE media_type='movie' AND media_id=?", (merge_into_movie_id,)))
                if any(str(uniqueids[p]) != merge_ids[p] for p in ('tmdb','imdb') if uniqueids.get(p) and merge_ids.get(p)):
                    raise ValueError('Merge target has a different identity')
                target = merge_into_movie_id
            changed_identity = False
            if old_movie:
                old_ids = dict(cur.execute("SELECT type,value FROM uniqueid WHERE media_type='movie' AND media_id=?", (old_movie,)))
                changed_identity = any(str(uniqueids[p]) != old_ids[p]
                                       for p in ('tmdb', 'imdb') if uniqueids.get(p) and old_ids.get(p))
            if target is None and old_movie:
                siblings = cur.execute("SELECT 1 FROM videoversion WHERE idMedia=? AND media_type='movie' AND idFile<>? LIMIT 1", (old_movie, id_file)).fetchone()
                if not changed_identity or not siblings:
                    target = old_movie
            if target is None:
                cur.execute('INSERT INTO movie (idFile) VALUES (?)', (id_file,))
                target = cur.lastrowid
            id_movie = target

            # UPDATE preserves version art/streamdetails; REPLACE fires Kodi's
            # delete_videoversion trigger and silently removes those records.
            if old_rows:
                cur.execute("UPDATE videoversion SET idMedia=? WHERE idFile=? AND media_type='movie'", (id_movie, id_file))
            else:
                cur.execute("INSERT INTO videoversion (idFile,idMedia,media_type,itemType,idType) VALUES (?,?,'movie',1,40400)", (id_file, id_movie))
            if old_movie and old_movie != id_movie:
                remaining = cur.execute("SELECT v.idFile,p.strPath||f.strFilename FROM videoversion v JOIN files f ON f.idFile=v.idFile JOIN path p ON p.idPath=f.idPath WHERE v.idMedia=? AND v.media_type='movie' ORDER BY v.idFile LIMIT 1", (old_movie,)).fetchone()
                if remaining:
                    cur.execute('UPDATE movie SET idFile=?,c22=? WHERE idMovie=? AND idFile=?', (remaining[0], remaining[1], old_movie, id_file))
                else:
                    # delete_movie deletes its default version. Clear that
                    # pointer first so it cannot delete the newly moved file.
                    cur.execute('UPDATE movie SET idFile=NULL WHERE idMovie=?', (old_movie,))
                    cur.execute('DELETE FROM movie WHERE idMovie=?', (old_movie,))
            if changed_identity and old_movie == id_movie:
                cur.execute("DELETE FROM uniqueid WHERE media_type='movie' AND media_id=?", (id_movie,))

            default = cur.execute('SELECT idFile FROM movie WHERE idMovie=?', (id_movie,)).fetchone()[0]
            if default is None:
                default = id_file
                cur.execute('UPDATE movie SET idFile=? WHERE idMovie=?', (id_file, id_movie))
            default_path = cur.execute('SELECT p.strPath||f.strFilename FROM files f JOIN path p ON p.idPath=f.idPath WHERE f.idFile=?', (default,)).fetchone()

            # 4. Update movie details
            c00 = info.get('title', '')
            c01 = info.get('plot', '')
            c02 = info.get('plotoutline', '')
            c03 = info.get('tagline', '')
            c04 = ''  # votes string
            c05 = 0   # idRating (set after rating insert)
            c06 = ' / '.join(info.get('credits', []))
            c07 = info.get('year', '') or (info.get('premiered', '')[:4] if info.get('premiered') else '')
            c09 = 0   # ident_id (set after uniqueid insert)
            c10 = ''  # sorttitle
            c11 = info.get('duration', 0)
            c12 = info.get('mpaa', '')
            c13 = info.get('top250', 0)
            c14 = ' / '.join(info.get('genre', []))
            c15 = ' / '.join(info.get('director', []))
            c16 = info.get('originaltitle', '')
            c17 = ''  # unused
            c18 = ' / '.join(info.get('studio', []))
            c19 = info.get('trailer', '')
            c21 = ' / '.join(info.get('country', []))
            c22 = default_path[0] if default_path else file_path
            # Kodi 21 Omega reads premiere date from this column
            premiered = info.get('premiered', '')

            cur.execute("""
                UPDATE movie SET
                    c00=?, c01=?, c02=?, c03=?, c04=?, c05=?, c06=?, c07=?,
                    c09=?, c10=?, c11=?, c12=?, c13=?, c14=?, c15=?, c16=?,
                    c17=?, c18=?, c19=?, c21=?, c22=?, premiered=?, c08='', c20='', idSet=NULL
                WHERE idMovie=?
            """, (c00, c01, c02, c03, c04, c05, c06, c07,
                  c09, c10, c11, c12, c13, c14, c15, c16,
                  c17, c18, c19, c21, c22, premiered, id_movie))

            # 4b. Write art table (Kodi 21+ uses this for posters/fanart)
            cur.execute("DELETE FROM art WHERE media_id=? AND media_type='movie'",
                        (id_movie,))
            poster_url = fanart_url = logo_url = ''
            for p in available_art.get('poster', [])[:1]:
                if p.get('url'):
                    # w500 is plenty for wall thumbnails (~60% smaller than
                    # w780) and dramatically faster to download via proxy.
                    poster_url = _art_url(p['url'], 'w500')
                    cur.execute(
                        "INSERT INTO art (media_id, media_type, type, url) "
                        "VALUES (?, 'movie', 'poster', ?)",
                        (id_movie, poster_url))
            for fa in available_art.get('fanart', [])[:1]:
                if fa.get('url'):
                    # w1280 instead of w1920: fanart is the biggest file in
                    # the chain; halving it roughly halves proxy download time.
                    fanart_url = _art_url(fa['url'], 'w1280')
                    cur.execute(
                        "INSERT INTO art (media_id, media_type, type, url) "
                        "VALUES (?, 'movie', 'fanart', ?)",
                        (id_movie, fanart_url))
            for logo in available_art.get('clearlogo', [])[:1]:
                if logo.get('url'):
                    logo_url = _art_url(logo['url'], 'w500')
                    cur.execute(
                        "INSERT INTO art (media_id, media_type, type, url) "
                        "VALUES (?, 'movie', 'clearlogo', ?)",
                        (id_movie, logo_url))

            # 4c. Mirror to c08/c20 (Kodi 19/20 + setInfo path). Some
            # installations and addons read c08/c20 directly; not all
            # skin helpers see the art table. Keeping the same URLs there
            # is a free safety net.
            if poster_url:
                _xml_posters = ['<thumb spoof="" cache="" aspect="poster" '
                                'preview="">%s</thumb>' % _xml_escape(poster_url)]
                cur.execute(
                    "UPDATE movie SET c08=? WHERE idMovie=?",
                    (''.join(_xml_posters), id_movie))
            if fanart_url:
                _xml_fanart = '<fanart><thumb colors="" preview="">%s</thumb></fanart>' % _xml_escape(fanart_url)
                cur.execute(
                    "UPDATE movie SET c20=? WHERE idMovie=?",
                    (_xml_fanart, id_movie))

            # 5. Write unique IDs — capture default ident_id for c09
            default_uniqueid_id = 0
            for id_type, id_val in uniqueids.items():
                # Kodi's table does not uniquely constrain media/type/provider.
                # INSERT OR IGNORE therefore adds another row on every merge.
                cur.execute(
                    "SELECT uniqueid_id FROM uniqueid WHERE media_id=? "
                    "AND media_type='movie' AND type=? ORDER BY uniqueid_id LIMIT 1",
                    (id_movie, id_type))
                row = cur.fetchone()
                if row:
                    uniqueid_id = row[0]
                    cur.execute('UPDATE uniqueid SET value=? WHERE uniqueid_id=?',
                                (str(id_val), uniqueid_id))
                    cur.execute(
                        "DELETE FROM uniqueid WHERE media_id=? AND media_type='movie' "
                        "AND type=? AND uniqueid_id<>?", (id_movie, id_type, uniqueid_id))
                else:
                    cur.execute("""
                        INSERT INTO uniqueid (media_id, media_type, type, value)
                        VALUES (?, 'movie', ?, ?)
                    """, (id_movie, id_type, str(id_val)))
                    uniqueid_id = cur.lastrowid
                if id_type == 'tmdb':
                    default_uniqueid_id = uniqueid_id
            if default_uniqueid_id:
                cur.execute("UPDATE movie SET c09 = ? WHERE idMovie = ?",
                            (default_uniqueid_id, id_movie))

            # 6. Delete old ratings and write new ones
            cur.execute("DELETE FROM rating WHERE media_id=? AND media_type='movie'",
                        (id_movie,))
            for rating_type, rating_data in ratings.items():
                rating_val = rating_data.get('rating', 0)
                votes = rating_data.get('votes', 0)
                is_default = rating_data.get('default', False)
                cur.execute("""
                    INSERT INTO rating (media_id, media_type, rating_type, rating, votes)
                    VALUES (?, 'movie', ?, ?, ?)
                """, (id_movie, rating_type, rating_val, votes))

                if is_default:
                    rating_id = cur.lastrowid
                    cur.execute("UPDATE movie SET c05 = ? WHERE idMovie = ?",
                                (rating_id, id_movie))

            # 7. Delete old cast / directors / writers / genre / studio / country links
            cur.execute("DELETE FROM actor_link WHERE media_id=? AND media_type='movie'",
                        (id_movie,))
            cur.execute("DELETE FROM director_link WHERE media_id=? AND media_type='movie'",
                        (id_movie,))
            cur.execute("DELETE FROM writer_link WHERE media_id=? AND media_type='movie'",
                        (id_movie,))
            cur.execute("DELETE FROM genre_link WHERE media_id=? AND media_type='movie'",
                        (id_movie,))
            cur.execute("DELETE FROM studio_link WHERE media_id=? AND media_type='movie'",
                        (id_movie,))
            cur.execute("DELETE FROM country_link WHERE media_id=? AND media_type='movie'",
                        (id_movie,))

            # 8. Write cast
            _log(xbmc.LOGINFO, 'save_movie id=%d: cast list has %d actors' % (id_movie, len(cast)))
            for actor in cast:
                name = actor.get('name', '')
                role = actor.get('role', '')
                order = actor.get('order', 0)
                thumb = actor.get('thumbnail', '')

                id_actor = _get_or_create_actor(cur, name, thumb)

                cur.execute("""
                    INSERT OR IGNORE INTO actor_link
                    (actor_id, media_id, media_type, role, cast_order)
                    VALUES (?, ?, 'movie', ?, ?)
                """, (id_actor, id_movie, role, order))

            # 9. Write directors (to director_link table)
            for name in info.get('director', []):
                if name:
                    id_actor = _get_or_create_actor(cur, name, '')
                    cur.execute("""
                        INSERT OR IGNORE INTO director_link
                        (actor_id, media_id, media_type)
                        VALUES (?, ?, 'movie')
                    """, (id_actor, id_movie))

            # 10. Write writers (to writer_link table)
            for name in info.get('credits', []):
                if name:
                    id_actor = _get_or_create_actor(cur, name, '')
                    cur.execute("""
                        INSERT OR IGNORE INTO writer_link
                        (actor_id, media_id, media_type)
                        VALUES (?, ?, 'movie')
                    """, (id_actor, id_movie))

            # 11. Write genres (to genre_link table)
            for name in info.get('genre', []):
                if name:
                    id_genre = _get_or_create_genre(cur, name)
                    cur.execute("""
                        INSERT OR IGNORE INTO genre_link
                        (genre_id, media_id, media_type)
                        VALUES (?, ?, 'movie')
                    """, (id_genre, id_movie))

            # 12. Write studios (to studio_link table)
            for name in info.get('studio', []):
                if name:
                    id_studio = _get_or_create_studio(cur, name)
                    cur.execute("""
                        INSERT OR IGNORE INTO studio_link
                        (studio_id, media_id, media_type)
                        VALUES (?, ?, 'movie')
                    """, (id_studio, id_movie))

            # 13. Write countries (to country_link table)
            for name in info.get('country', []):
                if name:
                    id_country = _get_or_create_country(cur, name)
                    cur.execute("""
                        INSERT OR IGNORE INTO country_link
                        (country_id, media_id, media_type)
                        VALUES (?, ?, 'movie')
                    """, (id_country, id_movie))

            # 14. Movie set / collection
            set_name = info.get('set', '')
            if set_name:
                cur.execute("SELECT idSet FROM sets WHERE strSet = ?", (set_name,))
                row = cur.fetchone()
                if row:
                    id_set = row[0]
                else:
                    set_overview = info.get('setoverview', '')
                    cur.execute(
                        "INSERT INTO sets (strSet, strOverview) VALUES (?, ?)",
                        (set_name, set_overview))
                    id_set = cur.lastrowid

                cur.execute("UPDATE movie SET idSet = ? WHERE idMovie = ?",
                            (id_set, id_movie))

                # Set artwork
                for art_key, art_type, size in [('set.poster', 'poster', 'w500'), ('set.fanart', 'fanart', 'w1280')]:
                    for art_item in available_art.get(art_key, [])[:1]:
                        if art_item.get('url'):
                            cur.execute("DELETE FROM art WHERE media_id=? AND media_type='set' AND type=?",
                                        (id_set, art_type))
                            cur.execute(
                                "INSERT INTO art (media_id, media_type, type, url) VALUES (?, 'set', ?, ?)",
                                (id_set, art_type, _art_url(art_item['url'], size)))

            if commit:
                self.conn.commit()
            _log(xbmc.LOGINFO, 'Saved movie id=%d: %s (%s)' % (id_movie, c00, c07))
            return id_movie

        except Exception as e:
            _log(xbmc.LOGERROR, 'save_movie error: %s' % e)
            self.conn.rollback()
            return None


def _build_art_xml(art, title=''):
    """Build Kodi c08 thumb XML."""
    poster = art.get('poster', [])
    if not poster:
        return ''
    parts = ['<thumb spoof="" cache="" aspect="poster" preview="%s">%s</thumb>' %
             (_xml_escape(p.get('preview', '')), _xml_escape(p.get('url', '')))
             for p in poster[:5]]
    return ''.join(parts)


def _build_fanart_xml(art):
    """Build Kodi c20 fanart XML."""
    fanart = art.get('fanart', [])
    if not fanart:
        return ''
    parts = ['<thumb colors="" preview="%s">%s</thumb>' %
             (_xml_escape(f.get('preview', '')), _xml_escape(f.get('url', '')))
             for f in fanart[:5]]
    return '<fanart>%s</fanart>' % ''.join(parts)


def _get_or_create_actor(cur, name, art_urls=''):
    """Refresh both actor metadata and the artwork Kodi displays.

    actor.art_urls alone does not replace an existing actor/thumb in art;
    Kodi can otherwise keep using an old imported path after a new scrape.
    Empty director/writer entries must not erase a cast member's thumbnail.
    All writes share save_movie's transaction and DB_WRITE_LOCK.
    """
    cur.execute("SELECT actor_id, art_urls FROM actor WHERE name=?", (name,))
    row = cur.fetchone()
    if row:
        actor_id = row[0]
        if art_urls and (row[1] or '') != art_urls:
            cur.execute("UPDATE actor SET art_urls=? WHERE actor_id=?",
                        (art_urls, actor_id))
    else:
        cur.execute("INSERT INTO actor (name, art_urls) VALUES (?, ?)",
                    (name, art_urls))
        actor_id = cur.lastrowid
    if art_urls:
        cur.execute("UPDATE art SET url=? WHERE media_id=? "
                    "AND media_type='actor' AND type='thumb'",
                    (art_urls, actor_id))
        if cur.rowcount == 0:
            cur.execute("INSERT INTO art (media_id, media_type, type, url) "
                        "VALUES (?, 'actor', 'thumb', ?)",
                        (actor_id, art_urls))
    return actor_id


def _get_or_create_genre(cur, name):
    """Find or create genre, returns genre_id."""
    cur.execute("SELECT genre_id FROM genre WHERE name=?", (name,))
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute("INSERT INTO genre (name) VALUES (?)", (name,))
    return cur.lastrowid


def _get_or_create_studio(cur, name):
    """Find or create studio, returns studio_id."""
    cur.execute("SELECT studio_id FROM studio WHERE name=?", (name,))
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute("INSERT INTO studio (name) VALUES (?)", (name,))
    return cur.lastrowid


def _get_or_create_country(cur, name):
    """Find or create country, returns country_id."""
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
