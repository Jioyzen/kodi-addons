import threading
import socket
import ssl
import os
import time
import requests

try:
    import xbmc
except ModuleNotFoundError:
    xbmc = None

# --- DNS Customization Start ---
ORIGINAL_GETADDRINFO = socket.getaddrinfo
DNS_CACHE = {}
DNS_FAIL_CACHE = {}
DNS_LOCK = threading.Lock()
CUSTOM_IP_MAP = {}
SYSTEM_HOSTS_MAP = {}
DOH_SESSION = None

# Only route these domains through DoH. Everything else falls back to the
# system resolver, so non-TMDB traffic is never slowed down by DoH latency.
DOH_DOMAINS = (
    'api.tmdb.org', 'image.tmdb.org', 'www.themoviedb.org', 'themoviedb.org',
    'api.themoviedb.org', 'thetvdb.com', 'api.thetvdb.com',
    'api.deepseek.com', 'api.openai.com',
    'apis.netstart.cn', 'wsrv.nl', 'www.themoviedb.org',
)
# Substring match list (e.g. "cdn.tmdb.org" style hosts)
DOH_SUBSTRINGS = ('tmdb.org', 'themoviedb.org', 'thetvdb.com')

def _should_doh(host):
    """Only DoH-resolve domains that are likely blocked/polluted by GFW."""
    hl = host.lower()
    if hl in DOH_DOMAINS:
        return True
    return any(s in hl for s in DOH_SUBSTRINGS)

def is_ip_address(host):
    try:
        socket.inet_aton(host)
        return True
    except:
        return ':' in host

def log(msg, level=None):
    if xbmc:
        xbmc_level = xbmc.LOGINFO
        if isinstance(level, str):
            level = level.lower()
            if level == 'debug': xbmc_level = xbmc.LOGDEBUG
            elif level == 'info': xbmc_level = xbmc.LOGINFO
            elif level == 'warning': xbmc_level = xbmc.LOGWARNING
            elif level == 'error': xbmc_level = xbmc.LOGERROR
            elif level == 'fatal': xbmc_level = xbmc.LOGFATAL
        elif isinstance(level, int):
            xbmc_level = level
            
        xbmc.log(msg, xbmc_level)
    else:
        # Ignore level parameter if not in XBMC, just print
        print(msg)

def parse_hosts_file(path):
    mapping = {}
    try:
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    parts = line.split()
                    if len(parts) >= 2:
                        ip = parts[0]
                        if is_ip_address(ip):
                            for domain in parts[1:]:
                                mapping[domain] = ip
            log(f'[TMDB Scraper] Loaded {len(mapping)} entries from {path}', 'debug')
    except Exception as e:
        log(f'[TMDB Scraper] Failed to read hosts file {path}: {e}', 'warning')
    return mapping

def load_hosts():
    global SYSTEM_HOSTS_MAP
    SYSTEM_HOSTS_MAP = {}
    
    # System Hosts
    try:
        if os.name == 'nt':
            system_hosts = os.path.join(os.environ.get('SystemRoot', 'C:\\Windows'), 'System32', 'drivers', 'etc', 'hosts')
            SYSTEM_HOSTS_MAP.update(parse_hosts_file(system_hosts))
        else:
            system_hosts = '/etc/hosts'
            SYSTEM_HOSTS_MAP.update(parse_hosts_file(system_hosts))
    except:
        pass


def lookup_local_override(host):
    # 1. Custom IP Map (User Settings - Highest Priority)
    if host in CUSTOM_IP_MAP:
        return CUSTOM_IP_MAP[host]
        
    # 2. System Hosts File
    if host in SYSTEM_HOSTS_MAP:
        return SYSTEM_HOSTS_MAP[host]
        
    return None
        
def _get_doh_session():
    """Lazily create a shared requests session for DoH queries."""
    global DOH_SESSION
    if DOH_SESSION is None:
        s = requests.Session()
        s.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                          'AppleWebKit/537.36 (KHTML, like Gecko) '
                          'Chrome/120.0.0.0 Safari/537.36',
        })
        DOH_SESSION = s
    return DOH_SESSION

def lookup_doh(host):
    # 1. Cache
    with DNS_LOCK:
        if host in DNS_CACHE:
            return DNS_CACHE[host]
        # Fail cache: don't hammer DoH for a known-unreachable domain
        now = time.time()
        if host in DNS_FAIL_CACHE:
            if now - DNS_FAIL_CACHE[host] < 30:
                return None
            del DNS_FAIL_CACHE[host]

    # 2. DoH Providers - AliDNS first (China-optimized view, faster & more
    #    accurate for CN users), Cloudflare as backup.
    #    Using IPs to avoid recursion.
    doh_providers = [
        ("https://223.5.5.5/resolve", "application/json"),
        ("https://223.6.6.6/resolve", "application/json"),
        ("https://1.1.1.1/dns-query", "application/dns-json"),
        ("https://8.8.8.8/resolve", "application/json"),
    ]
    session = _get_doh_session()

    for url, accept_header in doh_providers:
        try:
            resp = session.get(
                url,
                params={"name": host, "type": "A"},
                headers={"Accept": accept_header},
                timeout=3
            )
            if resp.status_code == 200:
                data = resp.json()
                answers = data.get('Answer') if isinstance(data, dict) else None
                if not answers and isinstance(data, dict) and data.get('Status') == 0:
                    answers = []
                for answer in (answers or []):
                    if isinstance(answer, dict) and answer.get('type') == 1:
                        ip = answer['data']
                        with DNS_LOCK:
                            DNS_CACHE[host] = ip
                        log(f'[TMDB Scraper] DoH resolved {host} -> {ip}', 'info')
                        return ip
        except Exception:
            continue

    # Record failure so we don't retry this host for a while
    with DNS_LOCK:
        DNS_FAIL_CACHE[host] = time.time()
    return None

def patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    if is_ip_address(host):
        return ORIGINAL_GETADDRINFO(host, port, family, type, proto, flags)

    # 1. Priority: Local Overrides (Hosts / Custom Map)
    ip = lookup_local_override(host)
    if ip:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, port))]

    # 2. DoH only for whitelisted domains (avoids slowing down other traffic)
    if _should_doh(host):
        ip = lookup_doh(host)
        if ip:
            # Return IPv4 TCP address
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, port))]

    return ORIGINAL_GETADDRINFO(host, port, family, type, proto, flags)


def set_custom_hosts(ip_map):
    """
    Allows external setting of custom IP map (e.g. from per-path scraper settings).
    ip_map: { 'domain': 'ip' }
    """
    global CUSTOM_IP_MAP
    
    for domain, ip in ip_map.items():
        if not ip:
            # Remove override if exists
            if domain in CUSTOM_IP_MAP:
                del CUSTOM_IP_MAP[domain]
                log(f'[TMDB Service] Removed Global Custom IP for {domain}', 'info')
        else:
            # Update/Overwrite
            if CUSTOM_IP_MAP.get(domain) != ip:
                CUSTOM_IP_MAP[domain] = ip
                log(f'[TMDB Service] Updated Global Custom IP for {domain} -> {ip}', 'info')

load_hosts()

# Apply Patch
socket.getaddrinfo = patched_getaddrinfo