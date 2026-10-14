"""Actor disambiguation and local/online filmography identity regressions."""
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

LIB = Path(__file__).resolve().parents[1] / 'addons/plugin.video.filteredmovies/lib'
spec = importlib.util.spec_from_file_location('wall_person_tests.person_catalog', LIB / 'person_catalog.py')
person = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {'wall_person_tests': types.ModuleType('wall_person_tests'),
                              'wall_person_tests.tv_catalog': types.SimpleNamespace(rpc=None)}):
    spec.loader.exec_module(person)


class IdentityTests(unittest.TestCase):
    def test_person_metadata_never_requests_online_filmography(self):
        calls = []
        def get(path):
            calls.append(path)
            if path.startswith('tv/'):
                return {'cast': [{'id': 20, 'name': 'Actor'}]}
            return {'name': 'Actor', 'combined_credits': {'cast': [{'id': 1, 'media_type': 'movie'}]}}
        local = [{'title': 'Only local', 'movieid': 8}]
        data = person.load_person({'name': 'Actor'}, {'uniqueid': {'tmdb': '42'}}, local, get)
        self.assertEqual(data['works'], local)
        self.assertEqual(calls, ['tv/42/aggregate_credits', 'person/20'])

    def test_all_local_works_are_read_even_when_more_than_one_page(self):
        calls = []
        def query(method, params):
            calls.append((method, params['limits']['start']))
            if method.endswith('GetTVShows'):
                return {'tvshows': []}
            start = params['limits']['start']
            rows = [{'title': str(i), 'movieid': i + 1} for i in range(start, min(start + 120, 250))]
            return {'movies': rows, 'limits': {'total': 250}}
        works = person.local_works({'name': 'Actor'}, query)
        self.assertEqual(len(works), 250)
        self.assertEqual([offset for method, offset in calls if method.endswith('GetMovies')], [0, 120, 240])

    def test_closed_actor_does_not_query_more_pages_or_network(self):
        def forbidden(*args):self.fail('Cancelled query must not run')
        self.assertEqual(person.local_works({'name':'Actor'},forbidden,cancelled=lambda:True),[])
        self.assertEqual(person.load_person({'name':'Actor'},{},[],forbidden,
            cancelled=lambda:True)['works'],[])

    def test_repeated_page_from_backend_cannot_cause_an_infinite_loop(self):
        calls=[]
        def query(method,params):
            calls.append(method)
            return {'movies':[{'movieid':1,'title':'One'}], 'limits':{'total':100000}}
        self.assertEqual(len(person.local_works({'name':'Actor'},query)),1)
        self.assertEqual(len(calls),3)

    def test_profile_verifies_a_translated_name_in_source_show_cast(self):
        get = lambda _: {'cast': [{'id': 12, 'name': 'Other', 'profile_path': '/other.jpg'},
                                  {'id': 20, 'name': 'English Name', 'profile_path': '/same.jpg'}]}
        actor = {'name': '中文姓名', 'thumbnail': 'image://https%3A%2F%2Fimage.tmdb.org%2Ft%2Fp%2Fw185%2Fsame.jpg/'}
        self.assertEqual(person.resolve_person(actor, {'uniqueid': {'tmdb': '42'}}, get), 20)

    def test_same_name_ambiguity_is_not_silently_assigned_first_result(self):
        get = lambda _: {'cast': [{'id': 12, 'name': '同名'}, {'id': 20, 'name': '同名'}]}
        self.assertIsNone(person.resolve_person({'name': '同名'}, {'uniqueid': {'tmdb': '42'}}, get))

    def test_movie_source_uses_movie_credits_not_tv_show_id(self):
        calls=[]
        def get(path):
            calls.append(path);return {'cast':[{'id':20,'name':'Actor'}]}
        self.assertEqual(person.resolve_person({'name':'Actor'},{'movieid':4,'media_type':'movie','uniqueid':{'tmdb':'42'}},get),20)
        self.assertEqual(calls,['movie/42/credits'])

    def test_absent_source_identity_requires_no_network(self):
        def forbidden(_): raise AssertionError('No network expected')
        self.assertIsNone(person.resolve_person({'name': 'Actor'}, {}, forbidden))

    def test_movie_and_show_ids_never_collide_and_local_ids_are_preserved(self):
        local = [{'title': 'Local show', 'tvshowid': 5, 'media_type': 'tvshow',
                  'credit_type': 'tv', 'local': True, 'year': 1998, 'uniqueid': {'tmdb': '42'}}]
        credits = [{'media_type': 'movie', 'id': 42, 'title': 'Different movie', 'release_date': '2020-01-01'},
                   {'media_type': 'tv', 'id': 42, 'name': 'Show'}, {'media_type': 'tv', 'id': 42, 'name': 'Duplicate'}]
        result = person.merge_credits(credits, local)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]['tvshowid'], 5)
        self.assertTrue(result[0]['local'])
        self.assertFalse(result[1]['local'])

    def test_works_are_bounded_and_missing_identity_keeps_local_filmography(self):
        result = person.merge_credits([{'media_type': 'movie', 'id': i + 1, 'title': str(i)} for i in range(300)], [])
        self.assertEqual(len(result), 120)
        local = [{'title': 'Local', 'movieid': 8, 'media_type': 'movie', 'credit_type': 'movie', 'local': True}]
        data = person.load_person({'name': 'Actor'}, {}, local, get=lambda _: {})
        self.assertEqual(data['works'], local)


if __name__ == '__main__':
    unittest.main()
