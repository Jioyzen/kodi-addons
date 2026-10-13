"""TV parser/matcher/transaction regressions. No Kodi or network required."""
import ast
import copy
import concurrent.futures
import os
import queue
import sqlite3
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / 'addons/script.ai.movie.scanner/python'
sys.path.insert(0, str(SOURCE))
sys.modules['xbmc'] = types.SimpleNamespace(LOGINFO=1, LOGERROR=2, LOGWARNING=3, log=lambda *a, **k: None)
sys.modules['xbmcvfs'] = types.SimpleNamespace(translatePath=lambda _: '/nonexistent/addons')
sys.modules['ai_database'] = types.SimpleNamespace(current_database=lambda _: ':memory:')
from tv_context import build_tv_contexts, episode_marker
from tv_matcher import TvMatchSession, TvMatchError
import tvshow_db
import tmdb_api
import ai_parser
import movie_matcher


def show(identity=1, title='Test Show', year=2020, count=4):
    return {'info': {'title': title, 'year': str(year), 'premiered': '%d-01-01' % year},
            'uniqueids': {'tmdb': str(identity)}, 'ratings': {'tmdb': {'rating': 8, 'votes': 20, 'default': True}},
            'seasons': [{'season_number': 1, 'episode_count': count, 'air_date': '%d-01-01' % year}],
            'available_art': {}, 'cast': []}


def episode(number):
    return {'info': {'title': 'Episode %d' % number, 'plot': 'Plot %d' % number, 'aired': '2020-01-01'},
            'uniqueids': {'tmdb': str(100+number)}, 'ratings': {'tmdb': {'rating': 7, 'votes': 10, 'default': True}},
            'available_art': {}, 'cast': []}


def season(count=4):
    return {'episodes': {n: episode(n) for n in range(1, count+1)}}


def functions(*names, **namespace):
    tree = ast.parse((SOURCE / 'main.py').read_text())
    body = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    for n in body:
        n.decorator_list = []
    exec(compile(ast.Module(body=body, type_ignores=[]), str(SOURCE/'main.py'), 'exec'), namespace)
    return namespace


class ContextTests(unittest.TestCase):
    def test_numeric_episode_uses_parent_show_title(self):
        p = '/shows/西游记.1986/01 猴王初问世.mkv'
        c = build_tv_contexts([p])[p]
        self.assertEqual((c['title'], c['year'], c['episodes'], c['episode_title']), ('西游记', 1986, [1], '猴王初问世'))

    def test_numbered_movie_collection_is_not_tv(self):
        self.assertEqual(build_tv_contexts(['/movies/Collection/01 The Mummy.1999.mkv']), {})

    def test_explicit_movie_directory_disables_tv(self):
        self.assertEqual(build_tv_contexts(['/shows/Test.S01E01.mkv'], 'movie'), {})

    def test_unknown_tv_identity_is_reviewable(self):
        p = '/shows/01.mkv'
        c = build_tv_contexts([p], 'tvshow')[p]
        self.assertEqual((c['kind'], c['reason']), ('review', 'show_title_missing'))

    def test_season_directory_and_chinese_episode(self):
        p = '/shows/Show.2020/第二季/第十二集.mkv'
        c = build_tv_contexts([p])[p]
        self.assertEqual((c['season_number'], c['episodes'], c['show_root']), (2, [12], '/shows/Show.2020/'))

    def test_official_specials_season_zero(self):
        p = '/shows/Show.2020/Specials/01.mkv'
        self.assertEqual(build_tv_contexts([p])[p]['season_number'], 0)

    def test_combined_chained_and_invalid_ranges(self):
        self.assertEqual(episode_marker('Show.S01E01-E04.mkv')['episodes'], [1, 2, 3, 4])
        self.assertEqual(episode_marker('Show.S01E01E03.mkv')['episodes'], [1, 3])
        self.assertEqual(episode_marker('Show.S01E05-E02.mkv')['error'], 'invalid_episode_range')

    def test_bonus_not_a_fictional_movie(self):
        p = '/shows/Show.2020/Bonus.mkv'
        self.assertEqual(build_tv_contexts([p])[p]['kind'], 'extra')
        self.assertEqual(build_tv_contexts(['/movies/Bonus.2020.mkv']), {})

    def test_root_level_shows_have_independent_coverage(self):
        paths = ['/shows/Alpha.S01E01.mkv', '/shows/Beta.S01E09.mkv']
        contexts = build_tv_contexts(paths)
        self.assertEqual(contexts[paths[0]]['source_episodes'], [1])
        self.assertEqual(contexts[paths[1]]['source_episodes'], [9])

    def test_mixed_explicit_and_numeric_episodes_share_show_context(self):
        paths=['/media/Test Show.2020/S01E01.mkv','/media/Test Show.2020/02.mkv']
        self.assertEqual(build_tv_contexts(paths)[paths[1]]['episodes'],[2])

    def test_large_inventory_is_complete(self):
        paths = ['/shows/Show.%d/S01E%02d.mkv' % (y, e) for y in range(1900, 2000) for e in range(1, 51)]
        self.assertEqual(len(build_tv_contexts(paths)), 5000)


