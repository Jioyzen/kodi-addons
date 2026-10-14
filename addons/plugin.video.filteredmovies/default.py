# -*- coding: utf-8 -*-
import os
import sys
import urllib.parse
import json
import time
import threading
import base64

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs
import xbmcplugin

from lib.common import get_skin_name, notification, log
from lib import video_library as library
from lib import t9_helper

ADDON_ID = 'plugin.video.filteredmovies'
ADDON = xbmcaddon.Addon(id=ADDON_ID)
ADDON_PATH = xbmcvfs.translatePath(ADDON.getAddonInfo('path'))
ADDON_DATA_PATH = xbmcvfs.translatePath(ADDON.getAddonInfo('profile'))
if not os.path.exists(ADDON_DATA_PATH):
    os.makedirs(ADDON_DATA_PATH)
try:
    HANDLE = int(sys.argv[1])
except (IndexError, ValueError):
    HANDLE = -1


def open_playing_tvshow():
    # xbmc.executebuiltin("Dialog.Close(all,true)")
    # xbmc.sleep(100)
    
    try:
        if not xbmc.Player().isPlayingVideo():
            return
            
        json_query = {
            "jsonrpc": "2.0",
            "method": "Player.GetItem",
            "params": {
                "properties": ["tvshowid", "season"],
                "playerid": 1
            },
            "id": 1
        }
        json_response = xbmc.executeJSONRPC(json.dumps(json_query))
        response = json.loads(json_response)
        
        if 'result' in response and 'item' in response['result']:
            item = response['result']['item']
            tvshow_id = item.get('tvshowid')
            season = item.get('season', -1)
            item_type = item.get('type')
            
            if item_type == 'episode' and tvshow_id and tvshow_id != -1:
                if season != -1:
                    log(f"Opening TVShow ID: {tvshow_id} Season: {season}")
                    path = f"videodb://tvshows/titles/{tvshow_id}/{season}/"
                else:
                    log(f"Opening TVShow ID: {tvshow_id}")
                    path = f"videodb://tvshows/titles/{tvshow_id}/"
                xbmc.executebuiltin(f"ActivateWindow(Videos,{path},return)")
            else:
                log("Current playing item is not a TV show, opening Playlist")
                xbmc.executebuiltin("ActivateWindow(VideoPlaylist)")
    except Exception as e:
        log(f"Error opening playing tvshow: {e}")

def force_prev():
    try:
        player = xbmc.Player()
        if not player.isPlaying():
            return
        
        # 统一使用播放列表逻辑，不再单独处理剧集
        if player.isPlayingVideo():
            playlist_id = xbmc.PLAYLIST_VIDEO
        else:
            playlist_id = xbmc.PLAYLIST_MUSIC
            
        pl = xbmc.PlayList(playlist_id)
        pos = pl.getposition()
        
        if pos > 0:
            log(f"ForcePrev: Jumping to playlist pos {pos-1}")
            
            # 获取 active player id
            active_player_id = 1
            try:
                p_res = json.loads(xbmc.executeJSONRPC('{"jsonrpc":"2.0","method":"Player.GetActivePlayers","id":1}'))
                if 'result' in p_res and len(p_res['result']) > 0:
                    active_player_id = p_res['result'][0]['playerid']
            except: pass

            json_query_goto = {
                "jsonrpc": "2.0",
                "method": "Player.GoTo",
                "params": {
                    "playerid": active_player_id,
                    "to": pos - 1
                },
                "id": 1
            }
            xbmc.executeJSONRPC(json.dumps(json_query_goto))
        else:
            log("ForcePrev: At start of playlist, restarting")
            notification(ADDON.getLocalizedString(32165))
            player.seekTime(0)

    except Exception as e:
        log(f"Error in force_prev: {e}")
        notification(ADDON.getLocalizedString(32166), sound=True)








def launch_t9():
    from lib.launch_guard import single_window
    with single_window(ADDON_DATA_PATH) as acquired:
        if not acquired:
            log('WALL duplicate launch ignored; existing wall is still open')
            return
        _launch_t9_window()


