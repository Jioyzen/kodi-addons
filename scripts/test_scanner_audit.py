"""Inventory safety, cancellation, rate limiting and atomic texture writes."""
import ast
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
from pathlib import Path
import queue
import sqlite3
import struct
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import Mock,patch

SOURCE=Path(__file__).resolve().parents[1]/'addons/script.ai.movie.scanner/python'
sys.path.insert(0,str(SOURCE))

def isolated(name,stubs=None):
    spec=importlib.util.spec_from_file_location('audit_'+name,SOURCE/(name+'.py'))
    module=importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules,stubs or {}):spec.loader.exec_module(module)
    return module

def function(file,name,ns):
    tree=ast.parse((SOURCE/file).read_text())
    node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name==name)
    node.decorator_list=[]
    exec(compile(ast.Module(body=[node],type_ignores=[]),name,'exec'),ns)
    return ns[name]


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.history=isolated('history_store')
        self.folder=tempfile.TemporaryDirectory();self.addCleanup(self.folder.cleanup)
        self.path=Path(self.folder.name)/'scan_history.json'
        self.value={'directories':{'/movies':{'files':{'/movies/film.mkv':{'status':'success'}}}}}

    def test_new_inventory_and_round_trip(self):
        self.assertEqual(self.history.load_history_file(self.path),{'directories':{}})
        self.history.save_history_file(self.path,self.value)
        self.assertEqual(self.history.load_history_file(self.path),self.value)

    def test_corrupt_existing_inventory_is_not_treated_as_empty(self):
        for raw in ('broken JSON','null','[]','{}','{"directories":[]}','{"directories":{"x":{"files":[]}}}'):
            self.path.write_text(raw)
            with self.assertRaises(ValueError):self.history.load_history_file(self.path)
            self.assertEqual(self.path.read_text(),raw)

    def test_failed_replace_keeps_original_and_removes_temporary(self):
        self.history.save_history_file(self.path,self.value)
        before=self.path.read_bytes()
        with patch.object(self.history.os,'replace',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):self.history.save_history_file(self.path,{'directories':{}})
        self.assertEqual(self.path.read_bytes(),before)
        self.assertEqual(list(self.path.parent.iterdir()),[self.path])

    def test_readers_never_observe_partial_concurrent_writes(self):
        self.history.save_history_file(self.path,self.value)
        errors=[]
        def write(worker):
            try:
                for i in range(30):self.history.save_history_file(self.path,dict(self.value,sequence=[worker,i]))
            except Exception as exc:errors.append(exc)
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures=[pool.submit(write,i) for i in range(3)]
            for _ in range(60):self.assertIsInstance(self.history.load_history_file(self.path)['directories'],dict)
            for future in futures:future.result()
        self.assertEqual(errors,[])


