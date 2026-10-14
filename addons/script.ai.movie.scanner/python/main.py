# -*- coding: UTF-8 -*-
"""AI Movie Scanner - xbmcplugin menu-driven entry point."""

import json
import os
import queue
import sys
import threading
import time
import urllib.parse
import re
import difflib
import functools
from task_state import task_lock, TaskBusy
from history_store import load_history_file,save_history_file

import xbmc

# ============================================================
# Confidence-based match selection (replaces blind results[0])
# ============================================================
MATCH_THRESHOLD = 0.50  # below this, the best TMDB candidate is NOT auto-written; it goes to manual review
# Hard floor: matches scoring below this are almost always wrong (e.g.
# "不知不觉诱惑你.Frivolous Lola" matched "E&B" at -0.17 in real logs).
# They are never auto-written and go to manual review instead.
MATCH_FLOOR = 0.30


from matching import (norm_title as _norm_title, year_of as _year_of,
                      match_score as _match_score, ambiguous_matches)


def _select_best_match(results, title, year, threshold=MATCH_THRESHOLD, english_title=''):
    """Pick the TMDB candidate whose normalized title (+ year) best matches.
    Returns (best_candidate, score). Caller should NOT auto-write when
    score < threshold - push it to manual review instead."""
    if not results:
        return None, 0.0
    best, best_score = None, -1.0
    for r in results:
        sc = _match_score(r, title, year, english_title)
        if sc > best_score:
            best, best_score = r, sc
    return best, best_score
import xbmcaddon
import xbmcgui
import xbmcplugin
import xbmcvfs

from path_display import directory_label, private_query, resolve_query, redact_text


def ImageCacher(*args, **kwargs):
    # Menu browsing needs neither HTTP/DNS libraries nor texture-cache access.
    import dns_override
    from image_cacher import ImageCacher as implementation
    return implementation(*args, **kwargs)

ADDON = xbmcaddon.Addon()


def L(string_id):
    """Shortcut for getting localized string."""
    text = ADDON.getLocalizedString(string_id)
    if not text and 32200 <= string_id <= 32211:
        from actor_cache import L as actor_cache_string
        return actor_cache_string(string_id)
    return text



ADDONID = ADDON.getAddonInfo('id')
ADDON_NAME = ADDON.getAddonInfo('name')
ADDON_PATH = ADDON.getAddonInfo('path')
DATA_PATH = xbmcvfs.translatePath('special://userdata/addon_data/%s' % ADDONID)
HISTORY_FILE = os.path.join(DATA_PATH, 'scan_history.json')

try:
    addon_handle = int(sys.argv[1])
except Exception:
    addon_handle = -1

STATUS_LABELS = {
    'pending': L(32010),
    'success': L(32011),
    'merged': L(32012),
    'failed': L(32013),
    'skipped': L(32404) or '已略过',
}

# The standalone window installs these per-interpreter callbacks. Background
# service operations still run without a UI and never refresh Kodi Videos.
UI_LISTING = None
UI_REFRESH = None


def refresh_menu():
    if UI_REFRESH is not None:
        UI_REFRESH()


def context_menu(item, commands):
    item.setProperty('AIS.Context', json.dumps(commands, ensure_ascii=False))
    item.addContextMenuItems(commands)


def emit_listing(listing, category):
    for url, item, folder in listing:
        item.setProperty('AIS.Url', url)
        item.setProperty('AIS.Folder', 'true' if folder else 'false')
    if UI_LISTING is not None:
        UI_LISTING(listing, category)
    elif addon_handle >= 0:
        xbmcplugin.addDirectoryItems(addon_handle, listing, len(listing))
        xbmcplugin.setPluginCategory(addon_handle, category)
        xbmcplugin.endOfDirectory(addon_handle, cacheToDisc=False)
    return listing


def log(msg, level=xbmc.LOGINFO):
    xbmc.log('[%s] %s' % (ADDON_NAME, redact_text(msg)), level)


def guarded_task(function):
    @functools.wraps(function)
    def guarded(*args, **kwargs):
        try:
            with task_lock(DATA_PATH):
                if (function.__name__ != 'clear_all_data' and
                        os.path.exists(os.path.join(DATA_PATH, 'reset-pending.json'))):
                    raise TaskBusy('An interrupted reset must be completed first')
                return function(*args, **kwargs)
        except TaskBusy as exc:
            log(str(exc), xbmc.LOGWARNING)
            xbmcgui.Dialog().notification(ADDON_NAME, L(32301),
                                         xbmcgui.NOTIFICATION_WARNING, 5000)
            if function.__name__ == '_run_scan':
                return 0, 0, 0
            if function.__name__ == 'refresh_directory_files':
                return 0, 0
    return guarded


# ==========================================
# History persistence
# ==========================================
def ensure_data_dir():
    if not xbmcvfs.exists(DATA_PATH):
        xbmcvfs.mkdirs(DATA_PATH)


def load_history():
    ensure_data_dir()
    return load_history_file(HISTORY_FILE)


def save_history(data):
    ensure_data_dir()
    save_history_file(HISTORY_FILE,data)


# Cross-process scan state files (Kodi plugins run as separate processes)
SCANNING_FILE = os.path.join(DATA_PATH, '.scanning')
REFRESHING_FILE = os.path.join(DATA_PATH, '.refreshing')
STOP_FILE = os.path.join(DATA_PATH, '.stop_requested')


def _is_scanning():
    return xbmcvfs.exists(SCANNING_FILE)


def _is_refreshing():
    return xbmcvfs.exists(REFRESHING_FILE)


def _is_busy():
    """Check if any background task is running (refresh or scan)."""
    return _is_scanning() or _is_refreshing()


def _request_stop():
    try:
        with open(STOP_FILE, 'w') as f:
            f.write('1')
    except Exception:
        pass


def _is_stop_requested():
    return xbmcvfs.exists(STOP_FILE) or xbmc.Monitor().abortRequested()


def _clear_stop():
    if xbmcvfs.exists(STOP_FILE):
        try:
            os.remove(STOP_FILE)
        except Exception:
            pass


def _set_refreshing():
    ensure_data_dir()
    with open(REFRESHING_FILE, 'w') as f:
        f.write('1')


def _clear_refreshing():
    if xbmcvfs.exists(REFRESHING_FILE):
        try:
            os.remove(REFRESHING_FILE)
        except Exception:
            pass


def _set_scanning():
    ensure_data_dir()
    with open(SCANNING_FILE, 'w') as f:
        f.write('1')


def _clear_scanning():
    if xbmcvfs.exists(SCANNING_FILE):
        try:
            os.remove(SCANNING_FILE)
        except Exception:
            pass


def stop_scan():
    if _is_scanning() or _is_refreshing():
        _request_stop()
        xbmcgui.Dialog().notification(ADDON_NAME, L(32030), xbmcgui.NOTIFICATION_INFO, 2000)
    else:
        xbmcgui.Dialog().notification(ADDON_NAME, L(32031), xbmcgui.NOTIFICATION_INFO, 2000)


# ==========================================
# URL helpers
# ==========================================
def get_params():
    paramstring = sys.argv[2] if len(sys.argv) > 2 else ''
    params = {}
    if len(paramstring) >= 2 and paramstring[0] == '?':
        paramstring = paramstring[1:]
        params = dict(urllib.parse.parse_qsl(paramstring))
    if 'directory' in params or 'file' in params:
        params = resolve_query(params,load_history())
    return params


def build_url(query):
    return 'plugin://%s/?%s' % (ADDONID, urllib.parse.urlencode(private_query(query)))


def run_plugin_url(query):
    """Build a RunPlugin(...) URL for context menu items."""
    return 'RunScript(%s,?%s)' % (ADDONID, urllib.parse.urlencode(private_query(query)))


# ==========================================
# Router
# ==========================================
def router(paramstring='', params=None):
    params = get_params() if params is None else params
    action = params.get('action', None)

    if action is None:
        show_main_menu()
    elif action == 'add_directory':
        add_directory()
    elif action == 'view_directory':
        view_directory(params.get('dirpath', ''),params.get('group',''),params.get('page',0))
    elif action == 'scan_directory':
        scan_directory(params.get('dirpath', ''))
    elif action == 'scan_all':
        scan_all()
    elif action == 'remove_directory':
        remove_directory(params.get('dirpath', ''))
    elif action == 'refresh_all':
        action_refresh_all()
    elif action == 'refresh_directory':
        action_refresh_directory(params.get('dirpath',''))
    elif action == 'manual_match':
        manual_match(params.get('filepath'), params.get('dirpath'))
    elif action == 'manual_match_show':
        manual_match_show(params.get('dirpath',''),params.get('group',''))
    elif action == 'play_file':
        play_media_file(params.get('dirpath',''),params.get('filepath',''))
    elif action == 'review_pending':
        review_pending()
    elif action == 'cache_images':
        cache_images()
    elif action == 'cache_actor_images':
        cache_actor_images()
    elif action == 'stop_scan':
        stop_scan()
        refresh_menu()
    elif action == 'clear_all_data':
        clear_all_data()
    elif action == 'open_settings':
        ADDON.openSettings()
        refresh_menu()
    else:
        xbmc.log('[%s] Unknown action: %s' % (ADDON_NAME, action), xbmc.LOGERROR)


# ==========================================
# Main Menu
# Build new plugin items offscreen, then transfer the completed list to Kodi.
# ==========================================
def _needs_scan(info):
    status = info.get('status')
    return status in ('pending','failed') or (status=='skipped' and
           (info.get('is_tvshow') or info.get('match_evidence',{}).get('type')=='tvshow'))


