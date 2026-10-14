"""Continue watching contains individual bookmarks, never aggregate sets/shows."""
import ast
import importlib
import queue
import sqlite3
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch,Mock
LIB=Path(__file__).resolve().parents[1]/'addons/plugin.video.filteredmovies/lib'
package=types.ModuleType('wall_continue_tests');package.__path__=[str(LIB)]
sys.modules.setdefault('wall_continue_tests',package)
updates=importlib.import_module('wall_continue_tests.home_updates')

class ContinueTests(unittest.TestCase):
    def setUp(self):
        tree=ast.parse((LIB/'video_library.py').read_text())
        methods=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in
                 ('_read_continue_watching_items','get_continue_watching_items')]
        self.calls=[]
        fields={'movieid':'idMovie','title':'c00','runtime':'c11','playcount':'playCount',
                'lastplayed':'lastPlayed','resume':'resumeTimeInSeconds','_resume_total':'totalTimeInSeconds'}
        def format_item(row):
            row['resume']={'position':float(row.get('resume') or 0),'total':float(row.pop('_resume_total',0) or 0)}
        def art(conn,ids,media,rows,key):
            self.calls.append(media)
            for row in rows:row['art']={'poster':'show-poster' if media=='tvshow' else 'poster',
                'thumb':'episode-still','fanart':'show-background'}
        db=types.SimpleNamespace(_conn=self.database,_build_props=lambda _:fields,_format_item=format_item,_load_cast=lambda *a:None)
        self.patch=patch.dict(sys.modules,{'wall_continue_tests.db':db});self.patch.start();self.addCleanup(self.patch.stop)
        self.ns={'__package__':'wall_continue_tests','DB_PATH':'unused','_load_art_batch':art,
                 'log':lambda *a:None,'xbmc':types.SimpleNamespace(LOGERROR=4)}
        exec(compile(ast.Module(body=methods,type_ignores=[]),'continue_sql','exec'),self.ns)

    def database(self):
        c=sqlite3.connect(':memory:')
        c.execute('CREATE TABLE movie_view(idMovie,c00,c11,playCount,lastPlayed,resumeTimeInSeconds,totalTimeInSeconds,idSet)')
        c.executemany('INSERT INTO movie_view VALUES(?,?,?,?,?,?,?,?)',[
            (1,'Spider-Man',7200,0,'2026-10-14 15:00',100,7200,10),
            (2,'Finished',7200,1,'2026-10-14 16:00',0,7200,10)])
        c.execute('CREATE TABLE episode_view(idEpisode,idShow,c00,c01,c05,c09,c12,c13,strTitle,genre,studio,mpaa,rating,playCount,lastPlayed,dateAdded,strPath,strFileName,resumeTimeInSeconds,totalTimeInSeconds)')
        c.executemany('INSERT INTO episode_view VALUES('+','.join('?'*20)+')',[
            (77,5,'Third','Plot','2020-01-01',3600,1,3,'Show','Drama','','',7,0,'2026-10-14 14:00','','local://','third.mkv',30,3600),
            (78,5,'Fourth','Plot','2020-01-02',3600,1,4,'Show','Drama','','',7,0,'2026-10-14 16:00','','local://','fourth.mkv',60,3600),
            (79,5,'Not started','Plot','2020-01-03',3600,1,5,'Show','Drama','','',7,0,'2026-10-14 17:00','','local://','fifth.mkv',0,3600),
            (80,5,'Finished','Plot','2020-01-04',3600,1,6,'Show','Drama','','',7,1,'2026-10-14 18:00','','local://','sixth.mkv',100,3600)])
        return c

    def test_collection_is_not_queried_and_each_episode_keeps_its_own_identity(self):
        rows=self.ns['_read_continue_watching_items'](30)
        self.assertEqual([(r['media_type'],r.get('movieid') or r.get('episodeid')) for r in rows],
                         [('episode',78),('movie',1),('episode',77)])
        self.assertNotIn('set',self.calls);self.assertEqual(sum(r['media_type']=='movie' for r in rows),1)
        episode=rows[0]
        self.assertEqual((episode['season'],episode['episode'],episode['tvshowid']),(1,4,5))
        self.assertEqual(episode['art']['landscape'],'episode-still')
        self.assertEqual(episode['art']['poster'],'show-poster')
        self.assertEqual(episode['resume']['position'],60)

    def test_mixed_movie_and_episode_limit_is_applied_after_sort(self):
        rows=self.ns['_read_continue_watching_items'](2)
        self.assertEqual(len(rows),2);self.assertEqual(rows[0]['episodeid'],78)

    def test_old_aggregate_widget_cache_is_not_reused(self):
        seen=[]
        cache=types.SimpleNamespace(revision=lambda:'same-db',key=lambda filters,limit,offset:seen.append(filters) or 'new-key',
            get=lambda *a:[],put=lambda *a:None)
        with patch.dict(sys.modules,{'wall_continue_tests.catalog_cache':cache}):
            self.assertEqual(self.ns['get_continue_watching_items'](30),[])
        self.assertEqual(seen,[{'widget':'continue-movie-episode-v2'}])

    def test_episode_home_progress_diff_uses_concrete_bookmark(self):
        row={'media_type':'episode','episodeid':78,'runtime':3600,'resume':{'position':900,'total':3600},'playcount':0}
        state=updates.playback_fields(row)
        self.assertEqual((state['type'],state['id'],state['percent']),('episode',78,25))
        self.assertEqual(updates.row_key(row),('episode',78))

    def test_zero_limit_does_not_query_the_library(self):
        self.ns['_read_continue_watching_items']=lambda *a:self.fail('No read required')
        cache=types.SimpleNamespace()
        with patch.dict(sys.modules,{'wall_continue_tests.catalog_cache':cache}):
            self.assertEqual(self.ns['get_continue_watching_items'](0),[])

    def test_episode_click_resumes_exact_episode_and_info_opens_its_season(self):
        tree=ast.parse((LIB/'window_handler.py').read_text())
        cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='FilterWindow')
        method=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='_show_selected')
        self.ns.update(HOME_ENTRIES={},notification=Mock())
        exec(compile(ast.Module(body=[method],type_ignores=[]),'continue_action','exec'),self.ns)
        props={'MFG.TvshowId':'5','MFG.Season':'1','MFG.ShowTitle':'Show'}
        item=types.SimpleNamespace(getProperty=lambda k:props.get(k,''),getArt=lambda _:'',
            getVideoInfoTag=lambda:types.SimpleNamespace(getMediaType=lambda:'episode',getDbId=lambda:78))
        owner=types.SimpleNamespace(getControl=lambda _:types.SimpleNamespace(getSelectedItem=lambda:item),
            _capture_navigation_state=Mock())
        rpc=Mock();open_show=Mock()
        with patch.dict(sys.modules,{'wall_continue_tests.tv_catalog':types.SimpleNamespace(rpc=rpc),
                    'wall_continue_tests.tv_details':types.SimpleNamespace(open_tvshow=open_show)}):
            self.ns['_show_selected'](owner,1002)
            owner._capture_navigation_state.assert_called_once()
            rpc.assert_called_once_with('Player.Open',{'item':{'episodeid':78},'options':{'resume':True}})
            rpc.reset_mock();self.ns['_show_selected'](owner,1002,True)
            rpc.assert_not_called()
            self.assertEqual(open_show.call_args.args,(5,))
            self.assertEqual(open_show.call_args.kwargs['initial_season'],1)

if __name__=='__main__':unittest.main()
