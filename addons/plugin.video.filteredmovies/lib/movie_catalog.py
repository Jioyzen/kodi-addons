"""Movie details and playback state from Kodi's local metadata only."""
import math
from .tv_catalog import rpc,resolution,favourite_entries

PROPERTIES = ['title','originaltitle','plot','year','rating','genre','country','mpaa',
              'runtime','resume','playcount','art','cast','uniqueid','streamdetails','file','tag']


def number(value):
    try:
        value=float(value)
        return max(0,value) if math.isfinite(value) else 0
    except (TypeError,ValueError,OverflowError):return 0


def duration(seconds):
    minutes=int(number(seconds)//60)
    if not minutes:return ''
    hours,minutes=divmod(minutes,60)
    return ('%d 小时'%hours if hours else '')+(' %d 分钟'%minutes if hours and minutes else '%d 分钟'%minutes if minutes else '')


def progress(movie):
    resume=movie.get('resume') or {}
    total=number(resume.get('total')) or number(movie.get('runtime'))
    position=min(number(resume.get('position')),total) if total else 0
    return {'position':position,'total':total,'percent':round(100*position/total) if total else 0,
            'remaining':max(0,total-position),'resuming':bool(position>0 and position<total)}


def load_movie(movie_id,query=rpc):
    movie_id=int(movie_id)
    if movie_id<=0:raise ValueError('A local movie ID is required')
    result=query('VideoLibrary.GetMovieDetails',{'movieid':movie_id,'properties':PROPERTIES})
    movie=result.get('moviedetails')
    if not movie:raise RuntimeError('Movie information is unavailable')
    movie=dict(movie,movieid=movie_id,media_type='movie')
    movie['cast']=sorted(movie.get('cast') or [],key=lambda c:c.get('order',0))[:80]
    return movie


def set_watched(movie_id,watched,query=rpc):
    """A manual watched-state change also removes the native resume bookmark."""
    query('VideoLibrary.SetMovieDetails',{'movieid':int(movie_id),
        'playcount':1 if watched else 0,'resume':{'position':0.0,'total':0.0}})


def play(movie_id,from_start=False,query=rpc):
    query('Player.Open',{'item':{'movieid':int(movie_id)},'options':{'resume':not from_start}})


def metadata(movie):
    from .window_handler import _COUNTRY_MAP
    from .regions import region_code
    countries=' · '.join(_COUNTRY_MAP.get(region_code(value),str(value)) for value in movie.get('country') or [])
    return '  /  '.join(str(value) for value in (movie.get('mpaa'),movie.get('year') or '',
        duration(progress(movie)['total']),countries,' · '.join(movie.get('genre') or [])) if value)


def technical(movie):
    labels=[]
    quality=resolution(movie)
    if quality:labels.append(quality)
    video=(movie.get('streamdetails') or {}).get('video') or []
    hdr={str(row.get('hdrtype') or '').lower() for row in video}
    if 'dolbyvision' in hdr:labels.append('杜比视界')
    elif 'hdr10' in hdr:labels.append('HDR10')
    elif 'hlg' in hdr:labels.append('HLG')
    rating=number(movie.get('rating'))
    if rating:labels.append('评分 %.1f'%rating)
    return '   /   '.join(labels)