def show_main_menu(history=None):
    history = load_history() if history is None else history
    dirs = history.get('directories', {})
    log('show_main_menu: loaded %d directories, %d total keys in history'
        % (len(dirs), len(history)))
    listing = []

    # --- [Stop Scan] ---
    if _is_busy():
        li = xbmcgui.ListItem(L(32022), offscreen=True)
        li.setArt({'icon': 'DefaultAddonInfoProvider.png'})
        url = build_url({'action': 'stop_scan'})
        listing.append((url, li, False))

    # --- [AI fallback not configured] (visible hint + jump to settings) ---
    ai_enabled = ADDON.getSetting('ai_enabled') == 'true'
    ai_key = ADDON.getSetting('ai_api_key') or ''
    if ai_enabled and not ai_key:
        li = xbmcgui.ListItem('[COLOR=FFDD9933]%s[/COLOR]' % L(32182), offscreen=True)
        li.setArt({'icon': 'DefaultAddonInfoProvider.png'})
        li.setInfo('video', {'plot': L(32172)})
        url = build_url({'action': 'open_settings'})
        listing.append((url, li, False))

    # --- [Add Directory] ---
    li = xbmcgui.ListItem(L(32020), offscreen=True)
    li.setArt({'icon': 'DefaultAddonVideo.png'})
    url = build_url({'action': 'add_directory'})
    listing.append((url, li, False))

    if dirs:
        # --- [Scan All] ---
        pending = sum(
            1 for d in dirs.values()
            for f in d.get('files', {}).values()
            if _needs_scan(f))
        if pending > 0:
            li = xbmcgui.ListItem(L(32021) % pending, offscreen=True)
            li.setArt({'icon': 'DefaultAddonVideo.png'})
            url = build_url({'action': 'scan_all'})
            listing.append((url, li, False))

            # --- [Review Pending Matches] ---
            li = xbmcgui.ListItem(L(32161) % len(_collect_pending(history)), offscreen=True)
            li.setArt({'icon': 'DefaultAddonSearch.png'})
            url = build_url({'action': 'review_pending'})
            listing.append((url, li, False))

        # --- Directory items ---
        for dirpath, data in dirs.items():
            files = data.get('files', {})
            total = len(files)
            s = sum(1 for f in files.values() if f.get('status') in ('success', 'merged'))
            fl = sum(1 for f in files.values() if f.get('status') == 'failed')
            pn = sum(1 for f in files.values() if f.get('status') == 'pending')
            skipped = sum(1 for f in files.values() if f.get('status') == 'skipped')

            # Build label with color-coded stats
            if fl > 0:
                label2 = '%d文件 | [COLOR=FF44DD44]已匹配:%d[/COLOR] | [COLOR=FFDD4444]失败:%d[/COLOR] | 待处理:%d' % (total, s, fl, pn)
            else:
                label2 = L(32070) % (total, s, fl, pn)

            if pn > 0:
                label = '[COLOR=FFFF4444][待处理:%d][/COLOR] [COLOR=FF44DD44][%d匹配/%d失败][/COLOR] %s' % (pn, s, fl, directory_label(dirpath))
            elif fl > 0:
                label = '[COLOR=FF44DD44][%d匹配/%d失败][/COLOR] %s' % (s, fl, directory_label(dirpath))
            else:
                label = '[COLOR=FF44DD44][%d已匹配][/COLOR] %s' % (s, directory_label(dirpath))
            if skipped:
                label += ' [COLOR=FF999999][%s:%d][/COLOR]' % (STATUS_LABELS['skipped'], skipped)
                label2 += ' | %s:%d' % (STATUS_LABELS['skipped'], skipped)
            li = xbmcgui.ListItem(label, offscreen=True)
            li.setLabel2(label2)
            li.setArt({'icon': 'DefaultFolder.png'})

            # Context menu
            cm = [(L(32413),run_plugin_url({'action':'refresh_directory','dirpath':dirpath}))]
            from directory_listing import single_show_root
            from path_display import path_token
            root = single_show_root(files, data.get('content_type', 'auto'))
            if root:
                cm.append((L(32029),run_plugin_url({'action':'manual_match_show','dirpath':dirpath,'group':path_token(root)})))
            if pn > 0 or fl > 0:
                cm.append((L(32027), run_plugin_url({
                    'action': 'scan_directory',
                    'dirpath': dirpath,
                })))
            cm.append((L(32028), run_plugin_url({
                'action': 'remove_directory',
                'dirpath': dirpath,
            })))
            context_menu(li, cm)

            url = build_url({'action': 'view_directory', 'dirpath': dirpath})
            listing.append((url, li, True))

        # --- [Refresh All] ---
        li = xbmcgui.ListItem(L(32025), offscreen=True)
        li.setArt({'icon': 'DefaultAddonsUpdates.png'})
        url = build_url({'action': 'refresh_all'})
        listing.append((url, li, False))

    # --- [Cache Images] ---
    li = xbmcgui.ListItem(L(32023), offscreen=True)
    li.setArt({'icon': 'DefaultAddonsZip.png'})
    url = build_url({'action': 'cache_images'})
    listing.append((url, li, False))

    # --- [Cache Actor Images] (independent of poster/background caching) ---
    li = xbmcgui.ListItem(L(32200), offscreen=True)
    li.setArt({'icon': 'DefaultActor.png'})
    url = build_url({'action': 'cache_actor_images'})
    listing.append((url, li, False))

    # --- [Clear All Data] ---
    li = xbmcgui.ListItem(L(32026), offscreen=True)
    li.setArt({'icon': 'DefaultAddonInfoProvider.png'})
    url = build_url({'action': 'clear_all_data'})
    listing.append((url, li, False))

    # --- [Settings] ---
    li = xbmcgui.ListItem(L(32024), offscreen=True)
    li.setArt({'icon': 'DefaultAddonService.png'})
    url = build_url({'action': 'open_settings'})
    listing.append((url, li, False))

    emit_listing(listing, ADDON_NAME)


# ==========================================
# Refresh All Directories
# ==========================================
@guarded_task
def action_refresh_all():
    """Scan all managed directories for new/deleted files. No AI scan."""
    if _is_busy():
        xbmcgui.Dialog().notification(ADDON_NAME, L(32042), xbmcgui.NOTIFICATION_WARNING, 3000)
        return
    history = load_history()
    dirs = history.get('directories', {})
    if not dirs:
        xbmcgui.Dialog().notification(ADDON_NAME, L(32032), xbmcgui.NOTIFICATION_INFO, 2000)
        return

    _refresh_library_files(list(dirs))


@guarded_task
def action_refresh_directory(dirpath):
    """Discover files only in the selected managed directory, without scraping."""
    if _is_busy():
        xbmcgui.Dialog().notification(ADDON_NAME,L(32042),xbmcgui.NOTIFICATION_WARNING,3000)
        return
    if dirpath not in load_history().get('directories',{}):
        return
    _refresh_library_files([dirpath])


def _refresh_library_files(dir_list):
    ensure_data_dir()
    _clear_stop()
    _set_refreshing()
    total_new = total_del = 0
    pDialog = None
    stopped = False
    try:
        pDialog = xbmcgui.DialogProgressBG()
        pDialog.create(ADDON_NAME,L(32033))
        for i,dirpath in enumerate(dir_list):
            if _is_stop_requested():
                stopped = True
                break
            pDialog.update(int(i*100/max(1,len(dir_list))),
                           L(32034)%directory_label(dirpath),
                           L(32035)%(total_new,total_del))
            nc,dc = refresh_directory_files(dirpath)
            total_new += nc
            total_del += dc
        stopped = stopped or _is_stop_requested()
    finally:
        if pDialog is not None:
            pDialog.close()
        _clear_refreshing()
        _clear_stop()
        refresh_menu()
    if stopped:
        msg = L(32030)
    elif total_new or total_del:
        msg = L(32060)%(total_new,total_del)
    else:
        msg = L(32061)
    xbmcgui.Dialog().notification(ADDON_NAME,msg,xbmcgui.NOTIFICATION_INFO,5000)


# ==========================================
# Add Directory
# ==========================================
@guarded_task
def add_directory():
    if _is_busy():
        xbmcgui.Dialog().notification(ADDON_NAME, L(32042), xbmcgui.NOTIFICATION_WARNING, 3000)
        return
    dialog = xbmcgui.Dialog()
    dirpath = dialog.browse(0, L(32050), '', '', False, False)
    if not dirpath:
        return

    history = load_history()
    if dirpath in history.get('directories', {}):
        dialog.ok(ADDON_NAME, L(32038))
        return

    content_choice = dialog.select(L(32400) or '目录中的内容类型',
        [L(32401) or '自动识别', L(32082) or '电影', L(32083) or '电视剧'])
    if content_choice < 0:
        return
    content_type = ('auto','movie','tvshow')[content_choice]

    # Set refreshing marker to block other tasks
    _set_refreshing()
    try:
        from file_scanner import find_video_files

        # Run file scanning in background thread so UI stays responsive
        scan_result = [None]
        scan_done = threading.Event()

        def _scan_worker():
            try:
                scan_result[0] = find_video_files(dirpath,cancelled=_is_stop_requested)
            except Exception as e:
                log('File discovery incomplete: '+type(e).__name__,xbmc.LOGWARNING)
                scan_result[0] = []
            scan_done.set()

        t = threading.Thread(target=_scan_worker, name='FileScanner')
        t.daemon = True
        t.start()

        pDialog = xbmcgui.DialogProgressBG()
        pDialog.create(ADDON_NAME, L(32036))

        while not scan_done.wait(0.3):
            pDialog.update(50, L(32036), directory_label(dirpath))

        t.join(timeout=2)
        pDialog.close()
        video_files = scan_result[0] if scan_result[0] is not None else []

        if not video_files:
            dialog.ok(ADDON_NAME, L(32037))
            return

        files_dict = {}
        for f in sorted(video_files):
            files_dict[f] = {'status': 'pending'}

        history.setdefault('directories', {})[dirpath] = {
            'added': time.strftime('%Y-%m-%d %H:%M:%S'),
            'content_type': content_type,
            'files': files_dict,
        }
        save_history(history)
        refresh_menu()

        # Auto-scan immediately
        work_items = [(f, os.path.dirname(f)) for f in sorted(video_files)]
        _run_scan(work_items, [dirpath])

        pending = _count_pending([dirpath])
        if pending > 0 and xbmcgui.Dialog().yesno(ADDON_NAME, L(32164) % pending):
            review_pending()
    finally:
        _clear_refreshing()


