import threading
import socket
import ssl
import os
import requests

try:
    import xbmc
except ModuleNotFoundError:
    xbmc = None

# --- DNS Customization Start ---
ORIGINAL_GETADDRINFO = socket.getaddrinfo
DNS_CACHE = {}
DNS_LOCK = threading.Lock()
CUSTOM_IP_MAP = {}
SYSTEM_HOSTS_MAP = {}

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
        
def lookup_doh(host):
    # 1. Cache
    with DNS_LOCK:
        if host in DNS_CACHE:
            return DNS_CACHE[host]

    # 2. DoH Providers (Cloudflare, AliDNS)
    # Using IPs to avoid recursion
    doh_providers = [
        ("https://1.1.1.1/dns-query", "application/dns-json"),
        ("https://223.5.5.5/resolve", "application/json"),
        ("https://223.6.6.6/resolve", "application/json"),
    ]

    for url, accept_header in doh_providers:
        try:
            # We use a fresh requests call here, careful not to use the session if it has complex adapters
            resp = requests.get(
                url,
                params={"name": host, "type": "A"},
                headers={"Accept": accept_header},
                timeout=2 
            )
            if resp.status_code == 200:
                data = resp.json()
                if 'Answer' in data:
                    for answer in data['Answer']:
                        if answer['type'] == 1: # A Record
                            ip = answer['data']
                            with DNS_LOCK:
                                DNS_CACHE[host] = ip
                            log(f'[TMDB Scraper] DoH resolved {host} -> {ip}', 'info')
                            return ip
        except:
            continue
            
    return None

def patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    if is_ip_address(host):
        return ORIGINAL_GETADDRINFO(host, port, family, type, proto, flags)
    
    # 1. Priority: Local Overrides (Hosts / Custom Map)
    ip = lookup_local_override(host)
    if ip:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, port))]

    # 2. Try DoH for everything
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