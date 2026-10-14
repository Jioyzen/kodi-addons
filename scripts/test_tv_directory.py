"""Supplemental TV content, bounded listings and private-path regressions."""
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
sys.modules.setdefault('xbmc',types.SimpleNamespace(LOGINFO=1,LOGERROR=2,LOGWARNING=3,log=lambda *a,**k:None))
from tv_context import build_tv_contexts
from tv_matcher import TvMatchSession,TvMatchError
from path_display import directory_label,private_query,resolve_query,redact_text,path_token
from directory_listing import listing_page,PAGE_SIZE,episode_order


def metadata():
    return {'info':{'title':'示例剧','year':'2020'},'uniqueids':{'tmdb':'42'},
            'seasons':[{'season_number':1,'episode_count':2,'air_date':'2020-01-01'}],
            'available_art':{},'cast':[]}


class UnknownTests(unittest.TestCase):
    def session(self,paths,history=None):
        s=TvMatchSession(paths,'test',previous_results=history)
        s._resolve_show=lambda c:{'id':42,'details':metadata(),'reason':'verified_test_directory'}
        return s

    def test_extra_child_directory_belongs_to_parent_show(self):
        p='/shows/示例剧.2020/花絮/访谈01.mkv'
        c=build_tv_contexts([p])[p]
        self.assertEqual((c['title'],c['kind'],c['show_root']),('示例剧','extra','/shows/示例剧.2020/'))
        plan=self.session([p]).prepare_file(p)['plan']
        self.assertEqual((plan['mode'],plan['season']),('local_unmatched',1))

    def test_numbered_interview_is_not_assigned_official_plot(self):
        p='/shows/示例剧.2020/01 访谈.mkv'
        prepared=self.session([p]).prepare_file(p)
        ep=prepared['episodes'][0][2]
        self.assertEqual(ep['season_name'],'')
        self.assertEqual(ep['uniqueids'],{})
        self.assertIn('01 访谈',ep['info']['title'])

    def test_all_unrecognizable_files_still_attach_to_verified_show(self):
        paths=['/shows/示例剧.2020/'+n+'.mkv' for n in ('开篇','结尾','删减片段')]
        s=self.session(paths)
        for p in paths:
            prepared=s.prepare_file(p)
            self.assertEqual(prepared['status'],'ready')
            self.assertEqual(prepared['plan']['show']['id'],42)

    def test_out_of_range_number_becomes_local_unknown(self):
        p='/shows/示例剧.2020/E99.mkv'
        result=self.session([p]).prepare_file(p)
        self.assertEqual(result['plan']['mode'],'local_unmatched')
        self.assertEqual(result['plan']['local_reason'],'episode_number_out_of_range')

    def test_missing_official_episode_becomes_local_unknown(self):
        import tmdb_api
        p='/shows/示例剧.2020/E01.mkv'
        with patch.object(tmdb_api,'get_tv_season_details',return_value={'episodes':{}}):
            result=self.session([p]).prepare_file(p)
        self.assertEqual(result['plan']['local_reason'],'episode_not_found')

    def test_unknown_order_is_natural_not_worker_order(self):
        paths=['/shows/示例剧.2020/访谈%d.mkv'%i for i in (10,2,1)]
        s=self.session(paths)
        numbers={p:s.prepare_file(p)['plan']['episodes'][0] for p in paths}
        self.assertEqual([numbers[p] for p in reversed(paths)],[1,2,3])

    def test_new_supplement_is_inserted_in_natural_order(self):
        p='/shows/示例剧.2020/访谈2.mkv'
        history={p:{'match_evidence':{'numbering_mode':'local_unmatched','local_unknown_episode':7}}}
        s=self.session([p,'/shows/示例剧.2020/访谈1.mkv'],history)
        self.assertEqual(s.prepare_file(p)['plan']['episodes'],[2])
        self.assertEqual(s.prepare_file('/shows/示例剧.2020/访谈1.mkv')['plan']['episodes'],[1])

    def test_directory_identity_beats_conflicting_filename_alias(self):
        import tmdb_api
        p='/shows/示例剧.2020/Other.Show.S01E01.mkv'
        s=TvMatchSession([p],'test')
        def search(title,*args):
            self.assertEqual(title,'示例剧')
            return [{'id':42,'name':'示例剧','first_air_date':'2020-01-01'}]
        with patch.object(tmdb_api,'search_tv_best',side_effect=search),patch.object(tmdb_api,'get_tv_details',return_value=metadata()):
            self.assertEqual(s.resolve_show(s.context(p))['id'],42)

    def test_extra_inherits_show_directory_season_evidence(self):
        paths=['/shows/还珠格格2.1999/S02E01.mkv','/shows/还珠格格2.1999/Bonus.mkv']
        c=build_tv_contexts(paths)[paths[1]]
        self.assertEqual((c['season_number'],c['season_explicit']),(2,True))

    def test_transient_network_failure_does_not_replace_verified_data(self):
        import tmdb_api
        p='/shows/示例剧.2020/E01.mkv'
        with patch.object(tmdb_api,'get_tv_season_details',return_value=None):
            with self.assertRaises(TvMatchError) as error:self.session([p]).prepare_file(p)
        self.assertEqual(error.exception.reason,'season_metadata_unavailable')

    def test_numbered_show_folder_can_be_detected_outside_tv_category(self):
        paths=['/media/示例剧.2020/01.mkv','/media/示例剧.2020/02.mkv']
        self.assertEqual(len(build_tv_contexts(paths)),2)
        movies=['/media/合集.2020/01 Film.1999.mkv','/media/合集.2020/02 Film.2021.mkv']
        self.assertEqual(build_tv_contexts(movies),{})

    def test_old_skipped_tv_content_can_be_rescanned_after_upgrade(self):
        tree=ast.parse((SOURCE/'main.py').read_text())
        function=next(node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name=='_needs_scan')
        namespace={};exec(compile(ast.Module(body=[function],type_ignores=[]),'scan_policy','exec'),namespace)
        self.assertTrue(namespace['_needs_scan']({'status':'skipped','is_tvshow':True}))
        self.assertFalse(namespace['_needs_scan']({'status':'skipped'}))
        self.assertFalse(namespace['_needs_scan']({'status':'success','is_tvshow':True}))


