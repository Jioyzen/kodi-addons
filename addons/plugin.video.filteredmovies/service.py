# -*- coding: utf-8 -*-
import os
import json
import re
import time
import traceback
import threading

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs

from lib.common import get_skin_name, notification, log

ADDON_ID = 'plugin.video.filteredmovies'
ADDON = xbmcaddon.Addon(id=ADDON_ID)
ADDON_PATH = xbmcvfs.translatePath(ADDON.getAddonInfo('path'))
ADDON_DATA_PATH = xbmcvfs.translatePath(ADDON.getAddonInfo('profile'))
if not os.path.exists(ADDON_DATA_PATH):
    os.makedirs(ADDON_DATA_PATH)

MAX_PLAYLIST_ITEMS_BEFORE = 50
MAX_PLAYLIST_ITEMS_AFTER = 50

def warmup_xml_cache():
    try:
        # 预读取 XML 文件以利用文件系统缓存
        xml_dir = os.path.join(ADDON_PATH, 'resources', 'skins', 'Default', '1080i')
        target_files = ['Custom_5111_MovieFilter.xml', 'Custom_5111_MovieFilter_Horizon.xml']
        
        for f_name in target_files:
            f_path = os.path.join(xml_dir, f_name)
            if os.path.exists(f_path):
                with open(f_path, 'rb') as f:
                    _ = f.read()
                log(f"Warmed up cache for {f_name}")
    except Exception as e:
        log(f"-----Error warming up XML cache: {e}")

def jsonrpc_call(method, params=None, request_id=None):
    query = {
        "jsonrpc": "2.0",
        "method": method,
        "id": request_id or method,
    }
    if params is not None:
        query["params"] = params

    try:
        response = json.loads(xbmc.executeJSONRPC(json.dumps(query)))
    except Exception as e:
        log(f"JSON-RPC call failed for {method}: {e}", xbmc.LOGERROR)
        return None

    if isinstance(response, dict) and "error" in response:
        log(f"JSON-RPC error for {method}: {response.get('error')}", xbmc.LOGWARNING)
        return None
    return response.get("result") if isinstance(response, dict) else None

def normalize_media_path(path):
    if not path:
        return ""
    normalized = str(path).split('?', 1)[0].split('#', 1)[0]
    return normalized.replace('\\', '/').rstrip('/')

def natural_sort_key(value):
    """自然排序键：让 2 排在 10 前面。"""
    text = str(value or "")
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r'(\d+)', text)]

def get_parent_media_path(path):
    normalized = normalize_media_path(path)
    if not normalized:
        return None
    last_sep = normalized.rfind('/')
    if last_sep <= 0:
        return None
    return normalized[:last_sep]

def get_active_video_playlist_state():
    players = jsonrpc_call("Player.GetActivePlayers") or []
    video_player = next((p for p in players if p.get("type") == "video"), None)
    if not video_player:
        return None

    player_id = video_player.get("playerid")
    properties = jsonrpc_call(
        "Player.GetProperties",
        {
            "playerid": player_id,
            "properties": ["playlistid", "position"],
        },
    ) or {}
    item = jsonrpc_call(
        "Player.GetItem",
        {
            "playerid": player_id,
            "properties": ["file", "tvshowid", "season", "episode", "showtitle", "title"],
        },
    ) or {}

    current_item = item.get("item") or {}
    playlistid = int(properties.get("playlistid") or 1)
    position = int(properties.get("position") or 0)
    # playlistid/position 为负数说明 Kodi 未将当前项加入播放列表，跳过补全
    if playlistid < 0 or position < 0:
        return None
    return {
        "playerid": player_id,
        "playlistid": playlistid,
        "position": position,
        "file": current_item.get("file") or "",
        "tvshowid": current_item.get("tvshowid"),
        "season": current_item.get("season"),
        "episode": current_item.get("episode"),
        "showtitle": current_item.get("showtitle") or "",
        "title": current_item.get("title") or "",
    }

def get_playlist_files(playlist_id):
    result = jsonrpc_call(
        "Playlist.GetItems",
        {
            "playlistid": playlist_id,
            "properties": ["file"],
        },
    ) or {}
    items = result.get("items") or []
    return [item.get("file") for item in items if item.get("file")]

