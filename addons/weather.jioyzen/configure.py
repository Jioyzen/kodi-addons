"""Weather configuration and user-confirmed location search."""
import json
import os
import sys

import xbmc
import xbmcaddon
import xbmcgui

from lib.pipeline import DataError
from lib.preferences import manual_location, clear_manual_if_automatic
from lib.places import search_places


def refresh_weather(addon):
    # Weather.Refresh invalidates Kodi's manager, but it may defer starting
    # the provider until a weather label/window is queried. The desktop uses
    # our persistent Home properties, so update those directly as well.
    xbmc.executebuiltin('Weather.Refresh')
    xbmc.executebuiltin('RunScript(' + os.path.join(addon.getAddonInfo('path'), 'default.py') + ')')


def select_location(addon):
    dialog = xbmcgui.Dialog()
    query = dialog.input(addon.getLocalizedString(32007), type=xbmcgui.INPUT_ALPHANUM)
    if not query.strip():
        return
    progress = xbmcgui.DialogProgress()
    try:
        progress.create(addon.getAddonInfo('name'), addon.getLocalizedString(32008))
        choices = search_places(query)
        if progress.iscanceled():
            return
    except (OSError, ValueError):
        dialog.notification(addon.getAddonInfo('name'), addon.getLocalizedString(32011),
                            addon.getAddonInfo('icon'))
        return
    finally:
        progress.close()
    if not choices:
        dialog.ok(addon.getAddonInfo('name'), addon.getLocalizedString(32010))
        return
    items = []
    for choice in choices:
        item = xbmcgui.ListItem(choice['label'], label2=choice['region'], offscreen=True)
        items.append(item)
    selected = dialog.select(addon.getLocalizedString(32012), items, useDetails=True)
    if selected < 0 or selected >= len(choices):
        return
    choice = choices[selected]
    addon.setSetting('automatic_location', 'false')
    addon.setSetting('selected_location', json.dumps(choice['location'], ensure_ascii=False))
    addon.setSetting('location_label', choice['label'] + ' · ' + choice['region'])
    refresh_weather(addon)


def restore_location_label(addon):
    """Recover the display string from the validated selection on reopening.

    Older versions rejected the empty default of this read-only setting;
    the actual station selection remains intact and is the source of truth.
    """
    clear_manual_if_automatic(addon.getSetting, addon.setSetting)
    if not addon.getSetting('selected_location'):
        return
    try:
        location = manual_location(lambda key: 'false' if key == 'automatic_location'
                                   else addon.getSetting(key))
    except DataError:
        return
    label = (location.get('display_name') or location['city']) + ' · ' + location['province']
    if addon.getSetting('location_label') != label:
        addon.setSetting('location_label', label)


def activate_if_unset():
    """Use this provider for a fresh skin installation, preserving any choice."""
    def rpc(method, params):
        result = json.loads(xbmc.executeJSONRPC(json.dumps(
            {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params})))
        if 'error' in result:
            raise DataError('Weather setting unavailable')
        return result.get('result')
    try:
        value = rpc('Settings.GetSettingValue', {'setting': 'weather.addon'})
        if isinstance(value, dict) and value.get('value') == '':
            if rpc('Settings.SetSettingValue', {'setting': 'weather.addon',
                                               'value': 'weather.jioyzen'}) is True:
                xbmc.executebuiltin('Weather.Refresh')
    except (TypeError, ValueError):
        return


def main():
    if len(sys.argv) > 1 and sys.argv[1] == 'activate_if_unset':
        activate_if_unset()
        return
    if len(sys.argv) > 1 and sys.argv[1] == 'probe':
        # RunScript selects our script extension, not the weather extension.
        from default import main as probe_weather
        probe_weather()
        return
    addon = xbmcaddon.Addon('weather.jioyzen')
    selecting = len(sys.argv) > 1 and sys.argv[1] == 'select_location'
    if selecting:
        select_location(addon)
    restore_location_label(addon)
    addon.openSettings()
    clear_manual_if_automatic(addon.getSetting, addon.setSetting)
    try:
        manual_location(addon.getSetting)
    except DataError:
        # Closing the settings to start its search action can occur before a
        # location has been chosen. Leave the existing card until selection.
        return
    refresh_weather(addon)


if __name__ == '__main__':
    main()
