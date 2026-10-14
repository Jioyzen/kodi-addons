"""Show/season identities, bounded reads and modal worker lifetime regressions."""
import importlib.util
import queue
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

LIB = Path(__file__).resolve().parents[1] / 'addons/plugin.video.filteredmovies/lib'
spec = importlib.util.spec_from_file_location('wall_tv_catalog_tests', LIB / 'tv_catalog.py')
catalog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(catalog)


class CatalogTests(unittest.TestCase):
    def test_native_null_seasons_and_episodes_are_empty_not_failures(self):
        def query(method,params):
            if method.endswith('GetTVShowDetails'):return {'tvshowdetails':{'title':'Empty'}}
            if method.endswith('GetSeasons'):return {'seasons':None}
            return {'episodes':None,'limits':{'total':0}}
        self.assertEqual(catalog.load_show(5,query)['seasons'],[])
        self.assertEqual(catalog.load_episodes(5,1,query=query)['episodes'],[])
    def test_seasons_are_local_sorted_and_keep_unknown_season_names(self):
        def query(method, params):
            if method.endswith('GetTVShowDetails'):
                return {'tvshowdetails': {'title': '示例剧'}}
            if method.endswith('GetSeasons'):
                return {'seasons': [{'season': 2, 'title': '未知集', 'episode': 3},
                                    {'season': -1, 'episode': 20}, {'season': 9, 'episode': 0},
                                    {'season': 1, 'title': 'Season 1', 'episode': 2}]}
            return {'episodes': []}
        result = catalog.load_show(5, query)
        self.assertEqual([s['season'] for s in result['seasons']], [1, 2])
        self.assertEqual([catalog.season_title(s) for s in result['seasons']], ['第 1 季', '未知集'])
        self.assertIsNone(result['next'])

    def test_unwatched_selection_keeps_resume_and_requested_season(self):
        calls = []
        def query(method, params):
            calls.append(params)
            return {'episodes': [{'episodeid': 91, 'episode': 4, 'resume': {'position': 20}}]}
        episode = catalog.next_episode(5, 2, query)
        self.assertEqual(catalog.play_label(episode), '第 4 集')
        self.assertEqual(calls[0]['season'], 2)
        self.assertEqual(calls[-1]['filter']['field'], 'playcount')

    def test_latest_resume_beats_an_earlier_unwatched_gap(self):
        calls = []
        def query(method, params):
            calls.append(params)
            if params['sort']['method'] == 'lastplayed':
                return {'episodes': [{'episodeid': 94, 'episode': 4, 'lastplayed': '2026-10-13',
                                      'resume': {'position': 70}, 'playcount': 0}]}
            return {'episodes': [{'episodeid': 91, 'episode': 1}]}
        self.assertEqual(catalog.next_episode(5, 1, query)['episodeid'], 94)
        self.assertEqual(len(calls), 1)

    def test_finished_latest_episode_advances_instead_of_replaying_a_gap(self):
        def query(method, params):
            if params['sort']['method'] == 'lastplayed':
                return {'episodes': [{'episodeid': 94, 'season': 1, 'episode': 4,
                    'lastplayed': '2026-10-13', 'resume': {'position': 0}, 'playcount': 1}]}
            self.assertEqual(params['filter']['value'], '4')
            return {'episodes': [{'episodeid': 95, 'episode': 5}]}
        self.assertEqual(catalog.next_episode(5, 1, query)['episodeid'], 95)

    def test_fully_watched_show_can_be_started_again(self):
        def query(method, params):
            return {'episodes': [] if 'filter' in params else [{'episodeid': 8, 'episode': 1}]}
        self.assertEqual(catalog.next_episode(5, query=query)['episodeid'], 8)

    def test_large_season_is_paginated_without_requesting_media_paths(self):
        calls = []
        def query(method, params):
            calls.append(params)
            return {'episodes': [], 'limits': {'total': 500}}
        data = catalog.load_episodes(5, 1, 80, query)
        self.assertEqual(data['total'], 500)
        self.assertEqual(calls[0]['limits'], {'start': 80, 'end': 160})
        self.assertNotIn('file', calls[0]['properties'])
        self.assertEqual(calls[0]['season'], 1)

    def test_season_watch_changes_only_ids_returned_for_that_season(self):
        writes = []
        def query(method, params):
            if method.endswith('GetEpisodes'):
                self.assertEqual((params['tvshowid'], params['season']), (5, 2))
                return {'episodes': [{'episodeid': 8}, {'episodeid': 9}], 'limits': {'total': 2}}
            writes.append((method, params))
            return {}
        catalog.set_watched(5, 1, 2, query)
        self.assertEqual(writes, [('VideoLibrary.SetEpisodeDetails', {'episodeid': 8, 'playcount': 1}),
                                  ('VideoLibrary.SetEpisodeDetails', {'episodeid': 9, 'playcount': 1})])

    def test_resolution_and_missing_metadata_have_safe_fallbacks(self):
        self.assertEqual(catalog.resolution({'streamdetails': {'video': [{'height': 2160}]}}), '4K')
        self.assertEqual(catalog.resolution({}), '')
        self.assertEqual(catalog.play_label(None), '暂无可播放单集')
        self.assertEqual(catalog.season_title({'season': 0}), '特别篇')

    def test_episode_copy_is_one_block_with_three_lines_and_ellipsis(self):
        text = catalog.episode_summary({'episode': 1, 'title': '标题', 'plot': '剧情介绍' * 60})
        self.assertEqual(len(text.splitlines()), 3)
        self.assertTrue(text.endswith('…'))
        self.assertTrue(text.startswith('1. 标题'))

    def test_short_episode_copy_is_not_unnecessarily_truncated(self):
        self.assertEqual(catalog.episode_summary({'episode': 2, 'title': '单集', 'plot': '简介'}), '2. 单集 简介')

    def test_actor_cache_candidates_reuse_sizes_without_media_share_access(self):
        candidates = catalog.actor_image_candidates({'thumbnail': 'image://https%3A%2F%2Fimage.tmdb.org%2Ft%2Fp%2Fw185%2Fface.jpg/'})
        self.assertIn('https://image.tmdb.org/t/p/h632/face.jpg', candidates)
        self.assertEqual(catalog.actor_image_candidates({'thumbnail': '/local/avatar.jpg'}), ['/local/avatar.jpg'])


