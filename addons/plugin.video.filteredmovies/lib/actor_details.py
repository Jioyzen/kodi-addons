"""Independent actor page; background metadata cannot delay season navigation."""
import queue
import threading
import xbmc
import xbmcgui
from .common import ADDON_PATH, log, notification
from . import person_catalog, video_library as library


def open_actor(actor, show, parent):
    if parent.getProperty('MFG.ChildWindow') == 'true':
        return
    parent._capture_navigation_state()
    parent.setProperty('MFG.ChildWindow', 'true')
    child = None
    try:
        child = ActorWindow('Custom_5116_Actor.xml', ADDON_PATH, 'Default', '1080i')
        child.actor, child.show = actor, show
        child.doModal()
    finally:
        try:
            if child:
                child.cleanup()
        finally:
            parent.clearProperty('MFG.ChildWindow')
            parent._restore_navigation_state()


class ActorWindow(xbmcgui.WindowXML):
    def __init__(self, *args, **kwargs):
        self.closed = threading.Event()
        self.results = queue.Queue()
        self.works = []
        self.payload = None
        self.return_position = 0
        self._return_applied = True

    def onInit(self):
        self.window_id = xbmcgui.getCurrentWindowId()
        if hasattr(self, 'worker'):
            if self.payload:
                self._bind(self.payload)
            self._apply_results()
            return
        self.setProperty('MFG.Actor.Name', self.actor['name'])
        candidates = self.actor.get('thumbnail_candidates') or [self.actor.get('thumbnail')]
        library.prime_textures([{'cast': [{'thumbnail': url} for url in candidates if url]}])
        self.setProperty('MFG.Actor.Portrait', library._resolve_artwork(candidates))
        self.setProperty('MFG.Actor.Bio', '正在加载人物资料…')
        self.setProperty('MFG.Actor.State', 'loading')
        self.worker = threading.Thread(target=self._load, name='PosterWallActor', daemon=True)
        self.worker.start()
        self.setFocusId(200)

    def _deliver(self, data):
        if self.closed.is_set():
            return
        library.prime_textures(data['works'] + [{'art': {'portrait': data.get('portrait', '')}}])
        if self.closed.is_set():
            return
        self.results.put(data)
        xbmc.executebuiltin('SendClick(%d,900)' % self.window_id)

    def _load(self):
        try:
            local = person_catalog.local_works(self.actor,cancelled=self.closed.is_set)
        except Exception as exc:
            log('Local actor works unavailable: %s' % type(exc).__name__, xbmc.LOGWARNING)
            local = []
        self._deliver({'name': self.actor['name'], 'biography': '正在加载人物简介…',
                       'portrait': self.actor.get('thumbnail', ''), 'works': local, 'pending': True})
        if self.closed.is_set():
            return
        try:
            data = person_catalog.load_person(self.actor, self.show, local, cancelled=self.closed.is_set)
        except Exception as exc:
            log('Actor metadata unavailable: %s' % type(exc).__name__, xbmc.LOGWARNING)
            data = {'name': self.actor['name'], 'portrait': self.actor.get('thumbnail', ''),
                    'biography': '人物资料暂时无法加载。当前展示媒体库中的参演作品。', 'works': local}
        self._deliver(data)

    def _bind(self, data):
        panel = self.getControl(310)
        native_count = xbmc.getInfoLabel('Container(310).NumItems')
        unchanged = (self.works == data['works'] and panel.size() == len(data['works'])
                     and native_count.isdigit() and int(native_count)==len(data['works']))
        self.payload = data
        self.works = data['works']
        self.setProperty('MFG.Actor.HasWorks', 'true' if self.works else 'false')
        for key, value in {'Name': data['name'], 'OriginalName': data.get('originalname', ''),
                           'Bio': data.get('biography', ''),
                           'Meta': '  /  '.join(v for v in (data.get('birthday'), data.get('birthplace')) if v),
                           'Portrait': library._resolve_artwork([data.get('portrait')] +
                                (self.actor.get('thumbnail_candidates') or [self.actor.get('thumbnail')])),
                           'State': 'loading' if data.get('pending') else 'ready',
                           'Message': ''}.items():
            self.setProperty('MFG.Actor.' + key, value)
        if unchanged:
            return
        items = []
        for work in self.works:
            li = xbmcgui.ListItem(work.get('title', ''), offscreen=True)
            li.setContentLookup(False)
            li.setArt({'poster': library._resolve_artwork([work.get('art', {}).get('poster')])})
            li.setProperty('MFG.Actor.Year', str(work.get('year') or ''))
            li.setProperty('MFG.Actor.Rating', '%.1f' % work['rating'] if work.get('rating') else '')
            li.setProperty('MFG.Actor.Local', '已入库' if work.get('local') else '')
            items.append(li)
        old = panel.getSelectedPosition()
        panel.reset()
        panel.addItems(items)
        panel.selectItem(max(0, min(old, len(items) - 1)))

    def _apply_results(self):
        if self.closed.is_set() or xbmcgui.getCurrentWindowId()!=self.window_id:
            return
        while True:
            try:data=self.results.get_nowait()
            except queue.Empty:return
            self._bind(data)

    def _capture_navigation_state(self):
        self.return_position = max(0, self.getControl(310).getSelectedPosition())
        self._return_applied = False

    def _restore_navigation_state(self):
        if self._return_applied or self.closed.is_set() or xbmcgui.getCurrentWindowId()!=self.window_id:
            return
        self._return_applied = True
        panel = self.getControl(310)
        if panel.size():
            target=max(0,min(self.return_position,panel.size()-1))
            if panel.getSelectedPosition()!=target:
                panel.selectItem(target)
            if self.getFocusId()!=310:self.setFocusId(310)

    def onClick(self, cid):
        if cid == 200:
            self.close()
        elif cid == 900:
            self._apply_results()
        elif cid == 310:
            position = self.getControl(310).getSelectedPosition()
            if not 0 <= position < len(self.works):
                return
            work = self.works[position]
            if work.get('local') and work['media_type'] == 'tvshow':
                from .tv_details import open_tvshow
                open_tvshow(work['tvshowid'], parent=self, seed=work)
            elif work.get('local'):
                from .movie_details import open_movie
                open_movie(work['movieid'],parent=self,seed=work)
            else:
                xbmcgui.Dialog().textviewer(work.get('title', ''),
                    (work.get('plot') or '暂无作品简介') + '\n\n此作品尚未加入本地媒体库。')

    def onAction(self, action):
        if action.getId() in (1,2,3,4) and xbmcgui.getCurrentWindowId()==self.window_id:
            self._return_applied = True
        if action.getId() in (9, 10, 92):
            self.close()
        elif action.getId() == 11 and self.payload:
            xbmcgui.Dialog().textviewer(self.actor['name'], self.payload.get('biography') or '暂无人物简介')

    def close(self):
        self.closed.set()
        super().close()

    def cleanup(self):
        self.closed.set()
        if hasattr(self, 'worker'):
            self.worker.join(timeout=.1)
