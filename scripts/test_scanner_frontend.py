"""Exercise UI callbacks with a recording Kodi adapter; no device navigation."""
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch
import json

SOURCE = Path(__file__).resolve().parents[1] / 'addons/script.ai.movie.scanner/python'
sys.path.insert(0, str(SOURCE))


class Item:
    def __init__(self, label, url='', folder=False, context=None, **kwargs):
        self.label = label
        self.properties = {'AIS.Url': url, 'AIS.Folder': 'true' if folder else 'false',
                           'AIS.Context': json.dumps(context or [])}

    def getProperty(self, key):
        return self.properties.get(key, '')

    def getLabel(self):
        return self.label


class ListControl:
    def __init__(self):
        self.items = []
        self.selected = 0
        self.writes = 0

    def reset(self):
        self.items = []
        self.writes += 1

    def addItems(self, items):
        self.items.extend(items)

    def addItem(self, item):
        self.items.append(item)

    def selectItem(self, selected):
        self.selected = selected

    def getSelectedPosition(self):
        return self.selected

    def getSelectedItem(self):
        return self.items[self.selected] if self.items else None


class Window:
    def __init__(self, *args, **kwargs):
        self.control = ListControl()
        self.properties = {}
        self.closes = 0

    def getControl(self, control_id):
        assert control_id == 50
        return self.control

    def setProperty(self, key, value):
        self.properties[key] = value

    def close(self):
        self.closes += 1


class FrontendTests(unittest.TestCase):
    def setUp(self):
        self.dialog_selection = 0
        xbmcgui = types.SimpleNamespace(WindowXML=Window, ListItem=Item,
            Dialog=lambda: types.SimpleNamespace(contextmenu=lambda labels: self.dialog_selection))
        spec = importlib.util.spec_from_file_location('scanner_frontend_test', SOURCE / 'standalone.py')
        self.module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'xbmcgui': xbmcgui, 'xbmc': types.SimpleNamespace(log=lambda *a: None, LOGERROR=2, LOGINFO=1)}):
            spec.loader.exec_module(self.module)
        self.window = self.module.ScannerWindow()
        self.window.addon = types.SimpleNamespace(getAddonInfo=lambda key: 'AI Scanner')
        self.root_rows = [('plugin://script.ai.movie.scanner/?action=view_directory&directory=token',
                          Item('Movie source', 'plugin://script.ai.movie.scanner/?action=view_directory&directory=token', True), True)]
        frame = self.window.navigation.current
        frame.rows, frame.title, frame.signature = self.root_rows, 'AI Scanner', 'snapshot'
        self.window._history_signature = 'snapshot'
        self.window._render(frame)

    def back(self):
        self.window.onAction(types.SimpleNamespace(getId=lambda: 92))

    def test_folder_opens_inside_same_window_and_back_restores_cached_parent(self):
        self.window.onClick(50)
        generation, query = self.window._jobs.get_nowait()
        self.assertEqual(query, {'action': 'view_directory', 'directory': 'token'})
        child = [('file', Item('file', 'plugin://script.ai.movie.scanner/?action=play_file&file=f'), False)]
        self.assertTrue(self.window._apply(generation, child, 'Movie source', 'snapshot'))
        self.assertEqual(self.window.properties['AIS.Title'], 'Movie source')
        self.back()
        self.assertEqual(self.window.properties['AIS.Title'], 'AI Scanner')
        self.assertEqual(self.window.control.getSelectedItem().getLabel(), 'Movie source')
        self.assertEqual(self.window.closes, 0)

    def test_back_while_loading_child_rejects_its_late_result(self):
        self.window.onClick(50)
        generation, _ = self.window._jobs.get_nowait()
        self.back()
        writes = self.window.control.writes
        self.assertFalse(self.window._apply(generation, [], 'Late child', 'snapshot'))
        self.assertEqual(self.window.control.writes, writes)
        self.assertEqual(self.window.properties['AIS.Title'], 'AI Scanner')

    def test_root_back_closes_and_never_mutates_widgets_afterwards(self):
        generation = self.window.navigation.begin()
        self.back()
        writes = self.window.control.writes
        self.assertFalse(self.window._apply(generation, self.root_rows, 'Late root', 'snapshot'))
        self.assertEqual(self.window.control.writes, writes)
        self.assertEqual(self.window.closes, 1)

    def test_long_press_dispatches_only_selected_items_private_command(self):
        command = 'RunScript(script.ai.movie.scanner,?action=manual_match_show&directory=token&group=show)'
        self.window.control.items[0].properties['AIS.Context'] = json.dumps([('Match show', command)])
        actions = []
        self.window._action = actions.append
        self.window.onAction(types.SimpleNamespace(getId=lambda: 117))
        self.assertEqual(actions, [{'action': 'manual_match_show', 'directory': 'token', 'group': 'show'}])
        self.dialog_selection = -1
        self.window.onAction(types.SimpleNamespace(getId=lambda: 117))
        self.assertEqual(len(actions), 1)

    def test_refresh_restores_item_identity_even_when_menu_positions_change(self):
        frame = self.window.navigation.current
        frame.selected_url = self.root_rows[0][0]
        frame.rows = [('stop', Item('Stop', 'stop'), False)] + self.root_rows
        self.window._render(frame)
        self.assertEqual(self.window.control.selected, 1)
        self.assertEqual(self.window.control.getSelectedItem().getLabel(), 'Movie source')


if __name__ == '__main__':
    unittest.main()
