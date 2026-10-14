"""Atomic scanner inventory persistence; malformed existing data is not empty."""
import json
import os
import tempfile


def load_history_file(path):
    try:
        with open(path,encoding='utf-8') as stream:
            value=json.load(stream)
    except FileNotFoundError:
        return {'directories':{}}
    if not isinstance(value,dict) or not isinstance(value.get('directories'),dict):
        raise ValueError('Invalid scanner inventory structure')
    for directory,source in value['directories'].items():
        if (not isinstance(directory,str) or not isinstance(source,dict) or
                not isinstance(source.get('files',{}),dict) or
                any(not isinstance(p,str) or not isinstance(info,dict)
                    for p,info in source.get('files',{}).items())):
            raise ValueError('Invalid scanner source structure')
    return value


def save_history_file(path,value):
    folder=os.path.dirname(path)
    os.makedirs(folder,exist_ok=True)
    temporary=None
    try:
        with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=folder,
                prefix='.scan-history-',delete=False) as stream:
            temporary=stream.name
            json.dump(value,stream,ensure_ascii=False,indent=2)
            stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,path)
        temporary=None
    finally:
        if temporary:
            try:os.unlink(temporary)
            except FileNotFoundError:pass