def get_season_playlist_files(tvshow_id, season):
    if tvshow_id in (None, -1) or season in (None, -1):
        return []

    result = jsonrpc_call(
        "VideoLibrary.GetEpisodes",
        {
            "tvshowid": int(tvshow_id),
            "season": int(season),
            "properties": ["file", "episode", "season", "title"],
            "sort": {"method": "episode", "order": "ascending"},
        },
    ) or {}
    episodes = result.get("episodes") or []
    return [item.get("file") for item in episodes if item.get("file")]

def get_directory_playlist_files(current_file):
    parent_dir = get_parent_media_path(current_file)
    if not parent_dir:
        return []

    result = jsonrpc_call(
        "Files.GetDirectory",
        {
            "directory": parent_dir,
            "media": "video",
            "properties": ["file", "title"],
        },
    ) or {}
    files = result.get("files") or []

    # 文件夹模式按标题自然排序，再据此判断当前项前后应补哪些内容。
    playlist_items = []
    for item in files:
        file_path = item.get("file")
        if not file_path:
            continue
        file_type = item.get("filetype")
        if file_type and file_type != "file":
            continue
        sort_title = item.get("title") or item.get("label") or os.path.basename(normalize_media_path(file_path))
        playlist_items.append((sort_title, file_path))

    playlist_items.sort(key=lambda value: (natural_sort_key(value[0]), normalize_media_path(value[1]).casefold()))
    return [file_path for _, file_path in playlist_items]

def insert_playlist_item(playlist_id, position, file_path):
    result = jsonrpc_call(
        "Playlist.Insert",
        {
            "playlistid": playlist_id,
            "position": position,
            "item": {"file": file_path},
        },
    )
    return result == "OK"

def _sync_season_playlist(playlist_id, current_position, current_file_norm, target_norms, target_files):
    """刮削剧集：将播放列表同步为正确顺序（补全缺失 + 修正乱序，一次完成）。"""
    target_norm_to_idx = {n: i for i, n in enumerate(target_norms)}
    current_target_idx = target_norm_to_idx.get(current_file_norm, -1)
    if current_target_idx < 0:
        return

    # 确定应出现在播放列表中的集数（当前项前后各50集窗口）
    desired_before = list(zip(
        target_files[:current_target_idx], target_norms[:current_target_idx]
    ))[-MAX_PLAYLIST_ITEMS_BEFORE:]
    desired_after = list(zip(
        target_files[current_target_idx + 1:], target_norms[current_target_idx + 1:]
    ))[:MAX_PLAYLIST_ITEMS_AFTER]

    # 检查播放列表中当前项前后的季集数是否已是期望的内容和顺序
    fresh_norms = [normalize_media_path(p) for p in get_playlist_files(playlist_id)]
    season_before = [n for n in fresh_norms[:current_position] if n in target_norm_to_idx]
    season_after  = [n for n in fresh_norms[current_position + 1:] if n in target_norm_to_idx]
    if season_before == [n for _, n in desired_before] and season_after == [n for _, n in desired_after]:
        return

    # 删除播放列表中所有本季集数（不含当前项），从高位到低位避免偏移
    season_positions = [i for i, n in enumerate(fresh_norms) if i != current_position and n in target_norm_to_idx]
    for pos in sorted(season_positions, reverse=True):
        jsonrpc_call("Playlist.Remove", {"playlistid": playlist_id, "position": pos})

    # 删除后当前项位置前移（删掉了多少个在它之前的项）
    new_pos = current_position - sum(1 for p in season_positions if p < current_position)

    # 将前置集数插到当前项前面，后置集数插到当前项后面
    for i, (path, _) in enumerate(desired_before):
        insert_playlist_item(playlist_id, new_pos + i, path)
    for i, (path, _) in enumerate(desired_after):
        insert_playlist_item(playlist_id, new_pos + len(desired_before) + 1 + i, path)

    log(f"Synced season playlist: before={len(desired_before)}, after={len(desired_after)}, playlistid={playlist_id}")

