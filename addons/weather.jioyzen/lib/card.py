"""Presentation data for the home card, independent of Kodi and network I/O."""
import datetime
import math
import os

PREFIX = 'Jioyzen.Card.'
FIELDS = ('City', 'Condition', 'Icon', 'Temperature', 'FeelsLike', 'Humidity',
          'Wind', 'High', 'Low', 'Rain', 'Updated', 'Ready')
# Tianqi's condition codes are NOT WMO or Kodi weather codes.
TIANQI_CODES = {0: 0, 1: 1, 2: 1, 3: 2, 51: 7, 53: 7, 55: 8,
               56: 6, 57: 6, 61: 7, 63: 8, 65: 9, 66: 6, 67: 6,
               71: 14, 73: 15, 75: 15, 77: 14, 80: 3, 81: 3,
               82: 3, 85: 14, 86: 15, 95: 4, 96: 4, 99: 4}


def finite(value):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value))


def rounded(value, suffix=''):
    return str(round(value)) + suffix if finite(value) else ''


def properties(data, addon_path, outlook, fallback_icon, stale=False, today=None):
    """Always clear absent fields so an older update cannot leave phantom data."""
    values = dict.fromkeys(FIELDS, '')
    current = data.get('weather', {}).get('current', {})
    daily = data.get('weather', {}).get('daily', {})
    location = data.get('location', {})
    city = location.get('city', '')
    values['City'] = (location.get('display_name') or
                      (city if city.endswith('市') or not city else city + '市'))
    values['Condition'] = outlook if outlook != '未知' else ''
    values['Temperature'] = rounded(current.get('temperature_2m'), '°')
    values['FeelsLike'] = rounded(current.get('apparent_temperature'), '°')
    humidity = current.get('relative_humidity_2m')
    values['Humidity'] = rounded(humidity, '%') if finite(humidity) and 0 <= humidity <= 100 else ''
    code = current.get('native_code') if 'native_code' in current else TIANQI_CODES.get(current.get('weather_code'))
    if code not in (0, 1, 2, 3, 4, 6, 7, 8, 9, 14, 15):
        code = None
    values['Icon'] = (os.path.join(addon_path, 'resources', 'icons', f'b{code}.png')
                      if code is not None else fallback_icon)
    speed, direction = current.get('wind_speed_10m'), current.get('wind_direction_10m')
    if finite(speed) and speed >= 0 and finite(direction):
        winds = ('北风', '东北风', '东风', '东南风', '南风', '西南风', '西风', '西北风')
        level = sum(speed >= limit for limit in (1, 5.5, 11, 20, 29, 39, 50, 62, 75, 89, 103, 118))
        values['Wind'] = winds[round(direction / 45) % 8] + f' {level}级'
    if current.get('wind_text'):
        values['Wind'] = current['wind_text']
    stamp = current.get('time', '')
    try:
        parsed = datetime.datetime.fromisoformat(stamp)
        values['Updated'] = parsed.strftime('%H:%M') + (' 缓存' if stale else ' 更新')
    except (TypeError, ValueError):
        pass
    if today is None:
        today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).date().isoformat()
    dates = daily.get('time', [])
    if today in dates:
        i = dates.index(today)
        for field, key, suffix in (('High', 'temperature_2m_max', '°'),
                                   ('Low', 'temperature_2m_min', '°'),
                                   ('Rain', 'precipitation_probability_max', '%')):
            series = daily.get(key, [])
            value = series[i] if i < len(series) else None
            if field != 'Rain' or finite(value) and 0 <= value <= 100:
                values[field] = rounded(value, suffix)
    values['Ready'] = 'true' if values['City'] and values['Temperature'] else ''
    return {PREFIX + key: value for key, value in values.items()}
