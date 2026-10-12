# -*- coding: UTF-8 -*-
"""Auto-scan service: runs on Kodi startup, discovers and scans new movies."""

import json
import os
import threading
import time

import dns_override  # DoH bypass GFW DNS pollution for TMDB (incl. image CDN)

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs

ADDON = xbmcaddon.Addon()
ADDON_NAME = ADDON.getAddonInfo('name')
DATA_PATH = xbmcvfs.translatePath('special://userdata/addon_data/%s' % ADDON.getAddonInfo('id'))
HISTORY_FILE = os.path.join(DATA_PATH, 'scan_history.json')
SCANNING_FILE = os.path.join(DATA_PATH, '.scanning')
REFRESHING_FILE = os.path.join(DATA_PATH, '.refreshing')

INITIAL_DELAY = 60  # seconds after Kodi starts before first scan


def log(msg, level=xbmc.LOGINFO):
    xbmc.log('[%s] AutoScan: %s' % (ADDON_NAME, msg), level)


def _load_history():
    if not xbmcvfs.exists(HISTORY_FILE):
        return {'directories': {}}
    try:
        with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {'directories': {}}


def _is_enabled():
    return ADDON.getSetting('auto_scan_enabled') == 'true'


def _do_scan():
    """Refresh all directories, scan if new files found.
    Returns (success, merged, failed, new_count)."""
    from main import refresh_directory_files, _run_scan

    history = _load_history()
    dirs = list(history.get('directories', {}).keys())
    if not dirs:
        log('No directories to scan.')
        return 0, 0, 0, 0

    # Refresh: discover new/deleted files (with progress)
    with open(REFRESHING_FILE, 'w') as f:
        f.write('1')
    try:
        total_new = 0
        total_del = 0
        pDialog = None
        if len(dirs) > 1:
            pDialog = xbmcgui.DialogProgressBG()
            pDialog.create(ADDON_NAME, ADDON.getLocalizedString(32033))

        for i, dirpath in enumerate(dirs):
            ndir = os.path.basename(dirpath.rstrip('/'))
            if pDialog:
                pDialog.update(int((i + 1) * 100 / len(dirs)),
                              ADDON.getLocalizedString(32034) % ndir,
                              ADDON.getLocalizedString(32035) % (total_new, total_del))
            nc, dc = refresh_directory_files(dirpath)
            total_new += nc
            total_del += dc
        if pDialog:
            pDialog.close()
    finally:
        if xbmcvfs.exists(REFRESHING_FILE):
            try:
                os.remove(REFRESHING_FILE)
            except Exception:
                pass

    # Build work items: check both new and existing pending/failed
    history = _load_history()
    work_items = []
    affected_dirs = []
    pending_count = 0
    for dirpath in dirs:
        dir_files = history.get('directories', {}).get(dirpath, {}).get('files', {})
        dir_has_work = False
        for filepath, info in dir_files.items():
            if info.get('status') in ('pending', 'failed'):
                pending_count += 1
                if not xbmcvfs.exists(filepath):
                    continue
                work_items.append((filepath, os.path.dirname(filepath)))
                dir_has_work = True
        if dir_has_work:
            affected_dirs.append(dirpath)

    if not work_items:
        if total_new or total_del:
            xbmcgui.Dialog().notification(ADDON_NAME,
                ADDON.getLocalizedString(32155) % (0, 0),
                xbmcgui.NOTIFICATION_INFO, 3000)
        else:
            log('No work items, no changes.')
        return 0, 0, 0, total_new

    if total_new > 0:
        log('%d new file(s) found, %d total pending, starting scan...'
            % (total_new, pending_count))
        xbmcgui.Dialog().notification(
            ADDON_NAME,
            ADDON.getLocalizedString(32153) % total_new,
            xbmcgui.NOTIFICATION_INFO, 5000)
    else:
        log('%d pending file(s) from previous scans, starting scan...'
            % pending_count)

    # Run scan (sets .scanning flag internally)
    success, merged, failed = _run_scan(work_items, affected_dirs)

    msg = ADDON.getLocalizedString(32155) % (success, failed)
    xbmcgui.Dialog().notification(ADDON_NAME, msg,
                                  xbmcgui.NOTIFICATION_INFO, 5000)
    log('Scan done: %d success, %d merged, %d failed' %
        (success, merged, failed))
    return success, merged, failed, total_new


def _run_once():
    """First run: wait for Kodi to settle, then scan."""
    # Clear residual markers from previous session
    for f in (SCANNING_FILE, REFRESHING_FILE):
        if xbmcvfs.exists(f):
            try:
                os.remove(f)
                log('Cleared residual marker: %s' % os.path.basename(f))
            except Exception:
                pass

    log('Waiting %ds for Kodi to be ready...' % INITIAL_DELAY)
    if xbmc.Monitor().waitForAbort(INITIAL_DELAY):
        return

    try:
        if not _is_enabled():
            log('Auto-scan disabled, skipping.')
            return

        if xbmcvfs.exists(SCANNING_FILE) or xbmcvfs.exists(REFRESHING_FILE):
            log('Another task already running, skipping auto-scan.')
            return

        _do_scan()
    except Exception as e:
        log('Auto-scan error: %s' % e, xbmc.LOGERROR)
        import traceback
        log(traceback.format_exc(), xbmc.LOGERROR)

class AutoScanMonitor(threading.Thread):
    """Periodic background monitor for new files."""

    def __init__(self):
        threading.Thread.__init__(self)
        self.daemon = True
        self._stop_event = threading.Event()

    def run(self):
        # Wait until after initial scan
        if self._stop_event.wait(INITIAL_DELAY + 120):
            return

        while not self._stop_event.is_set():
            try:
                if not _is_enabled():
                    self._stop_event.wait(300)
                    continue

                interval_h = int(ADDON.getSetting('auto_scan_interval') or '6')
                interval_sec = max(1, interval_h) * 3600

                # Sleep in small chunks to respond to stop signal
                for _ in range(interval_sec // 10):
                    if self._stop_event.is_set():
                        return
                    self._stop_event.wait(10)

                if xbmcvfs.exists(SCANNING_FILE) or xbmcvfs.exists(REFRESHING_FILE):
                    continue

                _do_scan()

            except Exception as e:
                log('Monitor error: %s' % e, xbmc.LOGERROR)
                self._stop_event.wait(600)

    def stop(self):
        self._stop_event.set()


# ==========================================
# Entry point
# ==========================================
if __name__ == '__main__':
    monitor = None
    try:
        _run_once()
        monitor = AutoScanMonitor()
        monitor.start()

        # Keep alive until Kodi shuts down
        kodi_monitor = xbmc.Monitor()
        while not kodi_monitor.abortRequested():
            if kodi_monitor.waitForAbort(10):
                break

    finally:
        if monitor:
            monitor.stop()
            monitor.join(timeout=2)
        log('Service stopped.')
