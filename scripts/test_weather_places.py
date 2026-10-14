"""Search and selected-coordinate regressions; no UI or live network needed."""
import importlib
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from urllib.parse import parse_qs, urlsplit

LIB = Path(__file__).resolve().parents[1] / 'addons/weather.jioyzen/lib'
spec = importlib.util.spec_from_file_location('_weather_places_test', LIB/'__init__.py',
                                             submodule_search_locations=[str(LIB)])
package = importlib.util.module_from_spec(spec);sys.modules[spec.name] = package
spec.loader.exec_module(package)
places = importlib.import_module('_weather_places_test.places')
prefs = importlib.import_module('_weather_places_test.preferences')
pipeline = importlib.import_module('_weather_places_test.pipeline')
from unittest.mock import patch
from weather_test_data import NOW, OBSERVATION, FORECAST, search_row, search_body, assignment


class WeatherPlaceTests(unittest.TestCase):
    def test_suffix_is_only_stripped_after_empty_results(self):
        seen = []
        def fetch(url):
            name = parse_qs(urlsplit(url).query)['cityname'][0];seen.append(name)
            return search_body([] if name == '米东区' else [search_row()])
        choices = places.search_places('米东区', fetch)
        self.assertEqual(seen, ['米东区', '米东'])
        self.assertEqual(choices[0]['location']['station_id'], '101130112')

    def test_same_name_places_remain_distinct_user_choices(self):
        rows = [search_row('101010300', '朝阳', '北京'), search_row('101071201', '朝阳', '辽宁')]
        choices = places.search_places('朝阳', lambda _:search_body(rows))
        self.assertEqual(len(choices), 2)
        self.assertNotEqual(choices[0]['region'], choices[1]['region'])

    def test_scenic_town_invalid_and_duplicate_ids_not_offered(self):
        rows = [search_row(), search_row(), search_row('10113010102A'), search_row('101130101001'),
                search_row('../../bad'), search_row(province='')]
        self.assertEqual(len(places.search_places('米东', lambda _:search_body(rows))), 1)

    def test_missing_place_never_silently_selects_parent(self):
        self.assertEqual(places.search_places('不存在区', lambda _:search_body([])), [])

    def test_html_and_bad_response_shape_are_errors(self):
        for raw in [b'<html></html>', b'({})', b'null', b'([]); evil()']:
            with self.subTest(raw=raw),self.assertRaises(pipeline.DataError):
                places.search_places('北京', lambda _:raw)

    def test_saved_station_is_validated_and_defines_cache_identity(self):
        location = places.search_places('米东', lambda _:search_body([search_row()]))[0]['location']
        saved = prefs.manual_location({'automatic_location':'false', 'selected_location':json.dumps(location)}.get)
        self.assertEqual(saved['station_id'], '101130112')
        self.assertTrue(prefs.cache_matches({'location':saved}, saved))
        self.assertFalse(prefs.cache_matches({'location':dict(saved, station_id='101130101')}, saved))
        self.assertFalse(prefs.cache_matches({'location':saved}, None))
        for identity in ['bad', '../../bad', '101130101001']:
            with self.assertRaises(pipeline.DataError):
                prefs.manual_location({'automatic_location':'false',
                    'selected_location':json.dumps(dict(saved, station_id=identity))}.get)

    def test_legacy_coordinates_migrate_using_name_and_province(self):
        location = {'country_code':'CN','city':'米东','province':'新疆','latitude':43.9,'longitude':87.6}
        saved = prefs.manual_location({'automatic_location':'false','selected_location':json.dumps(location)}.get)
        result = pipeline.geocode(saved, lambda _:search_body([search_row()]))
        self.assertEqual(result['station_id'], '101130112')
        self.assertEqual(result['display_name'], '米东')

    def test_selected_station_never_searches_or_requests_ip(self):
        location = places.search_places('米东', lambda _:search_body([search_row()]))[0]['location']
        calls = []
        def fetch(url):
            calls.append(url)
            if 'sk_2d' in url:return assignment('dataSK', OBSERVATION)
            if 'weixinfc' in url:return assignment('fc', FORECAST)
            self.fail('Selected station was re-resolved')
        with patch.object(pipeline, 'china_now', return_value=NOW):
            data = pipeline.run_pipeline(fetch, manual_location=location)
        self.assertEqual(len(calls), 2)
        self.assertEqual(data['location']['city'], '米东')
        self.assertEqual(data['ip_source'], '')


if __name__ == '__main__':unittest.main()
