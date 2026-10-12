from . import api_utils
from . import get_imdb_id

HEADERS = (
    ('User-Agent', 'Kodi Movie scraper by Team Kodi'),
    ('Accept', 'application/json'),
    ('trakt-api-key', '5f2dc73b6b11c2ac212f5d8b4ec8f3dc4b727bb3f026cd254d89eda997fe64ae'),
    ('trakt-api-version', '2'),
    ('Content-Type', 'application/json'),
)

def get_trakt_url(settings=None):
    try:
        base = ""
        if settings:
            base = settings.getSettingString('trakt_base_url')
        if not base:
            base = 'api.trakt.tv'
        if not base.startswith('http'):
            base = 'https://' + base
        return base + '/movies/{}'
    except:
        return 'https://api.trakt.tv/movies/{}'

def parse_movie_response(responses):
    movie_info = responses.get('trakt_rating')
    result = {}
    if(movie_info):
        if 'votes' in movie_info and 'rating' in movie_info:
            result['ratings'] = {'trakt': {'votes': int(movie_info['votes']), 'rating': float(movie_info['rating'])}}
        elif 'rating' in movie_info:
            result['ratings'] = {'trakt': {'rating': float(movie_info['rating'])}}
    return result

def get_trakt_ratinginfo(uniqueids, settings=None):
    imdb_id = get_imdb_id(uniqueids)
    result = {}
    url = get_trakt_url(settings).format(imdb_id)
    params = {'extended': 'full'}
    try:
        movie_info = api_utils.get(url, params=params, headers=dict(HEADERS)).json()
    except:
        movie_info = {}
    
    if(movie_info):
        if 'votes' in movie_info and 'rating' in movie_info:
            result['ratings'] = {'trakt': {'votes': int(movie_info['votes']), 'rating': float(movie_info['rating'])}}
        elif 'rating' in movie_info:
            result['ratings'] = {'trakt': {'rating': float(movie_info['rating'])}}
    return result
