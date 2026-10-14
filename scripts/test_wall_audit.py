"""Concurrency, primary movie versions and asynchronous actor-page regressions."""
import ast
import importlib
import importlib.util
from pathlib import Path
import queue
import sqlite3
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.parse
from unittest.mock import Mock,patch

LIB=Path(__file__).resolve().parents[1]/'addons/plugin.video.filteredmovies/lib'


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.addCleanup(self.folder.cleanup)
        folder=Path(self.folder.name);(folder/'one.jpg').write_bytes(b'image')
        self.database=folder/'Textures.db'
        with sqlite3.connect(self.database) as conn:
            conn.execute('CREATE TABLE texture(url TEXT,cachedurl TEXT)')
            conn.executemany('INSERT INTO texture VALUES(?,?)',
                [('https://image.example/%d.jpg'%i,'one.jpg') for i in range(5000)])
        self.ns={'time':time,'urllib':urllib,'threading':threading,
            '_QUERY_CACHE':{},'_CACHE_TTL':30,'_QUERY_LOCK':threading.RLock(),
            '_TEXTURE_LOCK':threading.RLock(),'_TEXTURE_PATH_CACHE':{},'_TEXTURE_CHECKED_AT':{},
            '_CACHED_ARTWORK_ONLY':True,'TEXTURES_DB_PATH':str(self.database),'THUMBNAILS_PATH':str(folder)}
        names={'_cache_get','_cache_set','prime_textures','_get_cached_texture_path'}
        tree=ast.parse((LIB/'video_library.py').read_text())
        methods=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names]
        exec(compile(ast.Module(body=methods,type_ignores=[]),'caches','exec'),self.ns)

    def test_concurrent_read_write_and_prune_keep_cache_pairs_consistent(self):
        errors=[];barrier=threading.Barrier(4)
        def run(worker):
            try:
                barrier.wait()
                for round in range(8):
                    urls=[{'art':{'poster':'https://image.example/%d.jpg'%i}}
                          for i in range(worker*1100,worker*1100+1100)]
                    self.ns['prime_textures'](urls,force=True)
                    self.ns['_cache_set']((worker,round),urls[:5])
                    self.ns['_cache_get']((worker,round))
                    for row in urls[:20]:self.ns['_get_cached_texture_path'](row['art']['poster'])
            except Exception as exc:errors.append(exc)
        threads=[threading.Thread(target=run,args=(i,)) for i in range(4)]
        for thread in threads:thread.start()
        for thread in threads:thread.join(10)
        self.assertFalse(any(t.is_alive() for t in threads));self.assertEqual(errors,[])
        self.assertLessEqual(len(self.ns['_TEXTURE_PATH_CACHE']),4096)
        self.assertEqual(set(self.ns['_TEXTURE_PATH_CACHE']),set(self.ns['_TEXTURE_CHECKED_AT']))
        self.assertLessEqual(len(self.ns['_QUERY_CACHE']),16)

    def test_transient_texture_lock_keeps_previous_cached_cover(self):
        url='https://image.example/1.jpg';items=[{'art':{'poster':url}}]
        self.ns['prime_textures'](items)
        before=self.ns['_get_cached_texture_path'](url)
        with sqlite3.connect(self.database) as conn:
            conn.execute('BEGIN EXCLUSIVE')
            self.ns['prime_textures'](items,force=True)
        self.assertEqual(self.ns['_get_cached_texture_path'](url),before)
        self.assertTrue(before.endswith('one.jpg'))

    def test_null_cast_does_not_block_texture_lookup(self):
        url='https://image.example/1.jpg'
        self.ns['prime_textures']([{'art':{'poster':url},'cast':None}])
        self.assertTrue(self.ns['_get_cached_texture_path'](url).endswith('one.jpg'))