class InventoryTests(unittest.TestCase):
    def test_confirmed_movies_do_not_repeat_episode_inference_for_directory_menu(self):
        module=isolated('directory_listing')
        files={'/movies/%d.mkv'%i:{'movie_id':i+1,'status':'success'} for i in range(2000)}
        files['/shows/Show.S01E01.mkv']={'status':'pending'}
        original=module.build_tv_contexts
        with patch.object(module,'build_tv_contexts',wraps=original) as contexts:
            groups,plain=module.group_inventory(files)
        self.assertEqual(contexts.call_args.args[0],['/shows/Show.S01E01.mkv'])
        self.assertEqual(sum(bool(info.get('movie_id')) for _,info in plain),2000)
    def scanner(self,listing,exists=lambda _:True):
        return isolated('file_scanner',{'xbmc':types.SimpleNamespace(LOGINFO=1,LOGERROR=2,log=lambda *a,**k:None),
            'xbmcvfs':types.SimpleNamespace(listdir=listing,exists=exists)})

    def test_child_network_error_discards_partial_inventory(self):
        def listing(path):
            if path=='/root/':return ['child'],['Film.mkv']
            raise OSError('network unavailable')
        scanner=self.scanner(listing)
        with self.assertRaises(scanner.ScanIncomplete):scanner.find_video_files('/root')

    def test_cancel_and_unavailable_root_are_not_empty_success(self):
        scanner=self.scanner(lambda _: ([],[]),lambda _:False)
        with self.assertRaises(scanner.ScanIncomplete):scanner.find_video_files('/root')
        with self.assertRaises(scanner.ScanIncomplete):scanner.find_video_files('/root',cancelled=lambda:True)

    def test_complete_empty_directory_remains_valid(self):
        scanner=self.scanner(lambda _: ([],[]))
        self.assertEqual(scanner.find_video_files('/root'),[])

    def test_pathological_directory_depth_ends_without_recursion_overflow(self):
        scanner=self.scanner(lambda _: (['nested'],[]))
        with self.assertRaises(scanner.ScanIncomplete):scanner.find_video_files('/root')

    def test_refresh_unavailable_or_partial_source_cannot_delete_existing_records(self):
        scanner=self.scanner(lambda _: ([],[]))
        saved=Mock();delete=Mock();history={'directories':{'/root':{'files':{'/root/Film.mkv':{'status':'success'}}}}}
        ns={'load_history':lambda:history,'save_history':saved,'_delete_single_file_from_db':delete,
            'xbmcvfs':types.SimpleNamespace(exists=lambda _:False),'_is_stop_requested':lambda:False,
            'log':lambda *a:None,'directory_label':lambda _:'root','xbmc':types.SimpleNamespace(LOGWARNING=2)}
        refresh=function('main.py','refresh_directory_files',ns)
        with patch.dict(sys.modules,{'file_scanner':scanner}):
            self.assertEqual(refresh('/root'),(0,0))
            ns['xbmcvfs'].exists=lambda _:True
            scanner.find_video_files=Mock(side_effect=scanner.ScanIncomplete())
            self.assertEqual(refresh('/root'),(0,0))
            scanner.find_video_files=Mock(return_value=[])
            self.assertEqual(refresh('/root'),(0,0))
        saved.assert_not_called();delete.assert_not_called()


