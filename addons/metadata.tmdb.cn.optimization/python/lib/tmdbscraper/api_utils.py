# coding: utf-8

import xbmc
import xbmcgui
import json
import time
import socket
import requests

from urllib.parse import urlencode

HEADERS = {}
DNS_SETTINGS = {}
SERVICE_PORT = 56789

def set_headers(headers):
    HEADERS.clear()
    HEADERS.update(headers)

def ensure_daemon_started():
    global SERVICE_PORT
    """Ensure the daemon process is running."""
    if not xbmc: return False
    
    # Check if port is already set
    port = xbmcgui.Window(10000).getProperty('TMDB_OPTIMIZATION_SERVICE_PORT')
    if port:
        SERVICE_PORT = int(port)
        return True
        
    xbmc.log('[TMDB Scraper] Daemon not running, starting...', xbmc.LOGINFO)
    # Start daemon script
    addon_id = 'metadata.tmdb.cn.optimization'
    script_path = f'special://home/addons/{addon_id}/python/daemon.py'
    xbmc.executebuiltin(f'RunScript({script_path})')
    
    # Wait for port to be available (max 5 seconds)
    for _ in range(50):
        port = xbmcgui.Window(10000).getProperty('TMDB_OPTIMIZATION_SERVICE_PORT')
        if port:
            SERVICE_PORT = int(port)
            xbmc.log('[TMDB Scraper] Daemon started successfully', xbmc.LOGINFO)
            return True
        time.sleep(0.1)
        
    xbmc.log('[TMDB Scraper] Failed to start daemon', xbmc.LOGERROR)
    return False


def _send_payload(payload, timeout=35):
    try:
        if not ensure_daemon_started():
             return None

        # Get port dynamically from Window Property
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout) 
        try:
            sock.connect(('127.0.0.1', SERVICE_PORT))
        except ConnectionRefusedError:
            # Retry once if connection refused (maybe daemon just died or restarting)
            xbmc.log('[TMDB Scraper] Connection refused, retrying daemon start...', xbmc.LOGWARNING)
            xbmcgui.Window(10000).clearProperty('TMDB_OPTIMIZATION_SERVICE_PORT')
            if ensure_daemon_started():
                 sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                 sock.settimeout(timeout)
                 sock.connect(('127.0.0.1', SERVICE_PORT))
            else:
                 return None

        sock.sendall(json.dumps(payload).encode('utf-8'))
        
        # Read response
        response_data = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            response_data += chunk
            
        sock.close()
        
        if not response_data:
            return None
        
        return json.loads(response_data)
        
    except Exception as e:
        if xbmc:
            xbmc.log(f'[TMDB Scraper] Service IPC Error: {e}', xbmc.LOGERROR)
        return None

def set_custom_ip(hosts_map):
    """
    Set custom DNS mapping in daemon.
    :param hosts_map: dictionary of hostname -> ip
    """
    payload = {'custom_ip': hosts_map}
    resp = _send_payload(payload)
    if resp and 'custom_ip' in resp:
        return True
    return False

def get_pinyin_from_service(text):
    """Request pinyin conversion from daemon"""
    payload = {'pinyin': [text]} # New protocol: list of strings
    resp = _send_payload(payload, timeout=10)
    
    if resp and 'pinyin' in resp:
        results = resp['pinyin']
        if isinstance(results, list) and len(results) > 0:
            return results[0] # Returns list of permutations
            
    # Fallback
    if xbmc: xbmc.log('[TMDB Scraper] Pinyin failed or invalid response', xbmc.LOGWARNING)
    return []

def load_info_from_service(url, params=None, headers=None, batch_payload=None):
    """
    Send request to the background service daemon via TCP socket.
    Supports single request (url, params) or batch request (batch_payload).
    """
    # Construct Protocol Payload
    requests_list = []
    if batch_payload:
        requests_list = batch_payload
    else:
        requests_list = [{
            'url': url,
            'params': params,
            'headers': headers or {}
        }]
        
    payload = {'requests': requests_list}
    
    resp = _send_payload(payload)
    
    if not resp:
        return {'error': 'Service communication failed'}
    
    if 'requests' in resp:
        results = resp['requests']
        # If it was a single request call (not batch_payload), unwrap logic
        if not batch_payload:
            if results and len(results) > 0:
                 return results[0]
            else:
                 return {'error': 'No result in response'}
        return results
    
    if 'error' in resp:
         return {'error': resp['error']}

    return {'error': 'Invalid response format'}

def load_info(url, params=None, default=None, resp_type = 'json'):
    """
    Load info from external api using persistent service daemon

    :param url: API endpoint URL
    :param params: URL query params
    :default: object to return if there is an error
    :resp_type: what to return to the calling function
    :return: API response or default on error
    """
    theerror = ''
    
    if xbmc:
        # Log the request for debugging
        log_url = url
        if params:
            log_url += '?' + urlencode(params)
        xbmc.log('Calling URL "{}"'.format(log_url), xbmc.LOGDEBUG)
        if HEADERS:
            xbmc.log(str(HEADERS), xbmc.LOGDEBUG)
            
    # Try to use the service first
    service_result = load_info_from_service(url, params, HEADERS)
    
    if 'error' not in service_result:
        # Success
        if resp_type.lower() == 'json':
            return service_result.get('json') or json.loads(service_result.get('text', '{}'))
        else:
            return service_result.get('text')
    else:
        # Fallback to direct request if service fails (e.g. not running)
        if xbmc:
            xbmc.log('[TMDB Scraper] -----Service unavailable ({}), falling back to direct request'.format(service_result['error']), xbmc.LOGWARNING)
            
        try:
            # Direct request (non-persistent session, or local session)
            resp = requests.get(url, params=params, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            
            if resp_type.lower() == 'json':
                return resp.json()
            else:
                return resp.text
                
        except Exception as e:
            theerror = {'error': 'Direct request failed: {}'.format(e)}
            if default is not None:
                return default
            return theerror
