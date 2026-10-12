from . import api_utils
from urllib.parse import quote


API_KEY = '384afe262ee0962545a752ff340e3ce4'

def get_api_url(settings=None):
    try:
        base = ""
        if settings:
            base = settings.getSettingString('fanart_base_url')
        if not base:
            base = 'webservice.fanart.tv'
        if not base.startswith('http'):
            base = 'https://' + base
        return base + '/v3/movies/{}'
    except:
        return 'https://webservice.fanart.tv/v3/movies/{}'

ARTMAP = {
    'movielogo': 'clearlogo',
    'hdmovielogo': 'clearlogo',
    'hdmovieclearart': 'clearart',
    'movieart': 'clearart',
    'moviedisc': 'discart',
    'moviebanner': 'banner',
    'moviethumb': 'landscape',
    'moviebackground': 'fanart',
    'movieposter': 'poster'
}

HEADERS = (
    ('User-Agent', 'Kodi Movie scraper by Team Kodi'),
    ('api-key', API_KEY),
)

def get_details(uniqueids, clientkey, language, set_tmdbid, settings=None):
    media_id = _get_mediaid(uniqueids)
    if not media_id:
        return {}

    movie_data = _get_data(media_id, clientkey, settings)
    movieset_data = _get_data(set_tmdbid, clientkey, settings) if set_tmdbid else None
    if not movie_data and not movieset_data:
        return {}

    movie_art = {}
    movieset_art = {}
    if movie_data:
        movie_art = _parse_data(movie_data, language, settings=settings)
    if movieset_data:
        movieset_art = _parse_data(movieset_data, language, settings=settings)
        movieset_art = {'set.' + key: value for key, value in movieset_art.items()}

    available_art = movie_art
    available_art.update(movieset_art)

    return {'available_art': available_art}

def _get_mediaid(uniqueids):
    for source in ('tmdb', 'imdb', 'unknown'):
        if source in uniqueids:
            return uniqueids[source]

def _get_data(media_id, clientkey, settings=None):
    headers = dict(HEADERS)
    if clientkey:
        headers['client-key'] = clientkey
    fanarttv_url = get_api_url(settings).format(media_id)
    try:
        resp = api_utils.get(fanarttv_url, headers=headers).json()
        return resp
    except:
        return {}

def _parse_data(data, language, language_fallback='en', settings=None):
    try:
        proxy = ""
        if settings:
            proxy = settings.getSettingString('image_proxy_prefix')
        if not proxy:
            proxy = 'https://wsrv.nl/?url='
    except:
        proxy = 'https://wsrv.nl/?url='

    result = {}
    for arttype, artlist in data.items():
        if arttype not in ARTMAP:
            continue
        for image in artlist:
            image_lang = _get_imagelanguage(arttype, image)
            if image_lang and image_lang != language and image_lang != language_fallback:
                continue

            generaltype = ARTMAP[arttype]
            if generaltype == 'poster' and not image_lang:
                generaltype = 'keyart'
            if artlist and generaltype not in result:
                result[generaltype] = []

            raw_url = quote(image['url'], safe="%/:=&?~#+!$,;'@()*[]")
            preview_raw = raw_url.replace('.fanart.tv/fanart/', '.fanart.tv/preview/')
            
            resultimage = {'url': proxy + raw_url, 'preview': proxy + preview_raw, 'lang': image_lang}
            result[generaltype].append(resultimage)

    return result

def _get_imagelanguage(arttype, image):
    if 'lang' not in image or arttype == 'moviebackground':
        return None
    if arttype in ('movielogo', 'hdmovielogo', 'hdmovieclearart', 'movieart', 'moviebanner',
            'moviethumb', 'moviedisc'):
        return image['lang'] if image['lang'] not in ('', '00') else 'en'
    # movieposter may or may not have a title and thus need a language
    return image['lang'] if image['lang'] not in ('', '00') else None