def autofill_playlist_for_current_video():
    try:
        _autofill_playlist_for_current_video()
    except Exception as e:
        log(f"Autofill playlist error: {e}\n{traceback.format_exc()}", xbmc.LOGERROR)

def _autofill_playlist_for_current_video():
    if ADDON.getSetting('autofill_playlist_on_play') == 'false':
        return

    state = get_active_video_playlist_state()
    if not state:
        return

    current_file = state.get("file") or ""
    current_file_norm = normalize_media_path(current_file)
    if not current_file_norm:
        return

    tvshow_id = state.get("tvshowid")
    season = state.get("season")
    is_scraped_tvshow = tvshow_id not in (None, -1) and season not in (None, -1)

    if is_scraped_tvshow:
        target_files = get_season_playlist_files(tvshow_id, season)
    else:
        if current_file_norm.startswith("plugin://") or current_file_norm.startswith("pvr://"):
            return
        target_files = get_directory_playlist_files(current_file)

    target_files = [path for path in target_files if path]
    if len(target_files) <= 1:
        return

    target_norms = [normalize_media_path(path) for path in target_files]
    if current_file_norm not in target_norms:
        return

    playlist_id = state.get("playlistid", 1)
    current_position = state.get("position", 0)

    if is_scraped_tvshow:
        # 刮削剧集：补全缺失并修正乱序，一次完成
        _sync_season_playlist(playlist_id, current_position, current_file_norm, target_norms, target_files)
    else:
        # 文件夹模式：只补当前项附近的缺失项
        current_playlist_norms = set(
            normalize_media_path(p) for p in get_playlist_files(playlist_id) if p
        )
        if all(n in current_playlist_norms for n in target_norms):
            return

        current_target_index = target_norms.index(current_file_norm)
        missing_before = [
            path for path, norm in zip(target_files[:current_target_index], target_norms[:current_target_index])
            if norm not in current_playlist_norms
        ][-MAX_PLAYLIST_ITEMS_BEFORE:]
        missing_after = [
            path for path, norm in zip(target_files[current_target_index + 1:], target_norms[current_target_index + 1:])
            if norm not in current_playlist_norms
        ][:MAX_PLAYLIST_ITEMS_AFTER]

        inserted_before = 0
        for path in missing_before:
            if insert_playlist_item(playlist_id, current_position + inserted_before, path):
                inserted_before += 1
        insert_pos = current_position + inserted_before + 1
        inserted_after = 0
        for path in missing_after:
            if insert_playlist_item(playlist_id, insert_pos, path):
                inserted_after += 1
                insert_pos += 1

        if inserted_before or inserted_after:
            log(f"Autofilled directory playlist: before={inserted_before}, after={inserted_after}, playlistid={playlist_id}")

class TransparentOverlay(xbmcgui.WindowXML):
    def __init__(self, *args, **kwargs):
        self.should_close = False
        self.close_action_id = None
        xbmcgui.WindowXML.__init__(self, *args, **kwargs)

    def onInit(self):
        # 窗口初始化后的逻辑
        pass

    def onAction(self, action):
        # 收到任何按键操作，关闭自己
        self.close_action_id = action.getId()
        log(f"TransparentOverlay received action {self.close_action_id}, scheduling close...")
        self.should_close = True
        return


