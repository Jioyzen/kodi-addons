"""Match Kodi country names without changing library metadata."""
KNOWN_CODES = frozenset(('CN','HK','TW','US','JP','KR','TH','IN','GB','FR','DE','RU','CA'))
_NAMES = {
 'CN': ('China','中国','中国大陆','中华人民共和国'),
 'HK': ('Hong Kong','香港','中国香港'),
 'TW': ('Taiwan','台湾','中国台湾'),
 'US': ('United States','United States of America','USA','美国'),
 'JP': ('Japan','日本'),
 'KR': ('South Korea','Republic of Korea','Korea, Republic of','韩国'),
 'TH': ('Thailand','泰国'),
 'IN': ('India','印度'),
 'GB': ('United Kingdom','Great Britain','UK','英国'),
 'FR': ('France','法国'),
 'DE': ('Germany','德国'),
 'RU': ('Russia','Russian Federation','俄罗斯'),
 'CA': ('Canada','加拿大'),
}
_CODES = {name.casefold():code for code,names in _NAMES.items() for name in (code,)+names}
def region_code(value):
 value=str(value or '').strip()
 return _CODES.get(value.casefold(),value.upper() if len(value)==2 else value)
def region_match(countries,selected):
 values={region_code(v) for v in str(countries or '').split('/') if v.strip()}
 return int(bool(values-KNOWN_CODES) if selected=='__other__' else selected in values)
