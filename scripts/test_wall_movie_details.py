"""Movie identity, bookmark resets, single-layer labels and modal lifetime regressions."""
import ast
import importlib
import importlib.util
import json
import queue
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch,Mock
from xml.etree import ElementTree as ET
LIB=Path(__file__).resolve().parents[1]/'addons/plugin.video.filteredmovies/lib'
package=types.ModuleType('wall_movie_tests');package.__path__=[str(LIB)]
sys.modules.setdefault('wall_movie_tests',package)
catalog=importlib.import_module('wall_movie_tests.movie_catalog')

class CatalogTests(unittest.TestCase):
    def test_empty_or_malformed_favourites_are_optional_empty_lists(self):
        for result in (None,{}, {'favourites':None},{'favourites':[]},{'favourites':{}},'invalid'):
            self.assertEqual(catalog.favourite_entries(result),[])
        entry={'title':'Movie','path':'videodb://movies/titles/42'}
        self.assertEqual(catalog.favourite_entries({'favourites':[None,'invalid',entry]}),[entry])
    def test_load_only_queries_native_library_and_preserves_movie_identity(self):
        calls=[]
        def query(method,params):
            calls.append(method)
            if method=='VideoLibrary.GetMovieDetails':return {'moviedetails':{'title':'Movie','cast':[{'name':'Last','order':5},{'name':'First','order':0}]}}
            return []
        row=catalog.load_movie(42,query)
        self.assertEqual((row['movieid'],row['media_type']),(42,'movie'))
        self.assertEqual(row['cast'][0]['name'],'First')
        self.assertEqual(calls,['VideoLibrary.GetMovieDetails'])

    def test_missing_movie_id_does_not_query_or_play(self):
        with self.assertRaises(ValueError):catalog.load_movie(0,lambda *args:self.fail('No request expected'))

    def test_real_resume_total_overrides_scraped_duration(self):
        state=catalog.progress({'runtime':7200,'resume':{'position':1800,'total':7500}})
        self.assertEqual((state['percent'],state['remaining'],state['resuming']),(24,5700,True))
        self.assertEqual(catalog.duration(8700),'2 小时 25 分钟')

    def test_nonfinite_or_out_of_range_bookmarks_are_safe(self):
        self.assertEqual(catalog.progress({'runtime':100,'resume':{'position':200}})['percent'],100)
        self.assertEqual(catalog.progress({'runtime':float('nan'),'resume':{'position':float('inf')}})['percent'],0)

    def test_play_uses_movie_id_and_native_resume_option(self):
        calls=[];query=lambda method,params:calls.append((method,params))
        catalog.play(42,query=query);catalog.play(42,from_start=True,query=query)
        self.assertEqual(calls[0],('Player.Open',{'item':{'movieid':42},'options':{'resume':True}}))
        self.assertFalse(calls[1][1]['options']['resume'])

    def test_both_manual_watched_states_clear_native_resume(self):
        for watched in (True,False):
            calls=[]
            catalog.set_watched(42,watched,lambda method,params:calls.append((method,params)))
            self.assertEqual(calls,[('VideoLibrary.SetMovieDetails',{'movieid':42,
                'playcount':int(watched),'resume':{'position':0.0,'total':0.0}})])
            self.assertFalse(catalog.progress({'runtime':7200,'playcount':int(watched),
                'resume':calls[0][1]['resume']})['resuming'])

    def test_unproven_dolby_atmos_is_not_guessed_from_codec(self):
        movie={'streamdetails':{'video':[{'height':2160,'hdrtype':'dolbyvision'}],'audio':[{'codec':'eac3','channels':8}]}}
        self.assertIn('4K',catalog.technical(movie));self.assertIn('杜比视界',catalog.technical(movie))
        self.assertNotIn('Atmos',catalog.technical(movie))

    def test_movie_duration_writer_uses_seconds(self):
        file=LIB.parents[1]/'script.ai.movie.scanner/python/kodi_db.py'
        tree=ast.parse(file.read_text());node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='runtime_seconds')
        ns={};exec(compile(ast.Module(body=[node],type_ignores=[]),'runtime','exec'),ns)
        self.assertEqual(ns['runtime_seconds'](145),8700)
        self.assertEqual(ns['runtime_seconds'](None),0)
        self.assertEqual(ns['runtime_seconds'](float('nan')),0)

    def test_layout_has_unique_controls_and_reuses_absolute_static_resources(self):
        root=ET.parse(LIB.parent/'resources/skins/Default/1080i/Custom_5117_MovieDetails.xml').getroot()
        ids=[n.get('id') for n in root.iter('control') if n.get('id')]
        self.assertEqual(len(ids),len(set(ids)))
        self.assertTrue({'201','202','203','303','900'}.issubset(ids))
        self.assertFalse({'200','204','205','206'}.intersection(ids))
        play=root.find(".//control[@id='201']")
        self.assertFalse(play.findtext('label'))
        self.assertEqual(play.findtext('disabledcolor'),'00FFFFFF')
        for node in root.findall('.//onup')+root.findall('.//ondown')+root.findall('.//onleft')+root.findall('.//onright'):
            if str(node.text or '').isdigit():self.assertIn(node.text,ids)
        progress=root.find(".//control[@type='progress']")
        self.assertEqual(progress.findtext('height'),'10')
        self.assertEqual(progress.find('midtexture').get('border'),'5,0,5,0')
        outline=next(c for c in root.findall(".//control[@id='303']/focusedlayout/control") if c.findtext('visible')=='Control.HasFocus(303)')
        self.assertIsNotNone(outline)
        for node in root.iter('texture'):
            if node.text and not node.text.startswith('$INFO'):
                self.assertTrue(node.text.startswith('special://'),node.text)

class LifetimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Base:
            def __init__(self,*a,**k):
                self.closed=threading.Event();self.jobs=queue.Queue();self.results=queue.Queue();self.generation=1
        cls.events=[]
        xbmc=types.SimpleNamespace(Monitor=object,LOGERROR=4,executebuiltin=lambda text:cls.events.append(text))
        gui=types.SimpleNamespace(getCurrentWindowId=lambda:13000)
        spec=importlib.util.spec_from_file_location('wall_movie_tests.movie_details',LIB/'movie_details.py')
        cls.module=importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules,{'xbmc':xbmc,'xbmcgui':gui,
             'wall_movie_tests.common':types.SimpleNamespace(ADDON_PATH='/addon',log=lambda *a:None,notification=lambda *a:None),
             'wall_movie_tests.tv_details':types.SimpleNamespace(TvDetailsWindow=Base),
             'wall_movie_tests.video_library':types.SimpleNamespace(prime_textures=lambda *a,**k:None)}):spec.loader.exec_module(cls.module)

    def test_closed_window_discards_late_metadata_without_gui_updates(self):
        window=self.module.MovieDetailsWindow();window.movie_id=42;window.window_id=13000
        entered=threading.Event();release=threading.Event();self.events.clear()
        def load(_):entered.set();release.wait(1);return {'title':'Movie'}
        with patch.object(catalog,'load_movie',side_effect=load),patch.object(catalog,'rpc',return_value={'favourites':[]}):
            worker=threading.Thread(target=window._worker);worker.start();window.jobs.put((1,'load',None,0))
            self.assertTrue(entered.wait(1));window.closed.set();release.set();worker.join(1)
        self.assertFalse(worker.is_alive());self.assertTrue(window.results.empty());self.assertEqual(self.events,[])

    def test_inactive_window_defers_native_controls_until_return(self):
        window=self.module.MovieDetailsWindow();window.window_id=13001
        window.results.put((1,'load',{},[],False))
        window._apply_results()
        self.assertEqual(window.results.qsize(),1)

    def test_notifications_during_initial_load_coalesce_after_gui_binding(self):
        window=self.module.MovieDetailsWindow();window.window_id=13000;window._loading=True
        window.setProperty=Mock();window._bind=Mock()
        for _ in range(20):window._queue_progress()
        self.assertEqual(window.generation,1)
        self.assertTrue(window.jobs.empty())
        window.results.put((1,'load',{'movieid':42},[],False))
        window._apply_results()
        window._bind.assert_called_once_with({'movieid':42})
        self.assertEqual(window.generation,2)
        self.assertEqual(window.jobs.qsize(),1)
        self.assertTrue(window._loading)
        self.assertFalse(window._refresh_pending)
        window.results.put((2,'refresh',{'movieid':42},[],False))
        window._apply_results()
        self.assertFalse(window._loading)

    def test_malformed_library_notifications_are_ignored(self):
        window=self.module.MovieDetailsWindow();window.movie_id=42;window._queue_progress=Mock()
        monitor=self.module._MovieMonitor(window)
        for data in ('null','[]','{}','{"item":null}','{"item":[]}','not JSON'):
            monitor.onNotification('Kodi','VideoLibrary.OnUpdate',data)
        window._queue_progress.assert_not_called()
        monitor.onNotification('Kodi','VideoLibrary.OnUpdate','{"item":{"type":"movie","id":42}}')
        window._queue_progress.assert_called_once()

    def test_null_favourites_cannot_interrupt_movie_bind_or_disable_play(self):
        window=self.module.MovieDetailsWindow();window.movie_id=42;window._initial_focus=False
        values={};enabled=[]
        window.setProperty=lambda k,v:values.update({k:v})
        window._header=lambda _:None;window._bind_actors=lambda:None
        window.getControl=lambda _:types.SimpleNamespace(setEnabled=enabled.append)
        window._bind({'title':'Movie','_favourites':None})
        self.assertEqual(values['MFG.Movie.State'],'ready')
        self.assertEqual(values['MFG.Movie.Favourite'],'false')
        self.assertEqual(enabled,[True]);self.assertIsNone(window.favourite)

    def test_return_focus_cannot_target_removed_controls(self):
        window=self.module.MovieDetailsWindow()
        for cid in (200,204,205,206):self.assertEqual(window._safe_focus(cid),201)
        self.assertEqual(window._safe_focus(303),303)

    def test_header_has_one_label_layer_and_resume_reset_returns_play(self):
        window=self.module.MovieDetailsWindow();values={}
        window.setProperty=lambda name,value:values.update({name:value})
        window._image=lambda url:''
        window.getControl=lambda _:self.fail('Header must not call native button.setLabel')
        with patch.object(catalog,'metadata',return_value=''),patch.object(catalog,'technical',return_value=''):
            window._header({'title':'Movie'})
            self.assertEqual(values['MFG.Movie.PlayLabel'],'')
            window._playback_movie={'runtime':7200,'resume':{'position':1800,'total':7200}}
            window._header(window._playback_movie)
            self.assertEqual(values['MFG.Movie.PlayLabel'],'继续播放')
            window.movie={'title':'Movie','runtime':7200,'resume':{'position':1800,'total':7200}}
            window._header(window.movie)
            self.assertEqual(values['MFG.Movie.PlayLabel'],'继续播放')
            self.assertEqual(values['MFG.Movie.HasProgress'],'true')
            window.movie['resume']={'position':0,'total':0}
            window._header(window.movie)
            self.assertEqual(values['MFG.Movie.PlayLabel'],'播放')
            self.assertEqual(values['MFG.Movie.HasProgress'],'false')
            self.assertEqual(values['MFG.Movie.Remaining'],'')

    def test_play_without_bookmark_explicitly_starts_from_beginning(self):
        window=self.module.MovieDetailsWindow();window.movie_id=42
        window.movie={'runtime':7200,'resume':{'position':0,'total':0}}
        window.getFocusId=lambda:201
        with patch.object(catalog,'play') as play:
            window._play_movie()
            play.assert_called_once_with(42,True)

    def test_play_target_is_ready_before_full_metadata_arrives(self):
        window=self.module.MovieDetailsWindow();window.movie_id=42
        window._playback_movie={'runtime':7200,'resume':{'position':600,'total':7200}}
        window.getFocusId=lambda:201
        with patch.object(catalog,'play') as play:
            window._play_movie()
            play.assert_called_once_with(42,False)

    def test_modal_failure_restores_parent_guard_and_focus(self):
        events=[]
        class Child:
            def __init__(self,*a):pass
            def doModal(self):raise RuntimeError('cancelled')
            def cleanup(self):events.append('cleanup')
        class Parent:
            guard=''
            def getProperty(self,_):return self.guard
            def setProperty(self,_,value):self.guard=value
            def clearProperty(self,_):self.guard=''
            def _capture_navigation_state(self):events.append('capture')
            def _restore_navigation_state(self):events.append('restore')
        parent=Parent()
        with patch.object(self.module,'MovieDetailsWindow',Child):
            with self.assertRaises(RuntimeError):self.module.open_movie(42,parent)
        self.assertEqual(parent.guard,'');self.assertEqual(events,['capture','cleanup','restore'])

if __name__=='__main__':unittest.main()
