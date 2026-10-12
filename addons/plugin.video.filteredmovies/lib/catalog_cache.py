"""Bounded, cross-invocation metadata cache, invalidated by Kodi DB/WAL changes."""
import hashlib
import json
import os
import sqlite3
import time
from .common import ADDON_DATA_PATH, DB_PATH

def revision():
    values = []
    for path in (DB_PATH, DB_PATH + '-wal'):
        try:
            st = os.stat(path)
            values.append((path, st.st_ino, st.st_size, st.st_mtime_ns))
        except OSError:
            values.append((path, None))
    return json.dumps(values, separators=(',', ':'))

def key(filters, limit, offset):
    return hashlib.sha256(json.dumps(['collection-members-v4', filters or {}, limit, offset], sort_keys=True,
                                    ensure_ascii=False).encode()).hexdigest()

def _connect():
    os.makedirs(ADDON_DATA_PATH, exist_ok=True)
    conn = sqlite3.connect(os.path.join(ADDON_DATA_PATH, 'catalog-v3.sqlite'), timeout=.15)
    # Disposable cache: never delay the UI for durable fsync writes.
    # Kodi's library remains read-only; a failed cache is simply bypassed.
    conn.execute('PRAGMA synchronous=OFF')
    conn.execute('PRAGMA journal_mode=MEMORY')
    conn.execute('CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, rev TEXT, ts REAL, data TEXT)')
    return conn

def get(cache_key, rev):
    conn = None
    try:
        conn = _connect()
        row = conn.execute('SELECT data FROM cache WHERE key=? AND rev=? AND ts>?',
                           (cache_key, rev, time.time()-60)).fetchone()
        return json.loads(row[0]) if row else None
    except (OSError, sqlite3.Error, ValueError):
        return None
    finally:
        if conn is not None:conn.close()


def put(cache_key, rev, items):
    # Large legacy directory requests are not persisted; pages and widgets are.
    if len(items) > 240:
        return
    conn = None
    try:
        body = json.dumps(items, ensure_ascii=False)
        if len(body.encode('utf-8')) > 2 * 1024 * 1024:return
        conn = _connect()
        conn.execute('INSERT OR REPLACE INTO cache VALUES (?,?,?,?)',
                     (cache_key, rev, time.time(), body))
        conn.execute('DELETE FROM cache WHERE key NOT IN (SELECT key FROM cache ORDER BY ts DESC LIMIT 32)')
        conn.commit()
    except (OSError, sqlite3.Error, ValueError):
        pass
    finally:
        if conn is not None:
            conn.close()


def clear():
    conn=None
    try:
        conn=_connect();conn.execute('DELETE FROM cache');conn.commit()
    except (OSError,sqlite3.Error):
        pass
    finally:
        if conn is not None:conn.close()
