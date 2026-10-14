"""Location and cache isolation regressions for manual weather configuration."""
import importlib
import importlib.util
import json
from pathlib import Path
import sys
import unittest

LIB = Path(__file__).resolve().parents[1] / 'addons/weather.jioyzen/lib'
spec = importlib.util.spec_from_file_location('_weather_config_test', LIB / '__init__.py',
                                              submodule_search_locations=[str(LIB)])
package = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = package
spec.loader.exec_module(package)
prefs = importlib.import_module('_weather_config_test.preferences')
pipeline = importlib.import_module('_weather_config_test.pipeline')


class WeatherPreferencesTests(unittest.TestCase):
    def test_automatic_clears_all_manual_fields_and_switching_back_requires_selection(self):
        settings = {'automatic_location':'true','selected_location':'old',
                    'location_label':'米东','province':'新疆','city':'米东'}
        writes = []
        def write(key, value):
            writes.append(key);settings[key] = value
        self.assertTrue(prefs.clear_manual_if_automatic(settings.get, write))
        self.assertEqual(set(writes), {'selected_location','location_label','province','city'})
        self.assertFalse(prefs.clear_manual_if_automatic(settings.get, write))
        self.assertEqual(len(writes), 4)
        settings['automatic_location'] = 'false'
        with self.assertRaises(pipeline.DataError):prefs.manual_location(settings.get)

    def test_manual_mode_never_clears_saved_choice(self):
        settings = {'automatic_location':'false','selected_location':'saved','location_label':'米东'}
        before = dict(settings)
        self.assertFalse(prefs.clear_manual_if_automatic(settings.get, lambda k,v:settings.__setitem__(k,v)))
        self.assertEqual(settings, before)

    def test_default_automatic_ignores_unconfigured_manual_fields(self):
        self.assertIsNone(prefs.manual_location(lambda _: ''))

    def test_automatic_mode_ignores_old_manual_city(self):
        settings = {'automatic_location': 'true', 'province': '贵州', 'city': '贵阳'}
        self.assertIsNone(prefs.manual_location(settings.get))

    def test_manual_fields_are_trimmed_and_keep_city_precision(self):
        settings = {'automatic_location': 'false', 'province': ' 北京市 ', 'city': ' 北京市 '}
        data = prefs.manual_location(settings.get)
        self.assertEqual((data['province'], data['city']), ('北京市', '北京市'))
        self.assertEqual(data['provider'], 'manual')
        self.assertEqual(data['precision'], 'city')

    def test_partial_manual_input_is_not_treated_as_auto(self):
        for settings in [dict(automatic_location='false', province='北京'),
                         dict(automatic_location='false', city='北京'),
                         dict(automatic_location='false', province='北京', city='北京\n')]:
            # Trailing whitespace is trimmed; embedded control characters are invalid.
            if settings.get('city') == '北京\n': settings['city'] = '北\n京'
            with self.subTest(settings=settings), self.assertRaises(pipeline.DataError):
                prefs.manual_location(settings.get)

    def test_cache_can_only_be_reused_for_same_location_and_mode(self):
        location = {'city': '乌鲁木齐市', 'province': '新疆维吾尔自治区', 'provider': 'manual'}
        cached = {'location': location}
        self.assertTrue(prefs.cache_matches(cached, dict(city='乌鲁木齐', province='新疆')))
        self.assertFalse(prefs.cache_matches(cached, None))
        self.assertFalse(prefs.cache_matches(cached, dict(city='喀什', province='新疆')))
        self.assertFalse(prefs.cache_matches({'location': dict(location, provider='ip9')}, location))
        self.assertTrue(prefs.cache_matches({'location': dict(location, provider='ip9')}, None))



if __name__ == '__main__':unittest.main()
