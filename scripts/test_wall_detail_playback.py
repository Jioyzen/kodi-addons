"""First-frame playback targets must match resume/next-episode semantics."""
import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest

LIB=Path(__file__).resolve().parents[1]/'addons/plugin.video.filteredmovies/lib'
spec=importlib.util.spec_from_file_location('detail_playback_test',LIB/'detail_playback.py')
playback=importlib.util.module_from_spec(spec)
spec.loader.exec_module(playback)


class PlaybackTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path=Path(self.folder.name)/'library.db'
        with sqlite3.connect(self.path) as conn:
            conn.executescript('CREATE TABLE movie(idMovie,idFile,c11);'
                'CREATE TABLE episode(idEpisode,idFile,idShow,c12,c13);'
                'CREATE TABLE files(idFile,playCount,lastPlayed);'
                'CREATE TABLE bookmark(idFile,type,timeInSeconds,totalTimeInSeconds);'
                'INSERT INTO movie VALUES(42,42,7200);'
                'INSERT INTO episode VALUES(1,1,5,1,1),(2,2,5,1,2),(3,3,5,1,3),(4,4,5,2,1);'
                "INSERT INTO files VALUES(1,0,NULL),(2,1,'2026-10-14'),(3,0,NULL),(4,0,NULL),(42,0,NULL);"
                'INSERT INTO bookmark VALUES(42,1,600,7500);')

    def read(self,kind,media_id,season=None):
        return playback.read_playback(kind,media_id,season,database=self.path)

    def test_movie_snapshot_keeps_bookmark_total_and_is_read_only(self):
        before=self.path.read_bytes()
        known,row=self.read('movie',42)
        self.assertTrue(known)
        self.assertEqual(row['resume'],{'position':600,'total':7500})
        self.assertEqual(before,self.path.read_bytes())

    def test_finished_recent_episode_selects_next_not_earlier_unwatched_gap(self):
        self.assertEqual(self.read('tvshow',5)[1]['episodeid'],3)
        self.assertEqual(self.read('tvshow',5,2)[1]['episodeid'],4)

    def test_resume_and_season_boundary(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE files SET lastPlayed='2026-10-15' WHERE idFile=3")
            conn.execute('INSERT INTO bookmark VALUES(3,1,300,3600)')
        self.assertEqual(self.read('tvshow',5)[1]['episodeid'],3)
        with sqlite3.connect(self.path) as conn:
            conn.execute('UPDATE bookmark SET timeInSeconds=0 WHERE idFile=3')
            conn.execute('UPDATE files SET playCount=1 WHERE idFile=3')
        self.assertEqual(self.read('tvshow',5)[1]['episodeid'],4)
        # Within a season, replay its first unplayed episode when it is finished.
        self.assertEqual(self.read('tvshow',5,1)[1]['episodeid'],1)

    def test_empty_season_is_distinct_from_missing_schema(self):
        self.assertEqual(self.read('tvshow',5,9),(True,None))
        self.assertEqual(self.read('movie',999),(False,None))
        with sqlite3.connect(self.path) as conn:
            conn.execute('DROP TABLE episode')
        self.assertEqual(self.read('tvshow',5),(False,None))

    def test_locked_database_falls_back_without_waiting_for_loader(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute('BEGIN EXCLUSIVE')
            self.assertEqual(self.read('movie',42),(False,None))

    def test_standard_and_episode_bookmarks_cannot_override_resume(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute('INSERT INTO bookmark VALUES(42,0,2000,7500)')
            conn.execute('INSERT INTO bookmark VALUES(1,2,1000,3600)')
        self.assertEqual(self.read('movie',42)[1]['resume']['position'],600)
        self.assertEqual(self.read('tvshow',5)[1]['episodeid'],3)


if __name__=='__main__':unittest.main()
