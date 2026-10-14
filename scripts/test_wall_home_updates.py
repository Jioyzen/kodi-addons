"""Home reuse and bounded row diffs preserve usable posters and selection."""
import ast
import importlib
import queue
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import Mock
LIB=Path(__file__).resolve().parents[1]/'addons/plugin.video.filteredmovies/lib'
package=types.ModuleType('wall_home_tests');package.__path__=[str(LIB)]
sys.modules.setdefault('wall_home_tests',package)
updates=importlib.import_module('wall_home_tests.home_updates')


def movie(identity,position=0):
    return {'media_type':'movie','movieid':identity,'title':'Movie '+str(identity),'runtime':1000,
        'resume':{'position':position,'total':1000},'playcount':0,'art':{'poster':'poster-'+str(identity)}}

class Item:
    def __init__(self,row):
        self.row=dict(row);self.art=dict(row.get('art',{}));self.props={};self.count=0;self.resume=None
        media,identity=updates.row_key(row)
        self.tag=types.SimpleNamespace(getMediaType=lambda:media,getDbId=lambda:identity,
            setPlaycount=lambda n:setattr(self,'count',n),setResumePoint=lambda p,t:setattr(self,'resume',(p,t)),
            setTitle=lambda n:self.row.update(title=n),setYear=lambda n:self.row.update(year=n),
            setPlot=lambda n:self.row.update(plot=n),setRating=lambda n:self.row.update(rating=n))
    def getVideoInfoTag(self):return self.tag
    def getProperty(self,key):return self.row.get('view_all','') if key=='MFG.ViewAll' else self.props.get(key,'')
    def setProperty(self,key,value):self.props[key]=value
    def getArt(self,key):return self.art.get(key,'')
    def setArt(self,art):self.art.update(art)
    def setLabel(self,label):self.row['title']=label

class Panel:
    def __init__(self,rows,position=0):self.items=[Item(r) for r in rows];self.position=position;self.resets=0;self.binds=0
    def size(self):return len(self.items)
    def getSelectedPosition(self):return self.position
    def getListItem(self,position):return self.items[position]
    def reset(self):self.items=[];self.position=0;self.resets+=1
    def addItems(self,items):self.items.extend(items);self.position=0;self.binds+=1
    def selectItem(self,position):self.position=position

class RowTests(unittest.TestCase):
    def test_progress_only_update_never_rebinds_the_row(self):
        old=[movie(1),movie(2,100),movie(3)];rows=[movie(1),movie(2,250),movie(3)]
        panel=Panel(old,2);original=panel.items[1]
        self.assertFalse(updates.sync_row(panel,old,rows,[Item(r) for r in rows]))
        self.assertEqual((panel.resets,panel.binds,panel.position),(0,0,2))
        self.assertIs(panel.items[1],original);self.assertEqual(original.props['SkinPercentPlayed'],'25')

    def test_new_continue_item_retains_previous_selected_movie_identity(self):
        old=[movie(1),movie(2),movie(3)];rows=[movie(9)]+old;panel=Panel(old,2)
        self.assertTrue(updates.sync_row(panel,old,rows,[Item(r) for r in rows]))
        self.assertEqual(panel.position,3);self.assertEqual(updates.item_key(panel.items[3]),('movie',3))
        self.assertEqual((panel.resets,panel.binds),(1,1))

    def test_finished_movie_removal_uses_nearest_remaining_item(self):
        old=[movie(1),movie(2),movie(3)];rows=old[:2];panel=Panel(old,2)
        updates.sync_row(panel,old,rows,[Item(r) for r in rows])
        self.assertEqual(panel.position,1)
        updates.sync_row(panel,rows,[],[]);self.assertEqual(panel.size(),0)

    def test_cached_art_and_metadata_can_update_without_reset(self):
        old=[movie(1)];rows=[dict(movie(1),title='Updated',year=2026)]
        template=Item(rows[0]);template.art['poster']='special://cached/one.jpg';panel=Panel(old)
        updates.sync_row(panel,old,rows,[template])
        self.assertEqual(panel.items[0].art['poster'],'special://cached/one.jpg')
        self.assertEqual(panel.items[0].row['title'],'Updated');self.assertEqual(panel.items[0].row['year'],2026)
        self.assertEqual(panel.resets,0)

    def test_tv_and_collection_progress_keep_existing_semantics(self):
        state=updates.playback_fields({'media_type':'tvshow','tvshowid':5,'episode':10,'watchedepisodes':2,'partial_progress':.5})
        self.assertEqual(state['percent'],25)
        state=updates.playback_fields({'media_type':'set','setid':5,'total':4,'watched':1,'partial_progress':.5})
        self.assertEqual(state['percent'],37)
        self.assertIsNone(updates.playback_fields({'media_type':'home_more','view_all':'movie'}))

