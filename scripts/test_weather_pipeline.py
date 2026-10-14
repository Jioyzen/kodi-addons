import importlib.util
import ipaddress
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from weather_test_data import NOW, OBSERVATION, FORECAST, search_row, search_body, assignment

MODULE = Path(__file__).resolve().parents[1] / 'addons/weather.jioyzen/lib/pipeline.py'
spec = importlib.util.spec_from_file_location('jioyzen_weather_pipeline', MODULE)
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)
IP = '1.1.1.1'
LOCATION = {'country_code': 'CN', 'province': '新疆', 'city': '乌鲁木齐市',
            'district': '', 'precision': 'city', 'provider': 'pconline'}


def body(value):
    return json.dumps(value, ensure_ascii=False).encode()


class WeatherPipelineTests(unittest.TestCase):
    def test_empty_successful_ip_response_uses_next_source(self):
        seen = []
        def fetch(url):
            seen.append(url)
            return b'' if len(seen) == 1 else IP.encode()
        self.assertEqual(p.discover_ip(fetch)[0], IP)
        self.assertEqual(len(seen), 2)

    def test_network_failure_uses_next_source(self):
        def fetch(url):
            if url == p.IP_SOURCES[0]:
                raise OSError('unavailable')
            return IP.encode()
        self.assertEqual(p.discover_ip(fetch)[1], p.IP_SOURCES[1])

    def test_error_html_with_example_ip_is_rejected(self):
        with self.assertRaises(p.DataError):
            p.discover_ip(lambda _: b'<html>Example: 1.1.1.1</html>')

    def test_private_invalid_and_ipv6_are_rejected(self):
        for address in ('127.0.0.1', '10.0.0.1', '100.64.1.1', '999.1.1.1', '2606:4700:4700::1111'):
            with self.subTest(address=address), self.assertRaises(p.DataError):
                p.public_ipv4(address)

    def test_multiple_ips_are_not_assumed_correct(self):
        with self.assertRaises(p.DataError):
            p.discover_ip(lambda _: b'1.1.1.1 8.8.8.8')

    def test_gbk_location_is_decoded(self):
        value = {'city': '乌鲁木齐市'}
        self.assertEqual(p.decode_json(json.dumps(value, ensure_ascii=False).encode('gb18030')), value)

    def test_location_must_echo_requested_ip(self):
        with self.assertRaises(p.DataError):
            p.parse_location('pconline', {'ip': '8.8.8.8', 'proCode': '650000',
                                        'pro': '新疆', 'city': '乌鲁木齐市'}, IP)

    def test_district_from_ip_api_does_not_change_city_weather(self):
        location = p.parse_location('pconline', {'ip': IP, 'proCode': '650000',
              'pro': '新疆', 'city': '乌鲁木齐市', 'region': '米东区'}, IP)
        self.assertEqual(location['precision'], 'city')
        self.assertEqual(location['district'], '')
        requests = []
        def fetch(url):
            from urllib.parse import parse_qs, urlsplit
            requests.append(parse_qs(urlsplit(url).query)['cityname'][0])
            return search_body([search_row('101130101', '乌鲁木齐')])
        result = p.geocode(dict(location, district='米东区'), fetch)
        self.assertEqual(requests, ['乌鲁木齐市'])
        self.assertEqual(result['precision'], 'city')

    def test_ip9_validates_numeric_ip_identity(self):
        data = {'ret': 200, 'data': {'country_code': 'cn', 'long_ip': int(ipaddress.ip_address(IP)),
                                   'prov': '新疆', 'city': '乌鲁木齐', 'area': ''}}
        self.assertEqual(p.parse_location('ip9', data, IP)['precision'], 'city')
        data['data']['long_ip'] += 1
        with self.assertRaises(p.DataError):
            p.parse_location('ip9', data, IP)

    def test_domestic_locator_falls_back_without_foreign_vote(self):
        calls = []
        def fetch(url):
            calls.append(url)
            if 'ip9.com.cn' in url:
                return body({'err': 'failure'})
            return body({'ip': IP, 'proCode': '650000', 'pro': '新疆', 'city': '乌鲁木齐市'})
        self.assertEqual(p.locate_ip(IP, fetch)['provider'], 'pconline')
        self.assertEqual(len(calls), 2)

    def test_missing_chinese_codec_is_a_controlled_data_error(self):
        class NoChineseCodec(bytes):
            def decode(self, encoding):
                if encoding == 'gb18030':raise LookupError('unknown encoding')
                return super().decode(encoding)
        raw = NoChineseCodec(json.dumps({'city': '乌鲁木齐'}, ensure_ascii=False).encode('gb18030'))
        with self.assertRaises(p.DataError):p.decode_json(raw)

    def test_automatic_location_requires_province_and_exact_city(self):
        for rows in ([search_row('101130101', '乌鲁木齐', '贵州')],
                     [search_row('101130113', '乌鲁木齐县')],
                     [search_row('101130101', '乌鲁木齐'), search_row('101130102', '乌鲁木齐')]):
            with self.subTest(rows=rows), self.assertRaises(p.DataError):
                p.geocode(LOCATION, lambda _: search_body(rows))

    def test_current_rejects_wrong_station_invalid_temperature_and_old_time(self):
        for key, value in [('city', '101010100'), ('temp', '999'), ('temp', 'NaN'),
                           ('temp', None), ('weather', ''), ('time', 'bad'), ('date', '10月1日')]:
            with self.subTest(key=key), self.assertRaises(p.DataError):
                p.parse_current(dict(OBSERVATION, **{key: value}), '101130112', NOW)

    def test_optional_missing_fields_are_not_fabricated(self):
        current = p.parse_current(dict(OBSERVATION, SD='999%', wse='unknown'), '101130112', NOW)
        self.assertIsNone(current['relative_humidity_2m'])
        self.assertIsNone(current['wind_speed_10m'])
        self.assertNotIn('apparent_temperature', current)
        forecast = p.parse_forecast(FORECAST, NOW)
        self.assertNotIn('precipitation_probability_max', forecast)
        self.assertEqual(forecast['time'][0], '2026-10-15')
        self.assertEqual(forecast['temperature_2m_max'][0], 16)

    def test_year_boundary_uses_explicit_date_not_today_label(self):
        now = NOW.replace(year=2027, month=1, day=1)
        forecast = {'f0':'20261231180000','f':[{'fi':'12/31','fc':'10','fd':'0','fa':'00'},
                    {'fi':'01/01','fc':'9','fd':'-1','fa':'01'}]}
        self.assertEqual(p.parse_forecast(forecast, now)['time'], ['2027-01-01'])
        self.assertEqual(p.month_day('12月31日', now).isoformat(), '2026-12-31')

    def test_javascript_is_never_evaluated_and_wrong_wrappers_fail(self):
        for raw in [b'var dataSK=alert(1)', b'<html>error</html>', b'var wrong={};',
                    b'var dataSK={}; alert(1)']:
            with self.subTest(raw=raw), self.assertRaises(p.DataError):
                p.website_json(raw, 'dataSK')

    def test_forecast_failure_does_not_discard_current_weather(self):
        def fetch(url):
            if 'sk_2d' in url:return assignment('dataSK', OBSERVATION)
            raise OSError('forecast unavailable')
        with patch.object(p, 'china_now', return_value=NOW):
            data = p.fetch_weather({'station_id':'101130112'}, fetch)
        self.assertEqual(data['current']['temperature_2m'], 8.5)
        self.assertEqual(data['daily']['time'], [])

    def test_full_pipeline_has_no_public_ip_in_cached_output(self):
        def fetch(url):
            if url == p.IP_SOURCES[0]:return IP.encode()
            if 'ip9.com.cn' in url:return body({'ret':200,'data':{'country_code':'cn',
                'long_ip':int(ipaddress.ip_address(IP)),'prov':'新疆','city':'乌鲁木齐市'}})
            if 'toy1.weather.com.cn' in url:return search_body([search_row('101130101', '乌鲁木齐')])
            if 'sk_2d' in url:return assignment('dataSK', dict(OBSERVATION, city='101130101'))
            if 'weixinfc' in url:return assignment('fc', FORECAST)
            self.fail('Unexpected endpoint')
        with patch.object(p, 'china_now', return_value=NOW):data = p.run_pipeline(fetch)
        self.assertEqual(data['weather']['provider'], '中国天气网')
        self.assertEqual(data['location']['station_id'], '101130101')
        self.assertNotIn(IP, json.dumps(data))

    def test_failed_atomic_replace_preserves_existing_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'weather.json'
            path.write_text('old')
            with patch.object(p.os, 'replace', side_effect=OSError('full')):
                with self.assertRaises(OSError):p.atomic_save(str(path), {'new': True})
            self.assertEqual(path.read_text(), 'old')
            self.assertEqual(list(Path(folder).iterdir()), [path])


if __name__ == '__main__':
    unittest.main()
