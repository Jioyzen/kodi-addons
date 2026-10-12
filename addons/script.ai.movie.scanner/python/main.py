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

import dns_override  # DoH bypass GFW DNS pollution for TMDB

from image_cacher import ImageCacher

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
}


def log(msg, level=xbmc.LOGINFO):
    xbmc.log('[%s] %s' % (ADDON_NAME, msg), level)


# ==========================================
# History persistence
# ==========================================
def ensure_data_dir():
    if not xbmcvfs.exists(DATA_PATH):
        xbmcvfs.mkdirs(DATA_PATH)


def load_history():
    ensure_data_dir()
    file_exists = xbmcvfs.exists(HISTORY_FILE)
    log('load_history: file=%s exists=%s' % (HISTORY_FILE, file_exists))
    if file_exists:
        try:
            with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            dirs = data.get('directories', {})
            log('load_history: OK, %d directories' % len(dirs))
            return data
        except Exception as e:
            log('load_history: JSON ERROR: %s' % e, xbmc.LOGERROR)
    return {'directories': {}}


def save_history(data):
    ensure_data_dir()
    try:
        tmp = HISTORY_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, HISTORY_FILE)
    except Exception as e:
        xbmc.log('[%s] Failed to save history: %s' % (ADDON_NAME, e), xbmc.LOGERROR)


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
    return xbmcvfs.exists(STOP_FILE)


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
        _clear_refreshing()
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
    return params


def build_url(query):
    return sys.argv[0] + '?' + urllib.parse.urlencode(query)


def run_plugin_url(query):
    """Build a RunPlugin(...) URL for context menu items."""
    return 'RunPlugin(%s)' % build_url(query)


# ==========================================
# Router
# ==========================================
def router(paramstring=''):
    params = get_params()
    action = params.get('action', None)

    if action is None:
        show_main_menu()
    elif action == 'add_directory':
        add_directory()
    elif action == 'view_directory':
        view_directory(params.get('dirpath', ''))
    elif action == 'scan_directory':
        scan_directory(params.get('dirpath', ''))
    elif action == 'scan_all':
        scan_all()
    elif action == 'remove_directory':
        remove_directory(params.get('dirpath', ''))
    elif action == 'refresh_all':
        action_refresh_all()
    elif action == 'manual_match':
        manual_match()
    elif action == 'review_pending':
        review_pending()
    elif action == 'cache_images':
        cache_images()
    elif action == 'cache_actor_images':
        cache_actor_images()
    elif action == 'stop_scan':
        stop_scan()
        xbmc.executebuiltin('Container.Refresh')
    elif action == 'clear_all_data':
        clear_all_data()
    elif action == 'open_settings':
        ADDON.openSettings()
        xbmc.executebuiltin('Container.Refresh')
    else:
        xbmc.log('[%s] Unknown action: %s' % (ADDON_NAME, action), xbmc.LOGERROR)


# ==========================================
# Main Menu
# ==========================================
def show_main_menu():
    history = load_history()
    dirs = history.get('directories', {})
    log('show_main_menu: loaded %d directories, %d total keys in history'
        % (len(dirs), len(history)))
    listing = []

    # --- [Stop Scan] ---
    li = xbmcgui.ListItem(L(32022))
    li.setArt({'icon': 'DefaultAddonInfoProvider.png'})
    url = build_url({'action': 'stop_scan'})
    listing.append((url, li, False))

    # --- [AI fallback not configured] (visible hint + jump to settings) ---
    ai_enabled = ADDON.getSetting('ai_enabled') == 'true'
    ai_key = ADDON.getSetting('ai_api_key') or ''
    if ai_enabled and not ai_key:
        li = xbmcgui.ListItem('[COLOR=FFDD9933]%s[/COLOR]' % L(32182))
        li.setArt({'icon': 'DefaultAddonInfoProvider.png'})
        li.setInfo('video', {'plot': L(32172)})
        url = build_url({'action': 'open_settings'})
        listing.append((url, li, False))

    # --- [Add Directory] ---
    li = xbmcgui.ListItem(L(32020))
    li.setArt({'icon': 'DefaultAddonVideo.png'})
    url = build_url({'action': 'add_directory'})
    listing.append((url, li, False))

    if dirs:
        # --- [Scan All] ---
        pending = sum(
            1 for d in dirs.values()
            for f in d.get('files', {}).values()
            if f.get('status') in ('pending', 'failed'))
        if pending > 0:
            li = xbmcgui.ListItem(L(32021) % pending)
            li.setArt({'icon': 'DefaultAddonVideo.png'})
            url = build_url({'action': 'scan_all'})
            listing.append((url, li, False))

            # --- [Review Pending Matches] ---
            li = xbmcgui.ListItem(L(32161) % pending)
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

            # Build label with color-coded stats
            if fl > 0:
                label2 = '%d文件 | [COLOR=FF44DD44]已匹配:%d[/COLOR] | [COLOR=FFDD4444]失败:%d[/COLOR] | 待处理:%d' % (total, s, fl, pn)
            else:
                label2 = L(32070) % (total, s, fl, pn)

            if pn > 0:
                label = '[COLOR=FFFF4444][待处理:%d][/COLOR] [COLOR=FF44DD44][%d匹配/%d失败][/COLOR] %s' % (pn, s, fl, dirpath)
            elif fl > 0:
                label = '[COLOR=FF44DD44][%d匹配/%d失败][/COLOR] %s' % (s, fl, dirpath)
            else:
                label = '[COLOR=FF44DD44][%d已匹配][/COLOR] %s' % (s, dirpath)
            li = xbmcgui.ListItem(label)
            li.setArt({'icon': 'DefaultFolder.png'})

            # Context menu
            cm = []
            if pn > 0 or fl > 0:
                cm.append((L(32027), run_plugin_url({
                    'action': 'scan_directory',
                    'dirpath': dirpath,
                })))
            cm.append((L(32028), run_plugin_url({
                'action': 'remove_directory',
                'dirpath': dirpath,
            })))
            li.addContextMenuItems(cm)

            url = build_url({'action': 'view_directory', 'dirpath': dirpath})
            listing.append((url, li, True))

        # --- [Refresh All] ---
        li = xbmcgui.ListItem(L(32025))
        li.setArt({'icon': 'DefaultAddonsUpdates.png'})
        url = build_url({'action': 'refresh_all'})
        listing.append((url, li, False))

    # --- [Cache Images] ---
    li = xbmcgui.ListItem(L(32023))
    li.setArt({'icon': 'DefaultAddonsZip.png'})
    url = build_url({'action': 'cache_images'})
    listing.append((url, li, False))

    # --- [Cache Actor Images] (independent of poster/background caching) ---
    li = xbmcgui.ListItem(L(32200))
    li.setArt({'icon': 'DefaultActor.png'})
    url = build_url({'action': 'cache_actor_images'})
    listing.append((url, li, False))

    # --- [Clear All Data] ---
    li = xbmcgui.ListItem(L(32026))
    li.setArt({'icon': 'DefaultAddonInfoProvider.png'})
    url = build_url({'action': 'clear_all_data'})
    listing.append((url, li, False))

    # --- [Settings] ---
    li = xbmcgui.ListItem(L(32024))
    li.setArt({'icon': 'DefaultAddonService.png'})
    url = build_url({'action': 'open_settings'})
    listing.append((url, li, False))

    xbmcplugin.addDirectoryItems(addon_handle, listing, len(listing))
    xbmcplugin.setPluginCategory(addon_handle, ADDON_NAME)
    xbmcplugin.endOfDirectory(addon_handle, cacheToDisc=False)