class PlayerMonitor(xbmc.Player):
    def __init__(self):
        xbmc.Player.__init__(self)
        self.transparent_overlay = None
        self.last_overlay_close_time = 0

    def onAVStarted(self):
        # 视频开始播放（包括切集）时触发
        # 稍微延迟一下，确保元数据已加载
        xbmc.sleep(1000)

        self.load_iso_subtitles()
        autofill_playlist_for_current_video()

    def get_video_zoom(self):
        try:
            json_query = {
                "jsonrpc": "2.0",
                "method": "Player.GetViewMode",
                "id": "Player.GetViewMode"
            }
            json_response = xbmc.executeJSONRPC(json.dumps(json_query))
            response = json.loads(json_response)
            
            if 'result' in response and 'zoom' in response['result']:
                return float(response['result']['zoom'])
        except Exception as e:
            pass
        return 1.0

    def get_screen_aspect_ratio(self):
        try:
            # 获取显示设备（屏幕）的实际宽高比，而非视频内容的
            width = float(xbmc.getInfoLabel('System.ScreenWidth'))
            height = float(xbmc.getInfoLabel('System.ScreenHeight'))
            if height > 0:
                return width / height
        except Exception as e:
            pass
        return 0.0

    def close_transparent_overlay(self):
        if self.transparent_overlay:
            try:
                self.transparent_overlay.close()
            except Exception as e:
                log(f"Error closing transparent overlay: {e}", xbmc.LOGERROR)
            finally:
                self.transparent_overlay = None
                log("Transparent filter overlay closed/referenced cleaned", xbmc.LOGDEBUG)

    def check_overlay_visibility(self):
        if self.transparent_overlay and self.transparent_overlay.should_close:
            action_id = self.transparent_overlay.close_action_id
            self.close_transparent_overlay()
            self.last_overlay_close_time = time.time()
            
            current_window_id = xbmcgui.getCurrentWindowId()
            if current_window_id == 10000:
                xbmc.executebuiltin("ActivateWindow(fullscreenvideo)")
            if action_id:
                # 智能映射：将Action ID映射为实际的 **播放控制命令**
                # 问题关键：
                #   当透明遮罩层(Dialog)处于活动状态时，"Right"键产生的Action ID是1/2 (Left/Right)
                #   而视频播放器(FullscreenVideo)通常只响应20/21 (StepForward/Back)用于快进退
                #   如果我们转发 "Action(Right)"，全屏播放器会忽略它，因为它期待的是 "StepForward"。
                # 已知问题，不支持自定以按键映射，只能根据常见的几个按键进行转发
                action_map = {
                    1: "stepback",       # Id 1 (Left) -> StepBack
                    2: "stepforward",    # Id 2 (Right) -> StepForward
                    3: "bigstepforward", # Id 3 (Up) -> BigStepForward (or ChapterNext)
                    4: "bigstepback",    # Id 4 (Down) -> BigStepBack
                    5: "pageup", 
                    6: "pagedown",
                    7: "osd",            # Id 7 (Select) -> Show OSD
                    8: "highlight", 
                    9: "parentdir", 
                    10: "back",          # Id 10 (PrevMenu) -> Back
                    11: "info", 
                    # 12: "pause", 
                    13: "stop", 
                    14: "skipnext", 
                    15: "skipprevious",
                    18: "fullscreen", 
                    19: "aspectratio", 
                    20: "stepforward", 
                    21: "stepback",
                    22: "bigstepforward", 
                    23: "bigstepback", 
                    24: "osd", 
                    25: "showsubtitles",
                    26: "nextsubtitle", 
                    36: "fullscreen", 
                    76: "smallstepback",
                    77: "fastforward", 
                    78: "rewind", 
                    79: "play", 
                    88: "volumeup", 
                    89: "volumedown", 
                    91: "mute", 
                    92: "back",
                    # 鼠标支持 (Mouse Support)
                    100: "leftclick",    # Mouse Left Click
                    101: "rightclick",   # Mouse Right Click
                    102: "middleclick",  # Mouse Middle Click
                    103: "doubleclick",  # Mouse Double Click
                    104: "wheelup",      # Mouse Wheel Up
                    105: "wheeldown",    # Mouse Wheel Down
                    106: "mousedrag",    # Mouse Drag
                    107: "mousemove",    # Mouse Move
                }
                action_str = action_map.get(action_id)
                
                if action_str:
                    log(f"Smart forwarding action '{action_str}' (from raw ID:{action_id}) to application layer", xbmc.LOGDEBUG)
                    xbmc.executebuiltin(f"Action({action_str})")
                else:
                    log(f"Unmapped action ID: {action_id}", xbmc.LOGWARNING)
            return
        
        if time.time() - self.last_overlay_close_time < 1.5:
            # 刚关闭过遮罩，短时间内不再打开（留出时间给后续操作）
            return

        # 仅在播放视频时处理
        if not self.isPlayingVideo():
            if self.transparent_overlay:
                self.close_transparent_overlay()
            return

        # 仅在Windows平台启用遮罩修复功能 (Only enable overlay fix on Windows)
        if not xbmc.getCondVisibility("System.Platform.Windows"):
            if self.transparent_overlay:
                self.close_transparent_overlay()
            return

        should_show = False
        ar = self.get_screen_aspect_ratio()
        is_wide = ar > 1.8
        if is_wide:
            # 宽屏显示器
            zoom = self.get_video_zoom()
            is_zoomed = zoom > 1.01
            if is_zoomed:
                # 画面经过缩放
                is_fullscreen_video = xbmc.getCondVisibility("Window.IsActive(fullscreenvideo)")
                if is_fullscreen_video:
                    # 正在全屏播放
                    has_osd = xbmc.getCondVisibility("Window.IsVisible(videoosd)")
                    has_seekbar = xbmc.getCondVisibility("Window.IsVisible(seekbar)") or xbmc.getCondVisibility("Window.IsVisible(playercontrols)")
                    has_dialog = xbmc.getCondVisibility("System.HasActiveModalDialog")
                    has_other_overlay = has_osd or has_seekbar or has_dialog
                    if not has_other_overlay:
                        # 没有OSD菜单 (Window.IsActive(videoosd))
                        # 没有进度条 (Window.IsActive(seekbar)) 或播放控制条
                        # 没有其他模态对话框 (System.HasActiveModalDialog)
                        should_show = True
                else:
                    if self.transparent_overlay:
                        should_show = True
        if self.transparent_overlay:
            if not should_show:
                self.close_transparent_overlay()
        else:
            if should_show:
                self.show_transparent_overlay()

    def onPlayBackStopped(self):
        if self.transparent_overlay:
            self.close_transparent_overlay()

    def onPlayBackEnded(self):
        if self.transparent_overlay:
            self.close_transparent_overlay()

    def show_transparent_overlay(self):
        # 如果已经存在实例，说明已经显示了，无需重复创建
        if self.transparent_overlay:
            return
            
        try:
            # 创建透明覆盖层
            # 这里的路径ADDON_PATH已经是绝对路径了
            self.transparent_overlay = TransparentOverlay('script-transparent-overlay.xml', ADDON_PATH, 'Default', '1080i')
            self.transparent_overlay.show()
        except Exception as e:
            log(f"Error showing transparent overlay: {e}", xbmc.LOGERROR)

    def load_iso_subtitles(self):
        log("Checking for ISO subtitles...")
        try:
            if not self.isPlayingVideo():
                log("Not playing video, skipping ISO subtitle check.")
                return
            
            # Try to get the file path from Player.GetItem
            json_query = {
                "jsonrpc": "2.0",
                "method": "Player.GetItem",
                "params": {
                    "properties": ["file"],
                    "playerid": 1
                },
                "id": "Player.GetItem"
            }
            json_response = xbmc.executeJSONRPC(json.dumps(json_query))
            response = json.loads(json_response)
            
            playing_file = None
            if 'result' in response and 'item' in response['result']:
                item_file = response['result']['item'].get('file')
                if item_file:
                    log(f"Player.GetItem returned file: {item_file}")
                    playing_file = item_file

            if not playing_file:
                 log("No playing file found via JSONRPC, skipping ISO subtitle check.")
                 return

            # Strip query parameters if any
            if '?' in playing_file:
                playing_file = playing_file.split('?')[0]
            
            # Check if it's an ISO file
            if not playing_file.lower().endswith('.iso'):
                # log(f"Not an ISO file {playing_file}, skipping ISO subtitle check.")
                return
                
            log(f"ISO file detected: {playing_file}, checking for external subtitles...")
            
            # Get directory and base name
            # Normalize path separators
            playing_file = playing_file.replace('\\', '/')
            last_sep_idx = playing_file.rfind('/')
            
            if last_sep_idx == -1:
                return
                
            dir_path = playing_file[:last_sep_idx + 1]
            file_name = playing_file[last_sep_idx + 1:]
            base_name = file_name[:-4] # remove .iso
            
            # Common subtitle extensions
            sub_exts = ['.srt', '.ass', '.ssa', '.sub', '.smi', '.vtt']
            
            # List directory contents
            dirs, files = xbmcvfs.listdir(dir_path)
            
            # Find matching subtitles
            subtitles_to_load = []
            for f in files:
                f_lower = f.lower()
                base_lower = base_name.lower()
                if any(f_lower.endswith(ext) for ext in sub_exts):
                    if f_lower.startswith(base_lower):
                        remainder = f_lower[len(base_lower):]
                        # Check strict matching (either exact match + ext, or followed by separator)
                        # e.g. movie.srt, movie.en.srt, movie_en.srt
                        if remainder in sub_exts or remainder.startswith('.') or remainder.startswith('-') or remainder.startswith('_'):
                            sub_path = dir_path + f
                            subtitles_to_load.append(sub_path)
            
            # Sort subtitles to have deterministic loading order
            subtitles_to_load.sort()
            
            if subtitles_to_load:
                log(f"Found {len(subtitles_to_load)} external subtitles for ISO: {subtitles_to_load}")
                for sub in subtitles_to_load:
                    self.setSubtitles(sub)
                    log(f"Loaded subtitle: {sub}")
        except Exception as e:
            log(f"Error loading ISO subtitles: {e}")