def _launch_t9_window():
    log("Launching T9 Input Window")
    # Clear stale query caches before fetching fresh data
    # 检查新增影片的 T9 索引

    # Set initial ReloadID to trigger list load immediately with current state
    # 初始状态设为刷新中，以便窗口打开时先隐藏列表，加载完后再淡入
    # xbmcgui.Window(10000).setProperty("MFG.IsRefreshing", "true")

    skin_name = get_skin_name()

    # 3. Set Font Properties
    xml_file = 'Custom_5111_MovieFilter.xml'

    if skin_name == "horizon":
        # Horizon 2 does not have font10/font12/font13.
        # Generate a specific XML with valid fonts if it doesn't exist.
        base_dir = os.path.join(ADDON_PATH, 'resources', 'skins', 'Default', '1080i')
        horizon_xml = 'Custom_5111_MovieFilter_Horizon.xml'
        horizon_xml_path = os.path.join(base_dir, horizon_xml)
        
        if not os.path.exists(horizon_xml_path):
            log("Generating Horizon specific XML...")
            src_xml_path = os.path.join(base_dir, xml_file)
            try:
                with open(src_xml_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                # Replace fonts with Horizon equivalents
                content = content.replace('font10', 'font_tiny')
                content = content.replace('font12', 'font_mini')
                content = content.replace('font13', 'font_small')
                
                with open(horizon_xml_path, 'w', encoding='utf-8') as f:
                    f.write(content)
                log(f"Generated {horizon_xml}")
            except Exception as e:
                log(f"Error generating Horizon XML: {e}")
        
        if os.path.exists(horizon_xml_path):
            xml_file = horizon_xml

    from lib import window_handler
    # 创建并显示窗口
    w = window_handler.FilterWindow(xml_file, ADDON_PATH, 'Default', '1080i')
    try:
        w.doModal()
    finally:
        w.cleanup()

    del w

def _launch_child_window(xml_name, mediatype=None):
    """Compatibility entrypoint for favourites and external launch scripts."""
    from lib.navigation import launch_child
    return launch_child(xml_name, ADDON_PATH, mediatype)


def launch_mediatype_view(mediatype="movie"):
    """Open a category using one explicit, window-local media-type filter."""
    if mediatype not in ('all', 'movie', 'set', 'tvshow', 'concert', 'documentary'):
        mediatype = 'all'
    log('Launching MediaType View: %s' % mediatype)
    _launch_child_window('Custom_5114_Movie.xml', mediatype)


def launch_all_movie():
    log('Launching All Movie Window')
    _launch_child_window('Custom_5112_AllMovie.xml', 'all')


def launch_search_movie():
    parent = xbmcgui.Window(xbmcgui.getCurrentWindowId())
    if parent.getProperty('MFG.SearchMode') == 'true':
        return
    log('Launching Search Movie Window')
    _launch_child_window('Custom_5113_SearchMovie.xml')


def filter_list(reload_param, page_start=0):
    reload_param = reload_param or ""
    # 先清空，再填充新的，保证页面永远都是显示前两行
    if reload_param.startswith("clear_"):
        xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=True)
        return

    # 1. Load state from Skin
    filter_state = {}
    blob = xbmc.getInfoLabel('Skin.String(MFG.State)')
    if blob:
        try:
            decoded = base64.b64decode(blob).decode('utf-8')
            filter_state = json.loads(decoded)
        except Exception as e:
            log(f"Error loading state blob: {e}")

    # 2. Convert state to filters
    filters = {}
    for group, item in filter_state.items():
        if group == 'filter.rating':
            for obj in item:
                val = obj['value']
                if val:
                    filters[f"{group}.{val}"] = True
        else:
            val = item['value']
            if val is not None:
                filters[group] = val

    # XML panels have no load-more action: return the configured result limit.
    
    # 4. T9 Input
    t9_input = xbmcgui.Window(10000).getProperty("MFG.T9Input")
    filter_limit = int(ADDON.getSetting('filter_limit')) or 9999  # 默认无限制
    search_limit = int(ADDON.getSetting('search_limit')) or 9999
    limit = filter_limit
    if t9_input:
        t9_digits = "".join(ch for ch in str(t9_input) if ch.isdigit())
        if t9_digits or t9_input.strip():
                # 纯数字输入加 | 前缀避免与原始标题内容误匹配，含字母时直接传递
                t9_value = t9_input.strip()
                if t9_value.isdigit():
                    t9_value = f"|{t9_value}"
                filters["filter.t9"] = t9_value
                limit = search_limit
                # 当有T9输入时，只保留影视范围、排序和T9条件
                keys_to_keep = ["filter.mediatype", "filter.sort", "filter.t9"]
                filters = {k: v for k, v in filters.items() if k in keys_to_keep}

    # 4. Get Items
    items = library.jsonrpc_get_items(filters=filters, limit=limit, offset=page_start)
    # 5. Populate List
    
    library.prime_textures(items)
    list_items = []
    for m in items:
        li, url, is_folder = library.create_list_item(m)
        if li:
            list_items.append((url, li, is_folder))
    
    total_items = len(list_items)
    xbmcplugin.addDirectoryItems(HANDLE, list_items, total_items)
    # 如果有更多数据，告知 Kodi 可以加载更多
    has_more = False
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False, updateListing=page_start > 0)
    xbmcgui.Window(10000).setProperty("MFG.HasMore", "true" if has_more else "false")
    
    if not reload_param.startswith("first_"):
        # 首次加载要的是快,不使用淡入效果
        # 列表加载完成，触发淡入动画
        xbmc.sleep(100)
        xbmcgui.Window(10000).setProperty("MFG.IsRefreshing", "false")
    log(f"Filtered list populated with {len(list_items)} items")

