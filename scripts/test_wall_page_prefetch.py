"""Opening a large category must bind once; warm metadata stays offscreen."""
import ast
import importlib.util
import queue
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import Mock,patch

LIB=Path(__file__).resolve().parents[1]/'addons/plugin.video.filteredmovies/lib'

class PrefetchTests(unittest.TestCase):
    def setUp(self):
        self.props={};self.binds=[];self.queries=[]
        def query(filters,limit,offset):
            self.queries.append((offset,limit))
            return [{'title':str(i)} for i in range(offset,min(600,offset+limit))]
        self.library=types.SimpleNamespace(jsonrpc_get_items=Mock(side_effect=query),
            prime_textures=Mock(),create_list_item=lambda row:(row,'',False),
            get_filtered_count=lambda filters:600,get_total_count=lambda:600)
        owner=types.SimpleNamespace(_has_filter_toolbar=False,_is_active=lambda:True,
            getProperty=lambda key:self.props.get(key,''),setProperty=lambda k,v:self.props.update({k:v}),
            bind_grid_items=lambda items,generation,append:self.binds.append((len(items),append)) or True)
        self.module=types.ModuleType('wall_prefetch_tests.wall_loader')
        spec=importlib.util.spec_from_file_location(self.module.__name__,LIB/'wall_loader.py')
        self.module=importlib.util.module_from_spec(spec)
        modules={
            'wall_prefetch_tests':types.ModuleType('wall_prefetch_tests'),
            'wall_prefetch_tests.video_library':self.library,
            'wall_prefetch_tests.catalog_cache':types.SimpleNamespace(revision=lambda:'one'),
            'wall_prefetch_tests.common':types.SimpleNamespace(log=lambda *a:None),
            'wall_prefetch_tests.window_handler':types.SimpleNamespace(_update_filter_availability=lambda:None),
            'xbmc':types.SimpleNamespace(Monitor=lambda:types.SimpleNamespace(abortRequested=lambda:False),LOGWARNING=2,LOGERROR=4),
            'xbmcgui':types.SimpleNamespace(Window=lambda _:types.SimpleNamespace(setProperty=lambda *a:None)),
            'xbmcaddon':types.SimpleNamespace(Addon=lambda _:types.SimpleNamespace(getSetting=lambda _:'0'))}
        self.import_patch=patch.dict(sys.modules,modules);self.import_patch.start()
        self.addCleanup(self.import_patch.stop)
        spec.loader.exec_module(self.module)
        self.loader=self.module.WallLoader(owner)

    def tearDown(self):
        self.loader.stop();self.loader.worker.join(2)

    def wait_for(self,predicate):
        deadline=time.monotonic()+2
        while not predicate():
            if time.monotonic()>deadline:self.fail('worker did not finish')
            threading.Event().wait(.005)

    def open_category(self):
        self.loader.refresh({'filter.mediatype':'movie'})
        self.wait_for(lambda:self.loader.prefetched_page is not None and not self.loader.prefetch_pending)

    def test_open_binds_once_and_prefetch_does_not_restart_the_visible_panel(self):
        self.open_category()
        self.assertEqual(self.binds,[(120,False)])
        self.assertEqual(self.loader.loaded,120)
        self.assertFalse(self.loader.pending)
        self.assertEqual(self.props['MFG.Panel.9999.State'],'ready')
        self.assertEqual(self.queries,[(0,120),(120,120)])
        self.assertTrue(all(isinstance(row,dict) for row in self.loader.prefetched_page[2]))

    def test_navigation_consumes_warmed_page_without_duplicate_database_request(self):
        self.open_category();self.loader.next_page()
        self.wait_for(lambda:self.loader.loaded==240 and self.loader.prefetched_page is not None and not self.loader.prefetch_pending)
        self.assertEqual(self.binds,[(120,False),(120,True)])
        self.assertEqual(self.queries,[(0,120),(120,120),(240,120)])

    def test_optional_prefetch_error_keeps_ready_page_and_normal_retry_available(self):
        self.open_category();self.loader.prefetched_page=None
        with patch.object(self.library,'jsonrpc_get_items',side_effect=RuntimeError('temporary')):
            self.loader._prefetch(self.loader.generation,self.loader.filters,120)
        self.assertEqual(self.props['MFG.Panel.9999.State'],'ready')
        self.assertIsNone(self.loader.prefetched_page)
        self.assertFalse(self.loader.pending)
        self.loader.next_page();self.wait_for(lambda:self.loader.loaded==240)
        self.assertEqual(self.binds[-1],(120,True))

    def test_refresh_discards_warmed_rows_from_previous_sort(self):
        self.open_category();generation=self.loader.generation
        self.loader.refresh({'filter.mediatype':'movie','filter.sort':'rating'})
        self.wait_for(lambda:self.loader.prefetched_page is not None and not self.loader.prefetch_pending)
        self.assertEqual(self.loader.prefetched_page[0],generation+1)
        self.assertEqual(self.binds,[(120,False),(120,False)])

    def test_stale_or_cancelled_prefetch_cannot_survive_close(self):
        self.open_category();generation=self.loader.generation
        self.loader.stop();self.loader.worker.join(2)
        self.loader._prefetch(generation,{},120)
        self.assertIsNone(self.loader.prefetched_page)

    def test_playback_revision_invalidates_warmed_metadata(self):
        self.open_category();old=self.loader.prefetched_page[2]
        with patch.object(self.module.catalog_cache,'revision',return_value='after playback'):
            new=self.loader._page_rows(self.loader.generation,self.loader.filters,120,120)
        self.assertIsNot(new,old)
        self.assertEqual(self.queries[-1],(120,120))
        self.assertIsNone(self.loader.prefetched_page)

    def test_initial_recommendations_use_gui_bridge_not_worker_controls(self):
        owner=self.loader.owner
        owner.getControl=lambda _:self.fail('Worker must not access home controls')
        owner.bind_home_rows=Mock(return_value=True)
        self.assertTrue(self.loader.populate(1004,[{'title':'One'}],self.loader.generation))
        owner.bind_home_rows.assert_called_once()

    def test_rebuilt_home_oninit_binds_directly_without_waiting_for_own_callback(self):
        owner=self.loader.owner
        owner.bind_home_rows=lambda *a:self.fail('onInit must not block on its own GUI queue')
        owner._apply_home_row=Mock(return_value=True)
        self.assertTrue(self.loader.populate(1004,[{'title':'One'}],self.loader.generation,on_gui=True))
        owner._apply_home_row.assert_called_once()

    def test_page_bind_interrupted_by_a_child_does_not_leave_navigation_pending(self):
        self.open_category();owner=self.loader.owner;bind=owner.bind_grid_items
        owner.bind_grid_items=lambda *a:False
        self.loader.next_page();self.wait_for(lambda:not self.loader.pending)
        self.assertEqual(self.loader.loaded,120)
        owner.bind_grid_items=bind
        self.loader.next_page();self.wait_for(lambda:self.loader.loaded==240)
        self.assertEqual(self.binds,[(120,False),(120,True)])

class InitialFocusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tree=ast.parse((LIB/'window_handler.py').read_text())
        window=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='FilterWindow')
        method=next(n for n in window.body if isinstance(n,ast.FunctionDef) and n.name=='_apply_grid_binds')
        cls.ns={'queue':queue,'threading':threading,'xbmc':types.SimpleNamespace(LOGERROR=4),'log':lambda *a:None}
        exec(compile(ast.Module(body=[method],type_ignores=[]),'grid_bind','exec'),cls.ns)

    def bind(self,focus):
        panel=types.SimpleNamespace(reset=lambda:None,addItems=lambda _:None,size=lambda:12)
        changes=[];loader=types.SimpleNamespace(lock=threading.RLock(),valid=lambda _:True)
        owner=types.SimpleNamespace(_grid_bind_queue=queue.Queue(),poster_loader=loader,_is_active=lambda:True,
            getControl=lambda _:panel,_grid_force_first_selection=False,_grid_pending_right=None,
            _grid_initial_focus=True,getFocusId=lambda:focus,setFocusId=changes.append)
        job={'loader':loader,'generation':1,'append':False,'items':list(range(12)),'done':threading.Event(),'ok':False}
        owner._grid_bind_queue.put(job);self.ns['_apply_grid_binds'](owner)
        self.assertTrue(job['ok']);self.assertTrue(job['done'].is_set());self.assertFalse(owner._grid_initial_focus)
        return changes

    def test_existing_default_focus_is_not_set_again(self):self.assertEqual(self.bind(9999),[])
    def test_toolbar_focus_moves_to_first_poster_once(self):self.assertEqual(self.bind(7200),[9999])

if __name__=='__main__':unittest.main()
