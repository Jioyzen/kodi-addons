"""Location preferences without Kodi imports; automatic location is the default."""
import json
from .pipeline import DataError, name_key, coordinates, station_id


def clear_manual_if_automatic(read_setting, write_setting):
    """Forget manual choices after automatic mode has been saved.

    Do not rewrite empty settings on every weather refresh. Weather caches
    are independent: an offline refresh may still show its last observation.
    """
    if read_setting('automatic_location') == 'false':
        return False
    changed = False
    for key in ('selected_location', 'location_label', 'province', 'city'):
        if read_setting(key):
            write_setting(key, '')
            changed = True
    return changed


def manual_location(read_setting):
    if read_setting('automatic_location') != 'false':
        return None
    selection = read_setting('selected_location')
    if selection:
        try:
            location = json.loads(selection)
            if (not isinstance(location, dict) or location.get('country_code') != 'CN' or
                    not isinstance(location.get('city'), str) or not location['city'].strip() or
                    not isinstance(location.get('province'), str) or not location['province'].strip() or
                    ('display_name' in location and (not isinstance(location['display_name'], str) or
                                                     not location['display_name'].strip())) or
                    any(isinstance(location.get(key), bool) for key in ('latitude', 'longitude'))):
                raise DataError('Invalid saved location')
            if 'station_id' in location:
                station_id(location['station_id'])
                return dict(location, provider='manual', precision='station')
            lat, lon = coordinates(location.get('latitude'), location.get('longitude'))
        except (TypeError, ValueError) as error:
            raise DataError('Invalid saved location') from error
        return {'country_code': 'CN', 'province': location['province'], 'city': location['city'],
                'district': '', 'display_name': location.get('display_name', location['city']),
                'latitude': lat, 'longitude': lon, 'provider': 'manual', 'precision': 'coordinates'}
    # Compatibility for previously configured province/city; these fields are
    # hidden in the new UI and replaced on the next explicit place selection.
    province = str(read_setting('province') or '').strip()
    city = str(read_setting('city') or '').strip()
    if (not province or not city or len(province) > 64 or len(city) > 64 or
            any(ord(char) < 32 for char in province + city)):
        raise DataError('Manual province and city required')
    return {'country_code': 'CN', 'province': province, 'city': city,
            'district': '', 'precision': 'city', 'provider': 'manual'}


def cache_matches(data, location):
    previous = data.get('location') or {}
    if location is None:
        return previous.get('provider') != 'manual'
    if location.get('station_id'):
        return (previous.get('provider') == 'manual' and
                previous.get('station_id') == location['station_id'])
    if 'latitude' in location or 'longitude' in location:
        try:
            selected = coordinates(location['latitude'], location['longitude'])
            cached = coordinates(previous.get('latitude'), previous.get('longitude'))
        except (DataError, KeyError):
            return False
        return (previous.get('provider') == 'manual' and selected == cached and
                previous.get('display_name', previous.get('city')) ==
                location.get('display_name', location.get('city')))
    return (previous.get('provider') == 'manual' and
            name_key(previous.get('province')) == name_key(location['province']) and
            name_key(previous.get('city')) == name_key(location['city']))
