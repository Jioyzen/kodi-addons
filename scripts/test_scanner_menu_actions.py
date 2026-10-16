"""Validate source-scan scope and stop state without accessing media shares."""
import ast
import types
import unittest
from pathlib import Path

SOURCE=Path(__file__).resolve().parents[1]/'addons/script.ai.movie.scanner/python/main.py'


def functions(names,namespace):
    namespace.setdefault('refresh_menu', lambda: None)
    tree=ast.parse(SOURCE.read_text())
    nodes=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names]
    for node in nodes:node.decorator_list=[]
    exec(compile(ast.Module(body=nodes,type_ignores=[]),str(SOURCE),'exec'),namespace)
    return namespace


class MenuActionsTests(unittest.TestCase):
    def test_context_scan_checks_only_selected_managed_directory(self):
        calls=[]
        dialog=types.SimpleNamespace(notification=lambda *a:None)
        n=functions(['action_refresh_directory'],{'_is_busy':lambda:False,
            'load_history':lambda:{'directories':{'/movies':{},'/shows':{}}},
            '_refresh_library_files':lambda paths:calls.append(paths),
            'xbmcgui':types.SimpleNamespace(Dialog=lambda:dialog,NOTIFICATION_WARNING=1),'ADDON_NAME':'Test','L':str})
        n['action_refresh_directory']('/shows')
        self.assertEqual(calls,[['/shows']])
        n['action_refresh_directory']('/not-managed')
        self.assertEqual(calls,[['/shows']])

    def test_stopping_file_scan_keeps_active_state_until_worker_finishes(self):
        requests=[]
        dialog=types.SimpleNamespace(notification=lambda *a:None)
        n=functions(['stop_scan'],{'_is_busy':lambda:True,
            '_request_stop':lambda:requests.append('stop'),
            '_clear_refreshing':lambda:self.fail('Worker has not finished yet'),
            'xbmcgui':types.SimpleNamespace(Dialog=lambda:dialog,NOTIFICATION_INFO=1),'ADDON_NAME':'Test','L':str})
        n['stop_scan']()
        self.assertEqual(requests,['stop'])

    def test_file_scan_stops_before_next_directory_and_cleans_up_state(self):
        state={'active':False,'stop':False};visited=[];closed=[]
        def scan(path):
            self.assertTrue(state['active'])
            visited.append(path);state['stop']=True
            return 1,0
        dialog=types.SimpleNamespace(create=lambda *a:None,update=lambda *a:None,close=lambda:closed.append(True))
        n=functions(['_refresh_library_files'],{'ensure_data_dir':lambda:None,
            '_set_refreshing':lambda:state.update(active=True),'_clear_refreshing':lambda:state.update(active=False),
            '_clear_stop':lambda:state.update(stop=False),'_is_stop_requested':lambda:state['stop'],
            'refresh_directory_files':scan,'directory_label':lambda p:p,
            'xbmcgui':types.SimpleNamespace(DialogProgressBG=lambda:dialog,Dialog=lambda:types.SimpleNamespace(notification=lambda *a:None),NOTIFICATION_INFO=1),
            'xbmc':types.SimpleNamespace(executebuiltin=lambda *a:None),'ADDON_NAME':'Test',
            'L':lambda key:{32034:'Checking %s',32035:'%d/%d',32060:'%d/%d'}.get(key,'Test')})
        n['_refresh_library_files'](['/one','/two'])
        self.assertEqual(visited,['/one'])
        self.assertFalse(state['active']);self.assertFalse(state['stop']);self.assertEqual(closed,[True])


if __name__=='__main__':unittest.main(verbosity=2)
