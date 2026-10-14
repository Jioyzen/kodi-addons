"""Apply bounded recommendation differences without clearing the home page."""
from .movie_catalog import number
from .playback_state import apply_state


def row_key(row):
    media=row.get('media_type','movie')
    if media=='home_more':return ('more',row.get('view_all'))
    media='movie' if media in ('concert','documentary') else media
    return (media,int(number(row.get({'movie':'movieid','tvshow':'tvshowid','set':'setid','episode':'episodeid'}.get(media,'')))))


def playback_fields(row):
    media,identity=row_key(row)
    if media not in ('movie','episode','tvshow','set') or not identity:return None
    state={'type':media,'id':identity,'playcount':int(number(row.get('playcount'))),'percent':0}
    if media in ('movie','episode'):
        resume=row.get('resume') or {}
        total=number(resume.get('total')) or number(row.get('runtime'))
        position=min(number(resume.get('position')),total) if total else 0
        state['resume']={'position':position,'total':total}
        state['percent']=min(100,int(100*position/total)) if total else 0
    else:
        total=number(row.get('episode' if media=='tvshow' else 'total'))
        watched=number(row.get('watchedepisodes' if media=='tvshow' else 'watched'))
        partial=number(row.get('partial_progress'))
        percent=min(100,int(100*(watched+partial)/total)) if total else 0
        if total and percent==0 and (partial>0 or (not watched and row.get('lastplayed') if media=='tvshow' else watched>0)):percent=1
        state['percent']=0 if percent==100 else percent
        if media=='set':state['playcount']=int(bool(total and watched>=total))
    return state


def item_key(item):
    more=item.getProperty('MFG.ViewAll')
    if more:return ('more',more)
    tag=item.getVideoInfoTag()
    return (tag.getMediaType(),tag.getDbId())


def sync_row(panel, previous, rows, items):
    """GUI callback only; preserve the selected identity if a row reorders."""
    old_keys=[row_key(row) for row in previous]
    new_keys=[row_key(row) for row in rows]
    if old_keys==new_keys and panel.size()==len(items):
        for pos,(old,row,template) in enumerate(zip(previous,rows,items)):
            item=panel.getListItem(pos)
            state=playback_fields(row)
            if state is not None and state!=playback_fields(old):apply_state(item,state)
            art={}
            for key in ('poster','thumb','fanart','landscape','clearlogo'):
                value=template.getArt(key)
                if value!=item.getArt(key):art[key]=value
            if art:item.setArt(art)
            if old.get('title')!=row.get('title'):
                item.setLabel(row.get('title',''))
                item.getVideoInfoTag().setTitle(row.get('title',''))
            for field,method in (('year','setYear'),('rating','setRating'),('plot','setPlot')):
                if old.get(field)!=row.get(field) and field in row:
                    getattr(item.getVideoInfoTag(),method)(row[field])
        return False
    position=max(0,panel.getSelectedPosition()) if panel.size() else 0
    key=item_key(panel.getListItem(position)) if panel.size() and position<panel.size() else None
    target=new_keys.index(key) if key in new_keys else min(position,max(0,len(items)-1))
    panel.reset()
    panel.addItems(items)
    if target:panel.selectItem(target)
    return True
