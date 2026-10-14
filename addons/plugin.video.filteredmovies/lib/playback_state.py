"""Update one surviving poster's playback fields without rebinding its list."""
from contextlib import closing
from .tv_catalog import rpc
from .movie_catalog import number


def show_partial(show_id):
    from .db import _conn
    with closing(_conn()) as conn:
        row=conn.execute('SELECT SUM(CASE WHEN totalTimeInSeconds>0 '
            'THEN CAST(resumeTimeInSeconds AS REAL)/totalTimeInSeconds ELSE 0 END) '
            'FROM episode_view WHERE idShow=? AND resumeTimeInSeconds>0', (int(show_id),)).fetchone()
    return number(row[0] if row else 0)


def load_state(media_type, media_id, query=rpc, partial=show_partial):
    media_id=int(media_id)
    if media_type=='movie':
        result=query('VideoLibrary.GetMovieDetails',{'movieid':media_id,
            'properties':['resume','playcount','runtime']}).get('moviedetails')
        if not result:return None
        resume=result.get('resume') or {}
        total=number(resume.get('total')) or number(result.get('runtime'))
        position=min(number(resume.get('position')),total) if total else 0
        percent=min(100,int(100*position/total)) if total else 0
        return {'type':media_type,'id':media_id,'playcount':int(number(result.get('playcount'))),
            'resume':{'position':position,'total':total},'percent':percent}
    if media_type=='tvshow':
        result=query('VideoLibrary.GetTVShowDetails',{'tvshowid':media_id,
            'properties':['episode','watchedepisodes','lastplayed','playcount']}).get('tvshowdetails')
        if not result:return None
        total=number(result.get('episode'));watched=number(result.get('watchedepisodes'))
        fraction=partial(media_id)
        percent=min(100,int(100*(watched+fraction)/total)) if total else 0
        if total and percent==0 and (fraction>0 or not watched and result.get('lastplayed')):percent=1
        if percent==100:percent=0
        return {'type':media_type,'id':media_id,'playcount':int(number(result.get('playcount'))),
            'percent':percent}
    return None


def apply_state(item, state):
    """Called on the GUI callback; identity checks discard stale row updates."""
    tag=item.getVideoInfoTag()
    if tag.getMediaType()!=state['type'] or tag.getDbId()!=state['id']:return False
    tag.setPlaycount(state['playcount'])
    if 'resume' in state:
        tag.setResumePoint(state['resume']['position'],state['resume']['total'])
    item.setProperty('SkinPercentPlayed',str(state['percent']))
    return True
