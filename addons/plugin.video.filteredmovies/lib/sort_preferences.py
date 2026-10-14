"""Remember the wall's last manual sort independently of temporary filters."""
import json
import os
import tempfile

DEFAULT_SORT = ('dateadded','descending')
METHODS = frozenset(('dateadded','rating','title','premiered'))
ORDERS = frozenset(('ascending','descending'))
SCOPES = frozenset(('all','movie','tvshow','set','concert','documentary'))


def _filename(scope):
    return 'sort-preference-'+scope+'.json' if scope in SCOPES else None


def load(profile,scope='all'):
    filename = _filename(scope)
    if filename is None:
        return DEFAULT_SORT
    try:
        with open(os.path.join(profile,filename),encoding='utf-8') as stream:
            raw = stream.read(2049)
        if len(raw)>2048:
            return DEFAULT_SORT
        data = json.loads(raw)
        if not isinstance(data,dict):
            return DEFAULT_SORT
        method,order = data.get('method'),data.get('order')
        if method in METHODS and order in ORDERS:
            return method,order
    except (OSError,ValueError,TypeError,UnicodeError):
        pass
    return DEFAULT_SORT


def save(profile,method,order,scope='all'):
    filename = _filename(scope)
    if filename is None:
        return False
    if method not in METHODS or order not in ORDERS:
        return False
    temporary = None
    try:
        os.makedirs(profile,exist_ok=True)
        descriptor,temporary = tempfile.mkstemp(prefix='.sort-',suffix='.tmp',dir=profile)
        with os.fdopen(descriptor,'w',encoding='utf-8') as stream:
            json.dump({'method':method,'order':order},stream,separators=(',',':'))
        os.replace(temporary,os.path.join(profile,filename))
        temporary = None
        return True
    except OSError:
        return False
    finally:
        if temporary:
            try:os.unlink(temporary)
            except OSError:pass
