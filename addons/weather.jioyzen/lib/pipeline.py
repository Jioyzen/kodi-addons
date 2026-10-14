"""Weather data without Kodi imports or third-party Python dependencies.

IP lookup is city-level evidence, not a device GPS fix. Station selection must agree
with the domestic province and city; a same-name first result is not enough.
"""
import datetime
import ipaddress
import json
import math
import os
import re
import tempfile
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

IP_SOURCES = ('https://ip.3322.net', 'http://v4.666666.host:66/ip',
              'https://myip.ipip.net')
MAX_BODY = 1024 * 1024


class DataError(ValueError):
    pass


def fetch_bytes(url):
    request = Request(url, headers={'User-Agent': 'Mozilla/5.0 (compatible; JioyzenWeather/0.5)',
                                   'Referer': 'https://www.weather.com.cn/',
                                   'Accept': 'application/json,text/plain,*/*'})
    with urlopen(request, timeout=8) as response:
        data = response.read(MAX_BODY + 1)
    if len(data) > MAX_BODY:
        raise DataError('Response too large')
    return data


def decode_json(data):
    for encoding in ('utf-8-sig', 'gb18030'):
        try:
            value = json.loads(data.decode(encoding))
            if isinstance(value, dict):
                return value
        except (UnicodeError, ValueError, LookupError):
            pass
    raise DataError('Invalid JSON response')


def public_ipv4(value):
    try:
        address = ipaddress.ip_address(value)
    except ValueError as error:
        raise DataError('Invalid IP') from error
    if address.version != 4 or not address.is_global:
        raise DataError('Public IPv4 required')
    return str(address)


def discover_ip(fetch=fetch_bytes, events=None):
    for index, source in enumerate(IP_SOURCES):
        try:
            text = fetch(source).decode('utf-8', errors='replace').strip()
            if len(text) > 32768 or '<html' in text.lower() or '<!doctype' in text.lower():
                raise DataError('Unexpected IP response')
            # The primary returns an IP only. Do not extract an example IP
            # from arbitrary HTML/error pages, even when HTTP status is 200.
            candidates = ([text] if index == 0 else
                          re.findall(r'(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])', text))
            addresses = set()
            for value in candidates:
                try:
                    addresses.add(public_ipv4(value))
                except DataError:
                    pass
            if len(addresses) != 1:
                raise DataError('No unambiguous public IP')
            return addresses.pop(), source
        except (OSError, ValueError) as error:
            if events is not None:
                events.append({'stage': 'ip', 'source': source,
                               'error': type(error).__name__})
    raise DataError('Domestic IP sources unavailable')


def name_key(value):
    value = str(value or '').strip()
    for suffix in ('维吾尔自治区', '壮族自治区', '回族自治区', '特别行政区',
                   '自治区', '自治州', '地区', '省', '市', '区', '县'):
        if value.endswith(suffix):
            return value[:-len(suffix)]
    return value


def parse_location(provider, data, ip):
    public_ipv4(ip)
    if provider == 'pconline':
        if data.get('ip') != ip or data.get('err') or not data.get('city'):
            raise DataError('Invalid domestic location')
        code = str(data.get('proCode') or '')
        if not code.isdigit() or not 110000 <= int(code) <= 659999:
            raise DataError('Mainland province missing')
        province, city, district = data.get('pro'), data['city'], data.get('region')
    elif provider == 'ip9':
        details = data.get('data') or {}
        if (data.get('ret') != 200 or not isinstance(details, dict) or
                str(details.get('country_code', '')).lower() != 'cn' or
                str(details.get('long_ip')) != str(int(ipaddress.ip_address(ip)))):
            raise DataError('Invalid domestic location')
        province, city, district = details.get('prov'), details.get('city'), details.get('area')
    else:
        raise DataError('Unsupported location provider')
    if not isinstance(province, str) or not province.strip() or not isinstance(city, str) or not city.strip():
        raise DataError('Province/city missing')
    # This provider deliberately uses city weather, even when the IP API
    # happens to return a district. County-level IP evidence is less stable.
    return {'country_code': 'CN', 'province': province.strip(), 'city': city.strip(),
            'district': '', 'precision': 'city', 'provider': provider}


def locate_ip(ip, fetch=fetch_bytes, events=None):
    ip = public_ipv4(ip)
    # CoreELEC's minimal Python may omit GBK/GB18030 codecs. Prefer the
    # tested UTF-8 provider; a missing optional codec is a normal fallback.
    urls = (('ip9', 'https://ip9.com.cn/get?' + urlencode({'ip': ip})),
            ('pconline', 'https://whois.pconline.com.cn/ipJson.jsp?' +
             urlencode({'ip': ip, 'json': 'true'})))
    for provider, url in urls:
        try:
            return parse_location(provider, decode_json(fetch(url)), ip)
        except (OSError, ValueError) as error:
            if events is not None:
                events.append({'stage': 'location', 'source': provider,
                               'error': type(error).__name__})
    # Foreign IP databases demonstrably mislocated this China Mobile IP;
    # never silently substitute them or vote by number of agreeing sources.
    raise DataError('Domestic location unavailable')


