"""Read newly deployed strings before Kodi refreshes its language cache."""
import ast
from pathlib import Path

import xbmc

_catalogs = {}


def get_string(addon, string_id):
    text = addon.getLocalizedString(string_id)
    if text:
        return text
    try:
        language = xbmc.getLanguage(xbmc.ISO_639_1).lower().replace('_', '-')
    except AttributeError:
        language = 'en'
    locale = 'en_gb'
    if language.startswith('zh'):
        locale = 'zh_tw' if any(s in language for s in ('tw', 'hk', 'hant')) else 'zh_cn'
    if locale not in _catalogs:
        catalog = {}
        path = Path(__file__).resolve().parents[1] / 'resources' / 'language' / ('resource.language.' + locale) / 'strings.po'
        sid = None
        collecting = False
        with path.open(encoding='utf-8') as source:
            for line in source:
                line = line.strip()
                if line.startswith('msgctxt '):
                    context = ast.literal_eval(line[8:])
                    sid = int(context[1:]) if context.startswith('#') else None
                    collecting = False
                elif line.startswith('msgstr ') and sid is not None:
                    catalog[sid] = ast.literal_eval(line[7:])
                    collecting = True
                elif line.startswith('"') and collecting:
                    catalog[sid] += ast.literal_eval(line)
                else:
                    collecting = False
        _catalogs[locale] = catalog
    return _catalogs[locale].get(string_id, '')
