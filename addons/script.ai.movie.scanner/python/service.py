# -*- coding: UTF-8 -*-
"""Auto-scan service: runs on Kodi startup, discovers and scans new movies."""

import json
import os
import threading
import time
from task_state import task_lock, TaskBusy
from history_store import load_history_file
from localization import get_string
from path_display import redact_text,directory_label

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
    xbmc.log('[%s] AutoScan: %s' % (ADDON_NAME, redact_text(msg)), level)


def _load_history():
    return load_history_file(HISTORY_FILE)


def _is_enabled():
    return ADDON.getSetting('auto_scan_enabled') == 'true'


def _do_scan():
    if os.path.exists(os.path.join(DATA_PATH, 'reset-pending.json')):
        log('Pending library reset; auto-scan paused.')
        return 0, 0, 0, 0
    try:
        with task_lock(DATA_PATH):
            return _do_scan_unlocked()
    except TaskBusy:
        log('Another scanner/cache/reset task is running; auto-scan skipped.')
        return 0, 0, 0, 0


def _do_scan_unlocked():
    """Refresh all directories, scan if new files found.
    Returns (success, merged, failed, new_count)."""
    from main import refresh_directory_files, _run_scan, _needs_scan, _is_stop_requested, _clear_stop

    history = _load_history()
    dirs = list(history.get('directories', {}).keys())
    if not dirs:
        log('No directories to scan.')
        return 0, 0, 0, 0

    # Refresh: discover new/deleted files (with progress)
    _clear_stop()
    pDialog = None
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
            if _is_stop_requested():break
            ndir = directory_label(dirpath)
            if pDialog:
                pDialog.update(int((i + 1) * 100 / len(dirs)),
                              ADDON.getLocalizedString(32034) % ndir,
                              ADDON.getLocalizedString(32035) % (total_new, total_del))
            nc, dc = refresh_directory_files(dirpath)
            total_new += nc
            total_del += dc
    finally:
        if pDialog:pDialog.close()
        if xbmcvfs.exists(REFRESHING_FILE):
            try:
                os.remove(REFRESHING_FILE)
            except Exception:
                pass

    if _is_stop_requested():
        _clear_stop()
        log('Auto-scan stopped during file discovery; matching was not started.')
        return 0,0,0,total_new

    # Build work items: check both new and existing pending/failed
    history = _load_history()
    work_items = []
    affected_dirs = []
    pending_count = 0
    for dirpath in dirs:
        dir_files = history.get('directories', {}).get(dirpath, {}).get('files', {})
        dir_has_work = False
        for filepath, info in dir_files.items():
            if _needs_scan(info):
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
                get_string(ADDON, 32427),
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

    log('Scan done: %d success, %d merged, %d failed' %
        (success, merged, failed))
    return success, merged, failed, total_new


def _run_once():
    """First run: wait for Kodi to settle, then scan."""
    # Clear residual markers from previous session
    try:
        with task_lock(DATA_PATH):
            for f in (SCANNING_FILE, REFRESHING_FILE, os.path.join(DATA_PATH, '.caching')):
                if xbmcvfs.exists(f):
                    try:
                        os.remove(f)
                        log('Cleared residual marker: %s' % os.path.basename(f))
                    except OSError:pass
    except TaskBusy:
        log('Active task owns markers; startup cleanup skipped.')

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
