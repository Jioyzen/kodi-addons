"""Directory-first TV hierarchy and directory-scoped manual review regressions."""
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
from directory_listing import group_inventory,pending_reviews
from matching import merge_candidates
import tmdb_api


class HierarchyTests(unittest.TestCase):
    def test_tight_season_tag_does_not_pollute_english_name(self):
        p='/欧美剧/绝命律师S02.Better.Call.Saul.2016.2160p.WEB-DL/01.mkv'
        c=build_tv_contexts([p])[p]
        self.assertEqual(c['directory_titles'],['绝命律师','Better Call Saul'])
        self.assertEqual((c['season_number'],c['season_explicit']),(2,True))

    def test_chinese_named_season(self):
        p='/欧美剧/豺狼的日子.第一季/01.mkv'
        c=build_tv_contexts([p])[p]
        self.assertEqual((c['title'],c['season_number'],c['episodes']),('豺狼的日子',1,[1]))

    def test_nested_seasons_share_collection_and_keep_season_release_years(self):
        root='/欧美剧/剧名S01-S02.Show.Name.2019-2021/'
        paths=[root+'剧名S01.Show.Name.2019/01.mkv',root+'剧名S02.Show.Name.2021/01.mkv']
        c=build_tv_contexts(paths)
        for p in paths:self.assertEqual((c[p]['show_root'],c[p]['year']),(root,2019))
        self.assertEqual([c[p]['season_number'] for p in paths],[1,2])
        self.assertEqual([c[p]['season_year'] for p in paths],[2019,2021])
        self.assertEqual(len(group_inventory({p:{'status':'pending'} for p in paths})[0]),1)

    def test_plain_season_subfolder_with_release_tags(self):
        p='/shows/Show.Name.2020/Season.02.2021.1080p.WEB-DL/02.mkv'
        c=build_tv_contexts([p])[p]
        self.assertEqual(c['show_root'],'/shows/Show.Name.2020/')
        self.assertEqual(c['season_number'],2)

    def test_directory_identity_never_uses_conflicting_episode_title(self):
        p='/shows/Directory.Show.2020/Other.Show.S01E01.mkv'
        c=build_tv_contexts([p])[p]
        self.assertEqual(c['titles'],['Directory Show'])

    def test_standalone_seasons_remain_separate_physical_directories(self):
        paths=['/欧美剧/Show.S01.2020/01.mkv','/欧美剧/Show.S02.2021/01.mkv']
        self.assertEqual(len(group_inventory({p:{'status':'pending'} for p in paths})[0]),2)

    def test_root_level_episode_names_remain_independent(self):
        files={'/欧美剧/Alpha.S01E01.mkv':{'status':'pending'},'/欧美剧/Beta.S01E01.mkv':{'status':'pending'}}
        groups,plain=group_inventory(files,'tvshow')
        self.assertEqual(groups,{})
        self.assertEqual(len(plain),2)
        c=build_tv_contexts(files,'tvshow')
        self.assertEqual([c[p]['title'] for p in files],['Alpha','Beta'])

    def test_companion_feature_does_not_inherit_collection_identity(self):
        p='/shows/Show.S01-S05.2008-2012/Companion.Movie.2019/Movie.mkv'
        c=build_tv_contexts([p],'tvshow')[p]
        self.assertTrue(c['show_root'].endswith('/Companion.Movie.2019/'))
        self.assertEqual(c['title'],'Companion Movie')

    def test_confirmations_show_one_row_per_directory_and_keep_movies(self):
        files={'/欧美剧/Show.2020/%02d.mkv'%i:{'status':'pending'} for i in range(1,21)}
        files['/movies/Film.2020.mkv']={'status':'failed'}
        before=copy.deepcopy(files)
        rows=pending_reviews(files,'auto',lambda i:i.get('status') in ('pending','failed'))
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[0][1]['_review_count'],20)
        self.assertEqual(rows[0][1]['_review_show_root'],'/欧美剧/Show.2020/')
        self.assertEqual(files,before)

    def test_legacy_season_evidence_cannot_split_collection_review(self):
        root='/欧美剧/Show.S01-S02.2020-2021/'
        files={root+'Show.S%02d/01.mkv'%s:{'status':'pending','is_tvshow':True,'match_evidence':{'show_root':root+'Show.S%02d/'%s}} for s in (1,2)}
        rows=pending_reviews(files,'tvshow',lambda _:True)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0][1]['_review_show_root'],root)


