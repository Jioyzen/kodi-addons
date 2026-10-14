"""First-use setup and recovery must not reset subsequent manual locations."""
import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'addons/skin.embuary.mod.jioyzen/scripts/initialize_weather.py'


class SkinWeatherSetupTests(unittest.TestCase):
    def setUp(self):
        self.enabled = False
        self.selected = 'weather.other'
        self.initialized = False
        self.skin = 'skin.embuary.mod.jioyzen'
        self.settings = dict(automatic_location='false', selected_location='saved',
                             location_label='米东', province='新疆', city='米东')
        self.actions = []
        self.fail_enable = False
        self.logs = []
        def rpc(raw):
            request = json.loads(raw)
            method, params = request['method'], request['params']
            if method == 'Settings.GetSettingValue':
                result = {'value':self.skin if params['setting']=='lookandfeel.skin' else self.selected}
            elif method == 'Addons.GetAddonDetails':result = {'addon':{'enabled':self.enabled}}
            elif method == 'Addons.SetAddonEnabled':
                if self.fail_enable:return json.dumps({'error':{'message':'failure'}})
                self.enabled = params['enabled'];result = 'OK'
            elif method == 'Settings.SetSettingValue':self.selected = params['value'];result = True
            else:raise AssertionError(method)
            return json.dumps({'result':result})
        modules = {'xbmc':types.SimpleNamespace(executeJSONRPC=rpc,
            getCondVisibility=lambda _:self.initialized, executebuiltin=self.actions.append,
            log=lambda *args:self.logs.append(args), LOGWARNING=2),
            'xbmcaddon':types.SimpleNamespace(Addon=lambda _:types.SimpleNamespace(
                getSetting=self.settings.get, getAddonInfo=lambda _: '/addons/weather.jioyzen', setSetting=lambda k,v:self.settings.__setitem__(k,v)))}
        spec = importlib.util.spec_from_file_location('_skin_weather_setup', SCRIPT)
        self.module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, modules):spec.loader.exec_module(self.module)

    def test_first_use_enables_overrides_other_provider_and_uses_automatic_location(self):
        self.module.main()
        self.assertTrue(self.enabled)
        self.assertEqual(self.selected, 'weather.jioyzen')
        self.assertEqual(self.settings, dict(automatic_location='true', selected_location='',
                                          location_label='', province='', city=''))
        self.assertEqual(self.actions, ['Skin.SetBool(jioyzen_weather_initialized)', 'Weather.Refresh',
                                        'RunScript(/addons/weather.jioyzen/default.py)'])

    def test_later_provider_recovery_preserves_manual_city(self):
        self.initialized = True
        before = dict(self.settings)
        self.module.main()
        self.assertTrue(self.enabled)
        self.assertEqual(self.selected, 'weather.jioyzen')
        self.assertEqual(self.settings, before)
        self.assertEqual(self.actions, ['Weather.Refresh', 'RunScript(/addons/weather.jioyzen/default.py)'])

    def test_failed_enable_does_not_reset_location_or_mark_success(self):
        self.fail_enable = True
        before = dict(self.settings)
        self.module.main()
        self.assertEqual(self.settings, before)
        self.assertEqual(self.selected, 'weather.other')
        self.assertEqual(self.actions, [])
        self.assertTrue(self.logs)

    def test_queued_action_after_switching_skins_does_not_change_provider(self):
        self.skin = 'skin.estuary'
        before = dict(self.settings)
        self.module.main()
        self.assertFalse(self.enabled)
        self.assertEqual(self.selected, 'weather.other')
        self.assertEqual(self.settings, before)
        self.assertEqual(self.actions, [])


if __name__ == '__main__':unittest.main()
