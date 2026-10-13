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
        self.home_rows = {}
        self.home_revision = None
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

    def populate(self, cid, rows, generation, append=False):
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
            with self.lock:
                if not self.valid(generation) or not self.owner._is_active():
                    return False
                panel = self.owner.getControl(cid)
                if not append:
                    panel.reset()
                panel.addItems(items)
        with self.lock:
            if not self.valid(generation):
                return False
            if not rows:
                names = {'movie':'电影','tvshow':'电视剧','set':'系列合集','concert':'演唱会','documentary':'纪录片'}
                media = HOME_ROWS.get(cid, (self.filters.get('filter.mediatype','all'),0))[0]
                text = '暂无符合当前筛选条件的影片。请添加媒体或调整筛选条件。'
                if media == 'continue':text='暂无继续观看记录。'
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
        """Rebind unchanged recommendations on return from search, with no
        loading/empty phase. Called only from the window's onInit callback."""
        with self.lock:
            if (self.closed.is_set() or self.owner is None
                    or self.owner.getProperty('MFG.Home') != 'true'
                    or self.home_revision is None
                    or self.home_revision != catalog_cache.revision()
                    or len(self.home_rows) != len(HOME_ROWS)):
                return False
            generation = self.generation
            cached = dict(self.home_rows)
        for cid, rows in cached.items():
            # Native controls may survive the child window. Leave their
            # selection intact; rebind only controls rebuilt by Kodi.
            if rows and self.owner.getControl(cid).size() == 0:
                if not self.populate(cid, rows, generation):
                    return False
        return self.valid(generation)

    def run(self):
        monitor = xbmc.Monitor()
        while not self.closed.is_set() and not monitor.abortRequested():
            try:
                generation, kind, filters, offset = self.requests.get(timeout=.1)
            except queue.Empty:
                continue
            if not self.valid(generation):
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
                    rows = library.jsonrpc_get_items(filters, page_size, offset)
                    total = library.get_filtered_count(filters) if offset == 0 else self.total
                    if self.result_limit:total=min(total,self.result_limit)
                    with self.lock:
                        if not self.valid(generation):
                            continue
                        self.owner.setProperty('MFG.ResultTotal', str(total))
                    if not self.populate(9999, rows, generation, offset > 0):
                        continue
                    with self.lock:
                        if self.valid(generation):
                            self.loaded = offset + len(rows)
                            self.total = total if rows else self.loaded
                            self.pending = False
                            self.owner.setProperty('MFG.PageProgress', '已加载 %d / %d 部' % (self.loaded, total))
                            # Fill one extra page before navigation reaches the end.
                            if self.loaded < min(total, PAGE_SIZE * 2):
                                self.next_page()
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
            self.owner = None