def coordinates(latitude, longitude):
    try:
        lat, lon = float(latitude), float(longitude)
    except (TypeError, ValueError) as error:
        raise DataError('Invalid coordinates') from error
    if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
        raise DataError('Invalid coordinates')
    return lat, lon


# Website data endpoints; decode JSON only, never execute their JavaScript.
CHINA_TIME = datetime.timezone(datetime.timedelta(hours=8))
WEATHER_PROVIDER = '中国天气网'


def china_now():
    return datetime.datetime.now(CHINA_TIME)


def station_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'101[0-9]{6}', value):
        raise DataError('Invalid weather station')
    return value


def website_json(raw, variable=None):
    try:
        text = raw.decode('utf-8-sig').strip()
        if variable:
            match = re.fullmatch(r'var\s+' + re.escape(variable) + r'\s*=\s*(.*?)\s*;?', text, re.S)
            if not match:
                raise DataError('Unexpected weather response')
            text = match.group(1)
        else:
            match = re.fullmatch(r'\(\s*(.*?)\s*\)\s*;?', text, re.S)
            if not match:
                raise DataError('Unexpected search response')
            # The website emits JavaScript apostrophe escapes in some names.
            text = match.group(1).replace("\\'", "'")
        return json.loads(text)
    except (UnicodeError, ValueError) as error:
        raise DataError('Invalid website JSON') from error


def search_locations(query, fetch=fetch_bytes):
    query = str(query or '').strip()
    if not query or len(query) > 80 or any(ord(char) < 32 for char in query):
        raise DataError('Invalid place query')
    for name in dict.fromkeys((query, name_key(query))):
        rows = website_json(fetch('https://toy1.weather.com.cn/search?' +
                                  urlencode({'cityname': name})))
        if not isinstance(rows, list):
            raise DataError('Invalid search results')
        choices, seen = [], set()
        for row in rows:
            fields = str(row.get('ref', '')).split('~') if isinstance(row, dict) else []
            if len(fields) < 10:
                continue
            identity, title, province = fields[0], fields[2].strip(), fields[9].strip()
            # Exclude scenic sites and township IDs: these are not supported
            # by the city/county realtime endpoint.
            if not re.fullmatch(r'101[0-9]{6}', identity) or not title or not province or identity in seen:
                continue
            seen.add(identity)
            location = {'country_code': 'CN', 'province': province, 'city': title,
                        'district': '', 'display_name': title, 'station_id': identity,
                        'provider': 'manual', 'precision': 'station'}
            choices.append({'label': title, 'region': province, 'location': location})
        if choices:
            return choices
    return []


def geocode(location, fetch=fetch_bytes):
    if location.get('station_id'):
        station_id(location['station_id'])
        return dict(location)
    choices = search_locations(location['city'], fetch)
    matches = [row['location'] for row in choices
               if name_key(row['location']['province']) == name_key(location['province'])
               and name_key(row['location']['city']) == name_key(location['city'])
               and row['location']['city'].endswith('县') == location['city'].endswith('县')]
    if len(matches) != 1:
        raise DataError('Place missing or ambiguous')
    result = dict(location, station_id=matches[0]['station_id'],
                  display_name=location.get('display_name') or matches[0]['display_name'])
    # Upgrade pre-existing coordinate preferences without a new user choice.
    result['precision'] = 'station' if location.get('provider') == 'manual' else 'city'
    return result


def numeric(value, minimum, maximum):
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and minimum <= result <= maximum else None


def month_day(value, anchor):
    match = re.fullmatch(r'(\d{1,2})[月/](\d{1,2})(?:日.*)?', str(value))
    if not match:
        raise DataError('Weather date missing')
    candidates = []
    for year in (anchor.year - 1, anchor.year, anchor.year + 1):
        try:
            candidates.append(datetime.date(year, int(match[1]), int(match[2])))
        except ValueError:
            pass
    if not candidates:
        raise DataError('Invalid weather date')
    return min(candidates, key=lambda date: abs((date - anchor.date()).days))


# Only used for Kodi's fallback icons; retain original condition and native
# code independently instead of reducing all Chinese conditions to WMO.
CHINA_WMO = {0: 0, 1: 2, 2: 3, 3: 80, 4: 95, 5: 96, 6: 66,
             7: 61, 8: 63, 9: 65, 10: 65, 11: 65, 12: 65, 13: 85,
             14: 71, 15: 73, 16: 75, 17: 75, 18: 45, 19: 67,
             20: 45, 21: 63, 22: 65, 23: 65, 24: 65, 25: 65,
             26: 73, 27: 75, 28: 75, 29: 45, 30: 45, 31: 45,
             53: 45, 301: 61, 302: 71}