class MatcherTests(unittest.TestCase):
    def setUp(self):
        self.path = '/shows/Test Show.2020/E01.mkv'
        self.session = TvMatchSession([self.path], 'test')
        self.context = self.session.context(self.path)
        self.details = show()
        self.candidates = [{'id': 1, 'name': 'Test Show', 'first_air_date': '2020-01-01'}]

    def resolve(self, candidates=None):
        with patch.object(tmdb_api, 'search_tv_best', return_value=candidates or self.candidates), patch.object(tmdb_api, 'get_tv_details', return_value=self.details):
            return self.session.resolve_show(self.context)

    def test_exact_identity(self):
        self.assertEqual(self.resolve()['id'], 1)

    def test_same_name_same_year_is_pending(self):
        with self.assertRaises(TvMatchError) as error:
            self.resolve(self.candidates + [{'id': 2, 'name': 'Test Show', 'first_air_date': '2020-01-01'}])
        self.assertEqual(error.exception.reason, 'ambiguous_show_identity')

    def test_wrong_partial_title_is_rejected(self):
        with self.assertRaises(TvMatchError):
            self.resolve([{'id': 2, 'name': 'Another Test Show', 'first_air_date': '2020-01-01'}])

    def test_year_mismatch_is_rejected(self):
        with self.assertRaises(TvMatchError):
            self.resolve([{'id': 1, 'name': 'Test Show', 'first_air_date': '1990-01-01'}])

    def test_later_season_year_is_verified(self):
        p = '/shows/还珠格格2.1999/S02E01.mkv'
        session = TvMatchSession([p], 'test')
        details = show(title='还珠格格', year=1998)
        details['seasons'].append({'season_number': 2, 'episode_count': 48, 'air_date': '1999-01-01'})
        with patch.object(tmdb_api, 'search_tv_best', return_value=[{'id': 1, 'name': '还珠格格', 'first_air_date': '1998-01-01'}]), patch.object(tmdb_api, 'get_tv_details', return_value=details):
            self.assertEqual(session.resolve_show(session.context(p))['id'], 1)

    def test_concurrent_show_resolution_is_single_flight(self):
        with patch.object(tmdb_api, 'search_tv_best', return_value=self.candidates) as search, patch.object(tmdb_api, 'get_tv_details', return_value=self.details) as detail:
            with concurrent.futures.ThreadPoolExecutor(8) as pool:
                ids = list(pool.map(lambda _: self.session.resolve_show(self.context)['id'], range(32)))
            self.assertEqual(ids, [1]*32)
            self.assertEqual(detail.call_count, 1)
            self.assertEqual(search.call_count, 2)  # year + unfiltered

    def test_missing_episode_is_not_fabricated(self):
        with patch.object(tmdb_api, 'get_tv_season_details', return_value={'episodes': {}}):
            with self.assertRaises(TvMatchError) as error:
                self.session.episode_plan(self.context, {'id': 1, 'details': self.details})
            self.assertEqual(error.exception.reason, 'episode_not_found')

    def test_full_absolute_range_can_rebase(self):
        paths = ['/shows/Test Show.2020/E%d-E%d.mkv' % (a,a+1) for a in (5,7)]
        session = TvMatchSession(paths, 'test')
        with patch.object(tmdb_api, 'get_tv_season_details', return_value=season()):
            plan = session.episode_plan(session.context(paths[0]), {'id': 1, 'details': self.details})
        self.assertEqual((plan['episodes'], plan['offset']), ([1,2], 4))

    def test_incomplete_absolute_range_requires_confirmation(self):
        p = '/shows/Test Show.2020/E05-E06.mkv'
        session = TvMatchSession([p], 'test')
        with self.assertRaises(TvMatchError) as error:
            session.episode_plan(session.context(p), {'id': 1, 'details': self.details})
        self.assertEqual(error.exception.reason, 'episode_number_out_of_range')

    def test_edited_version_does_not_borrow_original_episode_plot(self):
        p = '/shows/Test Show.2020.精编版/E01.mkv'
        session = TvMatchSession([p], 'test')
        with patch.object(session, 'resolve_show', return_value={'id': 1, 'details': self.details}):
            prepared = session.prepare_file(p)
        self.assertEqual(prepared['plan']['mode'], 'local_edited_version')
        ep = prepared['episodes'][0][2]
        self.assertEqual(ep['uniqueids'], {})
        self.assertEqual(ep['info']['aired'], '')
        self.assertNotIn('Plot 1', ep['info']['plot'])


class WriterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/'MyVideos149.db'
        self.db = tvshow_db.TvShowDatabase(str(self.path));self.db.connect()
        self.db.conn.executescript((Path(__file__).parent/'fixtures/kodi22_tv_schema.sql').read_text())
        tvshow_db._SHOW_METADATA_CACHE.clear()
        self.file = '/shows/Test Show.2020/S01E01.mkv'

    def tearDown(self):
        self.db.close();self.tmp.cleanup()

    def test_combined_file_and_repeat_are_atomic_and_idempotent(self):
        details = [(1,n,episode(n)) for n in range(1,5)]
        first = self.db.save_episodes(show(), details, self.file)
        self.assertEqual(len(first[1]), 4)
        self.assertEqual(self.db.save_episodes(show(),details,self.file), first)
        self.assertEqual(self.db.conn.execute('SELECT COUNT(*) FROM episode').fetchone()[0], 4)

    def test_invalid_second_episode_rolls_back_entire_file(self):
        result = self.db.save_episodes(show(),[(1,1,episode(1)),(1,0,episode(2))],self.file)
        self.assertEqual(result,(None,[]))
        self.assertEqual(self.db.conn.execute('SELECT COUNT(*) FROM tvshow').fetchone()[0],0)
        self.assertEqual(self.db.conn.execute('SELECT COUNT(*) FROM files').fetchone()[0],0)

    def test_alternate_encode_does_not_overwrite_file(self):
        a = self.db.save_tvshow(show(),episode(1),1,1,self.file)
        b = self.db.save_tvshow(show(),episode(1),1,1,self.file.replace('.mkv','.mp4'))
        self.assertEqual(a[0],b[0]);self.assertNotEqual(a[1],b[1])
        self.assertEqual(self.db.conn.execute('SELECT COUNT(DISTINCT idFile) FROM episode').fetchone()[0],2)

    def test_playback_state_survives_numbering_correction(self):
        sid,ids = self.db.save_episodes(show(),[(1,4,episode(4))],self.file)
        fid = self.db.conn.execute('SELECT idFile FROM episode').fetchone()[0]
        self.db.conn.execute("UPDATE files SET playCount=2,dateAdded='2010-01-01',lastPlayed='2020-01-01' WHERE idFile=?",(fid,))
        self.db.conn.execute('INSERT INTO bookmark(idFile,timeInSeconds,totalTimeInSeconds,type) VALUES(?,10,100,1)',(fid,))
        self.db.conn.commit()
        self.db.save_episodes(show(),[(1,1,episode(1)),(1,2,episode(2))],self.file)
        row = self.db.conn.execute('SELECT playCount,dateAdded,lastPlayed FROM files WHERE idFile=?',(fid,)).fetchone()
        self.assertEqual(tuple(row),(2,'2010-01-01','2020-01-01'))
        self.assertEqual(self.db.conn.execute('SELECT timeInSeconds FROM bookmark WHERE idFile=?',(fid,)).fetchone()[0],10)
        self.assertEqual(self.db.conn.execute('SELECT c13 FROM episode ORDER BY CAST(c13 AS INTEGER)').fetchall()[0][0],'1')

    def test_show_uniqueids_deduplicated_and_default_pointers_valid(self):
        sid,ids = self.db.save_episodes(show(),[(1,1,episode(1))],self.file)
        self.db.conn.execute("INSERT INTO uniqueid(media_id,media_type,type,value) VALUES(?,'tvshow','tmdb','1')",(sid,));self.db.conn.commit()
        tvshow_db._SHOW_METADATA_CACHE.clear()
        self.db.save_episodes(show(),[(1,1,episode(1))],self.file)
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM uniqueid WHERE media_type='tvshow'").fetchone()[0],1)
        self.assertEqual(self.db.conn.execute('SELECT r.rating FROM episode e JOIN rating r ON e.c03=r.rating_id').fetchone()[0],7)
        self.assertEqual(self.db.conn.execute('SELECT u.value FROM episode e JOIN uniqueid u ON e.c20=u.uniqueid_id').fetchone()[0],'101')

    def test_concurrent_writers_share_movie_lock(self):
        from kodi_db import DB_WRITE_LOCK
        self.assertIs(tvshow_db.DB_WRITE_LOCK,DB_WRITE_LOCK)
        def write(n):
            db = tvshow_db.TvShowDatabase(str(self.path));db.connect()
            try:return db.save_episodes(show(count=30),[(1,n,episode(n))],'/shows/Test/S01E%02d.mkv'%n)
            finally:db.close()
        with concurrent.futures.ThreadPoolExecutor(6) as pool:results=list(pool.map(write,range(1,31)))
        self.assertTrue(all(s and len(e)==1 for s,e in results))
        self.assertEqual(self.db.conn.execute('SELECT COUNT(*) FROM tvshow').fetchone()[0],1)
        self.assertEqual(self.db.conn.execute('SELECT COUNT(*) FROM episode').fetchone()[0],30)

    def test_runtime_and_reidentification_remove_stale_ids(self):
        details=episode(1);details['info']['duration']=45;details['uniqueids']['imdb']='tt-old'
        sid,ids=self.db.save_episodes(show(),[(1,1,details)],self.file)
        self.assertEqual(self.db.conn.execute('SELECT c09 FROM episode').fetchone()[0],'2700')
        self.db.save_episodes(show(),[(1,1,episode(1))],self.file)
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM uniqueid WHERE media_type='episode' AND type='imdb'").fetchone()[0],0)