# ==========================================
# Refresh All Directories
# ==========================================
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

    ensure_data_dir()
    _set_refreshing()
    try:
        total_new = 0
        total_del = 0
        pDialog = xbmcgui.DialogProgressBG()
        pDialog.create(ADDON_NAME, L(32033))
        dir_list = list(dirs.keys())
        for i, dirpath in enumerate(dir_list):
            pDialog.update(
                int((i + 1) * 100 / len(dir_list)),
                L(32034) % os.path.basename(dirpath.rstrip('/')),
                L(32035) % (total_new, total_del))
            nc, dc = refresh_directory_files(dirpath)
            total_new += nc
            total_del += dc
        pDialog.close()
    finally:
        _clear_refreshing()

    xbmc.executebuiltin('Container.Refresh')
    if total_new or total_del:
        msg = L(32060) % (total_new, total_del)
        xbmcgui.Dialog().notification(ADDON_NAME, msg,
                                       xbmcgui.NOTIFICATION_INFO, 5000)
    else:
        xbmcgui.Dialog().notification(ADDON_NAME, L(32061),
                                       xbmcgui.NOTIFICATION_INFO, 3000)


# ==========================================
# Add Directory
# ==========================================
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

    # Set refreshing marker to block other tasks
    _set_refreshing()
    try:
        from file_scanner import find_video_files

        # Run file scanning in background thread so UI stays responsive
        scan_result = [None]
        scan_done = threading.Event()

        def _scan_worker():
            try:
                scan_result[0] = find_video_files(dirpath)
            except Exception as e:
                xbmc.log('[%s] find_video_files error: %s' % (ADDON_NAME, e), xbmc.LOGERROR)
                scan_result[0] = []
            scan_done.set()

        t = threading.Thread(target=_scan_worker, name='FileScanner')
        t.daemon = True
        t.start()

        pDialog = xbmcgui.DialogProgressBG()
        pDialog.create(ADDON_NAME, L(32036))

        while not scan_done.wait(0.3):
            pDialog.update(50, L(32036), dirpath)

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
            'files': files_dict,
        }
        save_history(history)
        xbmc.executebuiltin('Container.Refresh')

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
def cache_images():
    ImageCacher().start()
    xbmc.executebuiltin('Container.Refresh')


def cache_actor_images():
    from actor_cache import ActorImageCacher
    ActorImageCacher().start_actors()
    xbmc.executebuiltin('Container.Refresh')


# ==========================================
# Clear All Data (poster data + DB)
# ==========================================
def clear_all_data():
    dialog = xbmcgui.Dialog()
    if not dialog.yesno(ADDON_NAME,
                        L(32052)):
        return

    # 1. Clear addon own data
    try:
        if xbmcvfs.exists(HISTORY_FILE):
            os.remove(HISTORY_FILE)
            log('Removed scan_history.json')
        if xbmcvfs.exists(SCANNING_FILE):
            os.remove(SCANNING_FILE)
        if xbmcvfs.exists(STOP_FILE):
            os.remove(STOP_FILE)
    except Exception as e:
        log('Failed to clear addon data: %s' % e, xbmc.LOGERROR)

    # Restart-safe library reset service preserves a recoverable copy of current DBs.
    import subprocess
    subprocess.Popen(['systemctl', 'start', 'cebox-library-reset.service'])
    return


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


