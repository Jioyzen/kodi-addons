"""Kodi weather provider; `probe` exercises data only without UI changes."""
import json
import os
import sys
import time

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs

from lib.pipeline import atomic_save, run_pipeline, WEATHER_PROVIDER
from lib.card import properties as card_properties, rounded, finite, FIELDS, PREFIX
from lib.preferences import manual_location, cache_matches, clear_manual_if_automatic

ADDON = xbmcaddon.Addon('weather.jioyzen')
PROFILE = xbmcvfs.translatePath(ADDON.getAddonInfo('profile'))
CACHE = os.path.join(PROFILE, 'last_success.json')

# WMO codes -> Kodi's built-in weather icon codes.
CONDITIONS = {
    0: ('晴', 32), 1: ('晴间多云', 34), 2: ('局部多云', 30), 3: ('阴', 26),
    45: ('雾', 20), 48: ('冻雾', 20), 51: ('小毛毛雨', 9), 53: ('毛毛雨', 9),
    55: ('强毛毛雨', 9), 56: ('冻毛毛雨', 8), 57: ('强冻毛毛雨', 8),
    61: ('小雨', 11), 63: ('中雨', 12), 65: ('大雨', 12), 66: ('小冻雨', 10),
    67: ('强冻雨', 10), 71: ('小雪', 14), 73: ('中雪', 16), 75: ('大雪', 41),
    77: ('雪粒', 16), 80: ('小阵雨', 40), 81: ('阵雨', 40), 82: ('强阵雨', 40),
    85: ('阵雪', 42), 86: ('强阵雪', 43), 95: ('雷暴', 4),
    96: ('雷暴伴冰雹', 17), 99: ('强雷暴伴冰雹', 17)}


def load_cache():
    try:
        with open(CACHE, encoding='utf-8') as stream:
            data = json.load(stream)
        if (data.get('version') != 1 or data.get('status') != 'ok' or
                not isinstance(data.get('fetched_at'), (int, float)) or
                not isinstance(data.get('location'), dict) or
                not isinstance(data.get('weather'), dict)):
            return None
        return data
    except (OSError, ValueError, AttributeError):
        return None


def condition(code, is_day=True):
    label, icon = CONDITIONS.get(int(code), ('未知', 'na'))
    if not is_day:
        icon = {32: 31, 34: 33, 30: 29}.get(icon, icon)
    return label, str(icon)


def publish(data, stale=False):
    window = xbmcgui.Window(12600)
    weather, location = data['weather'], data['location']
    current, daily = weather['current'], weather['daily']
    label = location.get('display_name') or location.get('district') or location['city']
    outlook, icon = condition(current['weather_code'], current['is_day'])
    outlook = current.get('condition_text') or outlook
    icons = 'resource://resource.images.weathericons.default/'
    direction = ('N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE',
                 'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW')
    properties = {
        'WeatherProvider': ADDON.getAddonInfo('name'), 'Location': label,
        'Current.Location': label, 'Location1': label, 'Location2': '', 'Location3': '',
        'Locations': '1', 'Current.Condition': outlook,
        'Current.Temperature': str(round(current['temperature_2m'])),
        'Current.FeelsLike': rounded(current.get('apparent_temperature')),
        'Current.Humidity': rounded(current.get('relative_humidity_2m')),
        'Current.Wind': str(current['wind_speed_10m']) if finite(current.get('wind_speed_10m')) else '',
        'Current.WindDirection': direction[round(current['wind_direction_10m'] / 22.5) % 16] if finite(current.get('wind_direction_10m')) else '',
        'Current.OutlookIcon': icons + icon + '.png', 'Current.FanartCode': icon,
        'Current.IsFetched': 'true', 'Weather.IsFetched': 'true',
        'Daily.IsFetched': 'true' if daily['time'] else 'false', 'Hourly.IsFetched': 'false',
        'Jioyzen.Precision': location['precision'],
        'Jioyzen.LocationProvider': location['provider'],
        'Jioyzen.DataTime': current['time'], 'Jioyzen.Stale': str(stale).lower(),
        'Jioyzen.Attribution': weather['attribution'],
        'Today.Sunrise': (daily.get('sunrise') or [''])[0].split('T')[-1],
        'Today.Sunset': (daily.get('sunset') or [''])[0].split('T')[-1]}
    # Clear old extended forecasts when the new provider has fewer days.
    for i in range(16):
        for field in ('Title', 'HighTemp', 'LowTemp', 'Outlook', 'OutlookIcon', 'FanartCode'):
            properties[f'Day{i}.{field}'] = ''
        for field in ('ShortDate', 'ShortDay', 'HighTemperature', 'LowTemperature', 'Outlook', 'OutlookIcon', 'Precipitation'):
            properties[f'Daily.{i+1}.{field}'] = ''
    for i, date in enumerate(daily['time']):
        outlook, icon = condition(daily['weather_code'][i])
        high, low = daily['temperature_2m_max'][i], daily['temperature_2m_min'][i]
        properties.update({f'Day{i}.Title': date, f'Day{i}.HighTemp': str(round(high)),
                           f'Day{i}.LowTemp': str(round(low)), f'Day{i}.Outlook': outlook,
                           f'Day{i}.OutlookIcon': icons + icon + '.png', f'Day{i}.FanartCode': icon,
                           f'Daily.{i+1}.ShortDate': date[5:], f'Daily.{i+1}.ShortDay': date[5:],
                           f'Daily.{i+1}.HighTemperature': f'{round(high)}°C',
                           f'Daily.{i+1}.LowTemperature': f'{round(low)}°C',
                           f'Daily.{i+1}.Outlook': outlook,
                           f'Daily.{i+1}.OutlookIcon': icons + icon + '.png',
                           f'Daily.{i+1}.Precipitation': rounded((daily.get('precipitation_probability_max') or [None] * len(daily['time']))[i], '%')})
    card = card_properties(data, ADDON.getAddonInfo('path'),
                           properties['Current.Condition'],
                           properties['Current.OutlookIcon'], stale)
    properties.update(card)
    for key, value in properties.items():
        window.setProperty(key, value)
    # Kodi clears WINDOW_WEATHER properties before every network refresh.
    # Home is a persistent presentation snapshot, unaffected by that reset.
    home = xbmcgui.Window(10000)
    for key, value in card.items():
        home.setProperty(key, value)


