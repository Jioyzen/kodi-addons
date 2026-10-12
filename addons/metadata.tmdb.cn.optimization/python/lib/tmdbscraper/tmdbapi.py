# -*- coding: UTF-8 -*-
#
# Copyright (C) 2020, Team Kodi
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
# pylint: disable=missing-docstring

"""Functions to interact with TMDb API."""

import unicodedata
from . import api_utils
try:
    import xbmc
except ModuleNotFoundError:
    # only used for logging HTTP calls, not available nor needed for testing
    xbmc = None
try:
    from typing import Optional, Text, Dict, List, Any  # pylint: disable=unused-import
    InfoType = Dict[Text, Any]  # pylint: disable=invalid-name
except ImportError:
    pass


HEADERS = (
    ('User-Agent', 'Kodi Movie scraper by Team Kodi'),
    ('Accept', 'application/json'),
)

TMDB_PARAMS = {'api_key': 'f090bb54758cabf231fb605d3e3e0468'}

def get_base_url(settings=None):
    try:
        base = ""
        if settings:
            base = settings.getSettingString('tmdb_api_base_url')
        if not base:
            base = 'https://api.tmdb.org'
        if not base.startswith('http'):
            base = 'https://' + base
        return base + '/3/{}'
    except:
        return 'https://api.tmdb.org/3/{}'

def log(message):
    if xbmc:
        xbmc.log(message, xbmc.LOGDEBUG)

def _call_service(url, params):
    """
    Call service and unwrap the JSON response to match old load_info behavior
    """
    res = api_utils.load_info_from_service(url, params=params, headers=dict(HEADERS))
    if isinstance(res, dict):
        if 'error' in res:
            return res
        # Return the json content directly to match previous load_info(resp_type='json')
        return res.get('json') or {}
    return res

def search_movie(query, year=None, language=None, page=None, settings=None, include_adult=False):
    """
    Search for a movie

    :param title: movie title to search
    :param year: the year to search (optional)
    :param language: the language filter for TMDb (optional)
    :param page: the results page to return (optional)
    :param settings: addon settings object (optional)
    :param include_adult: whether to include adult content (optional)
    :return: a list with found movies
    """
    query = unicodedata.normalize('NFC', query)
    log('using title of %s to find movie' % query)
    theurl = get_base_url(settings).format('search/movie')
    params = _set_params(None, language)
    params['query'] = query
    if include_adult:
        params['include_adult'] = 'true'
    else:
        params['include_adult'] = 'false'
        
    if page is not None:
        params['page'] = page
    if year is not None:
        params['year'] = str(year)
    return _call_service(theurl, params)


def find_movie_by_external_id(external_id, language=None, settings=None):
    """
    Find movie based on external ID

    :param mid: external ID
    :param language: the language filter for TMDb (optional)
    :param settings: addon settings object (optional)
    :return: the movie or error
    """
    log('using external id of %s to find movie' % external_id)
    theurl = get_base_url(settings).format('find/{}').format(external_id)
    params = _set_params(None, language)
    params['external_source'] = 'imdb_id'
    return _call_service(theurl, params)



def get_movie(mid, language=None, append_to_response=None, settings=None):
    """
    Get movie details

    :param mid: TMDb movie ID
    :param language: the language filter for TMDb (optional)
    :append_to_response: the additional data to get from TMDb (optional)
    :param settings: addon settings object (optional)
    :return: the movie or error
    """
    log('using movie id of %s to get movie details' % mid)
    theurl = get_base_url(settings).format('movie/{}').format(mid)
    return _call_service(theurl, params=_set_params(append_to_response, language))

def get_movie_request(mid, language=None, append_to_response=None, settings=None):
    log('using movie id of %s to get movie details' % mid)
    theurl = get_base_url(settings).format('movie/{}').format(mid)
    return {'url': theurl, 'params': _set_params(append_to_response, language), 'headers': dict(HEADERS), 'type': 'tmdb'}



def get_collection(collection_id, language=None, append_to_response=None, settings=None):
    """
    Get movie collection information

    :param collection_id: TMDb collection ID
    :param language: the language filter for TMDb (optional)
    :append_to_response: the additional data to get from TMDb (optional)
    :param settings: addon settings object (optional)
    :return: the movie or error
    """
    log('using collection id of %s to get collection details' % collection_id)
    theurl = get_base_url(settings).format('collection/{}').format(collection_id)
    return _call_service(theurl, params=_set_params(append_to_response, language))


def get_configuration(settings=None):
    """
    Get configuration information

    :param settings: addon settings object (optional)
    :return: configuration details or error
    """
    log('getting configuration details')
    return _call_service(get_base_url(settings).format('configuration'), params=TMDB_PARAMS.copy())


def _set_params(append_to_response, language):
    params = TMDB_PARAMS.copy()
    if language is not None:
        params['language'] = language
    if append_to_response is not None:
        params['append_to_response'] = append_to_response
    return params