def _split_name_into_segments(fname):
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


def _manual_search_candidates(query, filename, auto_query, api_key, language):
    """Use scan-quality, multilingual candidates for the extracted-title row."""
    from ai_parser import regex_parse_filename
    from tmdb_api import search_movie_best, search_tv_best, search_multi
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
def manual_match(filepath=None, dirpath=None):
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

    fname = os.path.basename(filepath)
    dialog = xbmcgui.Dialog()

    # Auto-segment the original basename so the user can pick the real title
    # from a candidate list instead of retyping the whole thing every time a
    # match failed. Falls back to the manual input box when the file is so
    # garbled that segmentation produces zero usable chunks.
    seg_labels, seg_query = _split_name_into_segments(fname)
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
            ret = dialog.select(L(32081), [L(32082), L(32083)])
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
            results = _manual_search_candidates(query, fname, auto_query, tmdb_key, language)
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

        # Get episode details
        pDialog = xbmcgui.DialogProgress()
        pDialog.create(ADDON_NAME, L(32086))
        ep_details = get_tv_episode_details(tv_id, season_number, episode_number, tmdb_key, language)
        pDialog.close()

        if not ep_details:
            ep_details = {
                'info': {
                    'title': L(32103) % episode_number,
                    'plot': '',
                    'aired': '',
                    'duration': 0,
                    'director': [],
                    'credits': [],
                },
                'uniqueids': {},
                'ratings': {},
                'available_art': {'poster': [], 'fanart': []},
                'cast': [],
            }

        # Clean up old DB record before writing new match
        _delete_single_file_from_db(filepath)
        # Save to database
        tvdb = TvShowDatabase()
        try:
            tvdb.connect()
            existing_show_id = tvdb.find_tvshow_by_uniqueid('tmdb', str(tv_id))
            show_id, ep_id = tvdb.save_tvshow(
                tv_details, ep_details, season_number, episode_number, filepath)
            if show_id and ep_id:
                status_flag = 'merged' if existing_show_id else 'success'
                tv_year = tv_details.get('info', {}).get('year', '') or \
                    (tv_details.get('info', {}).get('premiered', '')[:4] if tv_details.get('info', {}).get('premiered') else '')
                history = load_history()
                dir_files = history.get('directories', {}).get(dirpath, {}).get('files', {})
                if filepath in dir_files:
                    dir_files[filepath] = {
                        'status': status_flag,
                        'show_id': show_id,
                        'tmdb_title': tv_title or '',
                        'season_number': season_number,
                        'episode_number': episode_number,
                        'is_tvshow': True,
                        'year': tv_year or '',
                    }
                save_history(history)
                dialog.notification(ADDON_NAME, L(32104) % (tv_title, season_number, episode_number))
                # Prefetch show art + cast thumbnails through the proxy
                # chain (manual matches bypass the scan-end prefetch).
                try:
                    ImageCacher().prefetch_art([_art_with_cast(tv_details)])
                except Exception as e:
                    log('Manual match TV prefetch error: %s' % e, xbmc.LOGERROR)
            else:
                dialog.ok(ADDON_NAME, L(32095))
        except Exception as e:
            log('Manual match TV DB error: %s' % e, xbmc.LOGERROR)
            dialog.ok(ADDON_NAME, L(32105) % str(e)[:120])
        finally:
            tvdb.close()

    xbmc.executebuiltin('Container.Refresh')


# ==========================================
# Review pending (low-confidence / failed) matches
# ==========================================
def _collect_pending():
    """Return a list of (dirpath, filepath, info) for pending/failed files."""
    history = load_history()
    items = []
    for dirpath, data in history.get('directories', {}).items():
        for filepath, info in data.get('files', {}).items():
            if info.get('status') in ('pending', 'failed'):
                items.append((dirpath, filepath, info))
    return items


def _count_pending(dirpaths):
    history = load_history()
    n = 0
    for dp in dirpaths:
        files = history.get('directories', {}).get(dp, {}).get('files', {})
        n += sum(1 for f in files.values()
                 if f.get('status') in ('pending', 'failed'))
    return n


