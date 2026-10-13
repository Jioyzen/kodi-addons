import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'addons/script.ai.movie.scanner/python'))
import library_reset as reset
from task_state import task_lock


class Fixture:
    def __init__(self, directory):
        self.profile = Path(directory) / 'profile';self.profile.mkdir()
        self.thumbs = Path(directory) / 'thumbs';self.thumbs.mkdir()
        self.database = Path(directory) / 'MyVideos.db'
        with sqlite3.connect(self.database) as db:
            db.executescript('CREATE TABLE art(url TEXT); CREATE TABLE actor(art_urls TEXT);')
            db.execute('INSERT INTO art VALUES (?)', ('https://tmdb.test/poster.jpg',))
            db.execute('INSERT INTO actor VALUES (?)', ('<thumb>https://tmdb.test/actor.jpg</thumb>',))
        (self.profile / 'settings.xml').write_text('KEEP API SETTINGS')
        (self.profile / 'scan_history.json').write_text('KEEP UNTIL SUCCESS')
        self.media = {'movies': [{'movieid': 7}], 'tvshows': [{'tvshowid': 8}], 'musicvideos': []}
        self.textures = {1: {'textureid': 1, 'url': 'image://https%3A%2F%2Ftmdb.test%2Fposter.jpg/', 'cachedurl': 'poster.jpg'},
                         2: {'textureid': 2, 'url': 'https://tmdb.test/actor.jpg', 'cachedurl': 'actor.jpg'},
                         3: {'textureid': 3, 'url': 'special://home/addons/icon.png', 'cachedurl': 'icon.png'}}
        for t in self.textures.values(): (self.thumbs / t['cachedurl']).write_bytes(b'cache')
        self.calls = [];self.fail = None

    def rpc(self, method, params):
        self.calls.append(method)
        if method == self.fail: raise RuntimeError('native API failure')
        if method == 'Textures.GetTextures':return {'textures': list(self.textures.values())}
        if method == 'Textures.RemoveTexture':self.textures.pop(params['textureid']);return 'OK'
        for getter, key, remover, idkey in reset.COLLECTIONS:
            if method == 'VideoLibrary.' + getter:return {key: self.media[key]}
            if method == 'VideoLibrary.' + remover:
                self.media[key] = [i for i in self.media[key] if i[idkey] != params[idkey]]
                return 'OK'
        raise AssertionError(method)

    def prepare(self):return reset.prepare(str(self.profile), str(self.database), self.rpc)
    def execute(self, plan):return reset.execute(str(self.profile), str(self.thumbs), plan, self.rpc)


class ResetTests(unittest.TestCase):
    def test_removes_media_and_actor_images_but_preserves_settings_and_icons(self):
        with tempfile.TemporaryDirectory() as d:
            f=Fixture(d);plan=f.prepare();report=f.execute(plan)
            self.assertEqual(report, {'media_removed': 2, 'textures_removed': 2})
            self.assertEqual(set(f.textures), {3})
            self.assertEqual(sorted(p.name for p in f.thumbs.iterdir()), ['icon.png'])
            self.assertTrue((f.profile/'settings.xml').exists())
            self.assertFalse((f.profile/'scan_history.json').exists())
            self.assertFalse((f.profile/reset.PENDING).exists())

    def test_failure_preserves_history_and_retry_completes(self):
        with tempfile.TemporaryDirectory() as d:
            f=Fixture(d);plan=f.prepare();f.fail='VideoLibrary.RemoveTVShow'
            with self.assertRaises(RuntimeError):f.execute(plan)
            self.assertTrue((f.profile/'scan_history.json').exists())
            self.assertTrue((f.profile/reset.PENDING).exists())
            f.fail=None;f.execute(f.prepare());self.assertFalse((f.profile/reset.PENDING).exists())

    def test_texture_failure_resume_retains_deleted_library_artwork_references(self):
        with tempfile.TemporaryDirectory() as d:
            f=Fixture(d);plan=f.prepare();f.fail='Textures.RemoveTexture'
            with self.assertRaises(RuntimeError):f.execute(plan)
            with sqlite3.connect(f.database) as db:db.executescript('DELETE FROM art; DELETE FROM actor;')
            f.fail=None;plan=f.prepare();self.assertEqual(len(plan['textures']),2)
            f.execute(plan);self.assertEqual(set(f.textures),{3})

    def test_does_not_delete_reused_texture_id(self):
        with tempfile.TemporaryDirectory() as d:
            f=Fixture(d);plan=f.prepare()
            f.textures[1]={'textureid':1,'url':'special://skin/icon.png','cachedurl':'poster.jpg'}
            f.execute(plan);self.assertIn(1,f.textures);self.assertTrue((f.thumbs/'poster.jpg').exists())

    def test_resume_removes_same_artwork_recached_under_new_id(self):
        with tempfile.TemporaryDirectory() as d:
            f=Fixture(d);plan=f.prepare();f.fail='Textures.RemoveTexture'
            with self.assertRaises(RuntimeError):f.execute(plan)
            with sqlite3.connect(f.database) as db:db.executescript('DELETE FROM art; DELETE FROM actor;')
            old=f.textures.pop(1);old['textureid']=4;f.textures[4]=old
            f.fail=None;f.execute(f.prepare())
            self.assertEqual(set(f.textures),{3})
            self.assertFalse((f.thumbs/'poster.jpg').exists())

    def test_refuses_path_traversal_and_symlinks(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'thumbs';root.mkdir();outside=Path(d)/'keep.jpg';outside.touch()
            with self.assertRaises(ValueError):reset.remove_cached_file(str(root),'../keep.jpg')
            (root/'link.jpg').symlink_to(outside)
            with self.assertRaises(ValueError):reset.remove_cached_file(str(root),'link.jpg')
            self.assertTrue(outside.exists())

    def test_abort_keeps_plan_and_history(self):
        with tempfile.TemporaryDirectory() as d:
            f=Fixture(d);plan=f.prepare()
            with self.assertRaises(InterruptedError):
                reset.execute(str(f.profile),str(f.thumbs),plan,f.rpc,aborted=lambda:True)
            self.assertTrue((f.profile/reset.PENDING).exists())
            self.assertTrue((f.profile/'scan_history.json').exists())

    def test_read_only_snapshot_never_creates_missing_database(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'missing.db'
            with self.assertRaises(sqlite3.OperationalError):reset.artwork_urls(str(p))
            self.assertFalse(p.exists())

    def test_task_lock_is_reentrant_and_excludes_other_processes(self):
        with tempfile.TemporaryDirectory() as d:
            with task_lock(d):
                with task_lock(d):pass
                code="from task_state import task_lock,TaskBusy\ntry:\n with task_lock(%r):pass\nexcept TaskBusy:raise SystemExit(17)"%d
                env=dict(os.environ,PYTHONPATH=str(Path(reset.__file__).parent))
                result=subprocess.run([sys.executable,'-c',code],env=env)
                self.assertEqual(result.returncode,17)
            with task_lock(d):pass

if __name__=='__main__':unittest.main(verbosity=2)