# ==========================================
# Cache Images
# ==========================================
@guarded_task
def cache_images():
    ImageCacher().start()
    refresh_menu()


@guarded_task
def cache_actor_images():
    from actor_cache import ActorImageCacher
    ActorImageCacher().start_actors()
    refresh_menu()


# ==========================================
# Clear All Data (poster data + DB)
# ==========================================
@guarded_task
def clear_all_data():
    from ai_database import current_database
    import library_reset

    dialog = xbmcgui.Dialog()
    if not dialog.yesno(ADDON_NAME, L(32052)):
        return
    booleans = json.loads(xbmc.executeJSONRPC(json.dumps({
        'jsonrpc': '2.0', 'id': 1, 'method': 'XBMC.GetInfoBooleans',
        'params': {'booleans': ['Library.IsScanningVideo', 'Library.IsCleaning']}})))
    if (any(booleans.get('result', {}).values())
            or xbmc.Player().isPlayingVideo()):
        dialog.ok(ADDON_NAME, L(32301))
        return

    def rpc(method, params):
        response = json.loads(xbmc.executeJSONRPC(json.dumps({
            'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params})))
        if 'error' in response or 'result' not in response:
            raise RuntimeError('%s: %s' % (method, response.get('error', 'no result')))
        return response['result']

    progress = xbmcgui.DialogProgressBG()
    progress.create(ADDON_NAME, L(32300))
    try:
        plan = library_reset.prepare(DATA_PATH, current_database('MyVideos'), rpc)
        report = library_reset.execute(
            DATA_PATH, xbmcvfs.translatePath('special://thumbnails/'), plan, rpc,
            progress=lambda done, total: progress.update(
                int(done * 100 / max(1, total)), ADDON_NAME, L(32300)),
            aborted=xbmc.Monitor().abortRequested)
        log('Independent library reset complete: %s' % report)
        refresh_menu()
        dialog.ok(ADDON_NAME, L(32046))
    except Exception as exc:
        log('Independent library reset failed: %s' % exc, xbmc.LOGERROR)
        dialog.ok(ADDON_NAME, L(32302))
    finally:
        progress.close()


# ==========================================
# Segment filenames for the manual-match picker
# ==========================================
# Same tag vocabulary as Kodi's AdvancedSettings.cpp + ai_parser._CLEAN_REGEX_1.
# Duplicated here so the manual-match picker never has to import ai_parser
# (keeps the failure path usable even when AI parsing is broken).
_NAME_SEGMENT_TAGS = (
    r'10bit|480p|480i|576p|576i|720p|720i|1080p|1080i|2160p|2d|3d|aac|ac3|'
    r'aka|atmos|avi|bd5|bdrip|bdremux|bluray|brrip|cam|cd[1-9]|custom|dc|ddp|'
    r'ddp5\.1|ddp7\.1|dd2\.0|dd5\.1|dd7\.1|dd7\.1\.2|dd7\.1\.4|dtshd\.ma|'
    r'5\.1|7\.1|2\.0|7\.1\.2|7\.1\.4|'
    r'divx|divx5|dolby|dolbydigital|dolbyvision|vision|dsr|dsrip|dts|dtshd|'
    r'dts-hdma|dts-hra|dts-x|dv|dvd|dvd5|dvd9|dvdivx|dvdrip|dvdscr|dvdscreener|'
    r'extended|fragment|'
    r'fs|h264|h265|hdr|hdr10|hevc|hddvd|hdrip|hdtv|hdtvrip|hrhd|hrhdtv|imax|'
    r'internal|limited|multisubs|nfofix|ntsc|ogg|ogm|pal|pdtv|proper|r3|r5|'
    r'read\.nfo|remastered|remux|repack|rerip|retail|screener|se|svcd|tc|'
    r'telecine|telesync|truehd|ts|uhd|unrated|ws|x264|x265|xvid|xvidvd|xxx|'
    r'web-dl|webrip|www\.www|4k'
)
# Match a release tag (with surrounding separator) OR a bracket block.
_NAME_SEGMENT_TAG_RE = re.compile(
    r'[ _\,\.\(\)\[\]\-\+](' + _NAME_SEGMENT_TAGS + r')([ _\,\.\(\)\[\]\-\+]|$)',
    re.IGNORECASE,
)
_NAME_SEGMENT_BRACKET_RE = re.compile(
    r'\[.*?\]|[【［〔].*?[】］〕]|（.*?）', re.IGNORECASE,
)
# Generic separator used to slice the leftover title into fine segments.
# Mirrors Kodi's AdvancedSettings.cpp title boundary set.
_NAME_SEGMENT_SPLIT_RE = re.compile(r'[ _\,\.\(\)\[\]\-]+')


def _split_name_into_segments(fname, tv_context=None):
    """Split a media filename into candidate title segments.

    Returns (labels, query_for_label):
      labels: list[str] for ``dialog.select``. First entry is the
              auto-extracted full title (line 32107). Each following entry is
              one of the fine-grained segment pieces (so the user can pick the
              real title when the tag-aware join is still wrong).
      query_for_label: callable(label_index) -> query string to feed TMDB.

    Empty / duplicate segments are de-duped while preserving order. When
    splitting yields nothing usable we fall back to the raw basename so the
    picker still has at least one row.

    Note: the per-segment list intentionally keeps release tags ("1080p",
    "DTS", "BluRay", ...) in the picker instead of dropping them. The user can
    always pick the auto-extracted full row at index 0; the granular rows are
    only there to help them quickly spot when one segment (e.g. the CJK
    title) really is the correct TMDB query and the rest is noise.
    """
    if tv_context:
        titles = tv_context.get('titles') or []
        if titles and titles[0]:
            return [L(32107) % titles[0]] + titles[1:] + [L(32408) or '输入剧名或 TMDb ID'], lambda index: titles[index] if 0 <= index < len(titles) else ''
        return [L(32408) or '输入剧名或 TMDb ID'], lambda _index: ''
    if not fname:
        return [L(32107) % (fname or '')], lambda _i: fname or ''

    # Strip the file extension (keep multi-part like .tar.gz honest by only
    # chopping the last ".xxx" off).
    stem = fname
    dot = stem.rfind('.')
    if dot > 0:
        stem = stem[:dot]

    # Trim disc-counter prefixes ("343.Movie Name" -> "Movie Name"). Skip
    # when the prefix is a year-like number, which is part of the title
    # ("2001.A.Space.Odyssey" stays intact).
    disc_m = re.match(r'^\s*(\d{2,4})\s*\.\s*(?=[A-Za-z\u4e00-\u9fff])', stem)
    if disc_m:
        year_prefix = int(disc_m.group(1))
        max_year = time.localtime().tm_year + 1
        if not (1920 <= year_prefix <= max_year):
            stem = stem[disc_m.end():]

    # Build the auto-extracted full title by collapsing release tags and
    # bracket blocks into spaces. "DTS-HDMA" -> ["DTS", "HDMA"] after split.
    cleaned = _NAME_SEGMENT_TAG_RE.sub(' ', stem)
    cleaned = _NAME_SEGMENT_BRACKET_RE.sub(' ', cleaned)
    cleaned = _NAME_SEGMENT_SPLIT_RE.sub(' ', cleaned)
    auto_full = re.sub(r'\s+', ' ', cleaned).strip()
    from ai_parser import regex_parse_filename
    parsed = regex_parse_filename(fname, '', mode='tvshow')
    if parsed and parsed.get('title'):
        auto_full = ' '.join(re.sub(r'[._]+', ' ', parsed['title']).split())

    # Fine-grained segments are produced directly from the raw stem - no
    # release-tag stripping. That matches the reference screenshot, where
    # release tokens like "1080p", "imax", "DTS" still appear as their own
    # rows so the user can dismiss them by eye without typing.
    raw_segments = [s for s in _NAME_SEGMENT_SPLIT_RE.split(stem) if s]

    seen = set()
    pieces = []
    for part in auto_full.split(' '):
        if part and part not in seen:
            seen.add(part)
            pieces.append(part)
    for part in raw_segments:
        if part and part not in seen:
            seen.add(part)
            pieces.append(part)

    labels = [L(32107) % (auto_full or stem)]
    for p in pieces:
        labels.append(p)

    def _query_for_label(idx):
        if idx <= 0 or not pieces:
            return auto_full or stem
        # +1 because labels[0] is the auto-extracted row, pieces[0..] follows.
        return pieces[idx - 1]

    return labels, _query_for_label


def _manual_search_candidates(query, filename, auto_query, api_key, language, tv_context=None):
    """Use scan-quality, multilingual candidates for the extracted-title row."""
    from ai_parser import regex_parse_filename
    from tmdb_api import search_movie_best, search_tv_best, search_multi
    if tv_context:
        results = search_tv_best(query,tv_context.get('year'),tv_context.get('english_title',''),api_key,language) or []
        from matching import merge_candidates
        combined,index = [],{}
        merge_candidates(combined,index,results)
        if tv_context.get('year'):
            merge_candidates(combined,index,search_tv_best(query,None,tv_context.get('english_title',''),api_key,language) or [])
        return [dict(r,media_type='tv') for r in sorted(combined,
            key=lambda r:_match_score(r,query,tv_context.get('year'),tv_context.get('english_title','')),reverse=True)]
    parsed = regex_parse_filename(filename, '', mode='tvshow') if auto_query else None
    if not parsed:
        return search_multi(query, api_key, language)
    year = parsed.get('year')
    media_type = 'tv' if parsed.get('type') == 'tvshow' else 'movie'
    search = search_tv_best if media_type == 'tv' else search_movie_best
    results = search(query, year, parsed.get('english_title', ''), api_key, language)
    return [dict(r, media_type=media_type) for r in sorted(
        results, key=lambda r: _match_score(r, query, year, parsed.get('english_title', '')), reverse=True)]


# ==========================================
# Manual Match
# ==========================================
def _manual_tv_apply(filepath, dirpath, tv_id, context, apply_all=False):
    """Apply a confirmed show to one file or its known show-directory inventory."""
    from tv_matcher import TvMatchSession, TvMatchError
    from media_identity import title_key
    history = load_history()
    source = history.get('directories',{}).get(dirpath,{})
    files = source.get('files',{})
    key = ADDON.getSetting('tmdb_api_key') or ''
    language = ADDON.getSetting('tmdb_language') or 'zh-CN'
    if apply_all:
        from directory_listing import group_inventory
        groups,_ = group_inventory(files,source.get('content_type','auto'))
        files = dict(groups.get(context['show_root'],[]))
    session = TvMatchSession(files or [filepath],key,language,'tvshow' if apply_all else source.get('content_type','auto'),
                             forced_ids={context['show_root']:tv_id},previous_results=files)
    session.contexts[filepath] = context
    if apply_all and not any(c.get('directory_titles') for c in session.contexts.values()) and len({title_key(c['title']) for c in session.contexts.values() if c['title']})>1:
        xbmcgui.Dialog().ok(ADDON_NAME,L(32415) or '该目录包含多部不同的剧，请按独立剧目录管理后再匹配。')
        return
    if apply_all:
        for other in session.contexts.values():
            if not other['title']:
                other.update(title=context['title'],titles=list(context['titles']))
                if other.get('reason')=='show_title_missing' and other.get('episodes'):
                    other.update(kind='episode',reason='')
    peers = [path for path,other in session.contexts.items()
             if other['show_root']==context['show_root']
             and title_key(other['title'])==title_key(context['title'])
             and other['year']==context['year']]
    targets = [filepath]
    if apply_all:
        targets = list(session.contexts)
    elif len(peers)>1:
        default = 1 if any(_needs_scan(files.get(path,{})) for path in peers if path!=filepath) else 0
        selection = xbmcgui.Dialog().select(L(32402) or '应用本次剧集匹配',
            [L(32406) or '仅当前文件', (L(32403) or '同目录的全部 %d 个视频文件') % len(peers)],preselect=default)
        if selection<0:
            return
        if selection==1:
            targets = peers
    try:
        # Validate the selected identity/numbering before the first database write.
        session.prepare_file(filepath)
    except TvMatchError as error:
        xbmcgui.Dialog().ok(ADDON_NAME,str(error))
        return
    if apply_all:
        from path_display import path_token
        confirmed_title = session.resolve_show(context)['details'].get('info',{}).get('title',context['title'])
        source.setdefault('tv_matches',{})[path_token(context['show_root'])] = {'tmdb_id':int(tv_id),'title':confirmed_title}
        save_history(history)
    _run_scan([(path,os.path.dirname(path)) for path in targets],[dirpath],session)
    refresh_menu()


def _tv_directory_files(source, group):
    from directory_listing import group_inventory
    from path_display import path_token
    from tv_context import _show_directory
    groups,_ = group_inventory(source.get('files',{}),source.get('content_type','auto'))
    root = next((root for root in groups if path_token(root)==group),None)
    if root is None:
        return None,{}
    files = dict(groups[root])
    return root,files


@guarded_task
def manual_match_show(dirpath, group):
    """The visible TV matching entry point selects one physical show directory."""
    if _is_busy():
        xbmcgui.Dialog().notification(ADDON_NAME,L(32042),xbmcgui.NOTIFICATION_WARNING,3000)
        return
    source = load_history().get('directories',{}).get(dirpath,{})
    root,files = _tv_directory_files(source,group)
    if not files:
        return
    from tv_context import build_tv_contexts
    from directory_listing import episode_order
    contexts = build_tv_contexts(files,'tvshow')
    # Prefer a regular episode as representative; search still uses the root
    # title/year. Supplements and all-unknown inventories remain supported.
    representative = min(files,key=lambda path:(contexts[path]['kind']!='episode',episode_order((path,files[path]))))
    manual_match(representative,dirpath,show_root=root)


@guarded_task
def manual_match(filepath=None, dirpath=None, show_root=None):
    """Manual match entry point.

    Called either via the file-list context menu (params fetched from the
    RunPlugin URL) or directly from the "review pending" dialog, in which case
    filepath/dirpath are passed explicitly.
    """
    if not filepath or not dirpath:
        params = get_params()
        filepath = filepath or params.get('filepath', '')
        dirpath = dirpath or params.get('dirpath', '')
    if not filepath or not dirpath:
        return

    fname = os.path.basename(show_root.rstrip('/')) if show_root else os.path.basename(filepath)
    dialog = xbmcgui.Dialog()
    history = load_history()
    source = history.get('directories',{}).get(dirpath,{})
    from tv_context import build_tv_contexts
    if show_root:
        from path_display import path_token
        _,matching_files = _tv_directory_files(source,path_token(show_root))
        tv_context = build_tv_contexts(matching_files,'tvshow').get(filepath)
    else:
        tv_context = build_tv_contexts(source.get('files',{}) or [filepath],source.get('content_type','auto')).get(filepath)

    # Auto-segment the original basename so the user can pick the real title
    # from a candidate list instead of retyping the whole thing every time a
    # match failed. Falls back to the manual input box when the file is so
    # garbled that segmentation produces zero usable chunks.
    seg_labels, seg_query = _split_name_into_segments(fname,tv_context)
    ret = dialog.select(L(32106), seg_labels)
    if ret < 0:
        return
    auto_query = ret == 0
    if ret == 0:
        # Default: take the auto-extracted full-title row as the query.
        query = seg_query(0)
    else:
        query = seg_query(ret)
    if not query:
        query = dialog.input(L(32080) % fname)
        if not query:
            return

    tmdb_key = ADDON.getSetting('tmdb_api_key') or 'f090bb54758cabf231fb605d3e3e0468'
    language = ADDON.getSetting('tmdb_language')

    import dns_override
    from tmdb_api import search_multi, get_movie_details, get_tv_details, get_tv_episode_details, get_tv_season_episodes
    from kodi_db import KodiDatabase
    from tvshow_db import TvShowDatabase

    media_type = None  # 'movie' or 'tvshow'
    movie_details = None
    tv_details = None
    tv_id = None

    try:
        if query.strip().isdigit():
            # Numeric ID - ask user which type
            ret = 1 if tv_context else dialog.select(L(32081), [L(32082), L(32083)])
            if ret < 0:
                return
            pDialog = xbmcgui.DialogProgress()
            if ret == 0:
                media_type = 'movie'
                pDialog.create(ADDON_NAME, L(32084))
                movie_details = get_movie_details(query.strip(), tmdb_key, language)
                pDialog.close()
                if not movie_details:
                    dialog.ok(ADDON_NAME, L(32085) % query.strip())
                    return
            else:
                media_type = 'tvshow'
                pDialog.create(ADDON_NAME, L(32086))
                tv_details = get_tv_details(query.strip(), tmdb_key, language)
                pDialog.close()
                if not tv_details:
                    dialog.ok(ADDON_NAME, L(32087) % query.strip())
                    return
                tv_id = query.strip()
        else:
            # Search by name using multi search
            pDialog = xbmcgui.DialogProgress()
            pDialog.create(ADDON_NAME, L(32088))
            results = _manual_search_candidates(query, fname, auto_query, tmdb_key, language,tv_context)
            pDialog.close()

            if not results:
                dialog.ok(ADDON_NAME,
                          '未找到匹配的结果\n"%s" 在 TMDB 中没有搜索结果' % query)
                return

            # Build select dialog with type labels
            items = []
            for r in results[:20]:
                mt = r.get('media_type', '')
                if mt == 'movie':
                    title = r.get('title', '')
                    year = r.get('release_date', '')[:4] if r.get('release_date') else ''
                    label = '[COLOR=FFFF4444]电影[/COLOR]  %s (%s)' % (title, year)
                else:
                    title = r.get('name', '')
                    year = r.get('first_air_date', '')[:4] if r.get('first_air_date') else ''
                    label = '[COLOR=FF4488FF]剧集[/COLOR]  %s (%s)' % (title, year)
                items.append(label)

            ret = dialog.select(L(32090) % len(items), items)
            if ret < 0:
                return

            selected = results[ret]
            media_type = 'movie' if selected.get('media_type') == 'movie' else 'tvshow'
            tmdb_id_val = selected.get('id')

            pDialog = xbmcgui.DialogProgress()
            if media_type == 'movie':
                pDialog.create(ADDON_NAME, L(32084))
                movie_details = get_movie_details(tmdb_id_val, tmdb_key, language)
            else:
                pDialog.create(ADDON_NAME, L(32086))
                tv_details = get_tv_details(tmdb_id_val, tmdb_key, language)
                tv_id = tmdb_id_val
            pDialog.close()

            if media_type == 'movie' and not movie_details:
                dialog.ok(ADDON_NAME, L(32091))
                return
            if media_type == 'tvshow' and not tv_details:
                dialog.ok(ADDON_NAME, L(32092))
                return
    except Exception as e:
        log('Manual match search error: %s' % e, xbmc.LOGERROR)
        try:
            pDialog.close()
        except Exception:
            pass
        dialog.ok(ADDON_NAME, L(32093) % str(e)[:120])
        return

    # ==========================================
    # Movie path
    # ==========================================
    if media_type == 'movie':
        # Reidentification is atomic in save_movie. Deleting first destroys
        # playback state and can detach other versions of a merged movie.
        db = KodiDatabase()
        try:
            db.connect()
            tmdb_id_str = movie_details.get('uniqueids', {}).get('tmdb', '')
            existing_id = db.find_movie_by_uniqueid('tmdb', str(tmdb_id_str)) if tmdb_id_str else None
            if existing_id:
                movie_id = db.save_movie(movie_details, filepath, merge_into_movie_id=existing_id)
                status_flag = 'merged'
            else:
                movie_id = db.save_movie(movie_details, filepath)
                status_flag = 'success' if movie_id else 'failed'
        except Exception as e:
            log('Manual match DB error: %s' % e, xbmc.LOGERROR)
            movie_id = None
            status_flag = 'failed'
        finally:
            db.close()

        if movie_id:
            title = movie_details.get('info', {}).get('title', '')
            set_movie_art(movie_id, movie_details.get('available_art', {}))
            # Prefetch posters/fanart AND cast thumbnails through the
            # proxy chain (manual matches bypass the scan-end prefetch).
            try:
                ImageCacher().prefetch_art([_art_with_cast(movie_details)])
            except Exception as e:
                log('Manual match prefetch error: %s' % e, xbmc.LOGERROR)
            history = load_history()
            dir_files = history.get('directories', {}).get(dirpath, {}).get('files', {})
            if filepath in dir_files:
                movie_year = movie_details.get('info', {}).get('year', '') or \
                    (movie_details.get('info', {}).get('premiered', '')[:4] if movie_details.get('info', {}).get('premiered') else '')
                dir_files[filepath] = {
                    'status': status_flag,
                    'movie_id': movie_id,
                    'tmdb_title': title or '',
                    'year': movie_year or '',
                    'match_evidence': {'tmdb_id':int(tmdb_id_str) if str(tmdb_id_str).isdigit() else tmdb_id_str,
                                       'reason':'manual_confirmation'},
                }
            save_history(history)
            dialog.notification(ADDON_NAME, L(32094) % title)
        else:
            dialog.ok(ADDON_NAME, L(32095))

    else:
        # ==========================================
        # TV Show path
        # ==========================================
        if tv_context:
            from tv_matcher import TvMatchSession, TvMatchError
            confirmed_context = dict(tv_context)
            if not confirmed_context['title']:
                confirmed_title = tv_details.get('info',{}).get('title','')
                confirmed_context.update(title=confirmed_title,titles=[confirmed_title])
                if confirmed_context.get('reason')=='show_title_missing' and confirmed_context.get('episodes'):
                    confirmed_context.update(kind='episode',reason='')
            check = TvMatchSession([],tmdb_key,language,forced_ids={confirmed_context['show_root']:tv_id})
            check.contexts[filepath] = confirmed_context
            try:
                check.prepare_file(filepath)
            except TvMatchError as error:
                if error.reason not in ('episode_number_out_of_range','episode_not_found','season_not_found'):
                    dialog.ok(ADDON_NAME,str(error))
                    return
                # A show confirmation cannot fix incorrect numbering. Continue
                # to explicit season/episode selection instead of a dead end.
                dialog.ok(ADDON_NAME,str(error))
            else:
                _manual_tv_apply(filepath,dirpath,tv_id,confirmed_context,
                                 apply_all=bool(show_root or confirmed_context.get('directory_identity')))
                return
        tv_title = tv_details.get('info', {}).get('title', '')
        seasons = tv_details.get('seasons', [])

        if not seasons:
            dialog.ok(ADDON_NAME, L(32096))
            return

        # Step 1: Select season
        season_items = []
        for s in seasons:
            sn = s.get('season_number', 0)
            sname = s.get('name', 'Season %d' % sn)
            scount = s.get('episode_count', 0)
            season_items.append(L(32097) % (sname, scount))

        season_ret = dialog.select(L(32098) % tv_title, season_items)
        if season_ret < 0:
            return

        season_number = seasons[season_ret].get('season_number', 1)

        # Step 2: Fetch episodes for this season
        pDialog = xbmcgui.DialogProgress()
        pDialog.create(ADDON_NAME, L(32099))
        episodes = get_tv_season_episodes(tv_id, season_number, tmdb_key, language)
        pDialog.close()

        if not episodes:
            dialog.ok(ADDON_NAME, L(32100))
            return

        # Step 3: Select episode
        ep_items = []
        for ep in episodes:
            en = ep.get('episode_number', 0)
            ename = ep.get('name', '')
            edate = ep.get('air_date', '')[:10]
            label = 'E%02d  %s' % (en, ename)
            if edate:
                label += '  (%s)' % edate
            ep_items.append(label)

        ep_ret = dialog.select(L(32101) % season_number, ep_items)
        if ep_ret < 0:
            return

        episode_number = episodes[ep_ret].get('episode_number', 1)

        # A manually selected season/episode is still validated against the
        # real season catalog. Do not delete old rows before a safe transaction.
        context = dict(tv_context or {})
        context.update(kind='episode',show_root=(tv_context or {}).get('show_root') or os.path.dirname(filepath).rstrip('/')+'/',
                       title=tv_title,titles=[tv_title],english_title='',
                       year=tv_details.get('info',{}).get('year'),
                       season_number=season_number,season_explicit=True,
                       episodes=[episode_number],source_episodes=[episode_number],
                       edition='',episode_title='',reason='')
        _manual_tv_apply(filepath,dirpath,tv_id,context)
        return

    refresh_menu()


# ==========================================
# Review pending (low-confidence / failed) matches
# ==========================================
def _collect_pending(history=None):
    """Collect movie files and TV show-directory confirmations separately."""
    from directory_listing import pending_reviews
    history = load_history() if history is None else history
    items = []
    for dirpath, data in history.get('directories', {}).items():
        for filepath, info in pending_reviews(data.get('files',{}),data.get('content_type','auto'),_needs_scan):
            items.append((dirpath,filepath,info))
    return items


def _count_pending(dirpaths):
    history = load_history()
    n = 0
    for dp in dirpaths:
        files = history.get('directories', {}).get(dp, {}).get('files', {})
        n += sum(1 for f in files.values()
                 if _needs_scan(f))
    return n


def review_pending():
    """Dialog-driven manual matching for all pending/failed files.

    Shows one item per TV show directory, plus individual movie files from the
    scan history. Picking one launches the standard manual-match flow, after
    which the list is rebuilt so the user can keep matching until none remain.
    """
    items = _collect_pending()
    if not items:
        xbmcgui.Dialog().notification(
            ADDON_NAME, L(32163), xbmcgui.NOTIFICATION_INFO, 3000)
        return

    dialog = xbmcgui.Dialog()
    while True:
        items = _collect_pending()
        if not items:
            xbmcgui.Dialog().notification(
                ADDON_NAME, L(32163), xbmcgui.NOTIFICATION_INFO, 3000)
            return

        # Sort by directory then filename for a stable, readable list
        items.sort(key=lambda t: (t[0], os.path.basename(t[1])))

        labels = []
        for dirpath, filepath, info in items:
            status = info.get('status', '')
            st_label = STATUS_LABELS.get(status, status)
            guessed = info.get('tmdb_title', '') or ''
            root = info.get('_review_show_root')
            from path_display import directory_label
            name = directory_label(root).rsplit('[',1)[0] if root else os.path.basename(filepath)
            line = '[COLOR=FFDD4444]%s[/COLOR]  %s' % (st_label, name)
            if root:
                line += ' · %d 个待处理视频' % info['_review_count']
            if guessed:
                line += '  →  [COLOR=FFFF8800]可能: %s[/COLOR]' % guessed
            labels.append(line)

        heading = L(32162) % len(items)
        ret = dialog.select(heading, labels)
        if ret < 0:
            return
        dirpath, filepath, info = items[ret]
        if info.get('_review_show_root'):
            from path_display import path_token
            manual_match_show(dirpath,path_token(info['_review_show_root']))
        else:
            manual_match(filepath, dirpath)
        refresh_menu()


# ==========================================
# View Directory (files inside)
# ==========================================
def view_directory(dirpath, group='', page=0, history=None):
    if not dirpath:
        return
    from directory_listing import listing_page, is_tv, group_label
    from path_display import path_token
    history = load_history() if history is None else history
    data = history.get('directories',{}).get(dirpath,{})
    files = data.get('files',{})
    model = listing_page(files,data.get('content_type','auto'),group,page)
    listing = []
    pending = sum(_needs_scan(info) for info in files.values())
    if pending:
        li = xbmcgui.ListItem('[B]%s (%d)[/B]' % (L(32027),pending), offscreen=True)
        li.setArt({'icon':'DefaultAddonVideo.png'})
        listing.append((build_url({'action':'scan_directory','dirpath':dirpath}),li,False))
    if model['page']>0:
        li = xbmcgui.ListItem(L(32411) or '上一页', offscreen=True)
        listing.append((build_url({'action':'view_directory','dirpath':dirpath,'group':group,'page':model['page']-1}),li,True))
    for kind,path,info in model['rows']:
        if kind=='group':
            entries = info['files']
            success = sum(i.get('status') in ('success','merged') for _,i in entries)
            unknown = sum(i.get('match_evidence',{}).get('numbering_mode')=='local_unmatched' for _,i in entries)
            li = xbmcgui.ListItem(group_label(path,entries), offscreen=True)
            li.setLabel2('%d 文件 · 已匹配 %d · 未知集 %d' % (len(entries),success,unknown))
            li.setArt({'icon':'DefaultFolder.png'})
            context_menu(li, [(L(32029),run_plugin_url({'action':'manual_match_show','dirpath':dirpath,'group':path_token(path)}))])
            listing.append((build_url({'action':'view_directory','dirpath':dirpath,'group':path_token(path)}),li,True))
            continue
        status = info.get('status','pending')
        fname = os.path.basename(path)
        evidence = info.get('match_evidence',{})
        unknown = evidence.get('numbering_mode')=='local_unmatched'
        tv = is_tv(info) or data.get('content_type')=='tvshow'
        prefix = (L(32409) or '未知集') if unknown else ('剧集' if tv else '电影')
        color = 'FF44DD44' if status in ('success','merged') else 'FFDD4444'
        label = '[COLOR=%s]%s[/COLOR] %s' % (color,prefix,redact_text(fname))
        details = info.get('tmdb_title','')
        if tv and status in ('success','merged'):
            numbers = info.get('episode_numbers') or [info.get('episode_number',0)]
            if not unknown:
                details += ' · S%02d %s' % (info.get('season_number',0), ', '.join('E%02d'%n for n in numbers))
            if evidence.get('numbering_mode')=='local_edited_version':
                details += ' · '+(L(32407) or '精编版独立编号')
        else:
            details += ' · '+STATUS_LABELS.get(status,STATUS_LABELS['pending'])
        if info.get('error'):
            details += ' · '+redact_text(info['error'])
        li = xbmcgui.ListItem(label, offscreen=True)
        li.setLabel2(redact_text(details))
        li.setArt({'icon':'DefaultVideo.png'})
        # These are single-file plugin commands. Marking the menu entry as
        # non-playable suppresses Kodi's automatic Play from here/queue menu;
        # the click action below explicitly plays only the selected file.
        li.setProperty('IsPlayable','false')
        li.setProperty('CanQueue','false')
        if not tv:
            context_menu(li, [(L(32029),run_plugin_url({'action':'manual_match','filepath':path,'dirpath':dirpath}))])
        listing.append((build_url({'action':'play_file','dirpath':dirpath,'filepath':path}),li,False))
    if model['page']+1<model['pages']:
        li = xbmcgui.ListItem('%s (%d/%d)' % (L(32412) or '下一页',model['page']+2,model['pages']), offscreen=True)
        listing.append((build_url({'action':'view_directory','dirpath':dirpath,'group':group,'page':model['page']+1}),li,True))
    if not listing:
        listing.append((build_url({}),xbmcgui.ListItem(L(32054), offscreen=True),False))
    category = model['label'] or directory_label(dirpath)
    if model['pages']>1:
        category += ' · %d/%d' % (model['page']+1,model['pages'])
    emit_listing(listing, category)


def play_media_file(dirpath, filepath):
    """Play one registered physical file; never queue the surrounding listing.

    A normal menu click invokes the non-playable plugin command with handle
    -1. Kodi favourites can invoke PlayMedia on the same URL with a resolver
    handle; support both paths without exposing credentials in menu URLs.
    """
    info = load_history().get('directories',{}).get(dirpath,{}).get('files',{}).get(filepath)
    if info is None or not filepath:
        if addon_handle>=0:
            xbmcplugin.setResolvedUrl(addon_handle,False,xbmcgui.ListItem(offscreen=True))
        xbmcgui.Dialog().notification(ADDON_NAME,L(32414) or '文件已不在插件媒体库中，请重新扫描目录。',
                                     xbmcgui.NOTIFICATION_WARNING,4000)
        return False
    from file_scanner import VIDEO_EXTENSIONS
    target = filepath
    if os.path.splitext(filepath.rstrip('/'))[1].lower() not in VIDEO_EXTENSIONS:
        # Scanner inventory represents Blu-ray directories by their parent.
        # Resolve the disc entry by string, without probing the video share.
        target = filepath.rstrip('/')+'/BDMV/index.bdmv'
    from directory_listing import is_tv
    tv = is_tv(info)
    title = os.path.splitext(os.path.basename(filepath))[0] if tv else info.get('tmdb_title') or os.path.basename(filepath)
    li = xbmcgui.ListItem(label=redact_text(title),path=target,offscreen=True)
    li.setProperty('IsPlayable','true')
    tag = li.getVideoInfoTag()
    tag.setTitle(redact_text(title))
    if tv:
        tag.setMediaType('episode')
        tag.setTvShowTitle(info.get('tmdb_title',''))
        tag.setSeason(int(info.get('season_number',1) or 0))
        tag.setEpisode(int(info.get('episode_number',0) or 0))
        ids = info.get('episode_ids') or info.get('match_evidence',{}).get('episode_ids') or []
        dbid = ids[0] if ids else info.get('episode_id')
    else:
        tag.setMediaType('movie')
        dbid = info.get('movie_id')
    if isinstance(dbid,int) and dbid>0:
        tag.setDbId(dbid)
    if addon_handle>=0:
        xbmcplugin.setResolvedUrl(addon_handle,True,li)
    else:
        xbmc.Player().play(target,listitem=li)
    return True


# ==========================================
# Remove Directory (cascade delete)
# ==========================================
@guarded_task
def remove_directory(dirpath):
    if not dirpath:
        return

    dialog = xbmcgui.Dialog()
    if not dialog.yesno(ADDON_NAME,
                        L(32051)):
        return

    cascade_delete_directory(dirpath)

    history = load_history()
    if dirpath in history.get('directories', {}):
        del history['directories'][dirpath]
        save_history(history)

    refresh_menu()
    dialog.notification(ADDON_NAME, L(32040))


def _delete_single_file_from_db(filepath):
    from kodi_db import KodiDatabase,DB_WRITE_LOCK
    from library_cleanup import file_ids_for_path,remove_files
    database=KodiDatabase()
    try:
        database.connect()
        with DB_WRITE_LOCK:
            result=remove_files(database.conn,file_ids_for_path(database.conn,filepath))
        return result['movies']+result['episodes']
    finally:
        database.close()


@guarded_task
def refresh_directory_files(dirpath):
    """Compare disk vs history: add new files as pending, remove deleted files from DB+history.
    Returns (new_count, deleted_count)."""
    from file_scanner import find_video_files,ScanIncomplete

    history = load_history()
    if dirpath not in history.get('directories', {}):
        return 0, 0

    dir_data = history['directories'][dirpath]
    old_files = dict(dir_data.get('files', {}))

    # Get current files on disk
    if not xbmcvfs.exists(dirpath):
        log('Source unavailable; existing inventory retained: '+directory_label(dirpath),xbmc.LOGWARNING)
        return 0,0
    try:
        current_set = set(find_video_files(dirpath,cancelled=_is_stop_requested))
    except ScanIncomplete:
        log('Source traversal incomplete; existing inventory retained: '+directory_label(dirpath),xbmc.LOGWARNING)
        return 0,0

    old_set = set(old_files.keys())
    if old_set and not current_set:
        # Kodi VFS can return an empty listing without reporting a network
        # error. Clearing an entire source requires explicit source removal.
        log('Empty source listing; existing inventory retained: '+directory_label(dirpath),xbmc.LOGWARNING)
        return 0,0

    # ---- New files: add as pending ----
    new_files = current_set - old_set
    for f in sorted(new_files):
        old_files[f] = {'status': 'pending'}
        log('New file discovered: %s' % os.path.basename(f))

    # ---- Deleted files: remove from DB and history ----
    deleted_files = old_set - current_set
    for f in sorted(deleted_files):
        old_status = old_files[f].get('status', 'pending')
        if old_status in ('success', 'merged'):
            _delete_single_file_from_db(f)
        log('File removed from history: %s (was %s)' % (os.path.basename(f), old_status))
        del old_files[f]

    # Save back
    history['directories'][dirpath]['files'] = old_files
    save_history(history)

    return len(new_files), len(deleted_files)


@guarded_task
def cascade_delete_directory(dirpath):
    from kodi_db import KodiDatabase,DB_WRITE_LOCK
    from library_cleanup import file_ids_for_source,remove_files
    database=KodiDatabase()
    try:
        database.connect()
        with DB_WRITE_LOCK:
            result=remove_files(database.conn,file_ids_for_source(database.conn,dirpath))
        log('Source removed: %d movies, %d episodes' % (result['movies'],result['episodes']))
        return result
    finally:
        database.close()


# ==========================================
# Scan Single Directory
# ==========================================
@guarded_task
def scan_directory(dirpath):
    if _is_busy():
        xbmcgui.Dialog().notification(ADDON_NAME, L(32042), xbmcgui.NOTIFICATION_WARNING, 3000)
        return
    history = load_history()
    if dirpath not in history.get('directories', {}):
        xbmcgui.Dialog().ok(ADDON_NAME, L(32039))
        return

    # Refresh: discover new files on disk, clean up deleted ones
    _set_refreshing()
    try:
        new_count, del_count = refresh_directory_files(dirpath)
    finally:
        _clear_refreshing()
    if new_count or del_count:
        xbmcgui.Dialog().notification(
            ADDON_NAME,
            L(32048) % (new_count, del_count),
            xbmcgui.NOTIFICATION_INFO, 3000)
    history = load_history()  # reload after refresh

    files = history['directories'][dirpath].get('files', {})
    work_items = []
    for filepath, info in files.items():
        if _needs_scan(info):
            if not xbmcvfs.exists(filepath) and not os.path.isdir(filepath):
                info['status'] = 'failed'
                info['error'] = L(32043)
                continue
            work_items.append((filepath, os.path.dirname(filepath)))

    if not work_items:
        save_history(history)
        refresh_menu()
        xbmcgui.Dialog().ok(ADDON_NAME, L(32044))
        return

    _run_scan(work_items, [dirpath])

    pending = _count_pending([dirpath])
    if pending > 0 and xbmcgui.Dialog().yesno(ADDON_NAME, L(32164) % pending):
        review_pending()


# ==========================================
# Scan All Directories
# ==========================================
@guarded_task
def scan_all():
    if _is_busy():
        xbmcgui.Dialog().notification(ADDON_NAME, L(32042), xbmcgui.NOTIFICATION_WARNING, 3000)
        return

    # Refresh all directories first
    total_new = 0
    total_del = 0
    _set_refreshing()
    try:
        history = load_history()
        for dirpath in list(history.get('directories', {}).keys()):
            nc, dc = refresh_directory_files(dirpath)
            total_new += nc
            total_del += dc
    finally:
        _clear_refreshing()
    if total_new or total_del:
        xbmcgui.Dialog().notification(
            ADDON_NAME,
            L(32048) % (total_new, total_del),
            xbmcgui.NOTIFICATION_INFO, 3000)

    history = load_history()  # reload after refresh
    work_items = []
    all_dirs = []

    for dirpath, data in history.get('directories', {}).items():
        dir_has_work = False
        for filepath, info in data.get('files', {}).items():
            if _needs_scan(info):
                if not xbmcvfs.exists(filepath):
                    info['status'] = 'failed'
                    info['error'] = L(32043)
                    continue
                work_items.append((filepath, os.path.dirname(filepath)))
                dir_has_work = True
        if dir_has_work:
            all_dirs.append(dirpath)

    if not work_items:
        save_history(history)
        refresh_menu()
        xbmcgui.Dialog().ok(ADDON_NAME, L(32044))
        return

    _run_scan(work_items, all_dirs)

    pending = _count_pending(all_dirs)
    if pending > 0 and xbmcgui.Dialog().yesno(ADDON_NAME, L(32164) % pending):
        review_pending()


# ==========================================
# Scan Engine (multi-threaded)
# ==========================================
def _store_scan_result(history, affected_dirs, result):
    parts = list(result)
    status, filepath, _name, media_id, title, _art = parts[:6]
    season, episode, error, year = (parts+[None]*10)[6:10]
    evidence = parts[10] if len(parts)>10 and isinstance(parts[10],dict) else None
    is_tv = bool(evidence and evidence.get('type')=='tvshow') or bool(episode and season is not None and season>=0)
    if status in ('success','merged'):
        entry = {'status':status,'tmdb_title':title or '', 'year':year or ''}
        if media_id:
            entry['show_id' if is_tv else 'movie_id'] = media_id
    elif status=='skipped':
        entry = {'status':'skipped','error':error or '附加内容已略过。','tmdb_title':title or '', 'year':year or ''}
    else:
        entry = {'status':'pending' if is_tv and status=='pending' else 'failed',
                 'error':error or L(32055),'season_number':season or 0,
                 'episode_number':episode or 0,'year':year or ''}
    if evidence:
        entry['match_evidence'] = evidence
    if is_tv:
        entry.update(is_tvshow=True,season_number=season if season is not None else 1,
                     episode_number=episode or 0)
        if evidence and evidence.get('episode_numbers'):
            entry['episode_numbers'] = evidence['episode_numbers']
            entry['episode_ids'] = evidence.get('episode_ids',[])
    for directory in affected_dirs:
        files = history.get('directories',{}).get(directory,{}).get('files',{})
        if filepath in files:
            files[filepath] = entry
            break
    return entry


@guarded_task
def _run_scan(work_items, affected_dirs, tv_session_override=None):
    if _is_scanning():
        xbmcgui.Dialog().notification(ADDON_NAME, L(32042), xbmcgui.NOTIFICATION_WARNING, 3000)
        return 0,0,0

    # Create scanning marker (cross-process)
    _set_scanning()
    _clear_stop()

    pDialog = None
    workers = []
    processed = 0
    success = 0
    merged = 0
    failed = 0
    skipped = 0
    history = None
    art_entries = []
    abort_event = threading.Event()
    try:
        ai_enabled = ADDON.getSetting('ai_enabled') == 'true'
        api_key = ADDON.getSetting('ai_api_key') or ''
        # Default to DeepSeek (reachable from China without a VPN).
        # api.openai.com times out on CoreELEC boxes without a proxy - it
        # must only be used when the user explicitly configures it.
        api_base = ADDON.getSetting('ai_api_base') or 'https://api.deepseek.com'
        api_model = ADDON.getSetting('ai_model') or 'deepseek-flash'
        tmdb_key = ADDON.getSetting('tmdb_api_key') or ''
        language = ADDON.getSetting('tmdb_language')
        try:scan_delay = max(0,min(10,int(ADDON.getSetting('scan_delay') or '1')))
        except (ValueError,TypeError):scan_delay=1

        # TMDB key is mandatory - validate before scanning
        import dns_override
        from tmdb_api import check_api_key
        tmdb_status = check_api_key(tmdb_key)
        if tmdb_status == 'empty':
            xbmcgui.Dialog().ok(ADDON_NAME, L(32180))
            return
        if tmdb_status == 'invalid':
            xbmcgui.Dialog().ok(ADDON_NAME, L(32181))
            return
        # 'error' (network/transient) is non-blocking: proceed anyway

        # AI is optional: if enabled but no key configured, silently fall
        # back to regex-only parsing instead of blocking the scan.
        if ai_enabled and not api_key:
            ai_enabled = False

        work_queue = queue.Queue()
        for item in work_items:
            work_queue.put(item)

        scanned_count = len(work_items)
        abort_event = threading.Event()
        result_queue = queue.Queue()
        from tv_matcher import TvMatchSession
        from tv_context import build_tv_contexts
        history = load_history()
        tv_session = tv_session_override or TvMatchSession([], tmdb_key, language)
        tv_session.forced_movies = set()
        if tv_session_override is None:
            for directory in affected_dirs:
                data = history.get('directories',{}).get(directory,{})
                tv_session.previous_results.update(data.get('files',{}))
                if data.get('content_type') == 'movie':
                    tv_session.forced_movies.update(data.get('files',{}))
                tv_session.register_source(data.get('files',{}),data.get('content_type','auto'),data.get('tv_matches',{}))
        # Parallel workers (configurable 1-24, default 12). The global
        # token-bucket limiter inside tmdb_api keeps the total TMDB request
        # rate under the free-tier limit, so more workers = more files in
        # flight (especially while DeepSeek AI parsing waits on its own
        # network round-trips) without tripping HTTP 429.
        try:
            num_workers = max(1, min(24,
                int(ADDON.getSetting('num_workers') or '12')))
        except (ValueError, TypeError):
            num_workers = 12

        for i in range(num_workers):
            t = threading.Thread(
                target=scan_worker,
                args=(work_queue, result_queue, ai_enabled, api_key, api_base,
                      api_model, tmdb_key, language, scan_delay, abort_event, tv_session,_is_stop_requested),
                name='AI-Scanner-%d' % (i + 1))
            t.daemon = True
            t.start()
            workers.append(t)

        pDialog = xbmcgui.DialogProgressBG()
        pDialog.create(ADDON_NAME, L(32045))

        # Load history ONCE and keep it in memory: the old per-result
        # load+save rewrote the whole JSON file for every movie, which was
        # the main-thread bottleneck. Now we persist in batches and once
        # at the end (crash-safe enough: a rescan picks up where we left).
        history = load_history()
        HISTORY_SAVE_EVERY = 10

        while processed < scanned_count:
            if pDialog.isFinished() or _is_stop_requested() or xbmc.Monitor().abortRequested():
                abort_event.set()
                break
            try:
                result = result_queue.get(timeout=0.5)
            except queue.Empty:
                if pDialog.isFinished() or _is_stop_requested():
                    abort_event.set()
                    break
                continue

            processed += 1
            result_parts = list(result)
            status, file_path, file_name, media_id, tmdb_title, art_urls = \
                result_parts[0], result_parts[1], result_parts[2], \
                result_parts[3], result_parts[4], result_parts[5]
            season_num = result_parts[6] if len(result_parts) > 6 else 0
            episode_num = result_parts[7] if len(result_parts) > 7 else 0
            error = result_parts[8] if len(result_parts) > 8 else None
            match_year = result_parts[9] if len(result_parts) > 9 else ''

            _store_scan_result(history,affected_dirs,result)
            if status in ('success','merged'):
                success += 1
                merged += status=='merged'
                if art_urls:
                    art_entries.append(art_urls)
            elif status=='skipped':
                skipped += 1
            else:
                failed += 1
            if processed % HISTORY_SAVE_EVERY == 0:
                save_history(history)

            percent = int(processed * 100 / scanned_count)
            heading = L(32062) % (processed, scanned_count)
            msg = L(32063) % (success, merged, failed)
            pDialog.update(percent, heading, msg)

    finally:
        # Signal workers to stop immediately so join() doesn't hang for
        # timeout=5s per thread (12 workers = 60s delay).
        abort_event.set()
        for t in workers:
            t.join()
        if history is not None:
            while True:
                try:
                    late_result = result_queue.get_nowait()
                except queue.Empty:
                    break
                _store_scan_result(history,affected_dirs,late_result)
                processed += 1
                if late_result[0] in ('success','merged'):
                    success += 1
                    merged += late_result[0]=='merged'
                elif late_result[0]=='skipped':skipped += 1
                else:failed += 1
                if late_result[0] in ('success','merged') and late_result[5]:
                    art_entries.append(late_result[5])
        stopped = _is_stop_requested() or xbmc.Monitor().abortRequested()
        try:
            if history is not None:
                save_history(history)
        finally:
            # Cleanup still runs if persistence failed (disk full/corruption).
            _clear_scanning()
            _clear_stop()
            if pDialog is not None:pDialog.close()
        if art_entries and not stopped:
            try:
                ImageCacher().prefetch_art(art_entries)
            except Exception as e:
                log('Image prefetch trigger error: %s' % e, xbmc.LOGERROR)
        refresh_menu()

    xbmcgui.Dialog().notification(ADDON_NAME,
                        L(32065) % (
                            success, merged, failed) + ('；略过 %d 个附加内容' % skipped if skipped else ''),
                        xbmcgui.NOTIFICATION_INFO, 5000)

    return success, merged, failed


# ==========================================
# Scan Worker Thread
# ==========================================
def _art_with_cast(details):
    """Return a copy of available_art with actor thumbnails appended under
    a 'cast' key, so the ImageCacher pre-fetches them through the proxy
    chain. Kodi itself only downloads actor thumbs on-demand (direct
    connection), which fails when image.tmdb.org is blocked."""
    art = dict(details.get('available_art', {}) or {})
    cast = details.get('cast', [])
    if cast and 'cast' not in art:
        art['cast'] = [{'url': c['thumbnail']}
                       for c in cast[:20] if c.get('thumbnail')]
    return art


def scan_worker(work_queue, result_queue, ai_enabled, api_key, api_base,
                api_model, tmdb_key, language, delay, abort_event, tv_session=None,stopped=None):
    from ai_parser import parse_filename
    from tmdb_api import (search_movie_best, get_movie_details,
                          search_tv_best, get_tv_details, get_tv_episode_details)
    from kodi_db import KodiDatabase
    from tvshow_db import TvShowDatabase
    from media_identity import build_evidence
    from movie_matcher import resolve_movie

    should_stop=lambda:abort_event.is_set() or bool(stopped and stopped())
    while not should_stop():
        try:
            filepath, dirpath = work_queue.get(timeout=0.5)
        except queue.Empty:
            continue

        fname = os.path.basename(filepath)

        try:
            from tv_matcher import TvMatchSession
            session = tv_session or TvMatchSession([filepath],tmdb_key,language)
            if session.context(filepath) is not None:
                if should_stop():
                    work_queue.task_done();continue
                session.cancelled=should_stop
                result_queue.put(session.process_file(filepath))
                work_queue.task_done()
                continue
            # ================================================
            # ① Regex pre-filter: try Kodi-style cleaning + TMDB first
            # ================================================
            ai = None
            used_ai = False
            media_type = 'movie'
            title = ''
            year = None
            english_title = ''
            season_number = 0
            episode_number = 0

            from ai_parser import regex_parse_filename
            expected_type = 'movie' if filepath in getattr(session,'forced_movies',set()) else 'auto'
            regex_result = regex_parse_filename(fname, dirpath, expected_type)
            if regex_result and expected_type=='movie':
                regex_result = dict(regex_result,type='movie',season_number=0,episode_number=0)
            source_evidence = build_evidence(fname, os.path.dirname(filepath))
            if regex_result is None and (source_evidence.get('tmdb_id') or source_evidence.get('imdb_id')):
                regex_result = {'type':'movie','title':source_evidence['titles'][0] if source_evidence['titles'] else fname,
                                'year':source_evidence['year']}
            if regex_result is not None:
                rt = regex_result.get('type', 'movie')
                rtitle = regex_result.get('title', '')
                ryear = regex_result.get('year')
                if rt == 'movie' and source_evidence['titles']:
                    regex_result = dict(regex_result)
                    rtitle = source_evidence['titles'][0]
                    ryear = source_evidence['year']
                    regex_result.update(title=rtitle, year=ryear)
                log('[Worker %s] Regex: "%s" (%s) type=%s'
                     % (threading.current_thread().name, rtitle, ryear, rt))

                if source_evidence.get('tmdb_id') or source_evidence.get('imdb_id'):
                    ai = regex_result
                elif rt == 'movie':
                    # Identity resolver does candidate retrieval once. Regex
                    # extraction alone is never reported as a verified match.
                    ai = regex_result
                else:
                    # TV show regex: search TV
                    rseason = regex_result.get('season_number', 1)
                    repisode = regex_result.get('episode_number', 1)
                    tv_results = search_tv_best(rtitle, ryear, '',
                                                tmdb_key, language)
                    if tv_results:
                        log('[Worker %s] Regex+TMDB TV hit: "%s" S%02dE%02d'
                             % (threading.current_thread().name, rtitle,
                                rseason, repisode))
                        ai = regex_result

            # ================================================
            # ② AI fallback: regex failed or TMDB didn't match
            # ================================================
            if ai is None:
                if ai_enabled:
                    ai = parse_filename(fname, dirpath, api_key, api_base,
                                        api_model, mode='movie' if expected_type=='movie' else (regex_result.get('type', 'auto') if regex_result else 'auto'),
                                        skip_regex=True)
                    used_ai = ai is not None
                else:
                    ai = None

            if not ai:
                if source_evidence['titles'] and (not regex_result or regex_result.get('type') != 'tvshow'):
                    ai = {'type':'movie','title':source_evidence['titles'][0],'year':source_evidence['year']}
            if not ai:
                result_queue.put((
                    'failed', filepath, fname, None, None, None, 0, 0,
                    L(32110), ''))
                work_queue.task_done()
                abort_event.wait(delay)
                continue

            if expected_type=='movie':
                ai = dict(ai,type='movie',season_number=0,episode_number=0)
            media_type = ai.get('type', 'movie')
            title = ai.get('title', '')
            year = ai.get('year')
            english_title = ai.get('english_title', '')
            season_number = ai.get('season_number', 0) or 0
            episode_number = ai.get('episode_number', 0) or 0

            # ================================================
            # Movie path
            # ================================================
            if media_type != 'tvshow':
                season_number = 0
                episode_number = 0

                decision = resolve_movie(fname, os.path.dirname(filepath),
                                         tmdb_key, language, parsed=ai)
                if decision['status'] == 'unresolved' and ai_enabled and not used_ai:
                    ai2 = parse_filename(fname, dirpath, api_key, api_base,
                                         api_model, mode='movie', skip_regex=True)
                    if ai2 and ai2.get('type', 'movie') == 'movie':
                        decision = resolve_movie(fname, os.path.dirname(filepath),
                                                 tmdb_key, language, parsed=ai2)
                if decision['status'] != 'accepted':
                    choices = ' / '.join('%s [TMDb %s]' %
                                        (c.get('title') or c.get('original_title'), c['id'])
                                        for c in decision.get('candidates', [])[:5])
                    if decision['status'] == 'ambiguous':
                        message = (L(32167) or '同名同年存在多个候选：%s (%s)：%s。请手动匹配确认。') % (
                            title, source_evidence.get('year') or '', choices)
                    else:
                        message = (L(32168) or '原始片名、年份与候选身份无法充分核实（%s），请手动匹配确认。') % decision['reason']
                    log('[Worker %s] Identity review required: "%s": %s' %
                        (threading.current_thread().name, fname, decision['reason']))
                    result_queue.put(('pending', filepath, fname, None, None, None,
                                      0, 0, message, str(source_evidence.get('year') or '')))
                    work_queue.task_done()
                    abort_event.wait(delay)
                    continue
                best = decision['candidate']
                log('[Worker %s] Verified identity: "%s" -> TMDb %s (%s)' %
                    (threading.current_thread().name, fname, best['id'], decision['reason']))
                tmdb_id = best.get('id')
                movie_title = best.get('title', '')

                details = get_movie_details(tmdb_id, tmdb_key, language)
                if not details:
                    result_queue.put((
                        'failed', filepath, fname, None, None, None, 0, 0,
                        L(32112) % movie_title, ''))
                    work_queue.task_done()
                    abort_event.wait(delay)
                    continue

                if should_stop():
                    work_queue.task_done();continue
                db = KodiDatabase()
                try:
                    db.connect()
                    existing_id = db.find_movie_by_uniqueid('tmdb', str(tmdb_id))
                    if existing_id:
                        movie_id = db.save_movie(details, filepath,
                                                 merge_into_movie_id=existing_id)
                        status_flag = 'merged'
                    else:
                        movie_id = db.save_movie(details, filepath)
                        status_flag = 'success' if movie_id else 'failed'
                except Exception as e:
                    log('Worker DB error: '+str(e),xbmc.LOGERROR)
                    movie_id = None
                    status_flag = 'error'
                finally:
                    db.close()

                movie_year = details.get('info', {}).get('year', '') or \
                    (details.get('info', {}).get('premiered', '')[:4] if details.get('info', {}).get('premiered') else '')
                if movie_id:
                    result_queue.put((
                        status_flag, filepath, fname, movie_id, movie_title,
                        _art_with_cast(details), 0, 0, None, movie_year,
                        {'tmdb_id': tmdb_id, 'reason': decision['reason'],
                         'source_year': source_evidence.get('year'),
                         'source_titles': source_evidence.get('titles', [])}))
                else:
                    result_queue.put((
                        'failed', filepath, fname, None, None, None, 0, 0,
                        L(32095), ''))
                work_queue.task_done()
                continue

            # A TV classification without a validated directory/episode
            # context must remain reviewable; never fabricate episode data.
            result_queue.put(('pending',filepath,fname,None,title,None,
                              season_number,episode_number,
                              '无法核实电视剧的季集编号，请按剧名手动确认。',str(year or ''),
                              {'type':'tvshow','reason':'episode_context_unverified','source_title':title}))

        except Exception as e:
            log('Worker exception: '+str(e),xbmc.LOGERROR)
            result_queue.put((
                'error', filepath, fname, None, None, None, 0, 0, redact_text(e), ''))

        # No success-path sleep: the token-bucket rate limiter inside
        # tmdb_api already keeps us under TMDB's request quota, so waiting
        # between every file only wasted time. Failure paths above still
        # sleep(delay) as a backoff.
        work_queue.task_done()


# ==========================================
# Art update
# ==========================================
def set_movie_art(movie_id, available_art):
    from kodi_db import KodiDatabase

    db = KodiDatabase()
    try:
        db.connect()
        cur = db.conn.cursor()
        poster = available_art.get('poster', [])
        fanart = available_art.get('fanart', [])
        logo = available_art.get('clearlogo', [])

        # Sanity: log the URL we're about to write so posters going
        # missing in the wall view are traceable from the Kodi log.
        p_url = poster[0].get('url') if poster else None
        f_url = fanart[0].get('url') if fanart else None
        l_url = logo[0].get('url') if logo else None
        if not any([p_url, f_url, l_url]):
            log('set_movie_art WARN: movie %d has NO art URLs in '
                'available_art (poster=%d fanart=%d clearlogo=%d)'
                % (movie_id, len(poster), len(fanart), len(logo)),
                xbmc.LOGWARNING)

        # Write art table (Kodi 21+)
        cur.execute("DELETE FROM art WHERE media_id=? AND media_type='movie'",
                    (movie_id,))
        if p_url:
            cur.execute(
                "INSERT INTO art (media_id, media_type, type, url) "
                "VALUES (?, 'movie', 'poster', ?)",
                (movie_id, p_url.replace('/original/', '/w500/')))
        if f_url:
            cur.execute(
                "INSERT INTO art (media_id, media_type, type, url) "
                "VALUES (?, 'movie', 'fanart', ?)",
                (movie_id, f_url.replace('/original/', '/w1280/')))
        if l_url:
            cur.execute(
                "INSERT INTO art (media_id, media_type, type, url) "
                "VALUES (?, 'movie', 'clearlogo', ?)",
                (movie_id, l_url.replace('/original/', '/w500/')))
        db.conn.commit()
        log('Art table updated for movie %d: poster=%s fanart=%s clearlogo=%s'
            % (movie_id,
               (p_url or '').replace('/original/', '/w500/') if p_url else '-',
               (f_url or '').replace('/original/', '/w1280/') if f_url else '-',
               (l_url or '').replace('/original/', '/w500/') if l_url else '-'))

    except Exception as e:
        log('set_movie_art error: %s' % e, xbmc.LOGERROR)
    finally:
        db.close()


def _xml_escape(text):
    if not text:
        return ''
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')


if __name__ == '__main__':
    try:
        router()
    except (OSError,ValueError) as error:
        log('Scanner operation stopped: '+type(error).__name__,xbmc.LOGERROR)
        xbmcgui.Dialog().ok(ADDON_NAME,L(32416))
        if addon_handle>=0:xbmcplugin.endOfDirectory(addon_handle,succeeded=False)