class PrivacyTests(unittest.TestCase):
    def setUp(self):
        self.path='smb://test-user:test-secret@example.invalid/share/%E5%9B%BD%E4%BA%A7%E5%89%A7/'
        self.file=self.path+'示例剧.2020/01.mkv'
        self.history={'directories':{self.path:{'files':{self.file:{'status':'success'}}}}}

    def test_label_is_only_directory_and_protocol(self):
        self.assertEqual(directory_label(self.path),'国产剧[SMB]')
        self.assertEqual(directory_label('/storage/媒体/国产剧/'),'国产剧[本地]')

    def test_plugin_query_has_no_credentials_or_media_path(self):
        q=private_query({'action':'manual_match','dirpath':self.path,'filepath':self.file})
        self.assertNotIn('test-secret',str(q));self.assertNotIn('example.invalid',str(q))
        resolved=resolve_query(q,self.history)
        self.assertEqual(resolved['dirpath'],self.path);self.assertEqual(resolved['filepath'],self.file)

    def test_removed_directory_token_does_not_resolve_to_another_source(self):
        resolved=resolve_query(private_query({'dirpath':self.path,'filepath':self.file}),{'directories':{}})
        self.assertEqual((resolved['dirpath'],resolved['filepath']),('',''))

    def test_exception_urls_are_redacted(self):
        text=redact_text('Cannot open '+self.path)
        self.assertNotIn('test-secret',text);self.assertNotIn('example.invalid',text)


class ListingTests(unittest.TestCase):
    def files(self,shows,episodes):
        return {('/shows/剧%d.2020/E%03d.mkv'%(s,e)):
                {'status':'success','is_tvshow':True,'season_number':1,'episode_number':e,
                 'match_evidence':{'type':'tvshow','numbering_mode':'verified_tmdb_episodes'}}
                for s in range(shows) for e in range(1,episodes+1)}

    def test_thousands_of_episodes_list_only_show_directories(self):
        result=listing_page(self.files(20,200))
        self.assertEqual((result['total'],len(result['rows'])),(20,20))
        self.assertTrue(all(row[0]=='group' for row in result['rows']))

    def test_single_show_is_paginated_without_missing_or_repeated_files(self):
        files=self.files(1,220)
        rows=[]
        for page in range(3):
            result=listing_page(files,page=page);rows.extend(result['rows'])
            self.assertLessEqual(len(result['rows']),PAGE_SIZE)
        self.assertEqual(len({row[1] for row in rows}),220)
        self.assertEqual(rows[0][2]['episode_number'],1)
        self.assertEqual(rows[-1][2]['episode_number'],220)

    def test_mixed_library_keeps_movies_and_tv_groups(self):
        files=self.files(2,3);files['/movies/Film.2020.mkv']={'status':'success','movie_id':1}
        result=listing_page(files)
        self.assertEqual([row[0] for row in result['rows']],['group','group','file'])

    def test_pending_tv_inventory_is_grouped_before_matching(self):
        files={'/media/国产剧/剧%d.2020/01.mkv'%n:{'status':'pending'} for n in range(12)}
        result=listing_page(files)
        self.assertTrue(all(row[0]=='group' for row in result['rows']))

    def test_unknown_items_are_after_official_episodes(self):
        files=self.files(1,3)
        p='/shows/剧0.2020/Bonus.mkv'
        files[p]={'status':'success','is_tvshow':True,'season_number':2,'episode_number':1,
                  'match_evidence':{'numbering_mode':'local_unmatched'}}
        self.assertEqual(listing_page(files)['rows'][-1][1],p)

    def test_group_tokens_select_exact_show(self):
        result=listing_page(self.files(3,3),group=path_token('/shows/剧1.2020/'))
        self.assertEqual(result['total'],3)
        self.assertTrue(all('/剧1.' in row[1] for row in result['rows']))


if __name__=='__main__':unittest.main(verbosity=2)
