"""Modal/player return preserves category identity, focus and native progress."""
import ast
import importlib
import queue
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
from xml.etree import ElementTree as ET
LIB=Path(__file__).resolve().parents[1]/'addons/plugin.video.filteredmovies/lib'
package=types.ModuleType('wall_return_tests');package.__path__=[str(LIB)]
sys.modules.setdefault('wall_return_tests',package)
playback=importlib.import_module('wall_return_tests.playback_state')

class PlaybackTests(unittest.TestCase):
    def test_movie_bookmark_and_completed_state_use_native_metadata_only(self):
        calls=[]
        def query(method,params):
            calls.append((method,params))
            return {'moviedetails':{'playcount':0,'runtime':7200,'resume':{'position':1800,'total':7500}}}
        state=playback.load_state('movie',42,query)
        self.assertEqual(state['percent'],24)
        self.assertEqual(calls[0][0],'VideoLibrary.GetMovieDetails')
        self.assertNotIn('file',calls[0][1]['properties'])
        completed=playback.load_state('movie',42,lambda *a:{'moviedetails':{'playcount':1,'runtime':7200,'resume':{'position':0,'total':0}}})
        self.assertEqual((completed['percent'],completed['playcount']),(0,1))

    def test_show_partial_progress_is_preserved_even_below_one_percent(self):
        query=lambda *a:{'tvshowdetails':{'episode':100,'watchedepisodes':0,'lastplayed':'today','playcount':0}}
        self.assertEqual(playback.load_state('tvshow',5,query,lambda _:0.25)['percent'],1)
        query=lambda *a:{'tvshowdetails':{'episode':10,'watchedepisodes':2,'playcount':0}}
        self.assertEqual(playback.load_state('tvshow',5,query,lambda _:0.5)['percent'],25)

    def test_update_for_old_row_cannot_modify_a_different_movie(self):
        tag=types.SimpleNamespace(getMediaType=lambda:'movie',getDbId=lambda:9,setPlaycount=Mock(),setResumePoint=Mock())
        item=types.SimpleNamespace(getVideoInfoTag=lambda:tag,setProperty=Mock())
        state={'type':'movie','id':42,'playcount':0,'percent':24,'resume':{'position':1800,'total':7500}}
        self.assertFalse(playback.apply_state(item,state));tag.setPlaycount.assert_not_called();item.setProperty.assert_not_called()
        tag.getDbId=lambda:42
        self.assertTrue(playback.apply_state(item,state));tag.setResumePoint.assert_called_once_with(1800,7500)
        item.setProperty.assert_called_once_with('SkinPercentPlayed','24')

