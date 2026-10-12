"""Find this addon's local Kodi databases without an external utility addon."""
import os
import re
import sqlite3
from contextlib import closing
from urllib.parse import quote
import xbmcvfs


def current_database(prefix):
    table = {'MyVideos': 'movie', 'Textures': 'texture'}.get(prefix)
    if table is None:
        raise ValueError('Unsupported Kodi database: ' + str(prefix))
    directory = xbmcvfs.translatePath('special://database/')
    candidates = []
    for name in os.listdir(directory):
        match = re.fullmatch(re.escape(prefix) + r'(\d+)\.db', name)
        if match:
            candidates.append((int(match.group(1)), os.path.join(directory, name)))
    for version, path in sorted(candidates, reverse=True):
        uri = 'file:' + quote(path.replace(os.sep, '/'), safe='/:') + '?mode=ro'
        try:
            with closing(sqlite3.connect(uri, uri=True, timeout=2)) as database:
                if database.execute(
                    'SELECT 1 FROM sqlite_master WHERE type=? AND name=?',
                    ('table', table)).fetchone():
                    return path
        except sqlite3.Error:
            continue
    raise RuntimeError('No valid Kodi database found: ' + prefix)
