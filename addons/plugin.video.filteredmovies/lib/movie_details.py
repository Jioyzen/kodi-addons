"""Movie hero page; shares cast rendering/cache retries with the TV detail page."""
import json
import queue
import threading
import weakref
import xbmc
import xbmcgui
from .common import ADDON_PATH,log,notification
from . import movie_catalog as catalog,video_library as library
from .tv_details import TvDetailsWindow
from .detail_playback import read_playback


def open_movie(movie_id,parent=None,seed=None):
    parent=parent or xbmcgui.Window(xbmcgui.getCurrentWindowId())
    if parent.getProperty('MFG.ChildWindow')=='true':return False
    capture=getattr(parent,'_capture_navigation_state',None)
    if capture:capture()
    parent.setProperty('MFG.ChildWindow','true')
    child=None
    try:
        child=MovieDetailsWindow('Custom_5117_MovieDetails.xml',ADDON_PATH,'Default','1080i')
        child.movie_id=int(movie_id);child.seed=seed or {}
        child.doModal()
        return True
    finally:
        try:
            if child:child.cleanup()
        finally:
            parent.clearProperty('MFG.ChildWindow')
            restore=getattr(parent,'_restore_navigation_state',None)
            if restore:restore()


class MovieDetailsWindow(TvDetailsWindow):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.movie=None
        self._playback_movie=None
        self._loading=False
        self._refresh_pending=False
        self._return_focus=201
        self.favourite=None

    def onInit(self):
        self.window_id=xbmcgui.getCurrentWindowId()
        if hasattr(self,'worker'):
            if self.movie:
                self._bind(self.movie)
                self._apply_results()
                if self._navigation_state:self._restore_navigation_state()
                else:
                    focus=self._safe_focus(self._return_focus)
                    if self.getFocusId()!=focus:self.setFocusId(focus)
            self._queue_progress()
            return
        self.setProperty('MFG.Tv.CastFocus','false')
        self.setProperty('MFG.Movie.State','loading')
        known,self._playback_movie=read_playback('movie',self.movie_id)
        self._header(dict(getattr(self,'seed',{}),**(self._playback_movie or {})))
        self.setProperty('MFG.Movie.Message','')
        self.getControl(201).setEnabled(known)
        self.worker=threading.Thread(target=self._worker,name='PosterWallMovie',daemon=True)
        self.worker.start()
        self.library_monitor=_MovieMonitor(self)
        self._request('load')
        self.setFocusId(201)

    def _active(self):
        return not self.closed.is_set() and xbmcgui.getCurrentWindowId()==self.window_id

    @staticmethod
    def _safe_focus(focus):
        return focus if focus in (201,202,203,303) else 201

    def _restore_navigation_state(self):
        if self._navigation_state:
            focus,position=self._navigation_state
            self._navigation_state=(self._safe_focus(focus),position)
        super()._restore_navigation_state()

    def _request(self,mode,argument=None):
        if self.closed.is_set():return
        self.generation+=1
        self._loading=True
        if mode=='load':
            self.setProperty('MFG.Movie.State','loading')
        if mode=='watched' or mode=='load' and not self._playback_movie:
            self.getControl(201).setEnabled(False)
        self.jobs.put((self.generation,mode,argument,0))

    def _queue_progress(self):
        if self.closed.is_set():return
        if self._loading:self._refresh_pending=True
        else:self._request('refresh')

    def _worker(self):
        while not self.closed.is_set():
            try:generation,mode,argument,_=self.jobs.get(timeout=.2)
            except queue.Empty:continue
            if self.closed.is_set():return
            if generation!=self.generation:continue
            try:
                if mode in ('refresh','watched') and self.closed.wait(.15):return
                if mode=='watched':catalog.set_watched(self.movie_id,bool(argument))
                data=catalog.load_movie(self.movie_id)
                library.prime_textures([data])
                try:
                    data['_favourites']=catalog.favourite_entries(catalog.rpc('Favourites.GetFavourites',{
                        'properties':['path','windowparameter','window','thumbnail']}))
                except RuntimeError:data['_favourites']=[]
                error=False
            except Exception as exc:
                log('Movie details request failed: %s'%type(exc).__name__,xbmc.LOGERROR)
                data,error={},True
            if not self.closed.is_set() and generation==self.generation:
                self.results.put((generation,mode,data,[],error))
                xbmc.executebuiltin('SendClick(%d,900)'%self.window_id)

    def _header(self,movie):
        art=movie.get('art') or {}
        state=catalog.progress(movie)
        library.prime_textures([movie])
        values={'Title':movie.get('title') or '电影信息','Plot':movie.get('plot') or '暂无剧情简介',
                'Fanart':self._image(art.get('fanart')),'Logo':self._image(art.get('clearlogo')),
                'Meta':catalog.metadata(movie),'Technical':catalog.technical(movie),
                'PlayLabel':('继续播放' if state['resuming'] else '播放') if self.movie or self._playback_movie else '',
                'Progress':state['percent'],'HasProgress':'true' if state['resuming'] else 'false',
                'Remaining':('剩余 '+catalog.duration(state['remaining'])) if state['resuming'] else '',
                'Watched':'true' if movie.get('playcount',0) else 'false'}
        for key,value in values.items():self.setProperty('MFG.Movie.'+key,str(value))

    def _bind(self,movie):
        self.movie=movie;self._playback_movie=movie;self.show_data={'show':movie};self.season=None
        self._header(movie)
        self._bind_actors()
        self._favourite_state(movie.get('_favourites',[]))
        self.setProperty('MFG.Movie.State','ready')
        self.setProperty('MFG.Movie.Message','')
        self.getControl(201).setEnabled(True)
        if self._initial_focus:
            self._initial_focus=False
            self.setFocusId(201)

    def _favourite_state(self,entries):
        entries=catalog.favourite_entries({'favourites':entries})
        path='videodb://movies/titles/%d'%self.movie_id
        actual_file=(self.movie or {}).get('file')
        self.favourite=next((entry for entry in entries if
            str(entry.get('path') or entry.get('windowparameter') or '').rstrip('/')==path or
            actual_file and entry.get('path')==actual_file),None)
        self.setProperty('MFG.Movie.Favourite','true' if self.favourite else 'false')

    def _apply_results(self):
        if not self._active():return
        while True:
            try:generation,mode,data,_items,error=self.results.get_nowait()
            except queue.Empty:return
            if generation!=self.generation:continue
            if mode!='portraits':self._loading=False
            if error:
                self.setProperty('MFG.Movie.State','error')
                self.setProperty('MFG.Movie.Message','信息暂时无法加载，点击播放按钮重试。')
                self.setProperty('MFG.Movie.PlayLabel','重试')
                self.getControl(201).setEnabled(True)
            elif mode=='portraits':
                panel=self.getControl(303)
                for position,path in data:
                    if position<panel.size():panel.getListItem(position).setArt({'portrait':path})
            else:self._bind(data)
            if mode!='portraits' and self._refresh_pending:
                self._refresh_pending=False
                if not error:self._request('refresh')

    def _play_movie(self,from_start=False):
        movie=self.movie or self._playback_movie
        if not movie:return
        try:
            self._return_focus=self.getFocusId()
            catalog.play(self.movie_id,from_start or not catalog.progress(movie)['resuming'])
        except RuntimeError:notification('无法播放此影片，请检查媒体来源。')

    def _toggle_favourite(self):
        if not self.movie:return
        if self.favourite:
            payload={k:v for k,v in self.favourite.items() if k in ('title','type','path','window','windowparameter','thumbnail')}
        else:
            payload={'title':self.movie['title'],'type':'media','path':'videodb://movies/titles/%d'%self.movie_id,
                     'thumbnail':self.movie.get('art',{}).get('poster','')}
        try:
            catalog.rpc('Favourites.AddFavourite',payload)
            entries=catalog.favourite_entries(catalog.rpc('Favourites.GetFavourites',{'properties':['path','windowparameter','window','thumbnail']}))
            self._favourite_state(entries)
        except RuntimeError:notification('收藏状态未能保存，请重试。')

    def _watched(self):
        if not self.movie or self._loading:return
        flag=0 if self.movie.get('playcount',0) else 1
        if xbmcgui.Dialog().yesno('观看状态','将《%s》标记为%s，并清除续播进度？'%(self.movie['title'],'已观看' if flag else '未观看')):
            self._request('watched',flag)

    def onClick(self,cid):
        if cid==900:self._apply_results()
        elif cid==201:
            if self.getProperty('MFG.Movie.State')=='error':self._request('load')
            else:self._play_movie()
        elif cid==202:self._toggle_favourite()
        elif cid==203:self._watched()
        elif cid==303:
            pos=self.getControl(303).getSelectedPosition()
            if 0<=pos<len(self.actors):
                from .actor_details import open_actor
                open_actor(self.actors[pos],self.movie,self)

    def onFocus(self,cid):
        self.setProperty('MFG.Tv.CastFocus','true' if cid==303 else 'false')

    def onAction(self,action):
        if action.getId() in (1,2,3,4) and self._active():self._navigation_state=None
        if action.getId() in (9,10,92):self.close()
        elif action.getId()==117 and self.movie and self.getFocusId()==201:
            options=['继续播放','从头播放'] if catalog.progress(self.movie)['resuming'] else ['从头播放']
            selected=xbmcgui.Dialog().select('播放方式',options)
            if selected>=0:self._play_movie(options[selected]=='从头播放')


class _MovieMonitor(xbmc.Monitor):
    def __init__(self,owner):
        super().__init__();self.owner=weakref.ref(owner)

    def onNotification(self,sender,method,data):
        owner=self.owner()
        if owner is None or owner.closed.is_set():return
        if method=='Player.OnStop':owner._queue_progress()
        elif method=='VideoLibrary.OnUpdate':
            try:
                payload=json.loads(data)
                item=payload.get('item',{}) if isinstance(payload,dict) else {}
            except (ValueError,TypeError):return
            if not isinstance(item,dict):return
            if item.get('type')=='movie' and item.get('id')==owner.movie_id:owner._queue_progress()
