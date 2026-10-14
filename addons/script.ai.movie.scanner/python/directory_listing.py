"""Bounded source listings: show directories first, naturally ordered episodes."""
import os
import re
from collections import defaultdict
from path_display import path_token, directory_label, redact_text
from tv_context import _show_directory, build_tv_contexts

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
    return (int(info.get('season_number', 0) or 0), local,
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
    # Confirmed movies cannot form TV rows. Avoid repeatedly parsing their
    # release filenames (and using them as evidence for unrelated episodes).
    contexts = build_tv_contexts([path for path,info in files.items() if not info.get('movie_id')],content_type)
    for path, info in files.items():
        context = contexts.get(path)
        tv = not info.get('movie_id') and (content_type=='tvshow' or (content_type!='movie' and (is_tv(info) or context)))
        # Files placed directly in a generic source have no show directory;
        # retain their separate filename-based confirmation workflow.
        if tv and (not context or context.get('directory_identity')):
            root = (context or {}).get('show_root') or info.get('match_evidence', {}).get('show_root') or _show_directory(path)[0]
            groups[root].append((path,info))
        else:
            plain.append((path,info))
    return groups, plain


def single_show_root(files, content_type='auto'):
    """Fast menu hint from stored identities, without rebuilding episode contexts.

    This only enables a convenience context action. Unconfirmed inventories
    retain directory/file matching inside their listing instead of guessing.
    Stop at the first movie, unknown item or second show root.
    """
    root = None
    if content_type == 'movie':
        return None
    for path, info in files.items():
        if info.get('movie_id') or not (content_type == 'tvshow' or is_tv(info)):
            return None
        candidate = info.get('match_evidence', {}).get('show_root')
        if not candidate:
            candidate = _show_directory(path)[0]
        if root is not None and candidate != root:
            return None
        root = candidate
    return root


def pending_reviews(files, content_type, needs_scan):
    """One review per physical show directory; no network/share requests."""
    groups, plain = group_inventory(files, content_type)
    rows = []
    for root, members in groups.items():
        pending = [(path,info) for path,info in members if needs_scan(info)]
        if not pending:
            continue
        path, info = min(pending, key=episode_order)
        entry = dict(info, _review_show_root=root, _review_count=len(pending))
        rows.append((path,entry))
    rows.extend((path,info) for path,info in plain if needs_scan(info))
    return rows


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
