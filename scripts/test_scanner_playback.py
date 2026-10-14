"""Single-file click playback and favourites resolver tests; no video/network I/O."""
import ast
import json
import types
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode,parse_qsl
import sys
SOURCE=Path(__file__).resolve().parents[1]/'addons/script.ai.movie.scanner/python'
sys.path.insert(0,str(SOURCE))
from path_display import private_query,resolve_query,redact_text


class Tag:
    def __init__(self):self.values={}
    def __getattr__(self,name):return lambda value:self.values.update({name:value})


class Item:
    def __init__(self,*args,**kwargs):self.args=args;self.kwargs=kwargs;self.props={};self.context=[];self.tag=Tag()
    def setProperty(self,key,value):self.props[key]=value
    def getVideoInfoTag(self):return self.tag
    def setLabel2(self,*args):pass
    def setArt(self,*args):pass
    def addContextMenuItems(self,items):self.context.extend(items)


def namespace(history,handle=-1):
    played=[];resolved=[];listed=[]
    n={'load_history':lambda:history,'addon_handle':handle,'redact_text':redact_text,'ADDON_NAME':'Test','L':lambda key:'手动匹配' if key==32029 else '',
       'xbmcgui':types.SimpleNamespace(ListItem=Item,Dialog=lambda:types.SimpleNamespace(notification=lambda *args:None),NOTIFICATION_WARNING=1),
       'xbmc':types.SimpleNamespace(Player=lambda:types.SimpleNamespace(play=lambda path,**kwargs:played.append((path,kwargs['listitem'])))),
       'xbmcplugin':types.SimpleNamespace(setResolvedUrl=lambda *args:resolved.append(args),addDirectoryItems=lambda handle,rows,*args:listed.extend(rows),setPluginCategory=lambda *args:None,endOfDirectory=lambda *args,**kwargs:None),
       'os':__import__('os'),'_needs_scan':lambda _:False,
       'build_url':lambda query:'plugin://script.ai.movie.scanner/?'+urlencode(private_query(query)),
       'run_plugin_url':lambda query:'RunScript(script.ai.movie.scanner,?'+urlencode(private_query(query))+')','directory_label':lambda _: 'Movies[SMB]','STATUS_LABELS':{'pending':'Pending','success':'Matched'},
       'json':json,'UI_LISTING':lambda rows,title:listed.extend(rows)}
    tree=ast.parse((SOURCE/'main.py').read_text())
    nodes=[node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name in ('play_media_file','view_directory','context_menu','emit_listing')]
    exec(compile(ast.Module(body=nodes,type_ignores=[]),'playback','exec'),n)
    n.update(played=played,resolved=resolved,listed=listed)
    return n


class PlaybackTests(unittest.TestCase):
    def setUp(self):
        self.source='smb://test-user:test-secret@example.invalid/Movies/'
        self.path=self.source+'Movie.2020.mkv'
        self.info={'status':'success','movie_id':42,'tmdb_title':'Movie'}
        self.history={'directories':{self.source:{'files':{self.path:self.info}}}}

    def call(self,n,path=None):
        with patch.dict(sys.modules,{'file_scanner':types.SimpleNamespace(VIDEO_EXTENSIONS={'.mkv','.strm','.iso'})}):
            return n['play_media_file'](self.source,path or self.path)

    def test_menu_click_plays_exact_selected_encode_once(self):
        alternate=self.source+'Movie.2020.alternate.mkv';self.history['directories'][self.source]['files'][alternate]=dict(self.info)
        n=namespace(self.history)
        self.assertTrue(self.call(n,alternate))
        self.assertEqual([path for path,item in n['played']],[alternate])
        self.assertEqual(n['played'][0][1].tag.values['setDbId'],42)
        self.assertEqual(n['resolved'],[])

    def test_favourite_resolves_one_file_without_starting_another_player(self):
        n=namespace(self.history,handle=7)
        self.assertTrue(self.call(n))
        self.assertEqual(n['played'],[])
        handle,success,item=n['resolved'][0]
        self.assertEqual((handle,success,item.kwargs['path']),(7,True,self.path))

    def test_unmatched_video_can_play_without_fabricated_database_id(self):
        self.info.clear();self.info['status']='pending'
        n=namespace(self.history);self.assertTrue(self.call(n))
        self.assertNotIn('setDbId',n['played'][0][1].tag.values)

    def test_tv_and_combined_file_keep_correct_episode_identity(self):
        self.info.update(is_tvshow=True,season_number=2,episode_number=3,episode_ids=[93,94],show_id=12)
        n=namespace(self.history);self.assertTrue(self.call(n))
        values=n['played'][0][1].tag.values
        self.assertEqual((values['setMediaType'],values['setDbId'],values['setSeason'],values['setEpisode']),('episode',93,2,3))
        self.assertEqual(len(n['played']),1)

    def test_removed_or_unmanaged_file_never_starts_playback(self):
        n=namespace(self.history);self.assertFalse(self.call(n,self.source+'Removed.mkv'))
        self.assertEqual(n['played'],[])
        n=namespace(self.history,handle=3);self.assertFalse(self.call(n,self.source+'Removed.mkv'))
        self.assertFalse(n['resolved'][0][1])

    def test_bluray_directory_resolves_disc_entry_without_listing_share(self):
        folder=self.source+'Movie.2020.BluRay';self.history['directories'][self.source]['files'][folder]=dict(self.info)
        n=namespace(self.history);self.assertTrue(self.call(n,folder))
        self.assertEqual(n['played'][0][0],folder+'/BDMV/index.bdmv')

    def test_file_menu_is_single_command_and_hides_native_play_from_here(self):
        n=namespace(self.history);n['view_directory'](self.source)
        url,item,folder=n['listed'][0]
        self.assertFalse(folder)
        self.assertEqual(item.props['IsPlayable'],'false')
        self.assertEqual(item.props['CanQueue'],'false')
        self.assertEqual(item.context[0][0],'手动匹配')
        query=dict(parse_qsl(url.split('?',1)[1]));self.assertEqual(query['action'],'play_file')
        self.assertNotIn('test-secret',url);self.assertNotIn('example.invalid',url)
        self.assertEqual(resolve_query(query,self.history)['filepath'],self.path)


if __name__=='__main__':unittest.main(verbosity=2)
