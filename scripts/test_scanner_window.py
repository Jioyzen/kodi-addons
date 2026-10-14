"""Standalone navigation, cancellation and fast root-menu identities."""
import ast
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / 'addons/script.ai.movie.scanner/python'
sys.path.insert(0, str(SOURCE))
from window_state import Navigation, item_query, context_query
from directory_listing import single_show_root


class ScannerWindowTests(unittest.TestCase):
    def test_back_during_initial_load_closes_and_rejects_late_result(self):
        nav = Navigation()
        pending = nav.begin()
        self.assertIsNone(nav.back())
        self.assertTrue(nav.closed)
        self.assertFalse(nav.accepts(pending))
        self.assertFalse(nav.accepts(nav.generation))

    def test_child_back_restores_parent_selection_and_cached_rows(self):
        nav = Navigation()
        nav.current.rows = ['cached root']
        pending = nav.enter({'action': 'view_directory', 'directory': 'a'}, 4, 'root-selection')
        nav.current.rows = ['cached directory']
        nav.enter({'action': 'view_directory', 'directory': 'a', 'group': 'show'}, 9)
        frame = nav.back()
        self.assertEqual(frame.rows, ['cached directory'])
        self.assertEqual(frame.selected, 9)
        self.assertFalse(nav.accepts(pending))
        frame = nav.back()
        self.assertEqual(frame.rows, ['cached root'])
        self.assertEqual((frame.selected, frame.selected_url), (4, 'root-selection'))

    def test_pagination_does_not_accumulate_parent_pages(self):
        nav = Navigation()
        nav.enter({'action': 'view_directory', 'directory': 'a'}, 2)
        for page in range(1, 40):
            nav.enter({'action': 'view_directory', 'directory': 'a', 'page': str(page)}, 79)
        self.assertEqual(len(nav.frames), 2)
        self.assertEqual(nav.back().query, {})

    def test_old_directory_response_cannot_overwrite_new_directory(self):
        nav = Navigation()
        old = nav.enter({'action': 'view_directory', 'directory': 'a'}, 0)
        new = nav.enter({'action': 'view_directory', 'directory': 'b'}, 0)
        self.assertFalse(nav.accepts(old))
        self.assertTrue(nav.accepts(new))

    def test_foreign_commands_never_reach_action_dispatch(self):
        self.assertEqual(item_query('plugin://script.ai.movie.scanner/?action=view_directory&directory=abc'),
                         {'action': 'view_directory', 'directory': 'abc'})
        self.assertEqual(context_query('RunScript(script.ai.movie.scanner,?action=manual_match&file=abc)'),
                         {'action': 'manual_match', 'file': 'abc'})
        for url in ('smb://user:secret@server/file', 'plugin://other/?action=clear_all_data'):
            with self.assertRaises(ValueError):
                item_query(url)
        with self.assertRaises(ValueError):
            context_query('System.Exec(untrusted)')

    def test_root_menu_hint_never_rebuilds_all_episode_contexts(self):
        import directory_listing
        movie_files = {'/movies/%d.mkv' % i: {'movie_id': i + 1} for i in range(5000)}
        tv_files = {'/shows/Alpha/S01E%02d.mkv' % i: {
            'is_tvshow': True, 'match_evidence': {'show_root': '/shows/Alpha/'}} for i in range(1, 100)}
        with patch.object(directory_listing, 'build_tv_contexts', side_effect=AssertionError('expensive root analysis')):
            self.assertIsNone(single_show_root(movie_files))
            self.assertEqual(single_show_root(tv_files), '/shows/Alpha/')
            tv_files['/shows/Beta/S01E01.mkv'] = {'is_tvshow': True,
                'match_evidence': {'show_root': '/shows/Beta/'}}
            self.assertIsNone(single_show_root(tv_files))

    def test_manual_match_dispatch_passes_resolved_paths_explicitly(self):
        tree = ast.parse((SOURCE / 'main.py').read_text())
        router = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'router')
        calls = []
        namespace = {'manual_match': lambda *args: calls.append(args)}
        exec(compile(ast.Module(body=[router], type_ignores=[]), 'router', 'exec'), namespace)
        namespace['router'](params={'action': 'manual_match', 'filepath': '/file', 'dirpath': '/source'})
        self.assertEqual(calls, [('/file', '/source')])


if __name__ == '__main__':
    unittest.main()
