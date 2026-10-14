"""Light script entry: show an independent page before importing scan code."""
import fcntl
import os
import sys
from urllib.parse import parse_qsl

import xbmc
import xbmcaddon
import xbmcvfs

from standalone import ScannerWindow
from window_state import Navigation


def main():
    addon = xbmcaddon.Addon('script.ai.movie.scanner')
    profile = xbmcvfs.translatePath(addon.getAddonInfo('profile'))
    os.makedirs(profile, exist_ok=True)
    raw = sys.argv[1] if len(sys.argv) > 1 else ''
    query = dict(parse_qsl(raw.lstrip('?'))) if raw.startswith('?') else {}
    with open(os.path.join(profile, '.ui.lock'), 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            xbmc.log('[AI Scanner UI] Duplicate launch ignored', xbmc.LOGINFO)
            return
        # Context commands are executed inside the active window, so a normal
        # launch always opens a page rather than a Kodi video-folder hierarchy.
        window = ScannerWindow('Scanner.xml', addon.getAddonInfo('path'),
                               'Default', '1080i')
        window.addon = addon
        if query.get('action') == 'view_directory':
            window.navigation = Navigation(query)
        elif query.get('action'):
            window._initial_action = query
        try:
            window.doModal()
        finally:
            window.cleanup()
            del window


if __name__ == '__main__':
    main()
