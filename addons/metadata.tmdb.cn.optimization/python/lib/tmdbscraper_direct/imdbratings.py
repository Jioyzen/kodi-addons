# -*- coding: UTF-8 -*-

import json
import re
from . import api_utils
from . import get_imdb_id

def get_imdb_url(settings=None):
    try:
        base = ""
        if settings:
            base = settings.getSettingString('imdb_base_url')
        if not base:
            base = 'www.imdb.com'
        if not base.startswith('http'):
            base = 'https://' + base
        return base + '/title/{}/'
    except:
        return 'https://www.imdb.com/title/{}/'

IMDB_LDJSON_REGEX = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.DOTALL)
IMDB_TOP250_REGEX = re.compile(r'Top rated movie #(\d+)')

# previous IMDB page design before June 2021
IMDB_RATING_REGEX_PREVIOUS = re.compile(r'itemprop="ratingValue".*?>.*?([\d.]+).*?<')
IMDB_VOTES_REGEX_PREVIOUS = re.compile(r'itemprop="ratingCount".*?>.*?([\d,]+).*?<')
IMDB_TOP250_REGEX_PREVIOUS = re.compile(r'Top Rated Movies #(\d+)')

HEADERS = (
    ('User-Agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/87.0.4280.88 Safari/537.36'),
    ('Accept', 'application/json'),
)


def parse_movie_response(responses):
    response = responses.get('imdb_rating')
    if not response:
        return {}
    
    # response is text because we set resp_type='text' in request
    votes, rating, top250 = _parse_imdb_result(response)
    return _assemble_imdb_result(votes, rating, top250)

def get_details(uniqueids, settings=None):
    imdb_id = get_imdb_id(uniqueids)
    if not imdb_id:
        return {}
    votes, rating, top250 = _get_ratinginfo(imdb_id, settings)
    return _assemble_imdb_result(votes, rating, top250)

def _get_ratinginfo(imdb_id, settings=None):
    try:
        response = api_utils.get(get_imdb_url(settings).format(imdb_id), headers=dict(HEADERS)).text
    except:
        response = ''
    return _parse_imdb_result(response)

def _assemble_imdb_result(votes, rating, top250):
    result = {}
    if top250:
        result['info'] = {'top250': top250}
    if votes and rating:
        result['ratings'] = {'imdb': {'votes': votes, 'rating': rating}}
    return result

def _parse_imdb_result(input_html):
    rating, votes = _parse_imdb_rating_and_votes(input_html)
    if rating is None or votes is None:
        # try previous parsers
        rating = _parse_imdb_rating_previous(input_html)
        votes = _parse_imdb_votes_previous(input_html)
    top250 = _parse_imdb_top250(input_html)
    if top250 is None:
        top250 = _parse_imdb_top250_previous(input_html)

    return votes, rating, top250

def _parse_imdb_rating_and_votes(input_html):
    match = re.search(IMDB_LDJSON_REGEX, input_html)
    if not match:
        return None, None

    try:
        ldjson = json.loads(match.group(1).replace('\n', ''))
    except json.decoder.JSONDecodeError:
        return None, None

    try:
        aggregateRating = ldjson.get('aggregateRating', {})
        rating_value = aggregateRating.get('ratingValue')
        return rating_value, aggregateRating.get('ratingCount')
    except AttributeError:
        return None, None

def _parse_imdb_top250(input_html):
    match = re.search(IMDB_TOP250_REGEX, input_html)
    if match:
        return int(match.group(1))
    return None

def _parse_imdb_rating_previous(input_html):
    match = re.search(IMDB_RATING_REGEX_PREVIOUS, input_html)
    if (match):
        return float(match.group(1))
    return None

def _parse_imdb_votes_previous(input_html):
    match = re.search(IMDB_VOTES_REGEX_PREVIOUS, input_html)
    if (match):
        return int(match.group(1).replace(',', ''))
    return None

def _parse_imdb_top250_previous(input_html):
    match = re.search(IMDB_TOP250_REGEX_PREVIOUS, input_html)
    if (match):
        return int(match.group(1))
    return None
