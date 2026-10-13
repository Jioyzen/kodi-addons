"""Private media paths stay in history, not in GUI labels/plugin URLs."""
import hashlib
import re
from urllib.parse import unquote, urlsplit


def path_token(path):
    return hashlib.sha256(str(path).encode('utf-8')).hexdigest()[:24]


def directory_label(path):
    value = str(path or '')
    parsed = urlsplit(value)
    protocol = parsed.scheme.upper() if parsed.scheme else '本地'
    name = unquote(parsed.path.rstrip('/').rsplit('/', 1)[-1]) or '目录'
    # Discard decoded URL credentials/full paths even if a folder itself
    # happens to contain a URL string. Never show authority or query strings.
    name = name.replace('\n', ' ').replace('\r', ' ')
    return '%s[%s]' % (name, protocol)


def redact_text(text):
    text = str(text or '')
    return re.sub(r'(?:smb|nfs|https?|ftp|sftp)://[^\s\[\]<>"\']+',
                  lambda m: directory_label(m[0]), text, flags=re.I)


def private_query(query):
    result = dict(query)
    for key, token_key in (('dirpath', 'directory'), ('filepath', 'file')):
        if key in result:
            result[token_key] = path_token(result.pop(key))
    return result


def resolve_query(query, history):
    result = dict(query)
    directories = history.get('directories', {})
    if 'directory' in result:
        result['dirpath'] = next((p for p in directories if path_token(p)==result['directory']), '')
    if 'file' in result:
        source = directories.get(result.get('dirpath'), {})
        result['filepath'] = next((p for p in source.get('files', {}) if path_token(p)==result['file']), '')
    return result
