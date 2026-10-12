# coding: utf-8
import threading
from urllib.parse import urlencode
from typing import Text, Dict, Any

import requests

try:
    import xbmc
except ModuleNotFoundError:
    xbmc = None

# Initialize DNS Override (this patches socket.getaddrinfo immediately on import)
try:
    from . import dns_override
except ImportError:
    # In case of running outside of package context during tests
    if xbmc:
        xbmc.log('[TMDB Scraper] Failed to import dns_override', xbmc.LOGWARNING)

HEADERS = {}

_SESSION = None
_SESSION_LOCK = threading.Lock()

def get_session():
    global _SESSION
    if _SESSION is None:
        with _SESSION_LOCK:
            if _SESSION is None:
                _SESSION = requests.Session()
                # Initialize headers from current global HEADERS
                _SESSION.headers.update(HEADERS)
                
                # Configure Connection Pool
                adapter = requests.adapters.HTTPAdapter(
                    pool_connections=20, 
                    pool_maxsize=80, 
                    max_retries=2
                )
                _SESSION.mount('http://', adapter)
                _SESSION.mount('https://', adapter)
    return _SESSION

def request(method, url, **kwargs):
    """Constructs and sends a :class:`Request <Request>`."""
    return get_session().request(method=method, url=url, **kwargs)

def get(url, params=None, **kwargs):
    """Sends a GET request."""
    return get_session().get(url, params=params, **kwargs)

def options(url, **kwargs):
    """Sends an OPTIONS request."""
    return get_session().options(url, **kwargs)

def head(url, **kwargs):
    """Sends a HEAD request."""
    return get_session().head(url, **kwargs)

def post(url, data=None, json=None, **kwargs):
    """Sends a POST request."""
    return get_session().post(url, data=data, json=json, **kwargs)

def put(url, data=None, **kwargs):
    """Sends a PUT request."""
    return get_session().put(url, data=data, **kwargs)

def patch(url, data=None, **kwargs):
    """Sends a PATCH request."""
    return get_session().patch(url, data=data, **kwargs)

def delete(url, **kwargs):
    """Sends a DELETE request."""
    return get_session().delete(url, **kwargs)