def review_pending():
    """Dialog-driven manual matching for all pending/failed files.

    Shows a select dialog listing every low-confidence / failed file from the
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
            line = '[COLOR=FFDD4444]%s[/COLOR]  %s' % (st_label, os.path.basename(filepath))
            if guessed:
                line += '  →  [COLOR=FFFF8800]可能: %s[/COLOR]' % guessed
            labels.append(line)

        heading = L(32162) % len(items)
        ret = dialog.select(heading, labels)
        if ret < 0:
            return
        dirpath, filepath, _ = items[ret]
        manual_match(filepath, dirpath)
        xbmc.executebuiltin('Container.Refresh')


# ==========================================
# View Directory (files inside)
# ==========================================
def view_directory(dirpath):
    if not dirpath:
        return

    history = load_history()
    dir_data = history.get('directories', {}).get(dirpath, {})
    files = dir_data.get('files', {})
    listing = []

    if not files:
        li = xbmcgui.ListItem(L(32054))
        listing.append((build_url({}), li, False))
    else:
        # Sort: failed first, then pending, then success
        def _sort_key(item):
            fp, info = item
            status = info.get('status', 'pending')
            order = {'failed': 0, 'pending': 1, 'success': 2, 'merged': 3}
            return (order.get(status, 9), fp)

        sorted_files = sorted(files.items(), key=_sort_key)

        # Stats header
        total = len(files)
        s = sum(1 for _, info in sorted_files if info.get('status') in ('success', 'merged'))
        fl = sum(1 for _, info in sorted_files if info.get('status') == 'failed')
        pn = sum(1 for _, info in sorted_files if info.get('status') == 'pending')

        # Rescan button
        if pn > 0 or fl > 0:
            li = xbmcgui.ListItem('[B]' + L(32027) + ' (%d成功 / %d失败 / %d待处理)[/B]' % (s, fl, pn))
            li.setArt({'icon': 'DefaultAddonVideo.png'})
            url = build_url({'action': 'scan_directory', 'dirpath': dirpath})
            listing.append((url, li, False))

        for filepath, info in sorted_files:
            status = info.get('status', 'pending')
            fname = os.path.basename(filepath)
            tmdb_title = info.get('tmdb_title', '')
            error = info.get('error', '')

            # Determine media type for label
            if info.get('is_tvshow') or info.get('season_number', 0) > 0:
                type_label = '[COLOR=FFFF4444]剧集[/COLOR] '
            elif status in ('success', 'merged'):
                type_label = '[COLOR=FFFF4444]电影[/COLOR] '
            elif status == 'failed' and 'season_number' in info:
                type_label = '[COLOR=FFFF4444]电影[/COLOR] '
            else:
                type_label = ''

            match_year = info.get('year', '')
            if status in ('success', 'merged'):
                # Build title suffix: "TMDB标题 (年份)" or just "TMDB标题"
                title_suffix = tmdb_title or 'OK'
                if match_year:
                    title_suffix += ' (%s)' % match_year
                label = type_label + '[COLOR=FF44DD44]' + STATUS_LABELS[status] + ' ' + title_suffix + '[/COLOR]  ' + fname
                if info.get('is_tvshow'):
                    sn = info.get('season_number', 0)
                    en = info.get('episode_number', 0)
                    label2 = 'S%02dE%02d' % (sn, en)
                else:
                    label2 = ''
            elif status == 'failed':
                err_msg = error or L(32055)
                label = type_label + '[COLOR=FFDD4444]' + STATUS_LABELS[status] + ' ' + err_msg + '[/COLOR]  ' + fname
                label2 = ''
            else:
                label = STATUS_LABELS[status] + ' ' + fname
                label2 = L(32056)

            li = xbmcgui.ListItem(label)
            li.setLabel2(label2)
            li.setArt({'icon': 'DefaultVideo.png' if status in (
                'success', 'merged') else 'DefaultIconInfo.png'})

            # Context menu: manual match for all files
            cm = [(L(32029), run_plugin_url({
                'action': 'manual_match',
                'filepath': filepath,
                'dirpath': dirpath,
            }))]
            li.addContextMenuItems(cm)

            url = build_url({})  # no-op click
            listing.append((url, li, False))

    xbmcplugin.addDirectoryItems(addon_handle, listing, len(listing))
    xbmcplugin.setPluginCategory(addon_handle, dirpath)
    xbmcplugin.endOfDirectory(addon_handle, cacheToDisc=False)


# ==========================================
# Remove Directory (cascade delete)
# ==========================================
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

    xbmc.executebuiltin('Container.Refresh')
    dialog.notification(ADDON_NAME, L(32040))


def _delete_single_file_from_db(filepath):
    """Delete a single file's movie/episode from Kodi DB. Returns number of deleted records."""
    from kodi_db import KodiDatabase
    db = KodiDatabase()
    try:
        db.connect()
        cur = db.conn.cursor()
        dir_name = os.path.dirname(filepath) + '/'
        file_name = os.path.basename(filepath)

        cur.execute("SELECT idPath FROM path WHERE strPath = ?", (dir_name,))
        row = cur.fetchone()
        if not row:
            return 0
        id_path = row[0]

        cur.execute("SELECT idFile FROM files WHERE strFilename = ? AND idPath = ?",
                    (file_name, id_path))
        row = cur.fetchone()
        if not row:
            return 0
        id_file = row[0]

        deleted = 0
        # Check and delete movie
        cur.execute("SELECT idMovie FROM movie WHERE idFile = ?", (id_file,))
        movie_row = cur.fetchone()
        if movie_row:
            id_movie = movie_row[0]
            cur.execute("DELETE FROM actor_link WHERE media_id=? AND media_type='movie'", (id_movie,))
            cur.execute("DELETE FROM rating WHERE media_id=? AND media_type='movie'", (id_movie,))
            cur.execute("DELETE FROM uniqueid WHERE media_id=? AND media_type='movie'", (id_movie,))
            cur.execute("DELETE FROM art WHERE media_id=? AND media_type='movie'", (id_movie,))
            cur.execute("DELETE FROM videoversion WHERE idFile=?", (id_file,))
            cur.execute("DELETE FROM movie WHERE idMovie=?", (id_movie,))
            deleted += 1

        # Check and delete episode
        cur.execute("SELECT idEpisode, idShow FROM episode WHERE idFile = ?", (id_file,))
        ep_row = cur.fetchone()
        if ep_row:
            id_episode, id_show = ep_row[0], ep_row[1]
            cur.execute("DELETE FROM actor_link WHERE media_id=? AND media_type='episode'", (id_episode,))
            cur.execute("DELETE FROM director_link WHERE media_id=? AND media_type='episode'", (id_episode,))
            cur.execute("DELETE FROM writer_link WHERE media_id=? AND media_type='episode'", (id_episode,))
            cur.execute("DELETE FROM uniqueid WHERE media_id=? AND media_type='episode'", (id_episode,))
            cur.execute("DELETE FROM art WHERE media_id=? AND media_type='episode'", (id_episode,))
            cur.execute("DELETE FROM episode WHERE idEpisode=?", (id_episode,))
            # Clean up show if no episodes remain
            cur.execute("SELECT COUNT(*) FROM episode WHERE idShow = ?", (id_show,))
            if cur.fetchone()[0] == 0:
                cur.execute("DELETE FROM actor_link WHERE media_type='tvshow' AND media_id=?", (id_show,))
                cur.execute("DELETE FROM director_link WHERE media_type='tvshow' AND media_id=?", (id_show,))
                cur.execute("DELETE FROM writer_link WHERE media_type='tvshow' AND media_id=?", (id_show,))
                cur.execute("DELETE FROM genre_link WHERE media_type='tvshow' AND media_id=?", (id_show,))
                cur.execute("DELETE FROM studio_link WHERE media_type='tvshow' AND media_id=?", (id_show,))
                cur.execute("DELETE FROM country_link WHERE media_type='tvshow' AND media_id=?", (id_show,))
                cur.execute("DELETE FROM uniqueid WHERE media_type='tvshow' AND media_id=?", (id_show,))
                cur.execute("DELETE FROM rating WHERE media_type='tvshow' AND media_id=?", (id_show,))
                cur.execute("DELETE FROM art WHERE media_type='tvshow' AND media_id=?", (id_show,))
                cur.execute("DELETE FROM tvshowlinkpath WHERE idShow=?", (id_show,))
                cur.execute("DELETE FROM seasons WHERE idShow=?", (id_show,))
                cur.execute("DELETE FROM tvshow WHERE idShow=?", (id_show,))
            deleted += 1

        # Delete file entry
        cur.execute("DELETE FROM files WHERE idFile=?", (id_file,))
        cur.execute("DELETE FROM bookmark WHERE idFile=?", (id_file,))
        cur.execute("DELETE FROM settings WHERE idFile=?", (id_file,))
        cur.execute("DELETE FROM streamdetails WHERE idFile=?", (id_file,))

        # Delete path if empty
        cur.execute("SELECT COUNT(*) FROM files WHERE idPath = ?", (id_path,))
        if cur.fetchone()[0] == 0:
            cur.execute("DELETE FROM path WHERE idPath = ?", (id_path,))

        db.conn.commit()
        if deleted:
            log('DB cleanup: %s -> %d records removed' % (file_name, deleted))
        return deleted
    except Exception as e:
        log('_delete_single_file_from_db error: %s' % e, xbmc.LOGERROR)
        db.conn.rollback()
        return 0
    finally:
        db.close()


