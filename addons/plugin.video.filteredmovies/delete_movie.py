# -*- coding: utf-8 -*-
import json, xbmc, xbmcaddon, xbmcgui

ADDON = xbmcaddon.Addon(id="plugin.video.filteredmovies")

dbid = xbmc.getInfoLabel("ListItem.DBID")
dbtype = xbmc.getInfoLabel("ListItem.DBType")
label = xbmc.getInfoLabel("ListItem.Label")
xbmc.log("[plugin.video.filteredmovies] delete_movie: DBID=%s DBType=%s" % (dbid, dbtype), xbmc.LOGINFO)

if dbid and dbtype:
    method_map = {"movie": ("VideoLibrary.RemoveMovie", "movieid"),
                  "tvshow": ("VideoLibrary.RemoveTVShow", "tvshowid"),
                  "set": ("VideoLibrary.RemoveMovieSet", "setid")}
    name_map = {"movie": "电影", "tvshow": "剧集", "set": "系列"}
    method, id_key = method_map.get(dbtype, ("VideoLibrary.RemoveMovie", "movieid"))
    name = name_map.get(dbtype, "影片")

    title = ADDON.getLocalizedString(32165) + name
    msg = ADDON.getLocalizedString(32166) % label
    ok = xbmcgui.Dialog().yesno(title, msg)
    if ok:
        query = {"jsonrpc": "2.0", "method": method, "params": {id_key: int(dbid)}, "id": 1}
        resp = json.loads(xbmc.executeJSONRPC(json.dumps(query)))
        if "error" not in resp:
            xbmcgui.Dialog().notification("FilteredMovies", ADDON.getLocalizedString(32167), time=2000)
            xbmcgui.Window(10000).setProperty("MFG.ReloadID", "del_cm_" + dbid)
        else:
            xbmcgui.Dialog().notification("FilteredMovies", ADDON.getLocalizedString(32168) + str(resp.get("error", "")), time=2000)