def init_skin_properties():
    skin_name = get_skin_name()
    # 1. Set current skin ID as a window property for use in context menu visibility conditions
    xbmc.executebuiltin('SetProperty(MFG.SkinID,' + xbmc.getSkinDir() + ',home)')
    # 2. Follow the active skin theme for selection accents.
    # CEBOX-NO defines button_focus as its default red; fixed blue breaks theme consistency.
    xbmc.executebuiltin('SetProperty(MFG.FocusColor,button_focus,home)')
    xbmc.executebuiltin('SetProperty(MFG.ProgressBarColor,button_focus,home)')
    if skin_name == "estuary":
            xbmc.executebuiltin('ClearProperty(MFG.CenterWindow,home)')
    else:
            xbmc.executebuiltin('SetProperty(MFG.CenterWindow,true,home)')
    # 方角模式
    xbmc.executebuiltin('ClearProperty(MFG.UseRounded,home)')
    
    # 3. Initialize Return Targets for Navigation Memory
    # UP Targets (Default Down)
    xbmc.executebuiltin('SetProperty(Return_1013,6001,home)')
    xbmc.executebuiltin('SetProperty(Return_6004,2005,home)')
    xbmc.executebuiltin('SetProperty(Return_6006,2007,home)')
    xbmc.executebuiltin('SetProperty(Return_4003,5003,home)')
    xbmc.executebuiltin('SetProperty(Return_4004,5005,home)')
    xbmc.executebuiltin('SetProperty(Return_3004,2005,home)')
    xbmc.executebuiltin('SetProperty(Return_Key6,6001,home)')
    xbmc.executebuiltin('SetProperty(Return_KeyClr,4001,home)')
    
    # DOWN Targets (Default Up)
    xbmc.executebuiltin('SetProperty(Return_4005,3006,home)')
    xbmc.executebuiltin('SetProperty(Return_4007,3008,home)')
    xbmc.executebuiltin('SetProperty(Return_4010,3012,home)')

if __name__ == '__main__':
    log("Service started")    
    # Start prefetch thread
    threading.Thread(target=warmup_xml_cache).start()

    # 预热 AVAIL 缓存（异步），避免首次打开窗口时卡顿 125ms
    def _warmup_avail():
        try:
            from lib.window_handler import _update_filter_availability
            _update_filter_availability(force=True)
            log("AVAIL cache pre-warmed")
        except Exception as e:
            log(f"AVAIL pre-warm failed: {e}")
    threading.Thread(target=_warmup_avail).start()

    init_skin_properties()
    # 启动时异步准备 T9 搜索索引（仅首次运行，后续检查秒过）
    from lib import t9_helper
    t9_helper.helper.ensure_search_index_ready_async(show_progress=False)
    monitor = xbmc.Monitor()
    player = PlayerMonitor()

    while not monitor.abortRequested():
        if monitor.waitForAbort(1):
            break
