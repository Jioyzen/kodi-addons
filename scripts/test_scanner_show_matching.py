"""Directory-level TV matching scope, remembered identity and menu behavior."""
import ast
import copy
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch
SOURCE=Path(__file__).resolve().parents[1]/'addons/script.ai.movie.scanner/python'
sys.path.insert(0,str(SOURCE))
from tv_context import build_tv_contexts
from tv_matcher import TvMatchSession
from path_display import path_token
from test_scanner_playback import namespace


def functions(names,namespace):
    tree=ast.parse((SOURCE/'main.py').read_text())
    nodes=[node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name in names]
    for node in nodes:node.decorator_list=[]
    exec(compile(ast.Module(body=nodes,type_ignores=[]),'show_matching','exec'),namespace)
    return namespace


class ShowMenuTests(unittest.TestCase):
    def history(self):
        files={}
        for name in ('Alpha','Beta'):
            for number in (1,2):
                path='/shows/%s.2020/S01E%02d.mkv'%(name,number)
                files[path]={'status':'success','is_tvshow':True,'season_number':1,'episode_number':number}
        return {'directories':{'/shows/':{'files':files}}}

    def test_show_cards_have_match_command_but_episode_rows_do_not(self):
        history=self.history();n=namespace(history)
        n['view_directory']('/shows/')
        self.assertEqual(len(n['listed']),2)
        for url,item,folder in n['listed']:
            self.assertTrue(folder)
            self.assertEqual(item.context[0][0],'手动匹配')
            self.assertIn('manual_match_show',item.context[0][1])
            self.assertNotIn('filepath',item.context[0][1])
        n['listed'].clear();n['view_directory']('/shows/',path_token('/shows/Alpha.2020/'))
        for url,item,folder in n['listed']:
            self.assertFalse(folder);self.assertEqual(item.context,[])
            self.assertIn('play_file',url)

    def test_representative_selection_uses_requested_show_only(self):
        history=self.history();calls=[]
        n=functions(['_tv_directory_files','manual_match_show'],{
            'load_history':lambda:history,'_is_busy':lambda:False,
            'manual_match':lambda *args,**kwargs:calls.append((args,kwargs))})
        n['manual_match_show']('/shows/',path_token('/shows/Beta.2020/'))
        self.assertEqual(calls[0][1]['show_root'],'/shows/Beta.2020/')
        self.assertIn('/Beta.2020/',calls[0][0][0])
        self.assertEqual(calls[0][0][1],'/shows/')
        n['manual_match_show']('/shows/','removed-group')
        self.assertEqual(len(calls),1)

    def test_directory_apply_includes_supplements_not_neighboring_shows_or_movies(self):
        history=self.history();files=history['directories']['/shows/']['files']
        extra='/shows/Alpha.2020/Extras/Interview.mkv';files[extra]={'status':'pending'}
        film='/movies/Film.2020.mkv';files[film]={'status':'success','movie_id':12}
        scans=[];saves=[]
        n=functions(['_manual_tv_apply'],{'load_history':lambda:history,'save_history':lambda h:saves.append(copy.deepcopy(h)),
            'ADDON':types.SimpleNamespace(getSetting=lambda key:'test'),'os':os,
            '_run_scan':lambda work,dirs,session:scans.append((work,dirs,session)),
            'xbmc':types.SimpleNamespace(executebuiltin=lambda *args:None),
            'xbmcgui':types.SimpleNamespace(Dialog=lambda:types.SimpleNamespace(select=lambda *args,**kwargs:self.fail('Directory matching must not offer a single episode'))),
            'L':lambda _: '', 'ADDON_NAME':'Test'})
        first='/shows/Alpha.2020/S01E01.mkv';context=build_tv_contexts(files)[first]
        with patch.object(TvMatchSession,'prepare_file',return_value={'status':'ready'}),patch.object(TvMatchSession,'resolve_show',return_value={'details':{'info':{'title':'Confirmed Alpha'}}}):
            n['_manual_tv_apply'](first,'/shows/',91,context,apply_all=True)
        targets={path for path,folder in scans[0][0]}
        self.assertEqual(targets,{first,'/shows/Alpha.2020/S01E02.mkv',extra})
        self.assertEqual(scans[0][1],['/shows/'])
        self.assertEqual(scans[0][2].forced_ids,{'/shows/Alpha.2020/':91})
        remembered=saves[0]['directories']['/shows/']['tv_matches'][path_token('/shows/Alpha.2020/')]
        self.assertEqual(remembered,{'tmdb_id':91,'title':'Confirmed Alpha'})


class RememberedIdentityTests(unittest.TestCase):
    def test_new_episodes_inherit_directory_match_without_researching_title(self):
        files={'/media/Alias.2020/01.mkv':{'status':'success'},'/media/Alias.2020/02.mkv':{'status':'pending'}}
        s=TvMatchSession([],'test')
        s.register_source(files,'auto',{path_token('/media/Alias.2020/'):{'tmdb_id':91,'title':'Confirmed'}})
        import tmdb_api
        with patch.object(tmdb_api,'get_tv_details',return_value={'info':{'title':'Confirmed'}}),patch.object(tmdb_api,'search_tv_best',side_effect=AssertionError('Already confirmed')):
            for path in files:self.assertEqual(s.resolve_show(s.context(path))['id'],91)

    def test_unknown_names_still_follow_confirmed_directory(self):
        path='/shows/01.mkv';s=TvMatchSession([],'test')
        s.register_source({path:{'status':'pending'}},'auto',{path_token('/shows/'):{'tmdb_id':91,'title':'Confirmed'}})
        self.assertEqual(s.context(path)['title'],'Confirmed')
        self.assertEqual(s.forced_ids,{'/shows/':91})

    def test_movie_source_does_not_inherit_tv_overrides(self):
        path='/movies/Alias.2020/Film.mkv';s=TvMatchSession([],'test')
        s.register_source({path:{'status':'pending'}},'movie',{path_token('/movies/Alias.2020/'):{'tmdb_id':91,'title':'Confirmed'}})
        self.assertEqual(s.contexts,{});self.assertEqual(s.forced_ids,{})

    def test_other_show_roots_do_not_receive_confirmed_identity(self):
        files={'/shows/Alpha.2020/S01E01.mkv':{},'/shows/Beta.2020/S01E01.mkv':{}}
        s=TvMatchSession([],'test');s.register_source(files,'auto',{path_token('/shows/Alpha.2020/'):{'tmdb_id':91,'title':'Confirmed'}})
        self.assertNotIn('/shows/Beta.2020/',s.forced_ids)


if __name__=='__main__':unittest.main(verbosity=2)