class ApiTests(unittest.TestCase):
    def test_whole_season_preserves_primary_ids_regular_cast_and_guest_cast(self):
        data={'name':'Season 1','credits':{'cast':[{'name':'Regular','profile_path':'/regular.jpg','order':0}]},
              'episodes':[{'id':1001,'episode_number':1,'name':'One','runtime':45,'vote_average':8,'vote_count':20,
                           'guest_stars':[{'name':'Guest','profile_path':'/guest.jpg'}],'crew':[{'job':'Director','name':'Director'}],
                           'still_path':'/one.jpg'}]}
        tmdb_api._details_cache.clear()
        response=types.SimpleNamespace(status_code=200,json=lambda:data)
        with patch.object(tmdb_api,'_tmdb_get',return_value=response) as get:
            details=tmdb_api.get_tv_season_details(10001,1,'test')
            tmdb_api.get_tv_season_details(10001,1,'test')
        ep=details['episodes'][1]
        self.assertEqual(ep['uniqueids']['tmdb'],'1001')
        self.assertEqual([actor['name'] for actor in ep['cast']],['Regular','Guest'])
        self.assertEqual(ep['info']['director'],['Director'])
        self.assertEqual(get.call_count,1)

    def test_missing_season_is_not_cached_as_fake_episodes(self):
        tmdb_api._details_cache.clear()
        with patch.object(tmdb_api,'_tmdb_get',return_value=types.SimpleNamespace(status_code=404)):
            self.assertIsNone(tmdb_api.get_tv_season_details(10002,1,'test'))


class ManualTests(unittest.TestCase):
    def test_picker_suggests_show_not_episode(self):
        ns=functions('_split_name_into_segments',L=lambda _: '自动提取: %s')
        p='/shows/西游记.1986/01 猴王初问世.mkv'
        labels,query=ns['_split_name_into_segments'](os.path.basename(p),build_tv_contexts([p])[p])
        self.assertEqual(query(0),'西游记')
        self.assertNotIn('猴王初问世',' '.join(labels))

    def test_unknown_show_picker_opens_name_input(self):
        ns=functions('_split_name_into_segments',L=lambda _: '')
        p='/shows/01.mkv';c=build_tv_contexts([p],'tvshow')[p]
        labels,query=ns['_split_name_into_segments']('01.mkv',c)
        self.assertEqual(query(0),'')

    def test_search_is_tv_only_and_keeps_release_year(self):
        from matching import match_score
        ns=functions('_manual_search_candidates',_match_score=match_score)
        p='/shows/西游记.1986/01 猴王初问世.mkv'
        c=build_tv_contexts([p])[p]
        with patch.object(tmdb_api,'search_tv_best',return_value=[{'id':1,'name':'西游记'}]) as tv,patch.object(tmdb_api,'search_movie_best',side_effect=AssertionError('TV must not search movies')):
            rows=ns['_manual_search_candidates']('西游记',os.path.basename(p),'西游记','test','zh-CN',c)
        self.assertEqual(rows[0]['media_type'],'tv')
        self.assertEqual(tv.call_args_list[0].args[1],1986)


if __name__ == '__main__':unittest.main(verbosity=2)