class MatchEvidenceTests(unittest.TestCase):
    def details(self,count=10,season=1):
        return {'info':{'title':'Show','year':'2020'},'seasons':[{'season_number':season,'episode_count':count,'air_date':'2020-01-01'}]}

    def test_missing_requested_season_disambiguates_same_name(self):
        p='/shows/Show.S03/01.mkv';s=TvMatchSession([p],'test')
        candidates=[{'id':1,'name':'Show'},{'id':2,'name':'Show'}]
        with patch.object(tmdb_api,'search_tv_best',return_value=candidates),patch.object(tmdb_api,'get_tv_details',side_effect=lambda i,*_:self.details(season=3 if i==1 else 1)):
            self.assertEqual(s.resolve_show(s.context(p))['id'],1)

    def test_complete_source_coverage_disambiguates_same_name(self):
        paths=['/shows/Show/EP%02d.mkv'%i for i in range(1,17)];s=TvMatchSession(paths,'test','zh-CN','tvshow')
        with patch.object(tmdb_api,'search_tv_best',return_value=[{'id':1,'name':'Show'},{'id':2,'name':'Show'}]),patch.object(tmdb_api,'get_tv_details',side_effect=lambda i,*_:self.details(count=16 if i==1 else 4)):
            self.assertEqual(s.resolve_show(s.context(paths[0]))['id'],1)

    def test_equally_supported_shows_still_require_confirmation(self):
        p='/shows/Show.S01/01.mkv';s=TvMatchSession([p],'test')
        with patch.object(tmdb_api,'search_tv_best',return_value=[{'id':1,'name':'Show'},{'id':2,'name':'Show'}]),patch.object(tmdb_api,'get_tv_details',return_value=self.details()):
            with self.assertRaises(TvMatchError) as e:s.resolve_show(s.context(p))
        self.assertEqual(e.exception.reason,'ambiguous_show_identity')

    def test_official_english_translation_is_retained_by_id(self):
        korean={'id':1,'name':'尸战朝鲜','original_name':'킹덤','first_air_date':'2019-01-01'}
        def search(title,year,key,language):
            return [dict(korean,name='Kingdom')] if language=='en-US' else [korean]
        with patch.object(tmdb_api,'search_tv',side_effect=search):
            results=tmdb_api.search_tv_best('王国',2019,'Kingdom','test')
        self.assertIn('Kingdom',results[0]['_match_titles'])

    def test_unknown_slots_do_not_restart_per_season_year(self):
        paths=['/shows/Show.S01-S02/Show.S01.2020/Bonus.mkv','/shows/Show.S01-S02/Show.S02.2021/Bonus.mkv']
        s=TvMatchSession(paths,'test')
        self.assertEqual(sorted(s._local_slots().values()),[1,2])


class SupplementSeasonTests(unittest.TestCase):
    def session(self,paths):
        s=TvMatchSession(paths,'test')
        show={'id':1,'details':{'info':{'title':'Show'},'seasons':[{'season_number':1,'episode_count':2,'air_date':'2020-01-01'},
            {'season_number':2,'episode_count':2,'air_date':'2021-01-01'}]},'reason':'test'}
        s.resolve_show=lambda c:show
        return s

    def test_bonus_is_after_normal_episodes_in_same_season(self):
        paths=['/shows/Show.2020/S01E01.mkv','/shows/Show.2020/S01E02.mkv','/shows/Show.2020/Bonus.mkv']
        s=self.session(paths)
        with patch.object(tmdb_api,'get_tv_season_details',return_value={'episodes':{1:{},2:{}}}):
            prepared=s.prepare_file(paths[-1])
        self.assertEqual((prepared['plan']['season'],prepared['plan']['episodes']),(1,[3]))
        self.assertEqual(prepared['episodes'][0][2]['season_name'],'')
        self.assertEqual(prepared['episodes'][0][2]['uniqueids'],{})

    def test_nested_commentary_stays_in_its_source_season(self):
        paths=['/shows/Show.2020/Season 02/01.mkv','/shows/Show.2020/Season 02/解说/评论.mkv']
        s=self.session(paths)
        with patch.object(tmdb_api,'get_tv_season_details',return_value={'episodes':{1:{},2:{}}}):
            prepared=s.prepare_file(paths[-1])
        self.assertEqual((prepared['plan']['season'],prepared['plan']['episodes']),(2,[2]))

    def test_missing_official_episode_and_bonus_get_distinct_trailing_numbers(self):
        paths=['/shows/Show.2020/S01E01.mkv','/shows/Show.2020/S01E02.mkv','/shows/Show.2020/Bonus.mkv']
        s=self.session(paths)
        with patch.object(tmdb_api,'get_tv_season_details',return_value={'episodes':{1:{'info':{'title':'One'}}}}):
            results=[s.prepare_file(p) for p in paths]
        self.assertEqual([r['plan']['episodes'][0] for r in results],[1,2,3])
        self.assertTrue(all(r['plan']['season']==1 for r in results))

    def test_directory_listing_places_extras_at_end_of_each_season(self):
        from directory_listing import episode_order
        rows=[('bonus',{'season_number':1,'episode_number':3,'match_evidence':{'numbering_mode':'local_unmatched'}}),
              ('second',{'season_number':2,'episode_number':1}),('first',{'season_number':1,'episode_number':1})]
        self.assertEqual([p for p,i in sorted(rows,key=episode_order)],['first','bonus','second'])

    def test_multi_season_extras_have_independent_numbering(self):
        paths=['/shows/Show.2020/Season 01/01.mkv','/shows/Show.2020/Season 01/Bonus.mkv',
               '/shows/Show.2020/Season 02/01.mkv','/shows/Show.2020/Season 02/Bonus.mkv']
        s=self.session(paths)
        with patch.object(tmdb_api,'get_tv_season_details',return_value={'episodes':{1:{},2:{}}}):
            a=s.prepare_file(paths[1]);b=s.prepare_file(paths[3])
        self.assertEqual((a['plan']['season'],a['plan']['episodes']),(1,[2]))
        self.assertEqual((b['plan']['season'],b['plan']['episodes']),(2,[2]))