class ReturnTests(unittest.TestCase):
    def setUp(self):
        self.revision='before';self.focus=9999;self.position=156;self.props={};self.selects=[];self.focuses=[]
        self.query={'filter.mediatype':'movie','filter.sort':'title','filter.sortorder':'ascending'}
        tag=types.SimpleNamespace(getMediaType=lambda:'movie',getDbId=lambda:42)
        self.panel=types.SimpleNamespace(size=lambda:240,getSelectedPosition=lambda:self.position,
            selectItem=lambda p:self.selects.append(p) or setattr(self,'position',p),
            getSelectedItem=lambda:types.SimpleNamespace(getVideoInfoTag=lambda:tag))
        def get_control(cid):
            if cid==9999:return self.panel
            raise RuntimeError('Absent home control')
        loader=types.SimpleNamespace(closed=threading.Event(),refresh=Mock(),restore_home=lambda:False,refresh_playback_state=Mock())
        self.owner=types.SimpleNamespace(_grid_page=True,_is_active=lambda:True,getFocusId=lambda:self.focus,
            setFocusId=lambda f:self.focuses.append(f) or setattr(self,'focus',f),getControl=get_control,
            _query_filters=lambda:dict(self.query),setProperty=lambda k,v:self.props.update({k:v}),
            getProperty=lambda k:self.props.get(k,''),poster_loader=loader,running=True,
            _input_stop=threading.Event(),worker=types.SimpleNamespace(is_alive=lambda:True),_update_toolbar=Mock())
        self.ns={'__package__':'wall_return_tests','time':time,'queue':queue,'threading':threading,'log':lambda *a:None,
            'xbmcgui':types.SimpleNamespace(getCurrentWindowId=lambda:13000,Window=lambda _:types.SimpleNamespace(getProperty=lambda _:'reload'))}
        tree=ast.parse((LIB/'window_handler.py').read_text())
        cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='FilterWindow')
        methods=[n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name in
            ('onInit','_capture_navigation_state','_restore_navigation_state','_can_restore_grid','_apply_grid_binds',
             '_commit_navigation_input')]
        exec(compile(ast.Module(body=methods,type_ignores=[]),'return_methods','exec'),self.ns)
        for name in ('_capture_navigation_state','_restore_navigation_state','_can_restore_grid','_commit_navigation_input'):
            setattr(self.owner,name,types.MethodType(self.ns[name],self.owner))
        self.cache_patch=patch.dict(sys.modules,{'wall_return_tests.catalog_cache':types.SimpleNamespace(revision=lambda:self.revision)})
        self.cache_patch.start();self.addCleanup(self.cache_patch.stop)
        self.owner._capture_navigation_state()

    def test_database_change_from_playback_does_not_invalidate_category_position(self):
        self.revision='after playback'
        self.assertTrue(self.owner._can_restore_grid())
        self.owner._restore_navigation_state()
        self.assertEqual(self.position,156);self.assertEqual(self.selects,[]);self.assertEqual(self.focuses,[])
        self.owner.poster_loader.refresh_playback_state.assert_called_once_with(('movie',42),156)

    def test_oninit_and_modal_finally_cannot_restore_focus_twice(self):
        self.position=0;self.focus=7200;self.props['MFG.ChildWindow']='true'
        self.ns['onInit'](self.owner)
        self.owner._restore_navigation_state()
        self.assertEqual(self.selects,[156]);self.assertEqual(self.focuses,[9999])
        self.owner.poster_loader.refresh.assert_not_called()
        self.owner.poster_loader.refresh_playback_state.assert_called_once()

    def test_late_oninit_after_modal_guard_clear_does_not_refresh_to_first_item(self):
        self.owner._restore_navigation_state();self.props['MFG.ChildWindow']=''
        self.revision='written bookmark'
        self.ns['onInit'](self.owner)
        self.assertEqual(self.position,156);self.owner.poster_loader.refresh.assert_not_called()

    def test_home_resume_reuses_cache_even_after_modal_guard_is_cleared(self):
        self.owner._grid_page=False
        self.owner.poster_loader.restore_home=lambda:True
        self.props['MFG.ChildWindow']=''
        self.ns['onInit'](self.owner)
        self.owner.poster_loader.refresh.assert_not_called()

    def test_inactive_parent_defers_restore_until_it_is_active(self):
        self.owner._is_active=lambda:False;self.owner._restore_navigation_state()
        self.assertFalse(self.owner._navigation_return_applied)
        self.owner._is_active=lambda:True;self.owner._restore_navigation_state()
        self.assertTrue(self.owner._navigation_return_applied)

    def test_new_native_selection_is_not_rewound_by_late_return_callback(self):
        self.position=157
        self.owner._restore_navigation_state()
        self.assertEqual(self.position,157)
        self.assertEqual(self.selects,[])
        self.assertTrue(self.owner._navigation_return_applied)
        self.owner.poster_loader.refresh_playback_state.assert_called_once_with(('movie',42),156)

    def test_direction_input_to_zero_also_takes_priority_over_saved_return(self):
        self.position=0
        self.owner._commit_navigation_input()
        self.owner._restore_navigation_state()
        self.assertEqual(self.position,0)
        self.assertEqual(self.selects,[])
        self.owner.poster_loader.refresh_playback_state.assert_called_once()

    def test_surviving_controls_keep_their_navigation_wiring_on_return(self):
        self.owner._filter_layout_signature=('existing',)
        self.ns['onInit'](self.owner)
        self.assertEqual(self.owner._filter_layout_signature,('existing',))

    def test_explicit_filter_or_sort_change_requires_fresh_data(self):
        self.query['filter.sort']='rating'
        self.assertFalse(self.owner._can_restore_grid())
        self.ns['onInit'](self.owner)
        self.owner.poster_loader.refresh.assert_called_once_with(self.query)

    def test_progress_update_mutates_one_item_without_rebinding_or_selecting(self):
        state={'type':'movie','id':42,'playcount':0,'percent':25,'resume':{'position':1800,'total':7200}}
        tag=types.SimpleNamespace(getMediaType=lambda:'movie',getDbId=lambda:42,setPlaycount=Mock(),setResumePoint=Mock())
        item=types.SimpleNamespace(getVideoInfoTag=lambda:tag,setProperty=Mock())
        self.panel.getListItem=lambda _:item;self.panel.reset=Mock();self.panel.addItems=Mock()
        loader=self.owner.poster_loader;loader.lock=threading.RLock();loader.valid=lambda _:True
        self.owner._grid_bind_queue=queue.Queue()
        job={'loader':loader,'generation':1,'playback_state':state,'position':156,'items':None,'done':threading.Event(),'ok':False}
        self.owner._grid_bind_queue.put(job);self.ns['_apply_grid_binds'](self.owner)
        self.assertTrue(job['ok']);self.panel.reset.assert_not_called();self.panel.addItems.assert_not_called()
        self.assertEqual(self.selects,[]);self.assertEqual(self.focuses,[])
        item.setProperty.assert_called_once_with('SkinPercentPlayed','25')

    def test_return_focus_has_an_instant_final_size_and_normal_navigation_remains_animated(self):
        self.assertEqual(self.props['MFG.ReturnFocus'],'true')
        for name in ('Custom_5112_AllMovie.xml','Custom_5114_Movie.xml'):
            root=ET.parse(LIB.parent/'resources/skins/Default/1080i'/name).getroot()
            anims=root.findall(".//focusedlayout//animation[@type='Focus']")
            regular=next(a for a in anims if a.get('condition','').startswith('!'))
            restored=next(a for a in anims if not a.get('condition','').startswith('!'))
            self.assertEqual(regular.find('effect').get('time'),'200')
            self.assertEqual(restored.find('effect').get('time'),'0')
            self.assertEqual(restored.find('effect').get('start'),restored.find('effect').get('end'))

if __name__=='__main__':unittest.main()
