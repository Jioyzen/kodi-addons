import copy
import importlib.util
from pathlib import Path
import unittest

PATH = Path(__file__).resolve().parents[1] / 'addons/weather.jioyzen/lib/card.py'
spec = importlib.util.spec_from_file_location('weather_card', PATH)
card = importlib.util.module_from_spec(spec)
spec.loader.exec_module(card)


class WeatherCardTests(unittest.TestCase):
    def setUp(self):
        self.data = {'location': {'city': '乌鲁木齐'}, 'weather': {
            'current': {'time': '2026-10-14T18:30', 'temperature_2m': 17.9,
                        'apparent_temperature': 14.6, 'relative_humidity_2m': 29,
                        'wind_speed_10m': 7.2, 'wind_direction_10m': 347, 'weather_code': 0},
            'daily': {'time': ['2026-10-14', '2026-10-15'],
                      'temperature_2m_max': [18.8, 13], 'temperature_2m_min': [7.8, 4],
                      'precipitation_probability_max': [0, 100]}}}

    def render(self, **kwargs):
        return card.properties(self.data, '/addon', '晴', 'fallback.png',
                               today=kwargs.pop('today', '2026-10-14'), **kwargs)

    def test_live_payload_and_zero_probability(self):
        p = self.render()
        self.assertEqual(p['Jioyzen.Card.City'], '乌鲁木齐市')
        self.assertEqual(p['Jioyzen.Card.Temperature'], '18°')
        self.assertEqual(p['Jioyzen.Card.Wind'], '北风 2级')
        self.assertEqual(p['Jioyzen.Card.Rain'], '0%')
        self.assertEqual(p['Jioyzen.Card.Updated'], '18:30 更新')
        self.assertEqual(p['Jioyzen.Card.Ready'], 'true')

    def test_selected_district_name_never_gets_a_city_suffix(self):
        self.data['location'].update(city='朝阳',display_name='朝阳区')
        self.assertEqual(self.render()['Jioyzen.Card.City'],'朝阳区')

    def test_missing_optional_values_clear_previous_properties(self):
        self.data['weather']['current'].update(apparent_temperature=None,
            relative_humidity_2m=None, wind_speed_10m=None)
        self.data['weather']['daily'].pop('precipitation_probability_max')
        p = self.render()
        for key in ('FeelsLike', 'Humidity', 'Wind', 'Rain'):
            self.assertEqual(p['Jioyzen.Card.' + key], '')
        self.assertEqual(p['Jioyzen.Card.Ready'], 'true')

    def test_unknown_code_uses_existing_kodi_icon(self):
        self.data['weather']['current']['weather_code'] = 45
        self.assertEqual(self.render()['Jioyzen.Card.Icon'], 'fallback.png')
        self.data['weather']['current']['weather_code'] = 95
        self.assertEqual(self.render()['Jioyzen.Card.Icon'], '/addon/resources/icons/b4.png')

    def test_today_changes_without_showing_yesterdays_forecast(self):
        p = self.render(today='2026-10-15', stale=True)
        self.assertEqual(p['Jioyzen.Card.High'], '13°')
        self.assertEqual(p['Jioyzen.Card.Rain'], '100%')
        self.assertEqual(p['Jioyzen.Card.Updated'], '18:30 缓存')
        p = self.render(today='2026-10-16')
        for key in ('High', 'Low', 'Rain'):
            self.assertEqual(p['Jioyzen.Card.' + key], '')

    def test_invalid_values_are_never_displayed_as_real_readings(self):
        for value in (float('nan'), float('inf'), True, '18', None):
            with self.subTest(value=value):
                self.data['weather']['current']['temperature_2m'] = value
                p = self.render()
                self.assertEqual(p['Jioyzen.Card.Temperature'], '')
                self.assertEqual(p['Jioyzen.Card.Ready'], '')
        self.data['weather']['current']['relative_humidity_2m'] = 110
        self.data['weather']['daily']['precipitation_probability_max'][0] = -1
        p = self.render()
        self.assertEqual(p['Jioyzen.Card.Humidity'], '')
        self.assertEqual(p['Jioyzen.Card.Rain'], '')


if __name__ == '__main__':
    unittest.main()