def refresh_directory_files(dirpath):
    """Compare disk vs history: add new files as pending, remove deleted files from DB+history.
    Returns (new_count, deleted_count)."""
    from file_scanner import find_video_files

    history = load_history()
    if dirpath not in history.get('directories', {}):
        return 0, 0

    dir_data = history['directories'][dirpath]
    old_files = dict(dir_data.get('files', {}))

    # Get current files on disk
    if xbmcvfs.exists(dirpath):
        current_set = set(find_video_files(dirpath))
    else:
        current_set = set()

    old_set = set(old_files.keys())

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


def cascade_delete_directory(dirpath):
    """Remove all movie/TV data from Kodi DB for files under dirpath."""
    from kodi_db import KodiDatabase

    db = KodiDatabase()
    try:
        db.connect()
        cur = db.conn.cursor()
        if not dirpath.endswith('/'):
            dirpath_search = dirpath + '/'
        else:
            dirpath_search = dirpath

        # Find all path IDs under this directory (including subdirectories like Season 1/)
        cur.execute("SELECT idPath, strPath FROM path WHERE strPath LIKE ?",
                     (dirpath_search + '%',))
        path_rows = cur.fetchall()

        if not path_rows:
            xbmc.log('[%s] No path entries under: %s' % (ADDON_NAME, dirpath), xbmc.LOGINFO)
            return

        deleted_movies = 0
        deleted_episodes = 0
        affected_shows = set()

        for path_row in path_rows:
            id_path = path_row[0]
            cur.execute("SELECT idFile, strFilename FROM files WHERE idPath = ?", (id_path,))
            file_rows = cur.fetchall()

            for file_row in file_rows:
                id_file = file_row[0]

                # Check movie table
                cur.execute("SELECT idMovie FROM movie WHERE idFile = ?", (id_file,))
                movie_row = cur.fetchone()
                if movie_row:
                    id_movie = movie_row[0]
                    cur.execute("DELETE FROM actor_link WHERE media_id=? AND media_type='movie'",
                                (id_movie,))
                    cur.execute("DELETE FROM rating WHERE media_id=? AND media_type='movie'",
                                (id_movie,))
                    cur.execute("DELETE FROM uniqueid WHERE media_id=? AND media_type='movie'",
                                (id_movie,))
                    cur.execute("DELETE FROM videoversion WHERE idFile=?", (id_file,))
                    cur.execute("DELETE FROM movie WHERE idMovie=?", (id_movie,))
                    deleted_movies += 1

                # Check episode table
                cur.execute("SELECT idEpisode, idShow FROM episode WHERE idFile = ?", (id_file,))
                ep_row = cur.fetchone()
                if ep_row:
                    id_episode, id_show = ep_row[0], ep_row[1]
                    affected_shows.add(id_show)
                    cur.execute("DELETE FROM actor_link WHERE media_id=? AND media_type='episode'",
                                (id_episode,))
                    cur.execute("DELETE FROM director_link WHERE media_id=? AND media_type='episode'",
                                (id_episode,))
                    cur.execute("DELETE FROM writer_link WHERE media_id=? AND media_type='episode'",
                                (id_episode,))
                    cur.execute("DELETE FROM uniqueid WHERE media_id=? AND media_type='episode'",
                                (id_episode,))
                    cur.execute("DELETE FROM art WHERE media_id=? AND media_type='episode'",
                                (id_episode,))
                    cur.execute("DELETE FROM episode WHERE idEpisode=?", (id_episode,))
                    deleted_episodes += 1

                cur.execute("DELETE FROM files WHERE idFile=?", (id_file,))
                cur.execute("DELETE FROM bookmark WHERE idFile=?", (id_file,))
                cur.execute("DELETE FROM settings WHERE idFile=?", (id_file,))
                cur.execute("DELETE FROM streamdetails WHERE idFile=?", (id_file,))

            # Delete path if no files remain
            cur.execute("SELECT COUNT(*) FROM files WHERE idPath = ?", (id_path,))
            if cur.fetchone()[0] == 0:
                cur.execute("DELETE FROM path WHERE idPath = ?", (id_path,))

        # Clean up TV shows that have no remaining episodes
        for show_id in affected_shows:
            cur.execute("SELECT COUNT(*) FROM episode WHERE idShow = ?", (show_id,))
            if cur.fetchone()[0] == 0:
                # Delete all show link data
                cur.execute("DELETE FROM actor_link WHERE media_type='tvshow' AND media_id=?", (show_id,))
                cur.execute("DELETE FROM director_link WHERE media_type='tvshow' AND media_id=?", (show_id,))
                cur.execute("DELETE FROM writer_link WHERE media_type='tvshow' AND media_id=?", (show_id,))
                cur.execute("DELETE FROM genre_link WHERE media_type='tvshow' AND media_id=?", (show_id,))
                cur.execute("DELETE FROM studio_link WHERE media_type='tvshow' AND media_id=?", (show_id,))
                cur.execute("DELETE FROM country_link WHERE media_type='tvshow' AND media_id=?", (show_id,))
                cur.execute("DELETE FROM uniqueid WHERE media_type='tvshow' AND media_id=?", (show_id,))
                cur.execute("DELETE FROM rating WHERE media_type='tvshow' AND media_id=?", (show_id,))
                cur.execute("DELETE FROM art WHERE media_type='tvshow' AND media_id=?", (show_id,))
                cur.execute("DELETE FROM tvshowlinkpath WHERE idShow=?", (show_id,))
                cur.execute("DELETE FROM tvshow WHERE idShow=?", (show_id,))
                # Delete seasons
                cur.execute("DELETE FROM seasons WHERE idShow=?", (show_id,))

        db.conn.commit()
        xbmc.log('[%s] Cascade deleted %d movies, %d episodes from: %s' %
                 (ADDON_NAME, deleted_movies, deleted_episodes, dirpath))

    except Exception as e:
        xbmc.log('[%s] Cascade delete error: %s' % (ADDON_NAME, e), xbmc.LOGERROR)
        db.conn.rollback()
    finally:
        db.close()


