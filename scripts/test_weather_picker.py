"""The location chooser must not replace preferences on cancel or failure."""
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

ADDON = Path(__file__).resolve().parents[1] / 'addons/weather.jioyzen'


class WeatherPickerTests(unittest.TestCase):
    def setUp(self):
        self.settings={'automatic_location':'false','selected_location':'old','location_label':'old'}
        self.query='北京';self.selection=0;self.actions=[];self.messages=[]
        self.addon=types.SimpleNamespace(getLocalizedString=lambda key:str(key),
            getAddonInfo=lambda key:'weather',getSetting=self.settings.get,setSetting=lambda k,v:self.settings.__setitem__(k,v))
        dialog=types.SimpleNamespace(input=lambda *a,**k:self.query,
            select=lambda *a,**k:self.selection,ok=lambda *a:self.messages.append(a),
            notification=lambda *a:self.messages.append(a))
        progress=types.SimpleNamespace(create=lambda *a:None,close=lambda:None,iscanceled=lambda:False)
        modules={'xbmc':types.SimpleNamespace(executebuiltin=self.actions.append),
            'xbmcaddon':types.SimpleNamespace(Addon=lambda _:self.addon),
            'xbmcgui':types.SimpleNamespace(Dialog=lambda:dialog,DialogProgress=lambda:progress,
                INPUT_ALPHANUM=0,ListItem=lambda *a,**k:types.SimpleNamespace()),
            'lib':types.ModuleType('lib'),
            'lib.pipeline':types.SimpleNamespace(DataError=ValueError),
            'lib.preferences':types.SimpleNamespace(manual_location=lambda read:__import__('json').loads(read('selected_location'))),
            'lib.places':types.SimpleNamespace(search_places=lambda _:[])}
        def clear_if_auto(read, write):
            if read('automatic_location') != 'false':
                for key in ('selected_location', 'location_label', 'province', 'city'):
                    if read(key):write(key, '')
        modules['lib.preferences'].clear_manual_if_automatic = clear_if_auto
        spec=importlib.util.spec_from_file_location('_weather_picker',ADDON/'configure.py')
        self.module=importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules,modules):spec.loader.exec_module(self.module)
        self.location={'country_code':'CN','city':'北京','province':'北京市','station_id':'101010100'}
        self.choices=[{'label':'北京','region':'北京市','location':self.location}]

    def test_manual_reopen_restores_display_from_saved_selection(self):
        import json
        self.settings.update(selected_location=json.dumps(dict(self.location, display_name='北京')),
                             location_label='', automatic_location='false')
        self.module.restore_location_label(self.addon)
        self.assertEqual(self.settings['location_label'], '北京 · 北京市')
        self.assertEqual(self.settings['automatic_location'], 'false')

    def test_automatic_reopen_clears_saved_selection_and_label(self):
        self.settings.update(automatic_location='true', province='新疆', city='米东')
        self.module.restore_location_label(self.addon)
        self.assertEqual(self.settings['selected_location'], '')
        self.assertEqual(self.settings['location_label'], '')
        self.assertEqual(self.settings['province'], '')
        self.assertEqual(self.settings['city'], '')

    def test_reopen_invalid_or_missing_selection_keeps_existing_settings(self):
        for raw in ['', '{bad']:
            self.settings['selected_location'] = raw
            before = dict(self.settings)
            self.module.restore_location_label(self.addon)
            self.assertEqual(self.settings, before)

    def test_provider_activation_only_fills_an_empty_global_choice(self):
        import json
        from unittest.mock import Mock
        for current in ('', 'weather.other', 'weather.jioyzen'):
            self.actions.clear()
            rpc = Mock(side_effect=[json.dumps({'result':{'value':current}}), json.dumps({'result':True})])
            with patch.object(self.module.xbmc, 'executeJSONRPC', rpc, create=True):
                self.module.activate_if_unset()
            self.assertEqual(rpc.call_count, 2 if current == '' else 1)
            self.assertEqual(self.actions, ['Weather.Refresh'] if current == '' else [])

    def test_probe_uses_weather_entry_without_opening_settings(self):
        from unittest.mock import Mock
        probe = Mock()
        with patch.dict(sys.modules, {'default':types.SimpleNamespace(main=probe)}), \
             patch.object(sys, 'argv', ['configure.py', 'probe']):
            self.module.main()
        probe.assert_called_once_with()
        self.assertEqual(self.actions, [])
        self.assertEqual(self.messages, [])

    def test_canceling_input_leaves_previous_location(self):
        before=dict(self.settings);self.query=''
        self.module.select_location(self.addon)
        self.assertEqual(self.settings,before);self.assertEqual(self.actions,[])

    def test_canceling_results_never_selects_first_candidate(self):
        before=dict(self.settings);self.selection=-1
        with patch.object(self.module,'search_places',return_value=self.choices):
            self.module.select_location(self.addon)
        self.assertEqual(self.settings,before);self.assertEqual(self.actions,[])

    def test_network_failure_and_empty_search_keep_previous_location(self):
        before=dict(self.settings)
        for result in [[],OSError('offline')]:
            if isinstance(result,Exception):ctx=patch.object(self.module,'search_places',side_effect=result)
            else:ctx=patch.object(self.module,'search_places',return_value=result)
            with ctx:self.module.select_location(self.addon)
            self.assertEqual(self.settings,before);self.assertEqual(self.actions,[])

    def test_user_confirmed_choice_is_saved_with_exact_station(self):
        import json
        with patch.object(self.module,'search_places',return_value=self.choices):
            self.module.select_location(self.addon)
        self.assertEqual(json.loads(self.settings['selected_location']),self.location)
        self.assertEqual(self.settings['location_label'],'北京 · 北京市')
        self.assertEqual(self.settings['automatic_location'],'false')
        self.assertEqual(self.actions,['Weather.Refresh', 'RunScript(weather/default.py)'])


if __name__=='__main__':unittest.main()
