"""A modal show/season browser; bounded background loading and native ID playback."""
import queue
import threading
import json
import weakref
import xbmc
import xbmcgui
from .common import ADDON_PATH, log, notification
from . import tv_catalog as catalog
from . import video_library as library
from .detail_playback import read_playback
from .movie_catalog import progress, number


def open_tvshow(show_id, parent=None, seed=None, initial_season=None):
    parent = parent or xbmcgui.Window(xbmcgui.getCurrentWindowId())
    if parent.getProperty('MFG.ChildWindow') == 'true':
        return False
    capture = getattr(parent, '_capture_navigation_state', None)
    if capture:
        capture()
    parent.setProperty('MFG.ChildWindow', 'true')
    child = None
    try:
        child = TvDetailsWindow('Custom_5115_TvDetails.xml', ADDON_PATH, 'Default', '1080i')
        child.show_id = int(show_id)
        child.seed = seed or {}
        child.initial_season = initial_season
        child.doModal()
        return True
    finally:
        try:
            if child:
                child.cleanup()
        finally:
            parent.clearProperty('MFG.ChildWindow')
            restore = getattr(parent, '_restore_navigation_state', None)
            if restore:
                restore()


class TvDetailsWindow(xbmcgui.WindowXML):
    def __init__(self, *args, **kwargs):
        self.closed = threading.Event()
        self.jobs, self.results = queue.Queue(), queue.Queue()
        self.generation = 0
        self.show_data = None
        self.season = None
        self.show_position = 0
        self.episodes = []
        self.next_episode = None
        self.grid_view = False
        self._initial_focus = True
        self._playback_selection = None
        self.actors = []
        self.season_cast = None
        self._progress_pending = False
        self._last_played_id = None
        self._navigation_state = None
        self._portrait_generation = None
        self._cast_signature = None

    def onInit(self):
        self.window_id = xbmcgui.getCurrentWindowId()
        if hasattr(self, 'worker'):
            # Kodi may rebuild controls on return from a full-screen player.
            # Keep only metadata/indices; create fresh native items when needed.
            if self.show_data:
                self._show_header(self.show_data['show'])
                self.setProperty('MFG.Tv.SeasonPage', 'true' if self.season else 'false')
                self.setProperty('MFG.Tv.Grid', 'true' if self.grid_view else 'false')
                self._ensure_panel(300,self.show_data['seasons'],
                    lambda row:self._season_item(row,self.show_data['show']),self.show_position)
                for cid in (301, 302):
                    self._ensure_panel(cid,self.episodes,self._episode_item)
                self._bind_actors()
                self._apply_results()
                if self._navigation_state:
                    self._restore_navigation_state()
                if self._playback_selection:
                    cid, position = self._playback_selection
                    self._playback_selection = None
                    if cid in (300, 301, 302):
                        self.getControl(cid).selectItem(position)
                    if self.getFocusId() != cid:
                        self.setFocusId(cid)
                self._queue_progress()
            return
        self.setProperty('MFG.Tv.SeasonPage', 'false')
        self.setProperty('MFG.Tv.Grid', 'false')
        self.setProperty('MFG.Tv.CastFocus', 'false')
        self._show_header(getattr(self, 'seed', {}))
        self.worker = threading.Thread(target=self._worker, name='PosterWallTV', daemon=True)
        self.worker.start()
        self.library_monitor = _LibraryMonitor(self)
        self._request('show')
        self.setFocusId(201)

    def _request(self, mode, offset=0):
        self.generation += 1
        known, target = (False, None)
        if mode in ('show','season'):
            known, target = read_playback('tvshow',self.show_id,
                self.season['season'] if self.season else getattr(self,'initial_season',None))
        if known:
            self._set_next_episode(target)
        else:
            self.next_episode = None
            self._layout_play_label('')
            self.getControl(201).setEnabled(False)
        self.setProperty('MFG.Tv.State', 'loading')
        self.setProperty('MFG.Tv.Message', '')
        self.jobs.put((self.generation, mode, self.season, offset))

    def _active(self):
        return not self.closed.is_set() and xbmcgui.getCurrentWindowId() == self.window_id

    def _ensure_panel(self,cid,rows,factory,position=0):
        """Preserve surviving native lists; rebuild only missing controls.

        Python ControlList.size() reflects its retained items, so also verify
        the renderer's count after fullscreen/child-window transitions.
        """
        panel = self.getControl(cid)
        native_count = xbmc.getInfoLabel('Container(%d).NumItems'%cid)
        if native_count.isdigit() and int(native_count)==len(rows) and panel.size()==len(rows):
            return
        panel.reset()
        panel.addItems([factory(row) for row in rows])
        if rows and position:
            panel.selectItem(max(0,min(position,len(rows)-1)))

    def _worker(self):
        while not self.closed.is_set():
            try:
                generation, mode, season, offset = self.jobs.get(timeout=.2)
            except queue.Empty:
                continue
            if self.closed.is_set():
                return
            if generation != self.generation:
                if mode == 'progress':
                    self._progress_pending = False
                continue
            try:
                if mode == 'progress':
                    if self.closed.wait(.15):
                        return
                    data = {'next': catalog.next_episode(self.show_id, season['season'] if season else None)}
                    if self._last_played_id:
                        data['played'] = catalog.rpc('VideoLibrary.GetEpisodeDetails', {
                            'episodeid': self._last_played_id, 'properties': ['resume', 'playcount']}).get('episodedetails')
                    items, error = [], False
                    self.results.put((generation, mode, data, items, error))
                    self._progress_pending = False
                    if not self.closed.is_set() and generation == self.generation:
                        xbmc.executebuiltin('SendClick(%d,900)' % self.window_id)
                    continue
                if mode == 'watched':
                    catalog.set_watched(self.show_id, offset, season['season'] if season else None,
                                        cancelled=self.closed.is_set)
                    if self.closed.is_set():
                        return
                    # Refresh cached show/season counts after native writes.
                    refreshed = catalog.load_show(self.show_id)
                    mode = 'season' if season else 'show'
                    offset = 0
                else:
                    refreshed = None
                if mode == 'show':
                    data = refreshed or catalog.load_show(self.show_id)
                    rows = data['seasons']
                    library.prime_textures([data['show']] + rows)
                    items = [self._season_item(row, data['show']) for row in rows]
                else:
                    data = catalog.load_episodes(self.show_id, season['season'], offset)
                    library.prime_textures(data['episodes'])
                    items = [self._episode_item(row) for row in data['episodes']]
                    if refreshed:
                        data['refreshed_show'] = refreshed
                error = False
            except Exception as exc:
                log('TV details load failed: %s' % type(exc).__name__, xbmc.LOGERROR)
                data, items, error = {}, [], True
                self._progress_pending = False
            if self.closed.is_set() or generation != self.generation:
                continue
            self.results.put((generation, mode, data, items, error))
            xbmc.executebuiltin('SendClick(%d,900)' % self.window_id)

    @staticmethod
    def _image(*urls):
        return library._resolve_artwork(urls)

    def _season_item(self, row, show):
        art, parent_art = row.get('art', {}), show.get('art', {})
        li = xbmcgui.ListItem(catalog.season_title(row), offscreen=True)
        li.setContentLookup(False)
        li.setArt({'poster': self._image(art.get('poster'), parent_art.get('poster'))})
        li.setProperty('MFG.Tv.Count', '%d 集' % row.get('episode', 0))
        li.setProperty('MFG.Tv.Rating', '%.1f' % show['rating'] if show.get('rating') else '')
        li.setProperty('MFG.Tv.Watched', 'true' if row.get('episode') == row.get('watchedepisodes') else 'false')
        return li

    def _episode_item(self, row):
        art = row.get('art', {})
        li = xbmcgui.ListItem('%d. %s' % (row.get('episode', 0), row.get('title') or '未命名单集'), offscreen=True)
        li.setContentLookup(False)
        li.setArt({'thumb': self._image(art.get('thumb'), row.get('thumbnail'),
                                      (self.show_data or {}).get('show', {}).get('art', {}).get('fanart'))})
        li.setProperty('MFG.Tv.Plot', row.get('plot') or '暂无单集简介')
        li.setProperty('MFG.Tv.Resolution', catalog.resolution(row))
        li.setProperty('MFG.Tv.Watched', 'true' if row.get('playcount', 0) else 'false')
        li.setProperty('MFG.Tv.Progress', str(progress(row)['percent']))
        li.setProperty('MFG.Tv.Number', str(row.get('episode', 0)))
        li.setProperty('MFG.Tv.Summary', catalog.episode_summary(row))
        li.setProperty('MFG.Tv.Duration', '%d 分钟' % (int(number(row.get('runtime'))) // 60)
                       if row.get('runtime') else '')
        return li

    def _bind_actors(self):
        if not self.show_data:
            return
        source = self.season_cast if self.season and self.season_cast else self.show_data['show'].get('cast')
        self.actors = [dict(actor) for actor in sorted(source or [], key=lambda c: c.get('order', 0))[:80]]
        for actor in self.actors:
            actor['thumbnail_candidates'] = catalog.actor_image_candidates(actor)
        library.prime_textures([{'cast': [{'thumbnail': url} for actor in self.actors
                               for url in actor['thumbnail_candidates']]}])
        self.setProperty('MFG.Tv.HasCast', 'true' if self.actors else 'false')
        panel = self.getControl(303)
        signature = tuple((actor.get('name'),actor.get('role'),actor.get('order'),
                           tuple(actor['thumbnail_candidates'])) for actor in self.actors)
        portraits = [self._image(*actor['thumbnail_candidates']) for actor in self.actors]
        native_count = xbmc.getInfoLabel('Container(303).NumItems')
        unchanged = (self._cast_signature == signature and panel.size() == len(self.actors)
                     and native_count.isdigit() and int(native_count)==len(self.actors))
        items = []
        if unchanged:
            for pos,path in enumerate(portraits):
                item = panel.getListItem(pos)
                if item.getArt('portrait') != path:
                    item.setArt({'portrait':path})
        else:
            for actor,path in zip(self.actors,portraits):
                li = xbmcgui.ListItem(actor.get('name', ''), offscreen=True)
                li.setContentLookup(False)
                li.setArt({'portrait': path})
                li.setProperty('MFG.Tv.Role', '饰 ' + actor['role'] if actor.get('role') else '')
                items.append(li)
            old = max(0, panel.getSelectedPosition())
            panel.reset()
            panel.addItems(items)
            panel.selectItem(min(old, max(0, len(items) - 1)))
            self._cast_signature = signature
        missing = any(not path or ('://' in path and not path.startswith('special://')) for path in portraits)
        if missing and self._portrait_generation != self.generation:
            self._portrait_generation = self.generation
            self.portrait_worker = threading.Thread(target=self._retry_portraits,
                args=(self.generation, [dict(actor) for actor in self.actors]),
                name='PosterWallPortraits', daemon=True)
            self.portrait_worker.start()

    def _retry_portraits(self, generation, actors):
        # A transient texture-DB lock or an image cached after the first bind
        # must not leave an empty ListItem art field for the entire visit.
        for delay in (.5, 1.5):
            if self.closed.wait(delay) or generation != self.generation:
                return
            library.prime_textures([{'cast': [{'thumbnail': url} for actor in actors
                for url in actor.get('thumbnail_candidates', [])]}], force=True)
            updates = [(position, self._image(*actor.get('thumbnail_candidates', [])))
                       for position, actor in enumerate(actors)]
            updates = [(position, path) for position, path in updates
                       if path and (path.startswith('special://') or '://' not in path)]
            if self.closed.is_set() or generation != self.generation:
                return
            self.results.put((generation, 'portraits', updates, [], False))
            xbmc.executebuiltin('SendClick(%d,900)' % self.window_id)

    def _queue_progress(self):
        if self.closed.is_set() or self._progress_pending or not self.show_data:
            return
        self._progress_pending = True
        self.jobs.put((self.generation, 'progress', self.season, 0))

    def _show_header(self, show):
        art = show.get('art') or {}
        library.prime_textures([show])
        for key, value in {'Title': show.get('title', ''), 'Plot': show.get('plot') or '暂无简介',
                           'Poster': self._image(art.get('poster')), 'Fanart': self._image(art.get('fanart')),
                           'Logo': self._image(art.get('clearlogo')), 'Year': show.get('year') or '',
                           'Rating': '%.1f 分' % show['rating'] if show.get('rating') else '',
                           'Meta': '  /  '.join(str(v) for v in (show.get('mpaa'), show.get('year'),
                                     ' · '.join(show.get('genre') or [])) if v)}.items():
            self.setProperty('MFG.Tv.' + key, str(value))

    def _apply_results(self):
        if not self._active():
            return
        while True:
            try:
                generation, mode, data, items, error = self.results.get_nowait()
            except queue.Empty:
                break
            if self.closed.is_set() or generation != self.generation:
                if mode == 'progress':
                    self._progress_pending = False
                continue
            if mode == 'portraits':
                panel = self.getControl(303)
                for position, path in data:
                    if position < panel.size():
                        panel.getListItem(position).setArt({'portrait': path})
                continue
            if mode == 'progress':
                if not error:
                    self._set_next_episode(data.get('next'))
                    updated = data.get('played') or {}
                    for position, row in enumerate(self.episodes):
                        if row.get('episodeid') != updated.get('episodeid'):
                            continue
                        row.update(updated)
                        template = self._episode_item(row)
                        for cid in (301, 302):
                            item = self.getControl(cid).getListItem(position)
                            for key in ('MFG.Tv.Progress', 'MFG.Tv.Watched'):
                                item.setProperty(key, template.getProperty(key))
                continue
            if error:
                self.setProperty('MFG.Tv.State', 'error')
                self.setProperty('MFG.Tv.Message', '剧集信息加载失败，请按“重试”。')
                self._layout_play_label('重试')
                self.getControl(201).setEnabled(True)
                continue
            if mode == 'show':
                self.show_data = data
                self._show_header(data['show'])
                self._bind_actors()
                panel = self.getControl(300)
                panel.reset()
                panel.addItems(items)
                self.setProperty('MFG.Tv.SeasonCount', '共 %d 季' % len(items))
                self._favourite_state()
                if not items:
                    self.setProperty('MFG.Tv.Message', '暂无已入库的单集，请先扫描此电视剧目录。')
            else:
                if data.get('cast') is not None:
                    self.season_cast = data['cast']
                    self._bind_actors()
                if data.get('refreshed_show'):
                    self.show_data = data['refreshed_show']
                    self.season = next((s for s in self.show_data['seasons']
                                        if s['season'] == self.season['season']), self.season)
                append = data['offset'] > 0
                self.episodes = self.episodes + data['episodes'] if append else data['episodes']
                old_position = data['offset'] if append else 0
                for cid in (301, 302):
                    panel = self.getControl(cid)
                    if not append:
                        panel.reset()
                    panel.addItems(items)
                    panel.selectItem(max(0, old_position))
                self.setProperty('MFG.Tv.EpisodeCount', '%d 集' % data['total'])
                self.episode_total = data['total']
                self.setProperty('MFG.Tv.HasMore', 'true' if len(self.episodes) < data['total'] else 'false')
                if not self.episodes:
                    self.setProperty('MFG.Tv.Message', '本季暂无已入库的单集。')
                if append:
                    self.setFocusId(self._episode_control())
            initial_season = getattr(self,'initial_season',None)
            if mode=='show' and initial_season is not None:
                self.initial_season = None
                selected = next((i for i,s in enumerate(data['seasons'])
                                 if s['season']==initial_season),None)
                if selected is not None:
                    self.getControl(300).selectItem(selected)
                    self._enter_season()
                    return
            self._set_next_episode(data.get('next'))
            self.setProperty('MFG.Tv.State', 'ready' if items or self.episodes else 'empty')
            if self._initial_focus:
                self._initial_focus = False
                self.setFocusId(201 if self.next_episode else (300 if mode == 'show' and items
                    else self._episode_control() if self.episodes else 200))

    def _set_next_episode(self, episode):
        self.next_episode = episode
        label = catalog.play_label(episode) if episode else '暂无单集'
        self._layout_play_label(label)
        self.getControl(201).setEnabled(bool(episode))

    def _layout_play_label(self, label):
        self.setProperty('MFG.Tv.PlayLabel', label)

    def _episode_control(self):
        return 302 if self.grid_view else 301

    def _enter_season(self):
        if not self.show_data:
            return
        self.show_position = self.getControl(300).getSelectedPosition()
        if not 0 <= self.show_position < len(self.show_data['seasons']):
            return
        self.season = self.show_data['seasons'][self.show_position]
        self.episodes = []
        self.season_cast = None
        self.episode_total = 0
        self.setProperty('MFG.Tv.SeasonPage', 'true')
        self.setProperty('MFG.Tv.EpisodeCount', '')
        self.setProperty('MFG.Tv.HasMore', 'false')
        self.setProperty('MFG.Tv.SeasonTitle', catalog.season_title(self.season))
        self.setProperty('MFG.Tv.SeasonPoster', self._image(
            self.season.get('art', {}).get('poster'), self.show_data['show'].get('art', {}).get('poster')))
        for cid in (301, 302):
            self.getControl(cid).reset()
        self.setFocusId(201)
        self._initial_focus = True
        self._request('season')

    def _return_show(self):
        self.generation += 1  # Invalidate a pending season request.
        self.season = None
        self.episodes = []
        self._playback_selection = None
        self._initial_focus = False
        for cid in (301, 302):
            self.getControl(cid).reset()
        self.setProperty('MFG.Tv.SeasonPage', 'false')
        self.setProperty('MFG.Tv.CastFocus', 'false')
        self.setProperty('MFG.Tv.State', 'ready')
        self._set_next_episode(self.show_data.get('next'))
        self._queue_progress()
        self.getControl(300).selectItem(self.show_position)
        self.setFocusId(300)

    def _play(self, episode):
        if not episode:
            return
        try:
            cid = self.getFocusId()
            position = self.getControl(cid).getSelectedPosition() if cid in (300, 301, 302) else 0
            self._playback_selection = (cid, position)
            self._last_played_id = int(episode['episodeid'])
            self._set_next_episode(episode)
            catalog.rpc('Player.Open', {'item': {'episodeid': int(episode['episodeid'])},
                                       'options': {'resume': True}})
        except (RuntimeError, KeyError, ValueError):
            notification('无法播放此单集，请检查媒体来源。')

    def _favourite_state(self):
        self.favourite_path = 'videodb://tvshows/titles/%d/' % self.show_id
        try:
            entries = catalog.favourite_entries(catalog.rpc('Favourites.GetFavourites', {'properties': ['windowparameter']}))
            self.is_favourite = any(f.get('windowparameter') == self.favourite_path for f in entries)
        except RuntimeError:
            self.is_favourite = False
        self.setProperty('MFG.Tv.Favourite', 'true' if self.is_favourite else 'false')

    def _toggle_favourite(self):
        if not self.show_data:
            return
        try:
            catalog.rpc('Favourites.AddFavourite', {'title': self.show_data['show']['title'],
                'type': 'window', 'window': 'videos', 'windowparameter': self.favourite_path,
                'thumbnail': self.show_data['show'].get('art', {}).get('poster', '')})
            self._favourite_state()
        except RuntimeError:
            notification('收藏状态未能保存，请重试。')

    def _watched(self):
        if not self.show_data or self.getProperty('MFG.Tv.State') != 'ready':
            return
        show = self.show_data['show']
        count = self.season.get('episode', 0) if self.season else show.get('episode', 0)
        watched = self.season.get('watchedepisodes', 0) if self.season else show.get('watchedepisodes', 0)
        flag = 0 if count and watched >= count else 1
        scope = catalog.season_title(self.season) if self.season else '整部电视剧'
        if not xbmcgui.Dialog().yesno('观看状态', '将%s标记为%s？' % (scope, '已观看' if flag else '未观看')):
            return
        self._request('watched', flag)
        self.setProperty('MFG.Tv.Message', '正在更新观看状态…')

    def onClick(self, cid):
        if cid == 900:
            self._apply_results()
        elif cid == 200:
            self.onAction(_BackAction())
        elif cid == 201:
            if self.getProperty('MFG.Tv.State') == 'error':
                self._request('season' if self.season else 'show')
            else:
                self._play(self.next_episode)
        elif cid == 202:
            self._toggle_favourite()
        elif cid == 203:
            self._watched()
        elif cid == 300:
            self._enter_season()
        elif cid in (301, 302):
            pos = self.getControl(cid).getSelectedPosition()
            if 0 <= pos < len(self.episodes):
                self._play(self.episodes[pos])
        elif cid == 303:
            position = self.getControl(303).getSelectedPosition()
            if 0 <= position < len(self.actors):
                from .actor_details import open_actor
                self._playback_selection = None
                open_actor(self.actors[position], self.show_data['show'], self)
        elif cid in (204, 205):
            old = self._episode_control()
            position = self.getControl(old).getSelectedPosition()
            self.grid_view = cid == 205
            self.setProperty('MFG.Tv.Grid', 'true' if self.grid_view else 'false')
            self.getControl(self._episode_control()).selectItem(max(0, position))
            self.setFocusId(self._episode_control())
        elif cid == 206 and self.getProperty('MFG.Tv.HasMore') == 'true':
            self._request('season', len(self.episodes))

    def onFocus(self, control_id):
        self.setProperty('MFG.Tv.CastFocus', 'true' if control_id == 303 else 'false')

    def _capture_navigation_state(self):
        focus = self.getFocusId()
        position = self.getControl(focus).getSelectedPosition() if focus in (300, 301, 302, 303) else 0
        self._navigation_state = (focus, position)

    def _restore_navigation_state(self):
        if self._navigation_state and self._active():
            focus, position = self._navigation_state
            self._navigation_state = None
            if focus in (300, 301, 302, 303):
                panel = self.getControl(focus)
                if panel.size():
                    target = max(0,min(position,panel.size()-1))
                    if panel.getSelectedPosition() != target:
                        panel.selectItem(target)
            if self.getFocusId() != focus:
                self.setFocusId(focus)

    def onAction(self, action):
        if action.getId() in (1,2,3,4) and self._active():
            self._navigation_state = None
        if action.getId() in (9, 10, 92):
            if self.season is not None and self.show_data:
                self._return_show()
            else:
                self.close()

    def cleanup(self):
        self.closed.set()
        self.generation += 1
        self.jobs.put((self.generation, 'stop', None, 0))
        if hasattr(self, 'worker'):
            self.worker.join(timeout=.25)
        if hasattr(self, 'portrait_worker'):
            self.portrait_worker.join(timeout=.1)
        self.library_monitor = None

    def close(self):
        self.closed.set()
        super().close()


class _BackAction:
    @staticmethod
    def getId():
        return 92


class _LibraryMonitor(xbmc.Monitor):
    def __init__(self, owner):
        super().__init__()
        self.owner = weakref.ref(owner)

    def onNotification(self, sender, method, data):
        owner = self.owner()
        if owner is None or owner.closed.is_set():
            return
        if method == 'Player.OnStop':
            owner._queue_progress()
        elif method == 'VideoLibrary.OnUpdate':
            try:
                payload = json.loads(data)
                item = payload.get('item', {}) if isinstance(payload,dict) else {}
                if not isinstance(item,dict):
                    return
                if item.get('type') == 'episode' and item.get('id') == owner._last_played_id:
                    owner._queue_progress()
            except (ValueError, TypeError):
                pass
