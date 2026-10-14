"""Sort choice survives page recreation without retaining temporary filters."""
import ast
import importlib
import json
import queue
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch
LIB=Path(__file__).resolve().parents[1]/'addons/plugin.video.filteredmovies/lib'
package=types.ModuleType('wall_sort_tests');package.__path__=[str(LIB)]
sys.modules.setdefault('wall_sort_tests',package)
preferences=importlib.import_module('wall_sort_tests.sort_preferences')
tree=ast.parse((LIB/'window_handler.py').read_text())
cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='FilterWindow')
methods=[n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name in ('__init__','_sort_scope','_restore_sort_preferences','_select_sort','_query_filters')]
sort_options=next(n for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='SORT_OPTIONS' for t in n.targets))

class SortPreferenceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.profile=Path(self.temp.name)
        self.namespace={'__package__':'wall_sort_tests','queue':queue,'ADDON_DATA_PATH':str(self.profile),
                        'log':lambda *a:None,'xbmc':types.SimpleNamespace(LOGWARNING=2)}
        exec(compile(ast.Module(body=[sort_options]+methods,type_ignores=[]),'wall_sort_methods','exec'),self.namespace)

    def tearDown(self):self.temp.cleanup()

    def window(self,xml='Custom_5114_Movie.xml',media='movie'):
        window=types.SimpleNamespace(_update_toolbar=lambda:None,setFocusId=lambda _:None,refresh_container=lambda:None,
                                     _input_window=lambda:types.SimpleNamespace(getProperty=lambda _:'') )
        self.namespace['__init__'](window,xml)
        window.initial_mediatype=media
        window._sort_scope=lambda:self.namespace['_sort_scope'](window)
        self.namespace['_restore_sort_preferences'](window)
        return window

    def test_first_use_is_date_added_descending(self):
        self.assertEqual(preferences.load(self.profile),('dateadded','descending'))

    def test_selected_sort_and_reversed_order_survive_same_category_only(self):
        first=self.window();self.namespace['_select_sort'](first,2)
        movie=self.window();self.assertEqual((movie._sort_method,movie._sort_order),('title','ascending'))
        self.namespace['_select_sort'](movie,2)
        again=self.window()
        self.assertEqual((again._sort_method,again._sort_order),('title','descending'))
        all_media=self.window('Custom_5112_AllMovie.xml','all')
        self.assertEqual((all_media._sort_method,all_media._sort_order),('dateadded','descending'))

    def test_reset_filter_state_does_not_reset_sort(self):
        preferences.save(self.profile,'rating','ascending','tvshow')
        window=self.window(media='tvshow');window.reset_filters=True;window.filter_state={'filter.mediatype':{'value':'tvshow'}}
        query=self.namespace['_query_filters'](window)
        self.assertEqual(query['filter.sort'],'rating')
        self.assertEqual(query['filter.sortorder'],'ascending')
        self.assertNotIn('filter.genre',query)

    def test_search_does_not_overwrite_shared_category_choice(self):
        preferences.save(self.profile,'rating','descending','movie')
        search=self.window('Custom_5113_SearchMovie.xml')
        self.assertEqual((search._sort_method,search._sort_order),('dateadded','descending'))
        self.assertEqual(preferences.load(self.profile,'movie'),('rating','descending'))

    def test_corrupt_oversized_or_untrusted_values_have_safe_default(self):
        for text in ('{','[]',json.dumps({'method':['rating'],'order':'descending'}),
                     json.dumps({'method':'rating','order':'INVALID'}),'x'*2049):
            (self.profile/'sort-preference-all.json').write_text(text)
            self.assertEqual(preferences.load(self.profile),('dateadded','descending'))

    def test_failed_atomic_save_preserves_last_valid_choice(self):
        preferences.save(self.profile,'title','ascending','movie')
        with patch.object(preferences.os,'replace',side_effect=OSError('unavailable')):
            self.assertFalse(preferences.save(self.profile,'rating','descending','movie'))
        self.assertEqual(preferences.load(self.profile,'movie'),('title','ascending'))
        self.assertEqual(list(self.profile.glob('*.tmp')),[])

    def test_all_categories_are_independent(self):
        choices={'all':('title','ascending'),'movie':('rating','descending'),
                 'tvshow':('premiered','ascending'),'set':('title','descending'),
                 'concert':('rating','ascending'),'documentary':('premiered','descending')}
        for scope,choice in choices.items():
            self.assertTrue(preferences.save(self.profile,*choice,scope))
        for scope,choice in choices.items():
            self.assertEqual(preferences.load(self.profile,scope),choice)
        preferences.save(self.profile,'dateadded','ascending','movie')
        self.assertEqual(preferences.load(self.profile,'tvshow'),choices['tvshow'])

    def test_media_filter_does_not_change_entry_category_sort(self):
        preferences.save(self.profile,'rating','ascending','all')
        window=self.window('Custom_5112_AllMovie.xml','all')
        window.filter_state={'filter.mediatype':{'value':'tvshow'}}
        self.namespace['_select_sort'](window,2)
        self.assertEqual(preferences.load(self.profile,'all'),('title','ascending'))
        self.assertEqual(preferences.load(self.profile,'tvshow'),('dateadded','descending'))

    def test_unvisited_category_ignores_legacy_global_preference(self):
        (self.profile/'sort-preference.json').write_text(json.dumps({'method':'rating','order':'ascending'}))
        self.assertEqual(preferences.load(self.profile,'movie'),('dateadded','descending'))
        self.assertEqual(preferences.load(self.profile,'tvshow'),('dateadded','descending'))

    def test_collection_child_uses_collection_preference_not_movie_preference(self):
        preferences.save(self.profile,'rating','descending','movie')
        preferences.save(self.profile,'title','ascending','set')
        window=self.window();window.collection_id=42
        self.namespace['_restore_sort_preferences'](window)
        self.assertEqual((window._sort_method,window._sort_order),('title','ascending'))
        self.namespace['_select_sort'](window,2)
        self.assertEqual(preferences.load(self.profile,'set'),('title','descending'))
        self.assertEqual(preferences.load(self.profile,'movie'),('rating','descending'))

if __name__=='__main__':unittest.main()
