"""Bounded source listings: show directories first, naturally ordered episodes."""
import os
import re
from collections import defaultdict
from path_display import path_token, directory_label, redact_text
from tv_context import _show_directory, _TV_ROOTS, episode_marker
from urllib.parse import urlsplit, unquote

PAGE_SIZE = 80


def natural_key(text):
    return tuple((1,int(part)) if part.isdigit() else (0,part.casefold())
                 for part in re.split(r'(\d+)', str(text)))


def is_tv(info):
    return bool(info.get('is_tvshow') or info.get('match_evidence', {}).get('type')=='tvshow')


def episode_order(item):
    path, info = item
    evidence = info.get('match_evidence', {})
    local = evidence.get('numbering_mode')=='local_unmatched'
    return (local, int(info.get('season_number', 0) or 0),
            int(info.get('episode_number', 0) or 0), natural_key(os.path.basename(path)))


def group_label(root, items):
    primary = next((info for _,info in items if info.get('tmdb_title')),items[0][1] if items else {})
    evidence = primary.get('match_evidence',{})
    title = evidence.get('source_title') or primary.get('tmdb_title')
    if not title:
        return directory_label(root).rsplit('[', 1)[0]
    year = evidence.get('source_year') or primary.get('year')
    return '%s%s' % (redact_text(title), ' (%s)'%year if year else '')


def group_inventory(files, content_type='auto'):
    """Group stored inventory by show root without reading a media share."""
    groups, plain = defaultdict(list), []
    for path, info in files.items():
        pending = info.get('status') in ('pending','failed','skipped')
        hinted = pending and (any(part.casefold() in _TV_ROOTS for part in unquote(urlsplit(path).path).split('/')) or episode_marker(os.path.basename(path)) is not None)
        tv = content_type=='tvshow' or (content_type!='movie' and (is_tv(info) or hinted))
        if tv:
            root = info.get('match_evidence', {}).get('show_root') or _show_directory(path)[0]
            groups[root].append((path,info))
        else:
            plain.append((path,info))
    return groups, plain


def listing_page(files, content_type='auto', group='', page=0):
    """Only process stored strings/metadata; never parse every file or touch a share."""
    groups, plain = group_inventory(files,content_type)
    rows = []
    label = ''
    if group:
        root = next((root for root in groups if path_token(root)==group), None)
        if root:
            label = group_label(root,groups[root])
            rows = [('file',path,info) for path,info in sorted(groups[root],key=episode_order)]
    elif len(groups)==1 and not plain:
        rows = [('file',path,info) for path,info in sorted(next(iter(groups.values())),key=episode_order)]
    else:
        for root, items in sorted(groups.items(),key=lambda row:natural_key(group_label(row[0],row[1]))):
            rows.append(('group',root,{'files':items}))
        rows += [('file',path,info) for path,info in sorted(plain,key=lambda row:natural_key(os.path.basename(row[0])))]
    total = len(rows)
    pages = max(1,(total+PAGE_SIZE-1)//PAGE_SIZE)
    page = max(0,min(int(page or 0),pages-1))
    return {'rows':rows[page*PAGE_SIZE:(page+1)*PAGE_SIZE], 'total':total,
            'page':page, 'pages':pages, 'label':label}
