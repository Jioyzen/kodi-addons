"""Configure the required weather provider when this skin starts; no UI/service."""
import json

import xbmc
import xbmcaddon

SKIN = 'skin.embuary.mod.jioyzen'
WEATHER = 'weather.jioyzen'
INITIALIZED = 'jioyzen_weather_initialized'


def rpc(method, params):
    response = json.loads(xbmc.executeJSONRPC(json.dumps(
        {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params})))
    if 'error' in response or 'result' not in response:
        raise ValueError('Weather setup RPC failed: ' + method)
    return response['result']


def main():
    try:
        # A queued action from a previous skin must not change the new skin.
        if rpc('Settings.GetSettingValue', {'setting': 'lookandfeel.skin'}).get('value') != SKIN:
            return
        details = rpc('Addons.GetAddonDetails', {'addonid': WEATHER, 'properties': ['enabled']})
        if not details['addon']['enabled']:
            if rpc('Addons.SetAddonEnabled', {'addonid': WEATHER, 'enabled': True}) != 'OK':
                raise ValueError('Could not enable weather provider')
        addon = xbmcaddon.Addon(WEATHER)
        initializing = not xbmc.getCondVisibility('Skin.HasSetting(' + INITIALIZED + ')')
        if initializing:
            addon.setSetting('automatic_location', 'true')
            for key in ('selected_location', 'location_label', 'province', 'city'):
                if addon.getSetting(key):
                    addon.setSetting(key, '')
        selected = rpc('Settings.GetSettingValue', {'setting': 'weather.addon'})
        if selected.get('value') != WEATHER:
            if rpc('Settings.SetSettingValue', {'setting': 'weather.addon', 'value': WEATHER}) is not True:
                raise ValueError('Could not select weather provider')
        # Set only after successful enable/configuration; failures retry on
        # the next window load. Later manual city choices remain available.
        if initializing:
            xbmc.executebuiltin('Skin.SetBool(' + INITIALIZED + ')')
        xbmc.executebuiltin('Weather.Refresh')
        # Seed the persistent home snapshot immediately. The native manager
        # subsequently uses this same cached provider without a UI timer.
        xbmc.executebuiltin('RunScript(' + addon.getAddonInfo('path') + '/default.py)')
    except (OSError, TypeError, ValueError, KeyError, RuntimeError) as error:
        xbmc.log('[Embuary Mod] Weather setup failed: ' + type(error).__name__, xbmc.LOGWARNING)


if __name__ == '__main__':
    main()
