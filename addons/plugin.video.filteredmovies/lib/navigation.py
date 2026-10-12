"""Window-local navigation; reuse the wall interpreter and its loaded modules."""
import os
import time
import xml.etree.ElementTree as ET
import xbmcgui

HOME_ENTRIES = {
    1001: ('Custom_5113_SearchMovie.xml', None),
    7101: ('Custom_5112_AllMovie.xml', 'all'),
    7102: ('Custom_5114_Movie.xml', 'movie'),
    7103: ('Custom_5114_Movie.xml', 'set'),
    7104: ('Custom_5114_Movie.xml', 'tvshow'),
    7105: ('Custom_5114_Movie.xml', 'concert'),
    7106: ('Custom_5114_Movie.xml', 'documentary'),
}
PAGE_TITLES = {'all': '全部影片', 'movie': '电影', 'set': '系列合集',
               'tvshow': '电视剧', 'concert': '演唱会', 'documentary': '纪录片'}


def launch_child(xml_name, addon_path, mediatype=None, parent=None,
                 factory=None, started=None, collection_id=None, page_title=None):
    """Guard modal reentry and release parent/child resources on every exit."""
    if parent is None:
        parent = xbmcgui.Window(xbmcgui.getCurrentWindowId())
    if parent.getProperty('MFG.ChildWindow') == 'true':
        return False
    capture = getattr(parent, '_capture_navigation_state', None)
    if capture is not None:
        capture()
    parent.setProperty('MFG.ChildWindow', 'true')
    child = None
    try:
        if factory is None:
            from .window_handler import FilterWindow
            factory = FilterWindow
        child = factory(xml_name, addon_path, 'Default', '1080i')
        child.reset_filters = True
        if mediatype is not None:
            child.initial_mediatype = mediatype
        if collection_id is not None:
            child.collection_id = int(collection_id)
        child.page_title = page_title or PAGE_TITLES.get(mediatype, '影片搜索')
        child.navigation_started = started if started is not None else time.monotonic()
        child.doModal()
        return True
    finally:
        try:
            if child is not None:
                child.cleanup()
        finally:
            parent.clearProperty('MFG.ChildWindow')
            restore = getattr(parent, '_restore_navigation_state', None)
            if restore is not None:
                restore()


def background_animations(skin_path):
    """Read a known background's own animations, without editing the skin.

    Kodi's setAnimations API only accepts simple space-delimited attributes.
    Skip unknown/complex definitions rather than dropping skin conditions.
    """
    try:
        root = ET.parse(os.path.join(skin_path, 'xml', 'Includes.xml')).getroot()
        groups = root.findall(".//include[@name='DefaultBackground']//control[@id='31111']")
        if len(groups) != 1:
            return None
        original, fast = [], []
        found_close = False
        for animation in groups[0].findall('animation'):
            event = (animation.text or '').strip()
            attributes = dict(animation.attrib)
            if (not event or list(animation) or not attributes
                    or any(not value or '=' in value or any(c.isspace() for c in value)
                           for value in attributes.values())):
                return None
            original.append((event, ' '.join('%s=%s' % pair for pair in attributes.items())))
            if event.lower() == 'windowclose':
                attributes['time'] = '0'
                attributes['delay'] = '0'
                found_close = True
            fast.append((event, ' '.join('%s=%s' % pair for pair in attributes.items())))
        return (original, fast) if found_close else None
    except (OSError, ET.ParseError):
        return None


def panel_animations(xml_path):
    """Keep the all-movies panel's layout effects, with an instant Back variant.

    These addon conditions contain no quoted string values with spaces; Kodi's
    boolean parser accepts their compact form required by setAnimations().
    """
    try:
        root = ET.parse(xml_path).getroot()
        panel = root.find(".//control[@id='9999']")
        if panel is None:
            return None
        original, instant = [], []
        for animation in panel.findall('animation'):
            effects = animation.findall('effect')
            if len(effects) != 1:
                return None
            attributes = dict(animation.attrib)
            event = attributes.pop('type', '')
            attributes.update(effects[0].attrib)
            attributes['effect'] = attributes.pop('type', '')
            if not event or not attributes['effect']:
                return None
            for k, v in list(attributes.items()):
                if k == 'condition':
                    if '"' in v or "'" in v:
                        return None
                    attributes[k] = ''.join(v.split())
                elif any(c.isspace() for c in v) or '=' in v:
                    return None
            original.append((event, ' '.join('%s=%s' % p for p in attributes.items())))
            attributes['time'] = '0'
            attributes['delay'] = '0'
            instant.append((event, ' '.join('%s=%s' % p for p in attributes.items())))
        return (original, instant) if original else None
    except (OSError, ET.ParseError):
        return None