class TransactionTests(unittest.TestCase):
    def test_stop_during_auto_discovery_cannot_start_matching_afterwards(self):
        with tempfile.TemporaryDirectory() as folder:
            state={'stop':False};visited=[];scan=Mock();closed=[]
            def refresh(path):visited.append(path);state['stop']=True;return 1,0
            progress=types.SimpleNamespace(create=lambda *a:None,update=lambda *a:None,close=lambda:closed.append(True))
            ns={'os':os,'REFRESHING_FILE':str(Path(folder)/'.refreshing'),
                '_load_history':lambda:{'directories':{'/one':{},'/two':{}}},
                'xbmcgui':types.SimpleNamespace(DialogProgressBG=lambda:progress),
                'ADDON_NAME':'test','ADDON':types.SimpleNamespace(getLocalizedString=lambda k:{32034:'%s',32035:'%d/%d'}.get(k,'text')),
                'directory_label':lambda p:p,'xbmcvfs':types.SimpleNamespace(exists=os.path.exists),
                'log':lambda *a:None}
            main=types.SimpleNamespace(refresh_directory_files=refresh,_run_scan=scan,_needs_scan=lambda _:True,
                _is_stop_requested=lambda:state['stop'],_clear_stop=lambda:state.update(stop=False))
            run=function('service.py','_do_scan_unlocked',ns)
            with patch.dict(sys.modules,{'main':main}):self.assertEqual(run(),(0,0,0,1))
            self.assertEqual(visited,['/one']);scan.assert_not_called()
            self.assertEqual(closed,[True]);self.assertFalse(Path(ns['REFRESHING_FILE']).exists())

    def test_database_close_rolls_back_unfinished_movie_and_episode_transactions(self):
        for file,clsname in [('kodi_db.py','KodiDatabase'),('tvshow_db.py','TvShowDatabase')]:
            tree=ast.parse((SOURCE/file).read_text())
            cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name==clsname)
            method=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='close')
            ns={};exec(compile(ast.Module(body=[method],type_ignores=[]),'close','exec'),ns)
            with tempfile.TemporaryDirectory() as folder:
                path=Path(folder)/'database.db';conn=sqlite3.connect(path)
                conn.execute('CREATE TABLE test(value)');conn.commit();conn.execute('INSERT INTO test VALUES(1)')
                owner=types.SimpleNamespace(conn=conn);ns['close'](owner)
                self.assertIsNone(owner.conn)
                with sqlite3.connect(path) as verify:self.assertEqual(verify.execute('SELECT COUNT(*) FROM test').fetchone()[0],0)

    def test_source_cleanup_failure_does_not_remove_inventory(self):
        history={'directories':{'/source':{'files':{}}}};saved=Mock()
        ns={'xbmcgui':types.SimpleNamespace(Dialog=lambda:types.SimpleNamespace(yesno=lambda *a:True,
                notification=lambda *a:None)), 'ADDON_NAME':'test','L':str,
            'cascade_delete_directory':Mock(side_effect=sqlite3.OperationalError('locked')),
            'load_history':lambda:history,'save_history':saved,
            'xbmc':types.SimpleNamespace(executebuiltin=lambda *a:None)}
        remove=function('main.py','remove_directory',ns)
        with self.assertRaises(sqlite3.OperationalError):remove('/source')
        self.assertIn('/source',history['directories']);saved.assert_not_called()

    def test_scan_finally_cleans_markers_when_inventory_save_fails(self):
        class Progress:
            def create(self,*a):pass
            def isFinished(self):return False
            def update(self,*a):pass
            def close(self):events.append('dialog-closed')
        events=[]
        settings={'num_workers':'1','scan_delay':'0','tmdb_api_key':'test'}
        history={'directories':{}}
        session=types.SimpleNamespace(forced_movies=set(),previous_results={})
        ns={'queue':queue,'threading':threading,'os':os,'time':time,
            '_is_scanning':lambda:False,'_set_scanning':lambda:events.append('scanning'),
            '_clear_stop':lambda:events.append('stop-cleared'),'_clear_scanning':lambda:events.append('scanning-cleared'),
            '_is_stop_requested':lambda:False,'load_history':lambda:history,
            'save_history':Mock(side_effect=OSError('disk full')),
            'ADDON':types.SimpleNamespace(getSetting=lambda k:settings.get(k,'')),
            'ADDON_NAME':'test','xbmc':types.SimpleNamespace(Monitor=lambda:types.SimpleNamespace(abortRequested=lambda:False),
                executebuiltin=lambda *a:None),
            'xbmcgui':types.SimpleNamespace(DialogProgressBG=Progress),'L':str,
            'ImageCacher':lambda:self.fail('No prefetch after save failure'),
            'scan_worker':lambda *a:None,'log':lambda *a:None}
        scan=function('main.py','_run_scan',ns)
        mods={'dns_override':types.SimpleNamespace(),
            'tmdb_api':types.SimpleNamespace(check_api_key=lambda _:'ok'),
            'tv_matcher':types.SimpleNamespace(TvMatchSession=lambda *a:session),
            'tv_context':types.SimpleNamespace(build_tv_contexts=lambda *a:{})}
        with patch.dict(sys.modules,mods),self.assertRaises(OSError):scan([],[])
        self.assertIn('scanning-cleared',events);self.assertIn('dialog-closed',events)


