"""Transactional removal by physical file, preserving other movie versions."""
import os

LINKS=('actor_link','director_link','writer_link','genre_link','studio_link',
       'country_link','tag_link','rating','uniqueid','art')


def _metadata(conn,tables,media,identity):
    for table in LINKS:
        if table in tables:
            conn.execute('DELETE FROM '+table+' WHERE media_type=? AND media_id=?',(media,identity))


def remove_files(conn,file_ids):
    """Own one transaction; never commit partial file removals on failure."""
    tables={row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    removed={'movies':0,'episodes':0,'files':0}
    shows=set();paths=set()
    try:
        conn.execute('BEGIN IMMEDIATE')
        for file_id in dict.fromkeys(file_ids):
            row=conn.execute('SELECT idPath FROM files WHERE idFile=?',(file_id,)).fetchone()
            if not row:continue
            paths.add(row[0])
            movies={r[0] for r in conn.execute('SELECT idMovie FROM movie WHERE idFile=?',(file_id,))}
            if 'videoversion' in tables:
                movies.update(r[0] for r in conn.execute("SELECT idMedia FROM videoversion WHERE idFile=? AND media_type='movie'",(file_id,)))
                conn.execute('DELETE FROM videoversion WHERE idFile=?',(file_id,))
            for movie in movies:
                remaining=None
                if 'videoversion' in tables:
                    remaining=conn.execute("SELECT v.idFile,p.strPath||f.strFilename FROM videoversion v "
                        "JOIN files f ON f.idFile=v.idFile JOIN path p ON p.idPath=f.idPath "
                        "WHERE v.idMedia=? AND v.media_type='movie' AND v.itemType=1 "
                        "ORDER BY v.idFile LIMIT 1",(movie,)).fetchone()
                if remaining:
                    conn.execute('UPDATE movie SET idFile=?,c22=? WHERE idMovie=? AND idFile=?',
                        (remaining[0],remaining[1],movie,file_id))
                else:
                    _metadata(conn,tables,'movie',movie)
                    if 'videoversion' in tables:
                        conn.execute("DELETE FROM videoversion WHERE idMedia=? AND media_type='movie'",(movie,))
                    # Kodi's delete_movie trigger must not remove a reassigned
                    # or shared physical file while we still inspect it.
                    conn.execute('UPDATE movie SET idFile=NULL WHERE idMovie=?',(movie,))
                    conn.execute('DELETE FROM movie WHERE idMovie=?',(movie,))
                    removed['movies']+=1
            episodes=conn.execute('SELECT idEpisode,idShow FROM episode WHERE idFile=?',(file_id,)).fetchall()
            for episode,show in episodes:
                _metadata(conn,tables,'episode',episode)
                conn.execute('DELETE FROM episode WHERE idEpisode=?',(episode,))
                shows.add(show);removed['episodes']+=1
            for table in ('bookmark','settings','streamdetails'):
                if table in tables:conn.execute('DELETE FROM '+table+' WHERE idFile=?',(file_id,))
            conn.execute('DELETE FROM files WHERE idFile=?',(file_id,))
            removed['files']+=1
        for show in shows:
            if conn.execute('SELECT 1 FROM episode WHERE idShow=? LIMIT 1',(show,)).fetchone():continue
            for (season,) in conn.execute('SELECT idSeason FROM seasons WHERE idShow=?',(show,)).fetchall():
                _metadata(conn,tables,'season',season)
            _metadata(conn,tables,'tvshow',show)
            conn.execute('DELETE FROM tvshowlinkpath WHERE idShow=?',(show,))
            conn.execute('DELETE FROM seasons WHERE idShow=?',(show,))
            conn.execute('DELETE FROM tvshow WHERE idShow=?',(show,))
        for path in paths:
            if not conn.execute('SELECT 1 FROM files WHERE idPath=? LIMIT 1',(path,)).fetchone():
                if not conn.execute('SELECT 1 FROM tvshowlinkpath WHERE idPath=? LIMIT 1',(path,)).fetchone():
                    conn.execute('DELETE FROM path WHERE idPath=?',(path,))
        conn.commit()
        return removed
    except Exception:
        conn.rollback()
        raise


def file_ids_for_path(conn,filepath):
    directory=os.path.dirname(filepath)+'/'
    filename=os.path.basename(filepath)
    rows=conn.execute('SELECT f.idFile FROM files f JOIN path p ON p.idPath=f.idPath '
        'WHERE p.strPath=? AND f.strFilename=?',(directory,filename)).fetchall()
    if not rows:
        # Scanner represents a Blu-ray disc by its parent directory string.
        rows=conn.execute('SELECT f.idFile FROM files f JOIN path p ON p.idPath=f.idPath '
            "WHERE p.strPath=? AND LOWER(f.strFilename)='index.bdmv'",
            (filepath.rstrip('/')+'/BDMV/',)).fetchall()
    return [row[0] for row in rows]


def file_ids_for_source(conn,directory):
    prefix=directory.rstrip('/')+'/'
    return [row[0] for row in conn.execute('SELECT f.idFile FROM files f JOIN path p ON p.idPath=f.idPath '
        'WHERE p.strPath=? OR substr(p.strPath,1,?)=?',(prefix,len(prefix),prefix))]
