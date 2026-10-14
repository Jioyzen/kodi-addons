"""Physical-file cleanup must preserve surviving versions and all episodes."""
import importlib.util
from pathlib import Path
import sqlite3
import unittest

SOURCE=Path(__file__).resolve().parents[1]/'addons/script.ai.movie.scanner/python'
spec=importlib.util.spec_from_file_location('scanner_cleanup_tests',SOURCE/'library_cleanup.py')
cleanup=importlib.util.module_from_spec(spec);spec.loader.exec_module(cleanup)


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.conn=sqlite3.connect(':memory:');self.addCleanup(self.conn.close)
        self.conn.executescript((Path(__file__).parent/'fixtures/kodi22_tv_schema.sql').read_text())
        self.conn.executescript('CREATE TABLE movie(idMovie INTEGER PRIMARY KEY,idFile INTEGER,c22 TEXT);'
            'CREATE TABLE videoversion(idFile INTEGER,idMedia INTEGER,media_type TEXT,itemType INTEGER);'
            "INSERT INTO path(idPath,strPath) VALUES(1,'/source/'),(2,'/other/');"
            "INSERT INTO files(idFile,idPath,strFilename,playCount) VALUES(1,1,'primary.mkv',1),(2,2,'alternate.mkv',2),(3,1,'combined.mkv',0),(4,2,'episode.mkv',0);"
            'INSERT INTO movie VALUES(10,1,"/source/primary.mkv");'
            "INSERT INTO videoversion VALUES(1,10,'movie',1),(2,10,'movie',1);"
            "INSERT INTO uniqueid(media_id,media_type,type,value) VALUES(10,'movie','tmdb','123');"
            "INSERT INTO tvshow(idShow,c00) VALUES(20,'Show');"
            "INSERT INTO seasons(idSeason,idShow,season) VALUES(21,20,1);"
            'INSERT INTO episode(idEpisode,idFile,idShow,idSeason,c12,c13) VALUES(31,3,20,21,1,1),(32,3,20,21,1,2),(33,4,20,21,1,3);'
            "INSERT INTO art(media_id,media_type,type,url) VALUES(31,'episode','thumb','one'),(32,'episode','thumb','two'),(21,'season','poster','season');"
            'INSERT INTO bookmark(idFile,timeInSeconds,type) VALUES(2,300,1),(4,100,1);')

    def test_remove_default_file_keeps_other_version_and_its_watching_state(self):
        result=cleanup.remove_files(self.conn,[1])
        self.assertEqual(result['movies'],0)
        self.assertEqual(tuple(self.conn.execute('SELECT idFile,c22 FROM movie').fetchone()),(2,'/other/alternate.mkv'))
        self.assertEqual(self.conn.execute('SELECT playCount FROM files WHERE idFile=2').fetchone()[0],2)
        self.assertEqual(self.conn.execute('SELECT timeInSeconds FROM bookmark WHERE idFile=2').fetchone()[0],300)
        self.assertEqual(self.conn.execute("SELECT value FROM uniqueid WHERE media_type='movie'").fetchone()[0],'123')

    def test_remove_alternate_file_leaves_primary_unchanged(self):
        cleanup.remove_files(self.conn,[2])
        self.assertEqual(self.conn.execute('SELECT idFile FROM movie').fetchone()[0],1)

    def test_combined_file_removes_every_episode_but_keeps_other_episode(self):
        result=cleanup.remove_files(self.conn,[3])
        self.assertEqual(result['episodes'],2)
        self.assertEqual(self.conn.execute('SELECT idEpisode FROM episode').fetchall(),[(33,)])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM tvshow').fetchone()[0],1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM art WHERE media_type='episode'").fetchone()[0],0)

    def test_last_episode_removes_show_seasons_and_season_art(self):
        cleanup.remove_files(self.conn,[3,4])
        for table in ('episode','tvshow','seasons'):
            self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM art WHERE media_type='season'").fetchone()[0],0)

    def test_source_cleanup_does_not_delete_surviving_media_in_another_source(self):
        result=cleanup.remove_files(self.conn,cleanup.file_ids_for_source(self.conn,'/source'))
        self.assertEqual(result,{'movies':0,'episodes':2,'files':2})
        self.assertEqual(self.conn.execute('SELECT idFile FROM movie').fetchone()[0],2)
        self.assertEqual(self.conn.execute('SELECT idEpisode FROM episode').fetchone()[0],33)

    def test_literal_source_names_do_not_expand_sql_wildcards(self):
        self.conn.execute("INSERT INTO path(idPath,strPath) VALUES(9,'/source_%/'),(10,'/source_ab/');")
        self.conn.execute("INSERT INTO files(idFile,idPath,strFilename) VALUES(9,9,'one.mkv'),(10,10,'two.mkv')")
        self.conn.commit()
        self.assertEqual(cleanup.file_ids_for_source(self.conn,'/source_%'),[9])

    def test_failure_rolls_back_all_files_and_metadata(self):
        self.conn.executescript("CREATE TRIGGER deny_delete BEFORE DELETE ON files WHEN OLD.idFile=3 BEGIN SELECT RAISE(ABORT,'test failure'); END;")
        before=self.conn.execute('SELECT idFile FROM movie').fetchone()[0]
        with self.assertRaises(sqlite3.IntegrityError):cleanup.remove_files(self.conn,[1,3])
        self.assertEqual(self.conn.execute('SELECT idFile FROM movie').fetchone()[0],before)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM files').fetchone()[0],4)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM episode').fetchone()[0],3)

    def test_disc_parent_matches_index_without_filesystem_probe(self):
        self.conn.executescript("INSERT INTO path(idPath,strPath) VALUES(9,'/discs/Film/BDMV/'); INSERT INTO files(idFile,idPath,strFilename) VALUES(9,9,'INDEX.BDMV');")
        self.assertEqual(cleanup.file_ids_for_path(self.conn,'/discs/Film'),[9])


if __name__=='__main__':unittest.main()