def native_code(value):
    match = re.fullmatch(r'[dn]?(\d{2,3})', str(value))
    return int(match[1]) if match else None


def parse_current(data, identity, now):
    if not isinstance(data, dict) or str(data.get('city')) != identity:
        raise DataError('Wrong weather station')
    temperature = numeric(data.get('temp'), -90, 65)
    label = data.get('weather')
    if temperature is None or not isinstance(label, str) or not label.strip():
        raise DataError('Current weather missing')
    day = month_day(data.get('date'), now)
    try:
        clock = datetime.time.fromisoformat(str(data.get('time')))
        stamp = datetime.datetime.combine(day, clock, CHINA_TIME)
    except ValueError as error:
        raise DataError('Invalid observation time') from error
    if not -900 <= (now - stamp).total_seconds() <= 48 * 3600:
        raise DataError('Observation too old')
    code = native_code(data.get('weathercode'))
    humidity = numeric(str(data.get('SD', '')).rstrip('%'), 0, 100)
    speed = numeric(str(data.get('wse', '')).removesuffix('km/h'), 0, 500)
    wind = str(data.get('WD') or '')
    scale = str(data.get('WS') or '')
    wind_text = ' '.join(item for item in (wind, scale) if item)
    return {'time': stamp.isoformat(), 'temperature_2m': temperature,
            'relative_humidity_2m': humidity, 'wind_speed_10m': speed,
            'wind_text': wind_text, 'condition_text': label.strip(), 'native_code': code,
            'weather_code': CHINA_WMO.get(code, -1),
            'is_day': 0 if str(data.get('weathercode', '')).startswith('n') else 1}


def parse_forecast(data, now):
    if not isinstance(data, dict) or not isinstance(data.get('f'), list):
        raise DataError('Forecast missing')
    try:
        anchor = datetime.datetime.strptime(str(data.get('f0')), '%Y%m%d%H%M%S').replace(tzinfo=CHINA_TIME)
    except ValueError as error:
        raise DataError('Forecast time missing') from error
    if not -900 <= (now - anchor).total_seconds() <= 72 * 3600:
        raise DataError('Forecast too old')
    days = {}
    for row in data['f']:
        if not isinstance(row, dict):
            continue
        try:
            day = month_day(row.get('fi'), anchor)
        except DataError:
            continue
        if not 0 <= (day - now.date()).days <= 6:
            continue
        high, low = numeric(row.get('fc'), -90, 65), numeric(row.get('fd'), -90, 65)
        if high is None or low is None or high < low:
            continue
        days[day] = (high, low, CHINA_WMO.get(native_code(row.get('fa')), -1))
    if not days:
        raise DataError('Usable forecast missing')
    dates = sorted(days)
    return {'time': [date.isoformat() for date in dates],
            'temperature_2m_max': [days[date][0] for date in dates],
            'temperature_2m_min': [days[date][1] for date in dates],
            'weather_code': [days[date][2] for date in dates]}


def fetch_weather(location, fetch=fetch_bytes):
    identity = station_id(location.get('station_id'))
    now = china_now()
    current = parse_current(website_json(fetch(
        'https://d1.weather.com.cn/sk_2d/' + identity + '.html'), 'dataSK'), identity, now)
    try:
        daily = parse_forecast(website_json(fetch(
            'https://d1.weather.com.cn/weixinfc/' + identity + '.html'), 'fc'), now)
    except (OSError, ValueError):
        # A broken forecast must not discard a valid realtime observation.
        daily = {'time': [], 'weather_code': [], 'temperature_2m_max': [], 'temperature_2m_min': []}
    return {'provider': WEATHER_PROVIDER, 'attribution': 'https://www.weather.com.cn/',
            'current': current, 'daily': daily, 'timezone': 'Asia/Shanghai'}


def run_pipeline(fetch=fetch_bytes, manual_location=None):
    start = time.monotonic()
    events = []
    if manual_location is None:
        ip, ip_source = discover_ip(fetch, events)
        location = geocode(locate_ip(ip, fetch, events), fetch)
    else:
        ip_source = ''
        location = geocode(manual_location, fetch)
    weather = fetch_weather(location, fetch)
    return {'version': 1, 'status': 'ok', 'fetched_at': time.time(),
            'elapsed_seconds': round(time.monotonic() - start, 3),
            'ip_source': ip_source, 'location': location, 'weather': weather,
            'fallback_events': events}


def atomic_save(path, data):
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=folder,
                                         prefix='.weather-', delete=False) as stream:
            temporary = stream.name
            json.dump(data, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary:
            os.unlink(temporary)