def toggle_favourite():
    """与原生右键菜单的"添加到收藏夹/从收藏夹移除"行为一致"""
    # 从当前选中的 ListItem 读取信息，与原生 context menu 一致
    title = xbmc.getInfoLabel('ListItem.Label')
    dbid = xbmc.getInfoLabel('ListItem.DBID')
    dbtype = xbmc.getInfoLabel('ListItem.DBType')
    is_folder = xbmc.getCondVisibility('ListItem.IsFolder')
    path = xbmc.getInfoLabel('ListItem.FolderPath') if is_folder else xbmc.getInfoLabel('ListItem.FilenameAndPath')
    thumb = xbmc.getInfoLabel('ListItem.Art(poster)') or xbmc.getInfoLabel('ListItem.Thumb')
    log(f"Toggle Favourite - Title: {title}, DBID: {dbid}, DBType: {dbtype}, IsFolder: {is_folder}, Path: {path}, Thumb: {thumb}")
    if not path or not title:
        notification(ADDON.getLocalizedString(32167), sound=True)
        return

    # script:// 路径去掉尾部斜杠，避免收藏夹中插件名多出 /
    if path.startswith("script://"):
        path = path.rstrip('/')

    # 对于库中的 folder 类型(tvshow/set)，使用 videodb 路径
    if dbid and dbid.isdigit() and int(dbid) > 0 and is_folder:
        if dbtype == "tvshow":
            path = f"videodb://tvshows/titles/{dbid}/"
        elif dbtype == "set":
            path = f"videodb://movies/sets/{dbid}/"

    # 判断收藏类型：folder 用 window，其余用 media
    if is_folder:
        fav_type = "window"
    else:
        fav_type = "media"

    # 检查是否已在收藏夹
    get_fav_query = {
        "jsonrpc": "2.0",
        "method": "Favourites.GetFavourites",
        "params": {"properties": ["path", "windowparameter"]},
        "id": 1
    }
    fav_resp = json.loads(xbmc.executeJSONRPC(json.dumps(get_fav_query)))
    from lib.tv_catalog import favourite_entries
    favourites = favourite_entries(fav_resp.get('result'))
    log(f"Current favourites: {favourites}")
    is_favourite = False
    if path.startswith("favourites://"):
        is_favourite = True
    else:
        for fav in favourites:
            fp = fav.get("path", "") or fav.get("windowparameter", "")
            if fp == path:
                is_favourite = True
                break

    # 调用 AddFavourite（内部实现为 AddOrRemove，即切换）
    add_params = {"title": title, "type": fav_type}
    if fav_type == "media":
        add_params["path"] = path
    else:
        add_params["window"] = "videos"
        add_params["windowparameter"] = path
    if thumb:
        add_params["thumbnail"] = thumb

    add_query = {
        "jsonrpc": "2.0",
        "method": "Favourites.AddFavourite",
        "params": add_params,
        "id": 1
    }
    xbmc.executeJSONRPC(json.dumps(add_query))
    if is_favourite:
        notification(ADDON.getLocalizedString(32168), title=title)
    else:
        notification(ADDON.getLocalizedString(32169), title=title)



