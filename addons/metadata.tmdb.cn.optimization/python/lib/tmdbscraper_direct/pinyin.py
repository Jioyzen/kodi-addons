# coding: utf-8
import os
import json
import itertools
try:
    import xbmc
    import xbmcaddon
except ModuleNotFoundError:
    xbmc = None
    xbmcaddon = None

CHAR_MAP = {}
ADDON = None
if xbmcaddon:
    try:
        ADDON = xbmcaddon.Addon(id='metadata.tmdb.cn.optimization')
    except:
        pass

def load_char_map():
    global CHAR_MAP
    if not ADDON:
        return

    try:
        addon_path = ADDON.getAddonInfo('path')
        # Handle potential encoding issues with path on Windows
        if isinstance(addon_path, bytes):
            addon_path = addon_path.decode('utf-8')
            
        map_path = os.path.join(addon_path, 'resources', 'char_map.json')
        if os.path.exists(map_path):
            with open(map_path, 'r', encoding='utf-8') as f:
                CHAR_MAP = json.load(f)
            if xbmc:
                xbmc.log(f'[TMDB Scraper] Loaded char_map.json with {len(CHAR_MAP)} entries', xbmc.LOGINFO)
        else:
            if xbmc:
                xbmc.log(f'[TMDB Scraper] char_map.json not found at {map_path}', xbmc.LOGWARNING)
    except Exception as e:
        if xbmc:
            xbmc.log(f'[TMDB Scraper] Failed to load char_map.json: {e}', xbmc.LOGERROR)

# Initialize on import
if xbmc:
    load_char_map()

def get_pinyin_permutations(text):
    if not text:
        return ""
    
    # Lazy load if empty (e.g. if import happened before xbmc was ready, though unlikely)
    if not CHAR_MAP and xbmc:
        load_char_map()

    # Convert each char to a list of possible initials
    char_initials = []
    for char in text:
        if char in CHAR_MAP:
            # Get all pinyin variations for this char
            pinyins = CHAR_MAP[char]
            # Extract initials and deduplicate preserving order
            initials = []
            seen = set()
            for p in pinyins:
                if p:
                    init = p[0].upper()
                    if init not in seen:
                        seen.add(init)
                        initials.append(init)
            
            if initials:
                char_initials.append(initials)
            else:
                if char.isalnum():
                    char_initials.append([char.upper()])
        else:
            if char.isalnum():
                char_initials.append([char.upper()])

    # Generate Cartesian product
    try:
        permutations = list(itertools.product(*char_initials))
        # Join them back into strings
        results = ["".join(p) for p in permutations]
        # Deduplicate preserving order
        seen_res = set()
        unique_results = []
        for r in results:
            if r not in seen_res:
                seen_res.add(r)
                unique_results.append(r)
        return "|".join(unique_results)
    except Exception as e:
        if xbmc:
            xbmc.log(f'[TMDB Scraper] Pinyin generation error: {e}', xbmc.LOGERROR)
        return text