def window_module():
    package = types.ModuleType('wall_tv_test')
    package.__path__ = [str(LIB)]
    sends = []
    mods = {'wall_tv_test': package,
            'wall_tv_test.common': types.SimpleNamespace(ADDON_PATH='test', log=lambda *a: None, notification=lambda *a: None),
            'wall_tv_test.tv_catalog': catalog,
            'wall_tv_test.video_library': types.SimpleNamespace(prime_textures=lambda *a: None),
            'xbmc': types.SimpleNamespace(LOGERROR=3, executebuiltin=sends.append, Monitor=object,getInfoLabel=lambda _:''),
            'xbmcgui': types.SimpleNamespace(WindowXML=object,getCurrentWindowId=lambda:13000)}
    spec = importlib.util.spec_from_file_location('wall_tv_test.tv_details', LIB / 'tv_details.py')
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, mods):
        spec.loader.exec_module(module)
    return module, sends


class LifetimeTests(unittest.TestCase):
    def test_return_keeps_existing_native_episode_list_and_horizontal_position(self):
        module,_=window_module();owner=module.TvDetailsWindow()
        module.xbmc.getInfoLabel=lambda _:'80'
        owner.getControl=lambda _:types.SimpleNamespace(size=lambda:80,
            reset=lambda:self.fail('Do not reset a surviving native episode list'))
        owner._ensure_panel(301,list(range(80)),lambda _:self.fail('Do not rebuild ListItems'))

    def test_native_recreated_panel_rebuilds_even_when_python_items_survive(self):
        module,_=window_module();owner=module.TvDetailsWindow()
        module.xbmc.getInfoLabel=lambda _:'0';reset=[];items=[];selected=[]
        owner.getControl=lambda _:types.SimpleNamespace(size=lambda:80,reset=lambda:reset.append(True),
            addItems=items.extend,selectItem=selected.append)
        owner._ensure_panel(301,list(range(80)),lambda row:row,9)
        self.assertEqual(reset,[True]);self.assertEqual(len(items),80);self.assertEqual(selected,[9])
    def test_playback_return_selection_is_consumed_once_without_refocusing_same_button(self):
        module,_=window_module();owner=module.TvDetailsWindow();owner.worker=object()
        owner.show_data={'show':{},'seasons':[]};owner._playback_selection=(201,0)
        owner._show_header=lambda _:None;owner._bind_actors=lambda:None
        owner._apply_results=lambda:None;owner._queue_progress=lambda:None
        owner.setProperty=lambda *a:None;owner.getFocusId=lambda:201
        owner.setFocusId=lambda _:self.fail('Same native focus should be left intact')
        owner.getControl=lambda _:types.SimpleNamespace(reset=lambda:None,addItems=lambda _:None,selectItem=lambda _:None)
        owner.onInit()
        self.assertIsNone(owner._playback_selection)

    def test_actor_return_does_not_reselect_same_avatar_and_consumes_state(self):
        module,_=window_module();owner=module.TvDetailsWindow();owner.window_id=13000
        owner._navigation_state=(303,2);owner.getFocusId=lambda:303
        owner.setFocusId=lambda _:self.fail('Redundant refocus')
        owner.getControl=lambda _:types.SimpleNamespace(size=lambda:8,getSelectedPosition=lambda:2,
            selectItem=lambda _:self.fail('Redundant reselect'))
        owner._restore_navigation_state();owner._restore_navigation_state()
        self.assertIsNone(owner._navigation_state)
    def test_malformed_notifications_do_not_interrupt_episode_monitor(self):
        module,_=window_module();owner=module.TvDetailsWindow();owner._last_played_id=78
        calls=[];owner._queue_progress=lambda:calls.append(True)
        monitor=module._LibraryMonitor(owner)
        for data in ('null','[]','{"item":null}','{"item":[]}','invalid'):
            monitor.onNotification('Kodi','VideoLibrary.OnUpdate',data)
        self.assertEqual(calls,[])
        monitor.onNotification('Kodi','VideoLibrary.OnUpdate','{"item":{"type":"episode","id":78}}')
        self.assertEqual(calls,[True])
    def test_cached_cast_refresh_updates_in_place_without_threads_or_source_mutation(self):
        module,_=window_module();owner=module.TvDetailsWindow()
        source=[{'name':'Actor','thumbnail':'/cache/actor.jpg','role':'Role'}]
        owner.show_data={'show':{'cast':source}}
        items=[];reset=[]
        class Item:
            def __init__(self,*a,**k):self.art={}
            def setContentLookup(self,_):pass
            def setArt(self,art):self.art.update(art)
            def getArt(self,key):return self.art.get(key,'')
            def setProperty(self,*a):pass
        module.xbmcgui.ListItem=Item
        panel=types.SimpleNamespace(size=lambda:len(items),getSelectedPosition=lambda:0,
            reset=lambda:reset.append(True) or items.clear(),addItems=items.extend,
            selectItem=lambda _:None,getListItem=items.__getitem__)
        module.xbmc.getInfoLabel=lambda _:str(len(items))
        owner.getControl=lambda _:panel;owner.setProperty=lambda *a:None
        owner._image=lambda *urls:urls[0]
        owner._bind_actors();owner._bind_actors()
        self.assertEqual(len(reset),1)
        self.assertNotIn('thumbnail_candidates',source[0])
        self.assertFalse(hasattr(owner,'portrait_worker'))
    def test_season_request_has_final_target_before_background_job(self):
        module,_=window_module();owner=module.TvDetailsWindow()
        owner.show_id=5;owner.season={'season':2}
        properties={};enabled=[]
        owner.setProperty=properties.__setitem__
        owner.getControl=lambda _:types.SimpleNamespace(setEnabled=enabled.append,
            setLabel=lambda _:self.fail('Do not replace native label layers'))
        episode={'episodeid':78,'episode':4}
        with patch.object(module,'read_playback',return_value=(True,episode)) as snapshot:
            owner._request('season')
        snapshot.assert_called_once_with('tvshow',5,2)
        self.assertIs(owner.next_episode,episode)
        self.assertEqual(properties['MFG.Tv.PlayLabel'],'第 4 集')
        self.assertEqual(properties['MFG.Tv.Message'],'')
        self.assertEqual(enabled,[True])

    def test_unavailable_snapshot_does_not_guess_or_retain_previous_season(self):
        module,_=window_module();owner=module.TvDetailsWindow()
        owner.show_id=5;owner.season={'season':2};owner.next_episode={'episodeid':1}
        properties={};enabled=[]
        owner.setProperty=properties.__setitem__
        owner.getControl=lambda _:types.SimpleNamespace(setEnabled=enabled.append)
        with patch.object(module,'read_playback',return_value=(False,None)):
            owner._request('season')
        self.assertIsNone(owner.next_episode)
        self.assertEqual(properties['MFG.Tv.PlayLabel'],'')
        self.assertEqual(enabled,[False])

    def test_empty_favourites_do_not_interrupt_show_progress_or_play_button(self):
        module,_=window_module();owner=module.TvDetailsWindow()
        owner.show_id=5;owner.generation=1;owner._initial_focus=False;owner.window_id=13000
        properties={};enabled=[]
        owner.setProperty=properties.__setitem__
        owner._show_header=lambda _:None;owner._bind_actors=lambda:None
        panel=types.SimpleNamespace(reset=lambda:None,addItems=lambda _:None,
            setLabel=lambda _:None,setEnabled=enabled.append)
        owner.getControl=lambda _:panel
        next_episode={'episodeid':78,'episode':3}
        owner.results.put((1,'show',{'show':{'title':'Show'},'seasons':[{'season':1}],
            'next':next_episode},['season item'],False))
        with patch.object(catalog,'rpc',return_value={'favourites':None}):owner._apply_results()
        self.assertIs(owner.next_episode,next_episode)
        self.assertEqual(properties['MFG.Tv.State'],'ready')
        self.assertEqual(properties['MFG.Tv.Favourite'],'false')
        self.assertTrue(enabled[-1])

    def test_child_or_player_keeps_pending_results_off_inactive_controls(self):
        module,_=window_module();owner=module.TvDetailsWindow();owner.window_id=13001
        owner.results.put((0,'show',{},[],False))
        owner.getControl=lambda _:self.fail('Inactive native controls must not be accessed')
        owner._apply_results()
        self.assertEqual(owner.results.qsize(),1)
    def test_closing_during_library_query_cannot_send_to_a_reused_window(self):
        module, sends = window_module()
        owner = module.TvDetailsWindow()
        owner.show_id, owner.window_id, owner.generation = 5, 13000, 1
        started, release = threading.Event(), threading.Event()
        def load(_id):
            started.set()
            release.wait(2)
            return {'show': {}, 'seasons': []}
        module.catalog = types.SimpleNamespace(load_show=load)
        worker = threading.Thread(target=owner._worker)
        worker.start()
        owner.jobs.put((1, 'show', None, 0))
        self.assertTrue(started.wait(1))
        owner.closed.set()
        release.set()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(sends, [])
        self.assertTrue(owner.results.empty())

    def test_back_from_season_invalidates_old_load_without_exiting_show(self):
        module, _ = window_module()
        owner = module.TvDetailsWindow()
        owner.generation, owner.season = 2, {'season': 1}
        owner.show_data = {'next': {'episodeid': 8, 'episode': 1}}
        properties, focus = {}, []
        owner.setProperty = properties.__setitem__
        owner.setFocusId = focus.append
        panel = types.SimpleNamespace(setLabel=lambda *a: None, setEnabled=lambda *a: None,
                                      selectItem=lambda *a: None, reset=lambda: None,
                                      setPosition=lambda *a: None, setWidth=lambda *a: None)
        owner.getControl = lambda _: panel
        owner._return_show()
        self.assertEqual(owner.generation, 3)
        self.assertIsNone(owner.season)
        self.assertEqual(properties['MFG.Tv.SeasonPage'], 'false')
        self.assertEqual(focus, [300])

    def test_failed_child_still_clears_parent_modal_guard_and_restores_focus(self):
        module, _ = window_module()
        events = []
        parent = types.SimpleNamespace(getProperty=lambda _: '',
            setProperty=lambda *a: events.append('guard'), clearProperty=lambda *a: events.append('clear'),
            _capture_navigation_state=lambda: events.append('capture'), _restore_navigation_state=lambda: events.append('restore'))
        class Child:
            def __init__(self, *a): pass
            def doModal(self): raise RuntimeError('XML failed')
            def cleanup(self): events.append('cleanup')
        module.TvDetailsWindow = Child
        with self.assertRaises(RuntimeError):
            module.open_tvshow(5, parent=parent)
        self.assertEqual(events, ['capture', 'guard', 'cleanup', 'clear', 'restore'])


if __name__ == '__main__':
    unittest.main()
