"""Regressions for Kodi resetting weather properties during an async refresh."""
import copy
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch

ADDON = Path(__file__).resolve().parents[1] / 'addons/weather.jioyzen'


class Window:
    def __init__(self):
        self.properties = {}

    def setProperty(self, key, value):
        self.properties[key] = value


class WeatherRefreshTests(unittest.TestCase):
    def setUp(self):
        self.settings = {}
        self.windows = {10000: Window(), 12600: Window()}
        modules = {'xbmc': types.SimpleNamespace(log=Mock(), LOGINFO=1, LOGWARNING=2),
                   'xbmcaddon': types.SimpleNamespace(Addon=lambda _: types.SimpleNamespace(
                       getSetting=lambda key: self.settings.get(key, ''),
                       setSetting=lambda key, value: self.settings.__setitem__(key, value),
                       getAddonInfo=lambda key: {'profile': '/profile', 'name': 'Weather', 'path': str(ADDON)}[key])),
                   'xbmcgui': types.SimpleNamespace(Window=lambda wid: self.windows[wid]),
                   'xbmcvfs': types.SimpleNamespace(translatePath=lambda value: value),
                   'lib': types.ModuleType('lib')}
        for name in ('pipeline', 'card', 'preferences'):
            spec = importlib.util.spec_from_file_location('lib.' + name, ADDON / 'lib' / (name + '.py'))
            module = importlib.util.module_from_spec(spec)
            with patch.dict(sys.modules, modules):
                spec.loader.exec_module(module)
            modules['lib.' + name] = module
        spec = importlib.util.spec_from_file_location('weather_provider', ADDON / 'default.py')
        self.provider = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, modules):
            spec.loader.exec_module(self.provider)
        self.cached = {'version': 1, 'status': 'ok', 'fetched_at': 100,
            'location': {'city': '乌鲁木齐', 'precision': 'city', 'provider': 'ip9'},
            'weather': {'provider': '中国天气网', 'attribution': 'https://www.weather.com.cn/',
                'current': {'time': '2026-10-14T18:30', 'temperature_2m': 18,
                    'apparent_temperature': 15, 'relative_humidity_2m': 29,
                    'wind_speed_10m': 7.2, 'wind_direction_10m': 347, 'weather_code': 0, 'is_day': 1},
                'daily': {'time': ['2026-10-14'], 'weather_code': [0],
                    'temperature_2m_max': [19], 'temperature_2m_min': [8],
                    'precipitation_probability_max': [0], 'sunrise': ['2026-10-14T08:22'],
                    'sunset': ['2026-10-14T19:28']}}}
        self.new = copy.deepcopy(self.cached)
        self.new['weather']['current']['temperature_2m'] = 20

    def invoke(self, network, save=None, argv=None, now=2000):
        with patch.object(self.provider, 'load_cache', return_value=self.cached), \
             patch.object(self.provider, 'run_pipeline', side_effect=network) as fetch, \
             patch.object(self.provider, 'atomic_save', side_effect=save), \
             patch.object(self.provider.time, 'time', return_value=now), \
             patch.object(sys, 'argv', argv or ['default.py', '1']):
            self.provider.main()
        return fetch

    def test_reset_does_not_clear_desktop_during_network_request(self):
        self.provider.publish(self.cached)
        self.windows[12600].properties.clear()  # GUI_MSG_WEATHER_RESET
        self.assertEqual(self.windows[10000].properties['Jioyzen.Card.Temperature'], '18°')
        def network():
            self.assertEqual(self.windows[10000].properties['Jioyzen.Card.Ready'], 'true')
            self.assertEqual(self.windows[10000].properties['Jioyzen.Card.Temperature'], '18°')
            return self.new
        self.invoke(network)
        self.assertEqual(self.windows[10000].properties['Jioyzen.Card.Temperature'], '20°')

    def test_network_failure_keeps_old_card(self):
        self.invoke(lambda: (_ for _ in ()).throw(OSError('network')))
        p = self.windows[10000].properties
        self.assertEqual(p['Jioyzen.Card.Ready'], 'true')
        self.assertEqual(p['Jioyzen.Card.Temperature'], '18°')
        self.assertEqual(p['Jioyzen.Card.Updated'], '18:30 缓存')

    def test_automatic_refresh_clears_manual_choice_even_with_fresh_cache(self):
        self.settings.update(automatic_location='true', selected_location='old',
                             location_label='米东', province='新疆', city='米东')
        fetch = self.invoke(lambda:self.fail('unexpected network'), now=200)
        fetch.assert_not_called()
        for key in ('selected_location','location_label','province','city'):
            self.assertEqual(self.settings[key], '')

    def test_fresh_cache_never_calls_network(self):
        fetch = self.invoke(lambda: self.fail('unexpected network'), now=200)
        fetch.assert_not_called()
        self.assertEqual(self.windows[10000].properties['Jioyzen.Card.Temperature'], '18°')

    def test_disk_cache_failure_does_not_replace_live_data(self):
        self.invoke(lambda: self.new, save=OSError('disk full'))
        self.assertEqual(self.windows[10000].properties['Jioyzen.Card.Temperature'], '20°')

    def test_new_manual_city_bypasses_fresh_cache(self):
        self.settings.update(automatic_location='false', province='北京', city='北京')
        self.new['location'].update(provider='manual', province='北京', city='北京')
        def network(**kwargs):
            p = self.windows[10000].properties
            self.assertEqual(p['Jioyzen.Card.City'], '北京')
            self.assertEqual(p['Jioyzen.Card.Temperature'], '--°')
            self.assertEqual(p['Jioyzen.Card.Updated'], '更新中')
            return self.new
        fetch = self.invoke(network, now=200)
        self.assertEqual(fetch.call_count, 1)
        location = fetch.call_args.kwargs['manual_location']
        self.assertEqual(location['city'], '北京')
        self.assertEqual(location['provider'], 'manual')
        self.assertEqual(self.windows[10000].properties['Jioyzen.Card.City'], '北京市')

    def test_failed_new_city_never_falls_back_to_previous_city(self):
        self.provider.publish(self.cached)
        self.settings.update(automatic_location='false', province='北京', city='北京')
        self.invoke(lambda **kw: (_ for _ in ()).throw(OSError('offline')), now=200)
        p = self.windows[10000].properties
        self.assertEqual(p['Jioyzen.Card.City'], '北京')
        self.assertEqual(p['Jioyzen.Card.Temperature'], '--°')
        self.assertEqual(p['Jioyzen.Card.Updated'], '更新失败')
        self.assertEqual(p['Jioyzen.Card.High'], '')
        self.assertEqual(self.windows[12600].properties['Current.Location'], '北京')
        self.assertEqual(self.windows[12600].properties['Current.Temperature'], '')

    def test_saved_station_is_used_immediately_instead_of_ip_location(self):
        import json
        selection = dict(country_code='CN', province='北京', city='北京', station_id='101010100')
        self.settings.update(automatic_location='false', selected_location=json.dumps(selection))
        self.new['location'].update(selection, provider='manual', precision='station')
        fetch = self.invoke(lambda **kw: self.new, now=200)
        self.assertEqual(fetch.call_args.kwargs['manual_location']['station_id'], '101010100')
        self.assertEqual(self.windows[10000].properties['Jioyzen.Card.City'], '北京市')

    def test_old_success_cannot_overwrite_card_or_cache_after_location_change(self):
        current = copy.deepcopy(self.new)
        current['location'].update(city='北京', province='北京', provider='manual')
        def network():
            self.settings.update(automatic_location='false', province='北京', city='北京')
            self.provider.publish(current)
            return self.new  # An earlier automatic-IP request just finished.
        save = Mock()
        self.invoke(network, save=save)
        self.assertEqual(self.windows[10000].properties['Jioyzen.Card.City'], '北京市')
        self.assertEqual(self.windows[10000].properties['Jioyzen.Card.Temperature'], '20°')
        save.assert_not_called()

    def test_old_failure_cannot_restore_old_city_after_new_selection(self):
        current = copy.deepcopy(self.new)
        current['location'].update(city='北京', province='北京', provider='manual')
        def network():
            self.settings.update(automatic_location='false', province='北京', city='北京')
            self.provider.publish(current)
            raise OSError('old request failed')
        self.invoke(network)
        self.assertEqual(self.windows[10000].properties['Jioyzen.Card.City'], '北京市')
        self.assertEqual(self.windows[10000].properties['Jioyzen.Card.Temperature'], '20°')

    def test_response_for_wrong_city_is_not_published_or_saved(self):
        self.settings.update(automatic_location='false', province='北京', city='北京')
        save = Mock()
        self.invoke(lambda **kw: self.new, save=save, now=200)
        self.assertEqual(self.windows[10000].properties['Jioyzen.Card.City'], '北京')
        self.assertEqual(self.windows[10000].properties['Jioyzen.Card.Updated'], '更新失败')
        save.assert_not_called()

    def test_matching_manual_city_reuses_fresh_cache(self):
        self.settings.update(automatic_location='false', province='新疆', city='乌鲁木齐市')
        self.cached['location'].update(provider='manual', province='新疆维吾尔自治区')
        fetch = self.invoke(lambda: self.fail('unexpected network'), now=200)
        fetch.assert_not_called()

    def test_switching_back_to_automatic_bypasses_manual_cache(self):
        self.cached['location'].update(provider='manual', province='新疆')
        fetch = self.invoke(lambda: self.new, now=200)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(fetch.call_args.kwargs, {})

    def test_provider_switch_bypasses_old_fresh_cache(self):
        self.cached['weather']['provider'] = 'Open-Meteo'
        fetch = self.invoke(lambda:self.new, now=200)
        self.assertEqual(fetch.call_count, 1)

    def test_missing_optional_fields_clear_old_values_without_crash(self):
        self.provider.publish(self.cached)
        current = self.new['weather']['current']
        for key in ('apparent_temperature','relative_humidity_2m','wind_speed_10m','wind_direction_10m'):
            current.pop(key)
        current.update(native_code=1, condition_text='多云', wind_text='东南风 1级')
        for key in ('sunrise','sunset','precipitation_probability_max'):
            self.new['weather']['daily'].pop(key)
        self.provider.publish(self.new)
        p = self.windows[12600].properties
        self.assertEqual(p['Current.Condition'], '多云')
        self.assertEqual(p['Current.FeelsLike'], '')
        self.assertEqual(p['Jioyzen.Card.Humidity'], '')
        self.assertEqual(p['Jioyzen.Card.Rain'], '')
        self.assertEqual(p['Jioyzen.Card.Wind'], '东南风 1级')
        self.assertTrue(p['Jioyzen.Card.Icon'].endswith('b1.png'))
        self.new['weather']['daily'] = {'time':[]}
        self.provider.publish(self.new)
        self.assertEqual(p['Day0.HighTemp'], '')
        self.assertEqual(p['Daily.IsFetched'], 'false')

    def test_probe_never_changes_window_properties(self):
        self.invoke(lambda: self.new, argv=['default.py', 'probe'])
        self.assertEqual(self.windows[10000].properties, {})
        self.assertEqual(self.windows[12600].properties, {})


if __name__ == '__main__':
    unittest.main()
