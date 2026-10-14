"""Failed directory matching falls back to corroborated episode show names."""
import concurrent.futures
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch
SOURCE=Path(__file__).resolve().parents[1]/'addons/script.ai.movie.scanner/python'
sys.path.insert(0,str(SOURCE))
sys.modules.setdefault('xbmc',types.SimpleNamespace(LOGINFO=1,LOGERROR=2,LOGWARNING=3,log=lambda *a,**k:None))
from tv_matcher import TvMatchSession,TvMatchError
import tmdb_api


class FilenameFallbackTests(unittest.TestCase):
    def session(self,filenames=None):
        self.paths=['/shows/中文别名.2024/'+name for name in (filenames or ['The.Day.Of.The.Jackal.S01E01.Pilot.mkv','The.Day.Of.The.Jackal.S01E02.Title.mkv'])]
        return TvMatchSession(self.paths,'test')

    def candidate(self):
        return {'id':123,'name':'豺狼之日','original_name':'The Day of the Jackal','first_air_date':'2024-01-01'}

    def details(self):
        return {'info':{'title':'豺狼之日','year':'2024'},'seasons':[{'season_number':1,'episode_count':10,'air_date':'2024-01-01'}]}

    def search(self,title,*args):
        return [self.candidate()] if title.casefold()=='the day of the jackal' else []

    def test_directory_failure_falls_back_and_retains_directory_identity(self):
        s=self.session();c=s.context(self.paths[0])
        with patch.object(tmdb_api,'search_tv_best',side_effect=self.search) as search,patch.object(tmdb_api,'get_tv_details',return_value=self.details()):
            resolved=s.resolve_show(c)
        self.assertEqual(resolved['id'],123)
        self.assertEqual(resolved['reason'],'verified_consistent_episode_show_prefix')
        self.assertEqual(c['titles'],['中文别名'])
        self.assertEqual(search.call_args_list[0].args[0],'中文别名')
        self.assertEqual(search.call_args_list[2].args[0],'The Day Of The Jackal')
        self.assertTrue(all(call.args[1] in (2024,None) for call in search.call_args_list))

    def test_successful_directory_does_not_compute_fallback_or_send_extra_requests(self):
        s=self.session()
        with patch.object(s,'_filename_consensus',side_effect=AssertionError('No fallback on success')),patch.object(tmdb_api,'search_tv_best',return_value=[dict(self.candidate(),name='中文别名')]) as search,patch.object(tmdb_api,'get_tv_details',return_value=self.details()):
            self.assertEqual(s.resolve_show(s.context(self.paths[0]))['id'],123)
        self.assertEqual(search.call_count,2)

    def test_single_named_episode_is_not_sufficient_evidence(self):
        s=self.session(['The.Day.Of.The.Jackal.S01E01.mkv'])
        self.assertEqual(s._filename_consensus(s.context(self.paths[0])),[])

    def test_alternate_encodes_of_one_episode_do_not_add_votes(self):
        s=self.session(['The.Day.Of.The.Jackal.S01E01.1080p.mkv','The.Day.Of.The.Jackal.S01E01.2160p.mkv'])
        self.assertEqual(s._filename_consensus(s.context(self.paths[0])),[])

    def test_conflicting_prefix_does_not_use_majority_vote(self):
        s=self.session(['The.Day.Of.The.Jackal.S01E01.mkv','The.Day.Of.The.Jackal.S01E02.mkv','Other.Show.S01E03.mkv'])
        self.assertEqual(s._filename_consensus(s.context(self.paths[0])),[])

    def test_episode_titles_are_not_search_terms(self):
        s=self.session(['S01E01.The.Day.Of.The.Jackal.mkv','S01E02.The.Day.Of.The.Jackal.mkv'])
        self.assertEqual(s._filename_consensus(s.context(self.paths[0])),[])

    def test_directory_year_conflict_remains_pending(self):
        s=self.session()
        def search(title,*args):return [dict(self.candidate(),first_air_date='1990-01-01')] if 'Jackal' in title else []
        details=self.details();details['seasons'][0]['air_date']='1990-01-01'
        with patch.object(tmdb_api,'search_tv_best',side_effect=search),patch.object(tmdb_api,'get_tv_details',return_value=details):
            with self.assertRaises(TvMatchError):s.resolve_show(s.context(self.paths[0]))

    def test_metadata_network_error_does_not_activate_fallback(self):
        s=self.session()
        with patch.object(s,'_filename_consensus',side_effect=AssertionError('Do not retry another identity')),patch.object(tmdb_api,'search_tv_best',return_value=[dict(self.candidate(),name='中文别名')]),patch.object(tmdb_api,'get_tv_details',return_value=None):
            with self.assertRaises(TvMatchError) as error:s.resolve_show(s.context(self.paths[0]))
        self.assertEqual(error.exception.reason,'show_details_unavailable')

    def test_concurrent_episode_workers_share_fallback_requests(self):
        s=self.session()
        with patch.object(tmdb_api,'search_tv_best',side_effect=self.search) as search,patch.object(tmdb_api,'get_tv_details',return_value=self.details()):
            with concurrent.futures.ThreadPoolExecutor(8) as pool:
                results=list(pool.map(lambda _:s.resolve_show(s.context(self.paths[0]))['id'],range(32)))
        self.assertEqual(results,[123]*32)
        self.assertEqual(search.call_count,4)


if __name__=='__main__':unittest.main()