def publish_status(location, failed=False):
    """Keep the card visible without showing another city's observations."""
    location = location or {}
    label = location.get('display_name') or location.get('city') or '天气'
    card = {PREFIX + field: '' for field in FIELDS}
    card.update({PREFIX + 'City': label, PREFIX + 'Temperature': '--°',
                 PREFIX + 'Condition': '暂时无法获取天气' if failed else '正在更新天气',
                 PREFIX + 'Updated': '更新失败' if failed else '更新中',
                 PREFIX + 'Ready': 'true'})
    for wid in (10000, 12600):
        window = xbmcgui.Window(wid)
        for key, value in card.items():
            window.setProperty(key, value)
    window = xbmcgui.Window(12600)
    # Do not leave the native weather page pointing at the previous city.
    for key in ('Location', 'Current.Location', 'Location1'):
        window.setProperty(key, label)
    for key in ('Current.Temperature', 'Current.Condition', 'Current.FeelsLike',
                'Current.Humidity', 'Current.Wind', 'Current.WindDirection',
                'Current.OutlookIcon', 'Current.FanartCode'):
        window.setProperty(key, '')
    for key in ('Current.IsFetched', 'Weather.IsFetched', 'Daily.IsFetched', 'Hourly.IsFetched'):
        window.setProperty(key, 'false')


def selection_is_current(location):
    # A request for the old city can finish after the user changes settings.
    # Get a fresh settings handle before allowing it to publish or persist.
    try:
        current = xbmcaddon.Addon('weather.jioyzen')
        return manual_location(current.getSetting) == location
    except (TypeError, ValueError):
        return False


def main():
    probe = len(sys.argv) > 1 and sys.argv[1] == 'probe'
    cached = load_cache()
    selected_location = None
    usable_cache = False
    try:
        clear_manual_if_automatic(ADDON.getSetting, ADDON.setSetting)
        selected_location = manual_location(ADDON.getSetting)
        usable_cache = bool(cached and cache_matches(cached, selected_location))
        if not probe and usable_cache:
            fresh = (cached['weather'].get('provider') == WEATHER_PROVIDER and
                     0 <= time.time() - cached['fetched_at'] < 1800)
            if not selection_is_current(selected_location):
                return
            publish(cached, stale=not fresh)
            if fresh:
                return
        elif not probe:
            if not selection_is_current(selected_location):
                return
            publish_status(selected_location)
        data = (run_pipeline(manual_location=selected_location)
                if selected_location is not None else run_pipeline())
        if not selection_is_current(selected_location):
            xbmc.log('[weather.jioyzen] Location changed; discarded superseded refresh', xbmc.LOGINFO)
            return
        if not cache_matches(data, selected_location):
            raise ValueError('Weather response does not match selected location')
        if probe:
            atomic_save(CACHE, data)
            atomic_save(os.path.join(PROFILE, 'probe_result.json'), data)
        else:
            publish(data)
            # A disk-cache error must not undo a successful live refresh.
            try:
                atomic_save(CACHE, data)
            except (OSError, ValueError):
                xbmc.log('[weather.jioyzen] Live data ready; disk cache unavailable', xbmc.LOGWARNING)
        xbmc.log('[weather.jioyzen] Data ready; precision=' + data['location']['precision'], xbmc.LOGINFO)
    except (OSError, ValueError) as error:
        xbmc.log('[weather.jioyzen] Data unavailable: ' + type(error).__name__, xbmc.LOGWARNING)
        if probe:
            atomic_save(os.path.join(PROFILE, 'probe_result.json'),
                        {'status': 'error', 'error_type': type(error).__name__})
        elif selection_is_current(selected_location):
            if usable_cache:
                publish(cached, stale=True)
            else:
                publish_status(selected_location, failed=True)


if __name__ == '__main__':
    main()