class LibraryTests(unittest.TestCase):
    def test_mixed_catalog_hydrates_primary_movie_version_only(self):
        with tempfile.TemporaryDirectory() as folder:
            database=Path(folder)/'Videos.db'
            with sqlite3.connect(database) as conn:
                conn.executescript('CREATE TABLE movie_view(idMovie,c00,dateAdded,strPath,strFileName,isDefaultVersion);'
                    'CREATE TABLE tvshow_view(idShow,c00,dateAdded);'
                    "INSERT INTO movie_view VALUES(1,'Movie','2026-01-01','/primary/','one.mkv',1),"
                    "(1,'Movie','2026-01-01','/alternate/','extra.mkv',0);"
                    "INSERT INTO tvshow_view VALUES(2,'Show','2026-01-02');")
            package=types.ModuleType('wall_db_audit');package.__path__=[str(LIB)]
            with patch.dict(sys.modules,{'wall_db_audit':package,
                    'wall_db_audit.common':types.SimpleNamespace(DB_PATH=str(database),log=lambda *a:None)}):
                module=importlib.import_module('wall_db_audit.db')
                with patch.object(module,'_hydrate'):
                    rows=module.sqlite_get_items({'filter.mediatype':'all'},120)
            self.assertEqual(len(rows),2)
            self.assertEqual(next(r for r in rows if r['media_type']=='movie')['file'],'/primary/one.mkv')

    def test_large_mixed_catalog_pages_are_complete_stable_and_unique(self):
        with tempfile.TemporaryDirectory() as folder:
            database=Path(folder)/'Videos.db'
            with sqlite3.connect(database) as conn:
                conn.executescript('CREATE TABLE movie_view(idMovie,c00,dateAdded,isDefaultVersion);'
                    'CREATE TABLE tvshow_view(idShow,c00,dateAdded);')
                conn.executemany('INSERT INTO movie_view VALUES(?,?,?,1)',
                    [(i,str(i),'2026-01-01') for i in range(1,3001)])
                conn.executemany('INSERT INTO movie_view VALUES(?,?,?,0)',
                    [(i,'alternate','2026-01-01') for i in range(1,101)])
                conn.executemany('INSERT INTO tvshow_view VALUES(?,?,?)',
                    [(i,str(i),'2026-01-01') for i in range(1,701)])
            package=types.ModuleType('wall_db_large');package.__path__=[str(LIB)]
            with patch.dict(sys.modules,{'wall_db_large':package,
                    'wall_db_large.common':types.SimpleNamespace(DB_PATH=str(database),log=lambda *a:None)}):
                module=importlib.import_module('wall_db_large.db')
                with patch.object(module,'_hydrate'):
                    filters={'filter.mediatype':'all'}
                    self.assertEqual(module.get_filtered_count(filters),3700)
                    rows=[row for offset in range(0,3700,120)
                          for row in module.sqlite_get_items(filters,120,offset)]
            identities=[(r['media_type'],r.get('movieid') or r.get('tvshowid')) for r in rows]
            self.assertEqual(len(rows),3700);self.assertEqual(len(set(identities)),3700)
            self.assertEqual(identities,sorted(identities))

    def test_failed_cache_initialization_closes_connection(self):
        tree=ast.parse((LIB/'catalog_cache.py').read_text())
        node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_connect')
        conn=Mock();conn.execute.side_effect=sqlite3.DatabaseError('corrupt cache')
        ns={'os':types.SimpleNamespace(makedirs=lambda *a,**k:None,path=types.SimpleNamespace(join=lambda *a:'cache.db')),
            'ADDON_DATA_PATH':'profile','sqlite3':types.SimpleNamespace(connect=lambda *a,**k:conn)}
        exec(compile(ast.Module(body=[node],type_ignores=[]),'cache_connect','exec'),ns)
        with self.assertRaises(sqlite3.DatabaseError):ns['_connect']()
        conn.close.assert_called_once()

    def test_wrong_shaped_disk_cache_is_ignored_and_connection_is_closed(self):
        import json
        tree=ast.parse((LIB/'catalog_cache.py').read_text())
        node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='get')
        for body in ('null','{}','[1]','not JSON'):
            conn=Mock();conn.execute.return_value.fetchone.return_value=(body,)
            ns={'_connect':lambda:conn,'time':time,'json':json,'sqlite3':sqlite3}
            exec(compile(ast.Module(body=[node],type_ignores=[]),'cache_get','exec'),ns)
            self.assertIsNone(ns['get']('key','revision'))
            conn.close.assert_called_once()


class ActorTests(unittest.TestCase):
    def setUp(self):
        package=types.ModuleType('wall_actor_audit');package.__path__=[str(LIB)]
        gui=types.SimpleNamespace(WindowXML=object,getCurrentWindowId=lambda:13000)
        modules={'wall_actor_audit':package,'xbmcgui':gui,
            'xbmc':types.SimpleNamespace(LOGWARNING=2,executebuiltin=lambda _:None,getInfoLabel=lambda _:'1'),
            'wall_actor_audit.common':types.SimpleNamespace(ADDON_PATH='/addon',log=lambda *a:None,notification=lambda *a:None),
            'wall_actor_audit.person_catalog':types.SimpleNamespace(),
            'wall_actor_audit.video_library':types.SimpleNamespace(_resolve_artwork=lambda _: '/cache/portrait')}
        spec=importlib.util.spec_from_file_location('wall_actor_audit.actor_details',LIB/'actor_details.py')
        self.module=importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules,modules):spec.loader.exec_module(self.module)
        self.owner=self.module.ActorWindow();self.owner.window_id=13000

    def test_actor_metadata_arriving_behind_child_does_not_touch_controls(self):
        self.owner.window_id=13001
        self.owner.results.put({'name':'Actor','works':[]})
        self.owner.getControl=lambda _:self.fail('Inactive control access')
        self.owner._apply_results();self.assertEqual(self.owner.results.qsize(),1)

    def test_biography_completion_does_not_rebind_local_works(self):
        self.owner.actor={'name':'Actor'}
        works=[{'movieid':1,'title':'Movie'}];self.owner.works=works
        panel=types.SimpleNamespace(size=lambda:1,reset=Mock(),addItems=Mock())
        self.owner.getControl=lambda _:panel;self.owner.setProperty=Mock()
        self.owner._bind({'name':'Actor','works':list(works),'biography':'Final biography'})
        panel.reset.assert_not_called();panel.addItems.assert_not_called()
        self.assertEqual(self.owner.payload['biography'],'Final biography')

    def test_return_to_local_work_is_applied_once_and_clamped(self):
        selects=[];focus=[]
        self.owner.return_position=20;self.owner._return_applied=False
        self.owner.getFocusId=lambda:200;self.owner.setFocusId=focus.append
        self.owner.getControl=lambda _:types.SimpleNamespace(size=lambda:3,getSelectedPosition=lambda:0,
            selectItem=selects.append)
        self.owner._restore_navigation_state();self.owner._restore_navigation_state()
        self.assertEqual(selects,[2]);self.assertEqual(focus,[310])


if __name__=='__main__':unittest.main()