class HomeReuseTests(unittest.TestCase):
    def setUp(self):
        self.revision='after playback';self.props={'MFG.Home':'true','MFG.Loaded':'true'}
        rows={1002:[movie(2,100)],1004:[movie(1),movie(2)],1005:[],1006:[],1007:[],1008:[]}
        self.panels={cid:Panel(value) for cid,value in rows.items()}
        self.owner=types.SimpleNamespace(getProperty=lambda k:self.props.get(k,''),
            setProperty=lambda k,v:self.props.update({k:v}),getControl=self.panels.__getitem__,_is_active=lambda:True)
        self.loader=types.SimpleNamespace(closed=threading.Event(),owner=self.owner,home_rows=rows,
            home_revision='before playback',home_refresh_pending=False,generation=1,lock=threading.RLock(),
            requests=queue.Queue(),valid=lambda generation:generation==1, pending=False,populate=Mock(return_value=True))
        self.ns={'queue':queue,'catalog_cache':types.SimpleNamespace(revision=lambda:self.revision),
            'library':types.SimpleNamespace(prime_textures=lambda _:None,create_list_item=lambda row:(Item(row),'',False)),
            'log':lambda *a:None,'xbmc':types.SimpleNamespace(LOGWARNING=2)}
        tree=ast.parse((LIB/'wall_loader.py').read_text())
        cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='WallLoader')
        constant=next(n for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='HOME_ROWS' for t in n.targets))
        methods=[n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name in ('restore_home','_update_home')]
        exec(compile(ast.Module(body=[constant]+methods,type_ignores=[]),'home_reuse','exec'),self.ns)
        self.ns['_home_row_items']=lambda media,limit:next(value for cid,value in rows.items() if self.ns['HOME_ROWS'][cid][0]==media)
        self.bound=[]
        def bind(cid,fresh,items,generation):
            self.bound.append(cid);updates.sync_row(self.panels[cid],self.loader.home_rows.get(cid,[]),fresh,items)
            self.loader.home_rows[cid]=fresh
            return True
        self.owner.bind_home_rows=bind

    def test_playback_database_change_does_not_blank_cached_home(self):
        self.assertTrue(self.ns['restore_home'](self.loader))
        self.assertEqual(self.props['MFG.Loaded'],'true');self.loader.populate.assert_not_called()
        self.assertEqual(self.loader.requests.get_nowait()[1],'home_update')
        self.assertTrue(all(panel.resets==0 for panel in self.panels.values()))

    def test_late_oninit_coalesces_background_home_update(self):
        self.ns['restore_home'](self.loader);self.ns['restore_home'](self.loader)
        self.assertEqual(self.loader.requests.qsize(),1)

    def test_unchanged_home_needs_no_background_read(self):
        self.loader.home_revision=self.revision
        self.assertTrue(self.ns['restore_home'](self.loader));self.assertTrue(self.loader.requests.empty())

    def test_background_update_keeps_unchanged_recent_rows_and_marks_revision(self):
        self.ns['_update_home'](self.loader,1)
        self.assertEqual(self.loader.home_revision,self.revision)
        self.assertEqual(self.props['MFG.Loaded'],'true')
        self.assertEqual(len(self.bound),6);self.assertFalse(self.loader.home_refresh_pending)
        self.assertTrue(all(panel.resets==0 for panel in self.panels.values()))

    def test_update_failure_retains_cached_rows_and_allows_next_return_to_retry(self):
        self.owner.bind_home_rows=lambda *a:False
        self.ns['_update_home'](self.loader,1)
        self.assertEqual(self.loader.home_revision,'before playback')
        self.assertEqual(self.props['MFG.Loaded'],'true');self.assertFalse(self.loader.home_refresh_pending)
        self.assertTrue(self.ns['restore_home'](self.loader))

    def test_partial_home_cache_is_reusable_and_missing_rows_fill_in_background(self):
        self.loader.home_rows={1002:self.loader.home_rows[1002],1004:self.loader.home_rows[1004]}
        self.loader.home_revision=None
        self.assertTrue(self.ns['restore_home'](self.loader))
        self.loader.populate.assert_not_called();self.assertEqual(self.loader.requests.qsize(),1)
        self.ns['_update_home'](self.loader,1)
        self.assertEqual(len(self.loader.home_rows),6)

if __name__=='__main__':unittest.main()