class AdditionalSafetyTests(unittest.TestCase):
    def test_all_seasons_use_whole_directory_coverage_to_disambiguate(self):
        paths=['/shows/Show/Show.S%02dE%02d.mkv'%(season,episode) for season in (1,2) for episode in range(1,9)]
        s=TvMatchSession(paths,'test')
        def details(identity,*_):
            return {'info':{'title':'Show'},'seasons':[{'season_number':1,'episode_count':16 if identity==1 else 1,'air_date':None}]}
        with patch.object(tmdb_api,'search_tv_best',return_value=[{'id':1,'name':'Show'},{'id':2,'name':'Show'}]),patch.object(tmdb_api,'get_tv_details',side_effect=details):
            self.assertEqual(s.resolve_show(s.context(paths[-1]))['id'],1)

    def test_only_independent_directory_can_fall_back_to_movie(self):
        movie='/shows/Companion.Movie.2020/Movie.mkv'
        episode='/shows/Show.2020/E01.mkv'
        extra='/shows/Show.2020/Bonus.mkv'
        s=TvMatchSession([movie,episode,extra],'test')
        self.assertTrue(s._standalone_directory(s.context(movie)))
        self.assertFalse(s._standalone_directory(s.context(episode)))
        self.assertFalse(s._standalone_directory(s.context(extra)))

    def test_tv_network_failure_cannot_be_reclassified_as_movie(self):
        p='/shows/Show.2020/Unknown.mkv';s=TvMatchSession([p],'test')
        with patch.object(tmdb_api,'search_tv_best',return_value=[{'id':1,'name':'Show','first_air_date':'2020-01-01'}]),patch.object(tmdb_api,'get_tv_details',return_value=None),patch.object(s,'_resolve_standalone_movie',side_effect=AssertionError('No movie fallback on network error')):
            with self.assertRaises(TvMatchError) as e:s.prepare_file(p)
        self.assertEqual(e.exception.reason,'show_details_unavailable')

    def test_independent_movie_uses_existing_strict_movie_matcher(self):
        p='/shows/Companion.Movie.2020/Movie.mkv';s=TvMatchSession([p],'test')
        with patch.object(s,'resolve_show',side_effect=TvMatchError('show_identity_unverified','missing')),patch.object(s,'_resolve_standalone_movie',return_value={'details':{'info':{'title':'Movie'}}}):
            self.assertEqual(s.prepare_file(p)['status'],'movie_ready')

    def test_review_dialog_dispatches_show_directory_not_representative_episode(self):
        from test_scanner_show_matching import functions
        root='/shows/Show.2020/';history={'directories':{'/shows/':{'files':{root+'E01.mkv':{'status':'pending'},root+'E02.mkv':{'status':'pending'}}}}}
        calls=[];labels=[];selections=iter((0,-1))
        dialog=types.SimpleNamespace(select=lambda heading,items: (labels.extend(items) or next(selections)))
        ns=functions(['_collect_pending','review_pending'],{
            'load_history':lambda:history,'_needs_scan':lambda i:i['status']=='pending',
            'xbmcgui':types.SimpleNamespace(Dialog=lambda:dialog),
            'xbmc':types.SimpleNamespace(executebuiltin=lambda *a:None),'os':os,
            'manual_match_show':lambda *a:calls.append(a),
            'manual_match':lambda *a:self.fail('Should not match a single episode'),
            'STATUS_LABELS':{'pending':'待确认'},'L':lambda _:'待确认 (%d)'})
        ns['review_pending']()
        self.assertEqual(len(calls),1)
        self.assertNotIn('E01.mkv',labels[0])
        self.assertIn('2 个待处理视频',labels[0])


if __name__=='__main__':unittest.main()
