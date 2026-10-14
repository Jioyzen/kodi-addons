"""One cancellable loader per window; bounded pages, no per-row plugin invokers."""
import queue
import threading
import time
import xbmc
import xbmcgui
import xbmcaddon
from . import video_library as library
from . import catalog_cache
from .common import log

PAGE_SIZE = 120
HOME_ROWS = {1002: ('continue', 30), 1004: ('movie', 10), 1005: ('tvshow', 10),
             1006: ('concert', 10), 1007: ('documentary', 10), 1008: ('set', 10)}


def _home_row_items(media, limit):
    """Read one extra record to detect overflow without a separate count query."""
    if media == 'continue':
        return list(library.get_continue_watching_items(limit)[:limit])
    rows = library.jsonrpc_get_items({'filter.mediatype': media,
                                     'filter.sort': 'dateadded'}, limit + 1)
    visible = list(rows[:limit])
    if len(rows) > limit:
        visible.append({'title': '查看全部', 'media_type': 'home_more',
                        'view_all': media})
    return visible


class WallLoader:
    def __init__(self, owner):
        self.owner = owner
        self.requests = queue.Queue()
        self.closed = threading.Event()
        self.lock = threading.RLock()
        self.generation = 0
        self.loaded = 0
        self.total = 0
        self.pending = False
        self.prefetch_pending = False
        self.prefetched_page = None
        self.prefetched_revision = None
        self.home_rows = {}
        self.home_revision = None
        self.home_refresh_pending = False
        self.filters = {}
        self.worker = threading.Thread(target=self.run, name='PosterWallLoader', daemon=True)
        self.worker.start()

    def message(self, cid, state, text):
        self.owner.setProperty('MFG.Panel.%d.State' % cid, state)
        self.owner.setProperty('MFG.Panel.%d.Message' % cid, text)

    def refresh(self, filters=None):
        with self.lock:
            if self.closed.is_set():
                return
            self.generation += 1
            self.owner.setProperty('MFG.Loaded','false')
            self.owner.setProperty('MFG.ResultTotal','0')
            self.loaded = self.total = 0
            self.prefetch_pending = False
            self.prefetched_page = None
            self.prefetched_revision = None
            try:
                setting = 'search_limit' if (filters or {}).get('filter.t9') else 'filter_limit'
                self.result_limit = max(0, int(xbmcaddon.Addon('plugin.video.filteredmovies').getSetting(setting) or 0))
            except (TypeError,ValueError):
                self.result_limit = 0
            self.pending = True
            self.filters = dict(filters or {})
            if self.filters.get('filter.sort') == 'random':
                self.filters['_random_seed'] = time.monotonic_ns() & 0x7fffffff
            if self.owner.getProperty('MFG.Home') == 'true':
                self.home_rows.clear()
                self.home_revision = None
                self.home_refresh_pending = False
                for cid in HOME_ROWS:
                    self.message(cid, 'loading', '正在加载…')
                kind = 'home'
            else:
                self.message(9999, 'loading', '正在加载影片…')
                kind = 'page'
            self.requests.put((self.generation, kind, self.filters.copy(), 0))

    def valid(self, generation):
        return not self.closed.is_set() and generation == self.generation

    def next_page(self):
        with self.lock:
            if self.closed.is_set() or self.pending or self.loaded >= self.total:
                return
            self.pending = True
            self.requests.put((self.generation, 'page', self.filters.copy(), self.loaded))

    def prefetch_next_page(self):
        """Warm one bounded metadata page without resetting the visible panel."""
        with self.lock:
            if (self.closed.is_set() or self.pending or self.prefetch_pending
                    or self.prefetched_page is not None or self.loaded >= self.total):
                return
            self.prefetch_pending = True
            self.requests.put((self.generation, 'prefetch', self.filters.copy(), self.loaded))

    def refresh_playback_state(self, identity, position):
        with self.lock:
            if not self.closed.is_set():
                self.prefetched_page = None
                self.prefetched_revision = None
                self.requests.put((self.generation, 'playback', tuple(identity), position))

    def _prefetch(self, generation, filters, offset):
        if not self.valid(generation):
            return
        try:
            revision = catalog_cache.revision()
            size = min(PAGE_SIZE, self.result_limit-offset) if self.result_limit else PAGE_SIZE
            rows = library.jsonrpc_get_items(filters, size, offset)
            if self.valid(generation):
                library.prime_textures(rows)
                with self.lock:
                    if self.valid(generation) and revision == catalog_cache.revision():
                        # Retain primitive metadata only, never native ListItems.
                        self.prefetched_page = (generation, offset, rows)
                        self.prefetched_revision = revision
        except Exception as exc:
            # Optional prefetch failure must not replace a ready page with an
            # error. Navigation can retry the normal query when it needs data.
            log('WALL prefetch failed: %s' % type(exc).__name__, xbmc.LOGWARNING)
        finally:
            with self.lock:
                if self.valid(generation):
                    self.prefetch_pending = False

    def _page_rows(self, generation, filters, offset, page_size):
        with self.lock:
            cached = self.prefetched_page
            if (cached and cached[:2] == (generation, offset)
                    and self.prefetched_revision == catalog_cache.revision()):
                self.prefetched_page = None
                self.prefetched_revision = None
                return cached[2]
            self.prefetched_page = None
            self.prefetched_revision = None
        return library.jsonrpc_get_items(filters, page_size, offset)

    def populate(self, cid, rows, generation, append=False, on_gui=False):
        # Build offscreen tags without repeatedly acquiring Kodi's GUI mutex.
        if not self.valid(generation) or not self.owner._is_active():
            return False
        library.prime_textures(rows)
        items = []
        for row in rows:
            if not self.valid(generation):
                return False
            try:
                li, path, _folder = library.create_list_item(row)
                if li is not None:
                    items.append(li)
            except Exception as exc:
                log('Skipping malformed item %s: %s' % (row.get('title'), exc), xbmc.LOGWARNING)
        with self.lock:
            if not self.valid(generation) or self.owner is None:
                return False
            owner = self.owner
        if cid == 9999:
            if not owner.bind_grid_items(items, generation, append):
                return False
        else:
            # Initial recommendations follow the same GUI bridge as updates.
            # A GUI onInit restore must bind directly, never wait on itself.
            if on_gui:
                if not owner._apply_home_row(cid, rows, items, generation):
                    return False
            elif not owner.bind_home_rows(cid, rows, items, generation):
                return False
        with self.lock:
            if not self.valid(generation):
                return False
            if not rows:
                names = {'movie':'电影','tvshow':'电视剧','set':'系列合集','concert':'演唱会','documentary':'纪录片'}
                media = HOME_ROWS.get(cid, (self.filters.get('filter.mediatype','all'),0))[0]
                text = '暂无符合当前筛选条件的影片。请添加媒体或调整筛选条件。'
                selected_genre = self.filters.get('filter.genre') if cid==9999 else None
                if selected_genre:
                    from .genres import genre_label
                    text = '暂无符合“%s”类型及当前筛选条件的影片。请切换类型或重置筛选。' % genre_label(selected_genre)
                elif media == 'continue':text='暂无继续观看记录。'
                elif media in names:text='暂无%s。添加到 Kodi 媒体库后将自动显示。' % names[media]
                self.message(cid, 'empty', text)
            elif not items:
                self.message(cid, 'error', '影片信息无法显示，请返回后重试。')
            else:
                self.message(cid, 'ready', '')
            if cid in HOME_ROWS:
                # Bounded raw metadata only: never retain native ListItems.
                self.home_rows[cid] = list(rows[:HOME_ROWS[cid][1] + (0 if cid == 1002 else 1)])
            return True

    def restore_home(self):
        """Show cached rows immediately; update changed data without blanking."""
        with self.lock:
            if (self.closed.is_set() or self.owner is None
                    or self.owner.getProperty('MFG.Home') != 'true'
                    or not self.home_rows):
                return False
            generation = self.generation
            cached = dict(self.home_rows)
        for cid, rows in cached.items():
            # Native controls may survive the child window. Leave their
            # selection intact; rebind only controls rebuilt by Kodi.
            if rows and self.owner.getControl(cid).size() == 0:
                if not self.populate(cid, rows, generation, on_gui=True):
                    return False
        with self.lock:
            if (self.valid(generation) and not self.home_refresh_pending and
                    (self.home_revision != catalog_cache.revision() or
                     len(self.home_rows) != len(HOME_ROWS))):
                self.home_refresh_pending = True
                self.requests.put((generation,'home_update',{},0))
        return self.valid(generation)

    def _update_home(self, generation):
        revision = catalog_cache.revision()
        complete = True
        try:
            for cid in (1002,1004,1005,1006,1007,1008):
                with self.lock:
                    owner = self.owner if self.valid(generation) else None
                if owner is None or not owner._is_active():
                    complete = False
                    break
                media,limit = HOME_ROWS[cid]
                rows = _home_row_items(media,limit)
                library.prime_textures(rows)
                items = [library.create_list_item(row)[0] for row in rows]
                if any(item is None for item in items):
                    raise RuntimeError('Invalid recommendation')
                if not owner.bind_home_rows(cid,rows,items,generation):
                    complete = False
                    break
            with self.lock:
                if complete and self.valid(generation) and revision == catalog_cache.revision():
                    self.home_revision = revision
                    self.pending = False
                    owner.setProperty('MFG.Loaded','true')
        except Exception as exc:
            # Keep the previous usable recommendations on transient failure.
            log('WALL home update failed: %s' % type(exc).__name__,xbmc.LOGWARNING)
        finally:
            with self.lock:
                if self.valid(generation):
                    self.home_refresh_pending = False

    def run(self):
        monitor = xbmc.Monitor()
        while not self.closed.is_set() and not monitor.abortRequested():
            try:
                generation, kind, filters, offset = self.requests.get(timeout=.1)
            except queue.Empty:
                continue
            if not self.valid(generation):
                continue
            if kind == 'home_update':
                self._update_home(generation)
                continue
            if kind == 'prefetch':
                self._prefetch(generation, filters, offset)
                continue
            if kind == 'playback':
                try:
                    from .playback_state import load_state
                    state = load_state(*filters)
                    with self.lock:
                        owner = self.owner if self.valid(generation) else None
                    if state and owner is not None:
                        owner.queue_playback_state(state, generation, offset)
                except Exception as exc:
                    log('WALL playback state failed: %s' % type(exc).__name__, xbmc.LOGWARNING)
                continue
            started = time.monotonic()
            rows = None
            try:
                # Direct-SQL scanners do not emit Kodi's library update event.
                # Refresh availability in this existing worker, never block onInit.
                if getattr(self.owner, '_has_filter_toolbar', False) and offset == 0:
                    from .db import get_filter_facets
                    facets = get_filter_facets(filters)
                    with self.lock:
                        if self.valid(generation):
                            self.owner.bind_filter_facets(facets, generation)
                elif not getattr(self.owner, '_has_filter_toolbar', False):
                    from .window_handler import _update_filter_availability
                    _update_filter_availability()
                if kind == 'home':
                    # Recent movies first: don't wait for expensive progress aggregation.
                    for cid in (1004, 1002, 1005, 1006, 1007, 1008):
                        if not self.valid(generation):
                            break
                        media, limit = HOME_ROWS[cid]
                        try:
                            rows = _home_row_items(media, limit)
                            self.populate(cid, rows, generation)
                        except Exception as exc:
                            with self.lock:
                                if self.valid(generation):
                                    self.message(cid, 'error', '加载失败，请返回后重试。')
                            log('Home row %d: %s' % (cid, exc), xbmc.LOGERROR)
                else:
                    page_size = min(PAGE_SIZE, self.result_limit-offset) if self.result_limit else PAGE_SIZE
                    rows = self._page_rows(generation, filters, offset, page_size)
                    total = library.get_filtered_count(filters) if offset == 0 else self.total
                    if self.result_limit:total=min(total,self.result_limit)
                    with self.lock:
                        if not self.valid(generation):
                            continue
                        self.owner.setProperty('MFG.ResultTotal', str(total))
                    if not self.populate(9999, rows, generation, offset > 0):
                        with self.lock:
                            if self.valid(generation):
                                self.pending = False
                        continue
                    with self.lock:
                        if self.valid(generation):
                            self.loaded = offset + len(rows)
                            self.total = total if rows else self.loaded
                            self.pending = False
                            self.owner.setProperty('MFG.PageProgress', '已加载 %d / %d 部' % (self.loaded, total))
                            # Kodi addItems() rebinds the entire native panel,
                            # restarting its focus zoom. Keep the next page in
                            # metadata cache until navigation approaches it.
                            self.prefetch_next_page()
                if self.valid(generation):
                    if kind == 'home':
                        with self.lock:
                            if self.valid(generation) and len(self.home_rows) == len(HOME_ROWS):
                                self.home_revision = catalog_cache.revision()
                                self.pending = False
                    xbmcgui.Window(10000).setProperty('MFG.TotalCount', str(library.get_total_count()))
                    self.owner.setProperty('MFG.Loaded', 'true')
                    log('WALL load %s offset=%d %.3fs' % (kind, offset, time.monotonic()-started))
            except Exception as exc:
                log('WALL load failed: %s' % exc, xbmc.LOGERROR)
                with self.lock:
                    if self.valid(generation):
                        self.pending = False
                        self.message(9999, 'error', '媒体库读取失败，请返回后重试。')
            finally:
                rows = None

    def stop(self):
        # Never join a SQL worker on Kodi's GUI callback thread.
        with self.lock:
            self.closed.set()
            self.generation += 1
            self.home_rows.clear()
            self.home_revision = None
            self.home_refresh_pending = False
            self.prefetch_pending = False
            self.prefetched_page = None
            self.prefetched_revision = None
            self.owner = None
