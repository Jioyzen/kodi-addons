"""Independent scanner UI: no Kodi video directory or GUI busy dialog."""
import json
import os
import queue
import threading
import time

import xbmc
import xbmcgui

from window_state import Navigation, item_query, context_query

BACK_ACTIONS = (9, 10, 92)
CONTEXT_ACTION = 117
LIST_ID = 50


class ScannerWindow(xbmcgui.WindowXML):
    def __init__(self, *args, addon=None, initial_query=None, initial_action=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.addon = addon
        self.navigation = Navigation(initial_query)
        self._lock = threading.RLock()
        self._jobs = queue.Queue()
        self._stop = threading.Event()
        self._dirty = threading.Event()
        self._backend = None
        self._history = None
        self._history_signature = None
        self._action_busy = False
        self._initialized = False
        self._initial_action = initial_action
        self._checked = 0.0
        self._started = time.monotonic()

    def onInit(self):
        if self._initialized:
            return  # Preserve page/selection after playback or native dialogs.
        self._initialized = True
        self.setProperty('AIS.AddonId', self.addon.getAddonInfo('id'))
        self.setProperty('AIS.Title', self.addon.getAddonInfo('name'))
        self.setProperty('AIS.Loading', 'true')
        self.setProperty('AIS.Status', '正在加载媒体库…')
        self.getControl(LIST_ID).addItem(xbmcgui.ListItem('正在加载媒体库…', offscreen=True))
        self.setFocusId(LIST_ID)
        xbmc.log('[AI Scanner UI] Window ready in %.3fs' %
                 (time.monotonic() - self._started), xbmc.LOGINFO)
        self._worker = threading.Thread(target=self._work, name='ScannerMenuLoader', daemon=True)
        self._worker.start()
        self._schedule(self.navigation.begin())

    def _signature(self):
        try:
            stat = os.stat(self._backend.HISTORY_FILE)
            return stat.st_mtime_ns, stat.st_size
        except FileNotFoundError:
            return 'absent', 0

    def _schedule(self, generation):
        self._jobs.put((generation, dict(self.navigation.current.query)))

    def _history_snapshot(self):
        signature = self._signature()
        if self._history is None or signature != self._history_signature:
            self._history = self._backend.load_history()
            self._history_signature = signature
        return self._history, signature

    def _work(self):
        try:
            import main
            self._backend = main
            main.UI_REFRESH = self._dirty.set
            # This interpreter is a script, never a native directory resolver.
            main.addon_handle = -1
        except Exception as error:
            self._error(error)
            return
        monitor = xbmc.Monitor()
        while not self._stop.is_set() and not monitor.abortRequested():
            try:
                generation, query = self._jobs.get(timeout=0.25)
            except queue.Empty:
                self._poll()
                continue
            if not self.navigation.accepts(generation):
                continue
            try:
                started = time.monotonic()
                history, signature = self._history_snapshot()
                captured = []
                main.UI_LISTING = lambda rows, title: captured.append((rows, title))
                if query.get('action') == 'view_directory':
                    from path_display import resolve_query
                    resolved = resolve_query(query, history)
                    main.view_directory(resolved.get('dirpath', ''), query.get('group', ''),
                                        query.get('page', 0), history=history)
                else:
                    main.show_main_menu(history=history)
                main.UI_LISTING = None
                if captured:
                    rows, title = captured[-1]
                    if self._apply(generation, rows, title, signature):
                        xbmc.log('[AI Scanner UI] Menu ready in %.3fs; rows=%d' %
                                 (time.monotonic() - started, len(rows)), xbmc.LOGINFO)
                    else:
                        xbmc.log('[AI Scanner UI] Stale menu discarded', xbmc.LOGINFO)
                else:
                    self._apply(generation, [], '目录已移除', signature)
            except Exception as error:
                main.UI_LISTING = None
                self._error(error, generation)
            self._poll()

    def _poll(self):
        if self._stop.is_set() or self._backend is None:
            return
        with self._lock:
            if self.navigation.closed:
                return
            self.setProperty('AIS.ScanBusy', 'true' if self._backend._is_busy() else 'false')
            self.setProperty('AIS.StopLabel', self._backend.L(32426))
            now = time.monotonic()
            if now - self._checked >= 1:
                self._checked = now
                if self._history is not None and self._signature() != self._history_signature:
                    self._dirty.set()  # Service updates use another interpreter.
            if self._dirty.is_set() and not self._action_busy:
                self._dirty.clear()
                self.navigation.current.selected = self.getControl(LIST_ID).getSelectedPosition()
                item = self.getControl(LIST_ID).getSelectedItem()
                self.navigation.current.selected_url = item.getProperty('AIS.Url') if item else ''
                self.navigation.invalidate_cache()
                self._schedule(self.navigation.begin())

    def _apply(self, generation, rows, title, signature):
        with self._lock:
            if not self.navigation.accepts(generation):
                return False
            frame = self.navigation.current
            frame.rows, frame.title, frame.signature = rows, title, signature
            self._render(frame)
            if self._initial_action:
                query, self._initial_action = self._initial_action, None
                self._action(query)
            return True

    def _render(self, frame):
        control = self.getControl(LIST_ID)
        control.reset()
        rows = frame.rows or []
        if rows:
            control.addItems([item for _, item, _ in rows])
            selected = next((i for i, (url, _, _) in enumerate(rows)
                             if url == frame.selected_url), frame.selected)
            control.selectItem(max(0, min(selected, len(rows) - 1)))
        else:
            control.addItem(xbmcgui.ListItem('此目录暂无媒体文件', offscreen=True))
        self.setProperty('AIS.Title', frame.title or self.addon.getAddonInfo('name'))
        if not frame.query.get('action'):
            status = '%d 个媒体目录' % sum(bool(folder) for _, _, folder in rows)
        else:
            status = '%d 项' % len(rows)
        self.setProperty('AIS.Status', status)
        self.setProperty('AIS.Loading', 'false')

    def _error(self, error, generation=None):
        xbmc.log('[AI Scanner UI] Operation failed: ' + type(error).__name__, xbmc.LOGERROR)
        with self._lock:
            if self.navigation.closed or (generation is not None and not self.navigation.accepts(generation)):
                return
            self.setProperty('AIS.Loading', 'false')
            self.setProperty('AIS.Status', '加载失败，请返回后重试。已有媒体库数据保持不变。')

    def onClick(self, control_id):
        if control_id == 61:
            if self._backend is not None:
                self._backend.stop_scan()
            return
        if control_id != LIST_ID:
            return
        with self._lock:
            if self.navigation.closed:
                return
            control = self.getControl(LIST_ID)
            item = control.getSelectedItem()
            url = item.getProperty('AIS.Url') if item is not None else ''
            if not url:
                return
            query = item_query(url)
            selected = control.getSelectedPosition()
            if item.getProperty('AIS.Folder') == 'true':
                generation = self.navigation.enter(query, selected, url)
                control.reset()
                control.addItem(xbmcgui.ListItem('正在加载…', offscreen=True))
                self.setProperty('AIS.Loading', 'true')
                self.setProperty('AIS.Title', item.getLabel())
                self.setProperty('AIS.Status', '正在加载…')
                self._schedule(generation)
            else:
                self._action(query)

    def _action(self, query):
        if self._backend is None or self.navigation.closed:
            return
        if query.get('action') == 'stop_scan':
            self._backend.stop_scan()
            return
        if self._action_busy:
            self._backend.notify_task_busy()
            return
        self._action_busy = True
        self.navigation.current.selected = self.getControl(LIST_ID).getSelectedPosition()
        item = self.getControl(LIST_ID).getSelectedItem()
        self.navigation.current.selected_url = item.getProperty('AIS.Url') if item else ''
        def run():
            try:
                from path_display import resolve_query
                history = self._backend.load_history()
                self._backend.router(params=resolve_query(query, history))
            except Exception as error:
                self._error(error)
            finally:
                self._action_busy = False
                # Playback does not need a full list rebuild on return.
                if query.get('action') != 'play_file':
                    self._dirty.set()
        threading.Thread(target=run, name='ScannerUserAction', daemon=True).start()

    def onAction(self, action):
        action_id = action.getId()
        if action_id in BACK_ACTIONS:
            with self._lock:
                frame = self.navigation.back()
                if frame is None:
                    self._stop.set()
                    self.close()
                elif frame.rows is not None and frame.signature == self._history_signature and not self._dirty.is_set():
                    self._render(frame)  # Back restores cached rows and focus immediately.
                else:
                    self.setProperty('AIS.Loading', 'true')
                    self.setProperty('AIS.Status', '正在加载…')
                    self._schedule(self.navigation.generation)
            return
        if action_id == CONTEXT_ACTION:
            item = self.getControl(LIST_ID).getSelectedItem()
            if item is None:
                return
            try:
                commands = json.loads(item.getProperty('AIS.Context') or '[]')
                if not commands:
                    return
                chosen = xbmcgui.Dialog().contextmenu([label for label, _ in commands])
                if chosen >= 0:
                    self._action(context_query(commands[chosen][1]))
            except (ValueError, TypeError, IndexError) as error:
                self._error(error)

    def cleanup(self):
        with self._lock:
            self.navigation.closed = True
            self.navigation.begin()
            self._stop.set()
        worker = getattr(self, '_worker', None)
        if worker is not None:
            worker.join(timeout=0.5)