# ==========================================
# Scan Single Directory
# ==========================================
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
        if info.get('status') in ('pending', 'failed'):
            if not xbmcvfs.exists(filepath) and not os.path.isdir(filepath):
                info['status'] = 'failed'
                info['error'] = L(32043)
                continue
            work_items.append((filepath, os.path.dirname(filepath)))

    if not work_items:
        save_history(history)
        xbmc.executebuiltin('Container.Refresh')
        xbmcgui.Dialog().ok(ADDON_NAME, L(32044))
        return

    _run_scan(work_items, [dirpath])

    pending = _count_pending([dirpath])
    if pending > 0 and xbmcgui.Dialog().yesno(ADDON_NAME, L(32164) % pending):
        review_pending()


# ==========================================
# Scan All Directories
# ==========================================
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
            if info.get('status') in ('pending', 'failed'):
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
        xbmc.executebuiltin('Container.Refresh')
        xbmcgui.Dialog().ok(ADDON_NAME, L(32044))
        return

    _run_scan(work_items, all_dirs)

    pending = _count_pending(all_dirs)
    if pending > 0 and xbmcgui.Dialog().yesno(ADDON_NAME, L(32164) % pending):
        review_pending()


# ==========================================
# Scan Engine (multi-threaded)
# ==========================================
def _run_scan(work_items, affected_dirs):
    if _is_scanning():
        xbmcgui.Dialog().notification(ADDON_NAME, L(32042), xbmcgui.NOTIFICATION_WARNING, 3000)
        return

    # Create scanning marker (cross-process)
    _set_scanning()
    _clear_stop()

    pDialog = None
    workers = []
    processed = 0
    success = 0
    merged = 0
    failed = 0
    history = None
    art_entries = []
    try:
        ai_enabled = ADDON.getSetting('ai_enabled') == 'true'
        api_key = ADDON.getSetting('ai_api_key') or ''
        # Default to DeepSeek (reachable from China without a VPN).
        # api.openai.com times out on CoreELEC boxes without a proxy - it
        # must only be used when the user explicitly configures it.
        api_base = ADDON.getSetting('ai_api_base') or 'https://api.deepseek.com/v1'
        api_model = ADDON.getSetting('ai_model') or 'deepseek-chat'
        tmdb_key = ADDON.getSetting('tmdb_api_key') or ''
        language = ADDON.getSetting('tmdb_language')
        scan_delay = max(0, int(ADDON.getSetting('scan_delay') or '1'))

        # TMDB key is mandatory - validate before scanning
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
                      api_model, tmdb_key, language, scan_delay, abort_event),
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

            for dirpath in affected_dirs:
                dir_files = history.get('directories', {}).get(
                    dirpath, {}).get('files', {})
                if file_path in dir_files:
                    if status in ('success', 'merged'):
                        entry = {
                            'status': status,
                            'tmdb_title': tmdb_title or '',
                            'year': match_year or '',
                        }
                        if len(result_parts) > 10 and isinstance(result_parts[10], dict):
                            entry['match_evidence'] = result_parts[10]
                        if media_id:
                            entry['movie_id'] = media_id
                        if season_num > 0:
                            entry['season_number'] = season_num
                            entry['episode_number'] = episode_num
                            entry['is_tvshow'] = True
                        dir_files[file_path] = entry
                        success += 1
                        if status == 'merged':
                            merged += 1
                        # NOTE: art rows were already written inside
                        # save_movie()/save_tvshow() in the worker thread;
                        # no extra per-movie DB round trip needed here.
                        if art_urls:
                            art_entries.append(art_urls)
                    else:
                        dir_files[file_path] = {
                            'status': 'failed',
                            'error': error or L(32055),
                            'season_number': season_num,
                            'episode_number': episode_num,
                            'year': match_year or '',
                        }
                        failed += 1
                    break
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
        # Clean up marker files
        _clear_scanning()
        _clear_stop()
        if pDialog is not None:
            pDialog.close()
        if history is not None:
            save_history(history)
        if art_entries:
            try:
                ImageCacher().prefetch_art(art_entries)
            except Exception as e:
                log('Image prefetch trigger error: %s' % e, xbmc.LOGERROR)
        for t in workers:
            t.join(timeout=2)
        xbmc.executebuiltin('Container.Refresh')

    xbmcgui.Dialog().notification(ADDON_NAME,
                        L(32065) % (
                            success, merged, failed),
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
                api_model, tmdb_key, language, delay, abort_event):
    from ai_parser import parse_filename
    from tmdb_api import (search_movie_best, get_movie_details,
                          search_tv_best, get_tv_details, get_tv_episode_details)
    from kodi_db import KodiDatabase
    from tvshow_db import TvShowDatabase
    from media_identity import build_evidence
    from movie_matcher import resolve_movie

    while not abort_event.is_set():
        try:
            filepath, dirpath = work_queue.get(timeout=0.5)
        except queue.Empty:
            continue

        fname = os.path.basename(filepath)

        try:
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
            regex_result = regex_parse_filename(fname, dirpath, 'tvshow')
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
                                        api_model, mode=regex_result.get('type', 'auto') if regex_result else 'auto',
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
                time.sleep(delay)
                continue

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
                    time.sleep(delay)
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
                    time.sleep(delay)
                    continue

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
                    xbmc.log('[%s] Worker DB error: %s' % (ADDON_NAME, e), xbmc.LOGERROR)
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

            # ================================================
            # TV Show path
            # ================================================
            if season_number <= 0 or episode_number <= 0:
                result_queue.put((
                    'failed', filepath, fname, None, None, None,
                    season_number, episode_number,
                    L(32113) % (season_number, episode_number), ''))
                work_queue.task_done()
                time.sleep(delay)
                continue

            # Search TV - primary language first, English merged when weak
            tv_results = search_tv_best(title, year, english_title,
                                        tmdb_key, language)

            if not tv_results:
                result_queue.put((
                    'failed', filepath, fname, None, None, None,
                    season_number, episode_number,
                    L(32114) % (title, year), ''))
                work_queue.task_done()
                time.sleep(delay)
                continue

            # --- confidence check: take the best candidate directly ---
            best_tv, score = _select_best_match(tv_results, title, year, MATCH_THRESHOLD, english_title)

            # --- AI second opinion for weak TV matches (same as movies) ---
            if (best_tv is None or score < MATCH_THRESHOLD) \
                    and ai_enabled and not used_ai:
                ai2 = parse_filename(fname, dirpath, api_key, api_base,
                                     api_model, mode='tvshow',
                                     skip_regex=True)
                if ai2 and ai2.get('type', 'movie') == 'tvshow':
                    t2 = ai2.get('title', '')
                    y2 = ai2.get('year')
                    e2 = ai2.get('english_title', '')
                    if t2 and (t2, y2, e2) != (title, year, english_title):
                        tv_results2 = search_tv_best(t2, y2, e2,
                                                     tmdb_key, language)
                        best2, score2 = _select_best_match(
                            tv_results2, t2, y2, MATCH_THRESHOLD, e2)
                        if best2 is not None and \
                                (best_tv is None or score2 > score):
                            log('[Worker %s] AI TV rescue: "%s" (%s) '
                                'score %.2f -> %.2f ("%s")'
                                % (threading.current_thread().name,
                                   title, year, score, score2, t2))
                            ai = ai2
                            title, year, english_title = t2, y2, e2
                            best_tv, score = best2, score2

            if best_tv is None or score < MATCH_FLOOR:
                guessed = ''
                log('[Worker %s] %s: "%s" (%s)'
                    % (threading.current_thread().name,
                       'No TV match' if best_tv is None
                       else 'Very low-confidence TV match (%.2f) NOT applied' % score,
                       title, year))
                result_queue.put((
                    'pending', filepath, fname, None, None, None,
                    season_number, episode_number,
                    L(32160) % (score, (best_tv or {}).get('title') or (best_tv or {}).get('name') or title), str(year or '')))
                work_queue.task_done()
                time.sleep(delay)
                continue
            if score < MATCH_THRESHOLD:
                log('[Worker %s] Low-confidence TV match (%.2f) applied: "%s" -> %s'
                     % (threading.current_thread().name, score, title,
                        best_tv.get('name', '') or best_tv.get('title', '')))
            tv_id = best_tv.get('id')
            tv_name = best_tv.get('name', '')

            # Get show details
            tv_details = get_tv_details(tv_id, tmdb_key, language)
            if not tv_details:
                result_queue.put((
                    'failed', filepath, fname, None, None, None,
                    season_number, episode_number,
                    L(32115) % tv_name, ''))
                work_queue.task_done()
                time.sleep(delay)
                continue

            # Get episode details
            ep_details = get_tv_episode_details(
                tv_id, season_number, episode_number, tmdb_key, language)
            if not ep_details:
                # Fallback: create minimal episode info so c00 isn't empty
                ep_details = {
                    'info': {
                        'title': L(32103) % episode_number,
                        'plot': '',
                        'aired': '',
                        'duration': 0,
                        'director': [],
                        'credits': [],
                    },
                    'uniqueids': {},
                    'ratings': {},
                    'available_art': {'poster': [], 'fanart': []},
                    'cast': [],
                }
                log('[Worker %s] Episode S%02dE%02d 404 from TMDB, using fallback'
                    % (threading.current_thread().name, season_number, episode_number))

            # Save to database
            tvdb = TvShowDatabase()
            try:
                tvdb.connect()
                existing_show_id = tvdb.find_tvshow_by_uniqueid('tmdb', str(tv_id))
                show_id, ep_id = tvdb.save_tvshow(
                    tv_details, ep_details, season_number,
                    episode_number, filepath)
                if show_id and ep_id:
                    status_flag = 'merged' if existing_show_id else 'success'
                    media_id = show_id
                else:
                    status_flag = 'failed'
                    media_id = None
            except Exception as e:
                xbmc.log('[%s] Worker TV DB error: %s' % (ADDON_NAME, e), xbmc.LOGERROR)
                show_id = None
                status_flag = 'error'
            finally:
                tvdb.close()

            tv_year = tv_details.get('info', {}).get('year', '') or \
                (tv_details.get('info', {}).get('premiered', '')[:4] if tv_details.get('info', {}).get('premiered') else '')
            if show_id:
                tv_art = _art_with_cast(tv_details)
                # Also prefetch this episode's cast / guest-star thumbs -
                # tvshow_db writes them into actor_link, so they must be
                # cached through the proxy chain as well.
                ep_cast = (ep_details or {}).get('cast', []) or []
                if ep_cast:
                    extra = [{'url': c['thumbnail']}
                             for c in ep_cast[:20] if c.get('thumbnail')]
                    tv_art['cast'] = (tv_art.get('cast') or []) + extra
                result_queue.put((
                    status_flag, filepath, fname, show_id, tv_name,
                    tv_art,
                    season_number, episode_number, None, tv_year))
            else:
                result_queue.put((
                    'failed', filepath, fname, None, None, None,
                    season_number, episode_number,
                    L(32095), ''))

        except Exception as e:
            xbmc.log('[%s] Worker exception: %s' % (ADDON_NAME, e), xbmc.LOGERROR)
            result_queue.put((
                'error', filepath, fname, None, None, None, 0, 0, str(e), ''))

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
    router()
