"""Genre alias and exact-filter regressions for movie and TV category facets."""
import importlib.util
import sqlite3
import ast
import types
import queue
import time
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET
LIB=Path(__file__).resolve().parents[1]/'addons/plugin.video.filteredmovies/lib'
spec=importlib.util.spec_from_file_location('wall_genre_tests',LIB/'genres.py')
genres=importlib.util.module_from_spec(spec);spec.loader.exec_module(genres)

class GenreTests(unittest.TestCase):
    def test_current_omitted_movie_genres_have_individual_buttons(self):
        for value in ('惊悚','奇幻','家庭','西部','纪录'):
            self.assertIn(genres.genre_name(value),genres.GENRE_BUTTONS)

    def test_tv_combined_genres_supply_each_component_filter(self):
        self.assertEqual(genres.genre_name('Sci-Fi & Fantasy'),'科幻与奇幻')
        self.assertEqual(genres.genre_name('War & Politics'),'战争与政治')
        self.assertEqual(genres.genre_name('Action & Adventure'),'动作冒险')
        self.assertEqual(genres.genre_match('Sci-Fi & Fantasy','科幻'),1)
        self.assertEqual(genres.genre_match('Sci-Fi & Fantasy','奇幻'),1)
        self.assertEqual(genres.genre_match('War & Politics','战争'),1)
        self.assertEqual(genres.genre_match('War & Politics','政治'),1)
        self.assertEqual(genres.genre_match('动作冒险','动作'),1)
        self.assertEqual(genres.genre_match('动作冒险','冒险'),1)

    def test_english_and_traditional_names_match_the_same_genre(self):
        for source,selected in (('Science Fiction','科幻'),('驚悚','惊悚'),('Documentary','纪录片'),('Sci-Fi & Fantasy','奇幻')):
            self.assertEqual(genres.genre_match(source,selected),1)

    def test_other_only_contains_unknown_genres(self):
        self.assertEqual(genres.genre_match('Drama / Thriller / 家庭','其他'),0)
        self.assertEqual(genres.genre_match('Drama / 自定义类别','其他'),1)

    def test_sql_filter_is_exact_and_parameterized(self):
        with sqlite3.connect(':memory:') as c:
            c.create_function('genre_match',2,genres.genre_match)
            c.execute('CREATE TABLE items(id INTEGER, genre TEXT)')
            c.executemany('INSERT INTO items VALUES(?,?)',[(1,'动作'),(2,'动作冒险'),(3,'Action / Thriller'),(4,'动作片')])
            self.assertEqual(c.execute('SELECT id FROM items WHERE genre_match(genre,?)=1 ORDER BY id',('动作',)).fetchall(),[(1,),(2,),(3,)])
            self.assertEqual(c.execute('SELECT id FROM items WHERE genre_match(genre,?)=1',("' OR 1=1 --",)).fetchall(),[])

    def test_both_filter_layouts_contain_every_button_with_unique_ids(self):
        directory=LIB.parent/'resources/skins/Default/1080i'
        for filename in ('Custom_5112_AllMovie.xml','Custom_5114_Movie.xml'):
            root=ET.parse(directory/filename).getroot()
            identifiers=[c.get('id') for c in root.findall('.//control') if c.get('id')]
            self.assertEqual(len(identifiers),len(set(identifiers)))
            buttons={int(c.get('id')):c for c in root.findall(".//control[@id='1002']/control[@type='button']")}
            for identifier in genres.GENRE_BUTTONS.values():
                self.assertIn(identifier,buttons)
                self.assertIn('MFG.HasGenre.%d'%identifier,buttons[identifier].findtext('visible'))

    def test_all_display_labels_are_two_characters_without_invented_combo_names(self):
        for value in list(genres.GENRE_BUTTONS)+['其他']:
            self.assertEqual(len(genres.genre_label(value)),2,value)
            self.assertNotIn(genres.genre_label(value),('科奇','动冒','战政'))

    def test_common_genres_remain_visible_when_absent_or_library_empty(self):
        tree=ast.parse((LIB/'window_handler.py').read_text())
        mapping=next(n for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='FILTER_MAP' for t in n.targets))
        cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='FilterWindow')
        method=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='_apply_filter_facets')
        namespace={'GENRE_BUTTONS':genres.GENRE_BUTTONS,'genre_values':genres.genre_values,'queue':queue,'time':time}
        exec(compile(ast.Module(body=[mapping,method],type_ignores=[]),'genre_visibility','exec'),namespace)
        for media in ('movie','tvshow'):
            for genre_set in (set(),{'剧情'}):
                properties={}
                fake=types.SimpleNamespace(_facet_queue=queue.Queue(),_is_active=lambda:True,
                    poster_loader=types.SimpleNamespace(valid=lambda _:True),xml_name='Custom_5112_AllMovie.xml',
                    filter_state={'filter.mediatype':{'value':media}},
                    setProperty=lambda key,value:properties.update({key:value}),_update_toolbar=lambda:None)
                fake._facet_queue.put((1,{'genre':genre_set,'region':set(),'year':set(),'rating':set()}))
                namespace['_apply_filter_facets'](fake)
                self.assertEqual(properties['MFG.FacetRow.1002'],'true')
                for identifier in genres.GENRE_BUTTONS.values():
                    self.assertEqual(properties['MFG.HasGenre.%d'%identifier],'true')

if __name__=='__main__':unittest.main()