def router(paramstring):
    log(f"Router called with: {paramstring}")
    if not paramstring:
        launch_t9()
        return

    # 解析路径，例如 plugin://..../?mode=play&movieid=1
    # Handle context menu call (sys.argv[1] is item path, not query string)
    if paramstring.startswith("videodb://") or paramstring.startswith("plugin://"):
        item_id = xbmc.getInfoLabel("ListItem.DBID")
        item_type = xbmc.getInfoLabel("ListItem.DBType")
        log(f"Context menu: DBID={item_id} DBType={item_type}")
        if item_id and item_type:
            router(f"?mode=clear_artwork&id={item_id}&type={item_type}")
        return

    params = dict(urllib.parse.parse_qsl(paramstring.lstrip('?')))
    mode = params.get("mode")

    if mode == 'movie_details':
        from lib.movie_details import open_movie
        open_movie(int(params.get('movieid',0)))
        return

    if mode == 'tvshow_details':
        from lib.tv_details import open_tvshow
        open_tvshow(int(params.get('tvshowid', 0)))
        return
    
    if mode == "launch_t9":
        launch_t9()
        return
    # 让我在主函数中添加 打开全部影片窗口
    if mode == "launch_mediatype_view":
        mt = params.get("mediatype", "movie")
        launch_mediatype_view(mt)
        return

    if mode == "launch_all_movie":
        launch_all_movie()
        return

    # 让我在主函数中添加 打开搜索影片窗口
    if mode == "launch_search_movie":
        launch_search_movie()
        return
        
    if mode == "get_total_count":
        total = library.get_total_count()
        xbmcgui.Window(10000).setProperty("MFG.TotalCount", str(total))
        return

    if mode == "filter_list":
        reload_param = params.get('reload')
        page_start = int(params.get('start', 0))
        filter_list(reload_param, page_start)
        return

    if mode == "open_playing_tvshow":
        open_playing_tvshow()
        return

    if mode == "force_prev":
        force_prev()
        return

    if mode == "clear_artwork":
        # Invalidate this addon's cache, never erase scraped artwork metadata.
        from lib import catalog_cache
        catalog_cache.clear()
        library.clear_query_cache()
        library._TEXTURE_PATH_CACHE.clear()
        xbmcgui.Window(10000).setProperty("MFG.ReloadID", "art_clear_"+str(time.time()))
        notification("海报墙缓存已刷新")
        return

    if mode == "toggle_favourite":
        toggle_favourite()
        return

    if mode == "delete_movie":
        # 从库中删除影片（电影/剧集/系列）
        item_id = params.get("id")
        item_type = params.get("type", "movie")
        if item_id:
            if item_type == "movie":
                method = "VideoLibrary.RemoveMovie"
                rpc_params = {"movieid": int(item_id)}
                name = "电影"
            elif item_type == "tvshow":
                method = "VideoLibrary.RemoveTVShow"
                rpc_params = {"tvshowid": int(item_id)}
                name = "剧集"
            elif item_type == "set":
                method = "VideoLibrary.RemoveMovieSet"
                rpc_params = {"setid": int(item_id)}
                name = "电影集"
            else:
                name = "影片"
            
            # 确认弹窗
            dialog = xbmcgui.Dialog()
            ok = dialog.yesno(ADDON.getLocalizedString(32170), ADDON.getLocalizedString(32171).format(name=name))
            if ok:
                query = {"jsonrpc": "2.0", "method": method, "params": rpc_params, "id": 1}
                result = json.loads(xbmc.executeJSONRPC(json.dumps(query)))
                if "error" not in result:
                    notification(ADDON.getLocalizedString(32172).format(name=name))
                    # 刷新列表
                    xbmcgui.Window(10000).setProperty("MFG.ReloadID", "del_" + str(time.time()))
                else:
                    notification(ADDON.getLocalizedString(32173).format(error=result.get("error", {}).get("message", "")), sound=True)
            else:
                notification(ADDON.getLocalizedString(32174))
        return

    if mode == "get_continue_watching":
        # 获取继续观看列表（混合电影、剧集、电影集）
        limit = int(params.get("limit", 20))
        items = library.get_continue_watching_items(limit=limit)

        list_items = []
        for item in items:
            li, url, is_folder = library.create_list_item(item)
            if li:
                list_items.append((url, li, is_folder))

        xbmcplugin.addDirectoryItems(HANDLE, list_items, len(list_items))
        xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=True)
        xbmcgui.Window(10000).setProperty("MFG.HasContinueWatching", "true" if items else "false")
        return

    if mode == "get_recently_added":
        # 获取最近加入的影片列表（混合电影、剧集）
        limit = int(params.get("limit", 10))

        # 使用 dateadded 排序获取最近加入的内容
        filters = {"filter.sort": "dateadded"}
        items = library.jsonrpc_get_items(filters=filters, limit=limit)

        list_items = []
        for item in items:
            li, url, is_folder = library.create_list_item(item)
            if li:
                list_items.append((url, li, is_folder))

        xbmcplugin.addDirectoryItems(HANDLE, list_items, len(list_items))
        xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=True)
        return

    if mode == "get_recently_added_movies":
        # 获取最近加入的电影（前10部）
        limit = int(params.get("limit", 10))
        filters = {"filter.sort": "dateadded", "filter.mediatype": "movie"}
        items = library.jsonrpc_get_items(filters=filters, limit=limit)

        list_items = []
        for item in items:
            li, url, is_folder = library.create_list_item(item)
            if li:
                list_items.append((url, li, is_folder))

        xbmcplugin.addDirectoryItems(HANDLE, list_items, len(list_items))
        xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=True)
        xbmcgui.Window(10000).setProperty("MFG.HasRecentMovies", "true" if items else "false")
        return

    if mode == "get_recently_added_tvshows":
        # 获取最近加入的剧集（前10部）
        limit = int(params.get("limit", 10))
        filters = {"filter.sort": "dateadded", "filter.mediatype": "tvshow"}
        items = library.jsonrpc_get_items(filters=filters, limit=limit)

        list_items = []
        for item in items:
            li, url, is_folder = library.create_list_item(item)
            if li:
                list_items.append((url, li, is_folder))

        xbmcplugin.addDirectoryItems(HANDLE, list_items, len(list_items))
        xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=True)
        xbmcgui.Window(10000).setProperty("MFG.HasRecentTVShows", "true" if items else "false")
        return

    if mode == "get_recently_added_sets":
        # 获取最近加入的系列电影（前10部）
        limit = int(params.get("limit", 10))
        filters = {"filter.sort": "dateadded", "filter.mediatype": "set"}
        items = library.jsonrpc_get_items(filters=filters, limit=limit)

        list_items = []
        for item in items:
            li, url, is_folder = library.create_list_item(item)
            if li:
                list_items.append((url, li, is_folder))

        xbmcplugin.addDirectoryItems(HANDLE, list_items, len(list_items))
        xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=True)
        xbmcgui.Window(10000).setProperty("MFG.HasRecentSets", "true" if items else "false")
        return

    if mode == "get_recently_added_concerts":
        # 获取最近加入的演唱会（前10部）
        limit = int(params.get("limit", 10))
        filters = {"filter.sort": "dateadded", "filter.mediatype": "concert"}
        items = library.jsonrpc_get_items(filters=filters, limit=limit)

        list_items = []
        for item in items:
            li, url, is_folder = library.create_list_item(item)
            if li:
                list_items.append((url, li, is_folder))

        xbmcplugin.addDirectoryItems(HANDLE, list_items, len(list_items))
        xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=True)
        xbmcgui.Window(10000).setProperty("MFG.HasRecentConcerts", "true" if items else "false")
        return

    if mode == "get_recently_added_documentaries":
        # 获取最近加入的纪录片（前10部）
        limit = int(params.get("limit", 10))
        filters = {"filter.sort": "dateadded", "filter.mediatype": "documentary"}
        items = library.jsonrpc_get_items(filters=filters, limit=limit)

        list_items = []
        for item in items:
            li, url, is_folder = library.create_list_item(item)
            if li:
                list_items.append((url, li, is_folder))

        xbmcplugin.addDirectoryItems(HANDLE, list_items, len(list_items))
        xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=True)
        xbmcgui.Window(10000).setProperty("MFG.HasRecentDocs", "true" if items else "false")
        return

if __name__ == "__main__":
    try:
        # sys.argv[0] 是 plugin://...; sys.argv[2] 是 '?xxx'
        # log(sys.argv)
        if HANDLE != -1 and len(sys.argv) > 2:
            router(sys.argv[2])
        elif len(sys.argv) > 1:
            router(sys.argv[1])
        else:
            router("")
    finally:
        ADDON = None