class ImageTests(unittest.TestCase):
    def setUp(self):
        addon=types.SimpleNamespace(getAddonInfo=lambda key:'scanner',getLocalizedString=lambda _: 'text')
        mods={'ai_database':types.SimpleNamespace(current_database=lambda _:':memory:'),
            'xbmcaddon':types.SimpleNamespace(Addon=lambda:addon),
            'xbmc':types.SimpleNamespace(LOGINFO=1,LOGERROR=2,LOGWARNING=3,log=lambda *a,**k:None,
                Monitor=lambda:types.SimpleNamespace(abortRequested=lambda:False)),
            'xbmcgui':types.SimpleNamespace(),'xbmcvfs':types.SimpleNamespace(translatePath=lambda p:p)}
        self.module=isolated('image_cacher',mods)
        self.image=self.module.ImageCacher();self.addCleanup(self.image.session.close)
        self.folder=tempfile.TemporaryDirectory();self.addCleanup(self.folder.cleanup)
        self.image.db_path=str(Path(self.folder.name)/'Textures.db')
        with sqlite3.connect(self.image.db_path) as conn:
            conn.executescript('CREATE TABLE texture(id INTEGER PRIMARY KEY,url TEXT UNIQUE,cachedurl,imagehash,lasthashcheck);'
                'CREATE TABLE sizes(idtexture,size,width,height,usecount,lastusetime);'
                "INSERT INTO texture VALUES(7,'url','old.jpg','hash','today');"
                "INSERT INTO sizes VALUES(7,1,10,10,4,'today');")

    def test_recache_preserves_texture_id_and_does_not_orphan_size_rows(self):
        self.assertTrue(self.image._write_to_db('url','new.jpg',300,450))
        with sqlite3.connect(self.image.db_path) as conn:
            self.assertEqual(conn.execute('SELECT id FROM texture WHERE url="url"').fetchone()[0],7)
            self.assertEqual(conn.execute('SELECT idtexture,width,height,usecount FROM sizes').fetchall(),[(7,300,450,4)])

    def test_cancelled_download_does_not_make_network_request(self):
        self.image.session.get=Mock();self.image.cancel.set()
        self.assertEqual(self.image._fetch_bytes('https://example.test/image')[1],'canceled')
        self.image.session.get.assert_not_called()

    def test_image_save_is_atomic_and_rejects_zero_dimensions(self):
        path=Path(self.folder.name)/'cover.png';path.write_bytes(b'previous')
        png=b'\x89PNG\r\n\x1a\n'+b'\0'*8+struct.pack('>ii',400,600)+b'\0'*8
        with patch.object(self.module.os,'replace',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):self.image._save_image(png,str(path),'url','cover.png','direct')
        self.assertEqual(path.read_bytes(),b'previous')
        self.assertFalse(list(path.parent.glob('.poster-cache-*')))
        self.assertFalse(self.image._save_image(b'\xff\xd8\xfferror',str(path),'url','cover.png','direct'))
        self.assertEqual(path.read_bytes(),b'previous')

    def test_bounded_downloads_stop_submitting_jobs_after_cancel(self):
        lock=threading.Lock();started=[]
        self.image.thread_count=3
        def download(url):
            with lock:started.append(url)
            time.sleep(.02);self.image.cancel.set();return 'canceled'
        self.image._download_image=download
        results=list(self.image._bounded_results(range(10000)))
        self.assertLessEqual(len(started),3);self.assertLessEqual(len(results),3)

    def test_null_art_rpc_returns_empty_not_exception(self):
        self.module.xbmc.executeJSONRPC=lambda _:json.dumps({'result':{'movies':None,'tvshows':None,'episodes':None,'sets':None}})
        self.assertEqual(self.image._get_video_items(),[])


class RateLimitTests(unittest.TestCase):
    def test_simultaneous_workers_cannot_all_spend_the_same_future_token(self):
        module=isolated('tmdb_api',{'xbmc':types.SimpleNamespace(LOGINFO=1,LOGERROR=2,LOGWARNING=3,log=lambda *a,**k:None)})
        limiter=module._RateLimiter(rate=40,burst=1)
        barrier=threading.Barrier(6);finished=[]
        def run():barrier.wait();limiter.wait();finished.append(time.monotonic())
        with ThreadPoolExecutor(max_workers=6) as pool:list(pool.map(lambda _:run(),range(6)))
        self.assertGreaterEqual(max(finished)-min(finished),.10)


if __name__=='__main__':unittest.main()
