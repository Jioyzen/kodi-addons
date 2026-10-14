# -*- coding: utf-8 -*-
from .common import notification, log, DB_PATH, ADDON_DATA_PATH
from . import t9_helper
import xbmc
import xbmcgui
import xbmcaddon
import xbmcvfs
import time
import threading
import queue
import json
import base64
from functools import lru_cache
from .navigation import HOME_ENTRIES, launch_child, background_animations, panel_animations

@lru_cache(maxsize=256)
def _loc(identifier):
    return xbmcaddon.Addon('plugin.video.filteredmovies').getLocalizedString(identifier)

# T9 多字符映射：数字 -> [数字, 字母...]
_MULTITAP_MAP = {
    '2': ['2', 'A', 'B', 'C'],
    '3': ['3', 'D', 'E', 'F'],
    '4': ['4', 'G', 'H', 'I'],
    '5': ['5', 'J', 'K', 'L'],
    '6': ['6', 'M', 'N', 'O'],
    '7': ['7', 'P', 'Q', 'R', 'S'],
    '8': ['8', 'T', 'U', 'V'],
    '9': ['9', 'W', 'X', 'Y', 'Z'],
}

# 国家/地区代码 → 中文名映射
_COUNTRY_MAP = {
    "US": "美国", "GB": "英国", "FR": "法国", "DE": "德国",
    "IT": "意大利", "JP": "日本", "KR": "韩国", "CN": "中国",
    "HK": "中国香港", "TW": "中国台湾", "IN": "印度", "RU": "俄罗斯",
    "CA": "加拿大", "AU": "澳大利亚", "NZ": "新西兰", "ES": "西班牙",
    "BR": "巴西", "MX": "墨西哥", "SE": "瑞典", "DK": "丹麦",
    "NO": "挪威", "NL": "荷兰", "BE": "比利时", "IE": "爱尔兰",
    "CH": "瑞士", "AT": "奥地利", "PL": "波兰", "CZ": "捷克",
    "TH": "泰国", "AR": "阿根廷", "ZA": "南非", "TR": "土耳其",
    "PT": "葡萄牙", "FI": "芬兰", "HU": "匈牙利", "IS": "冰岛",
    "GR": "希腊", "CL": "智利", "CO": "哥伦比亚", "PH": "菲律宾",
    "SG": "新加坡", "MY": "马来西亚", "ID": "印尼", "IL": "以色列",
    "EG": "埃及", "AE": "阿联酋", "SA": "沙特", "LU": "卢森堡",
    "RO": "罗马尼亚", "BG": "保加利亚", "UA": "乌克兰", "IR": "伊朗",
    "NG": "尼日利亚", "KE": "肯尼亚", "PE": "秘鲁", "PK": "巴基斯坦",
    "BY": "白俄罗斯", "RS": "塞尔维亚", "VN": "越南", "HR": "克罗地亚",
    "SK": "斯洛伐克", "SI": "斯洛文尼亚", "LT": "立陶宛", "LV": "拉脱维亚",
    "EE": "爱沙尼亚", "MA": "摩洛哥", "DZ": "阿尔及利亚", "BD": "孟加拉国",
    "LK": "斯里兰卡", "MM": "缅甸", "KH": "柬埔寨", "VE": "委内瑞拉",
    "UY": "乌拉圭", "CR": "哥斯达黎加", "PA": "巴拿马", "DO": "多米尼加",
    "QA": "卡塔尔", "KW": "科威特", "IQ": "伊拉克", "JO": "约旦",
    "CY": "塞浦路斯", "MT": "马耳他", "KZ": "哈萨克斯坦", "MN": "蒙古",
    "CU": "古巴", "PR": "波多黎各", "PY": "巴拉圭", "BO": "玻利维亚",
    "EC": "厄瓜多尔", "GT": "危地马拉", "SV": "萨尔瓦多", "HN": "洪都拉斯",
    "NI": "尼加拉瓜", "SN": "塞内加尔", "GH": "加纳", "ET": "埃塞俄比亚",
    "TZ": "坦桑尼亚", "UG": "乌干达", "AO": "安哥拉", "MZ": "莫桑比克",
}

# _COUNTRY_MAP 反向映射：中文名 → 代码
def _get_available_genres():
    """查询数据库中存在的所有类型，返回中文名 set"""
    import sqlite3
    genres = set()
    try:
        conn = sqlite3.connect(DB_PATH)
        for table,column in (('movie_view','c14'), ('tvshow_view','c08')):
            cur = conn.execute(f'SELECT {column} FROM {table} WHERE {column} IS NOT NULL')
            for row in cur:
                val = row[0]
                if val:
                    for g in str(val).split('/'):
                        g = g.strip()
                        if g: genres.add(g)
        conn.close()
    except Exception as e:
        log(f"Error getting available genres: {e}", xbmc.LOGERROR)
    return genres


def _get_available_regions():
    """查询数据库中存在的所有地区，返回原始值 set"""
    import sqlite3
    regions = set()
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.execute('SELECT c21 FROM movie_view WHERE c21 IS NOT NULL')
        for row in cur:
            val = row[0]
            if val:
                for c in str(val).split('/'):
                    c = c.strip()
                    if c: regions.add(c)
        conn.close()
    except Exception as e:
        log(f"Error getting available regions: {e}", xbmc.LOGERROR)
    return regions



_AVAIL_CACHE_TIME = 0
_AVAIL_CACHE_REVISION = None
_AVAIL_CACHE_TTL = 300

# 标识符 → PO ID 映射（用于从语言文件读取按钮显示名）
_LABEL_PO_MAP = {
    'dateadded': 32100, 'lastplayed': 32101, 'random': 32102, 'latest': 32103, 'rating': 32104,
    'movie': 32110, 'set': 32111, 'tvshow': 32112, 'concert': 32113, 'documentary': 32114, 'all': 32115,
    'this_year': 32120, '2020s': 32121, '2010s': 32122, '2000s': 32123,
    '1990s': 32124, '1980s': 32125, '1970s': 32126, '1960s': 32127, 'older': 32128, '__other__': 32129,
}
_REGION_PO_BASE = 32035
_REGION_CODES_IN_ORDER = [
    "US","GB","FR","DE","IT","JP","KR","CN","HK","TW","IN","RU",
    "CA","AU","NZ","ES","BR","MX","SE","DK","NO","NL","BE","IE",
    "CH","AT","PL","CZ","TH","AR","ZA","TR","PT","FI","HU","IS",
    "GR","CL","CO","PH","SG","MY","ID","IL","EG","AE","SA","LU",
    "RO","BG","UA","IR","NG","KE","PE","PK",
    "BY","RS","VN","HR","SK","SI","LT","LV","EE","MA","DZ","BD",
    "LK","MM","KH","VE","UY","CR","PA","DO","QA","KW","IQ","JO",
    "CY","MT","KZ","MN","CU","PR","PY","BO","EC","GT","SV","HN",
    "NI","SN","GH","ET","TZ","UG","AO","MZ",
]

def _get_filter_label(identifier):
    """从语言文件读取筛选按钮显示名，回退到标识符本身"""
    if identifier in GENRE_LABELS:
        return genre_label(identifier)
    try:
        addon = xbmcaddon.Addon('plugin.video.filteredmovies')
    except Exception:
        return str(identifier)
    # 地区特殊处理：标识符即 ISO 代码
    if len(identifier) == 2 and identifier.isupper():
        for i, code in enumerate(_REGION_CODES_IN_ORDER):
            if code == identifier:
                name = addon.getLocalizedString(_REGION_PO_BASE + i)
                return name if name else identifier
        return identifier
    # 其他：查 _LABEL_PO_MAP
    po_id = _LABEL_PO_MAP.get(identifier)
    if po_id:
        name = addon.getLocalizedString(po_id)
        return name if name else str(identifier)
    return str(identifier)

def _set_filter_labels():
    """将筛选按钮显示名写入窗口属性，供 XML $INFO 读取"""
    home = xbmcgui.Window(10000)
    for group_key in FILTER_MAP:
        for val, btn_id in FILTER_MAP[group_key].get('mapping', {}).items():
            label = _get_filter_label(val)
            home.setProperty(f"MFG.Label.{btn_id}", label)
    # 默认按钮也设 label
    for group_key, data in FILTER_MAP.items():
        def_id = data.get('default')
        if def_id:
            # 默认按钮 label：group 特定
            if group_key == 'filter.mediatype':
                label = _get_filter_label('all')
            elif group_key == 'filter.sort':
                label = _get_filter_label('dateadded')
            elif group_key == 'filter.region':
                label = _get_filter_label('all')
                home.setProperty(f"MFG.Label.{def_id}", label if label != 'all' else '地区')
                continue
            elif group_key == 'filter.genre':
                label = '类型'
            elif group_key == 'filter.year':
                label = '年份'
            elif group_key == 'filter.rating':
                label = '评分'
            else:
                label = ''
            home.setProperty(f"MFG.Label.{def_id}", label)

def _update_filter_availability(force=False):
    """根据数据库内容设置 MFG.HasGenre.xxx / MFG.HasRegion.xxx 窗口属性"""
    global _AVAIL_CACHE_TIME, _AVAIL_CACHE_REVISION
    from .catalog_cache import revision
    current_revision = revision()
    if not force and current_revision == _AVAIL_CACHE_REVISION and time.time() - _AVAIL_CACHE_TIME < _AVAIL_CACHE_TTL:
        return
    _AVAIL_CACHE_TIME = time.time()
    _AVAIL_CACHE_REVISION = current_revision
    home = xbmcgui.Window(10000)

    available_genres = {name for value in _get_available_genres() for name in genre_values(value)}
    log(f"[AVAIL] genres in DB: {sorted(available_genres)}")
    known_genre_vals = set(FILTER_MAP.get("filter.genre", {}).get("mapping", {}).keys())
    known_genre_vals.discard("其他")
    for val, btn_id in FILTER_MAP.get("filter.genre", {}).get("mapping", {}).items():
        if val == "其他":
            unmapped = available_genres - known_genre_vals
            visible = "true" if unmapped else "false"
            log(f"[AVAIL] genre other: unmapped={sorted(unmapped)}, visible={visible}")
        else:
            visible = "true"
        home.setProperty(f"MFG.HasGenre.{btn_id}", visible)

    available_regions = _get_available_regions()
    # DB 地区值统一转大写 ISO 代码用于和 FILTER_MAP 键比较
    from .regions import region_code
    region_codes = {region_code(r) for r in available_regions}
    log(f"[AVAIL] regions in DB (cn): {sorted(region_codes)}")
    known_region_vals = set(FILTER_MAP.get("filter.region", {}).get("mapping", {}).keys())
    known_region_vals.discard("__other__")
    for val, btn_id in FILTER_MAP.get("filter.region", {}).get("mapping", {}).items():
        if val == "__other__":
            unmapped = region_codes - known_region_vals
            visible = "true" if unmapped else "false"
            log(f"[AVAIL] region other: unmapped={sorted(unmapped)}, visible={visible}")
        else:
            visible = "true" if val in region_codes else "false"
        home.setProperty(f"MFG.HasRegion.{btn_id}", visible)



_ADDON_PATH = xbmcvfs.translatePath(xbmcaddon.Addon('plugin.video.filteredmovies').getAddonInfo('path'))


class CharSelectorDialog(xbmcgui.WindowXMLDialog):
    """T9 多字符选择弹窗：连续按同一数字键循环选择字符，超时自动确认。"""

    CONFIRM_DELAY = 1.0  # 秒

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.digit = None
        self.chars = []
        self.current_index = 0
        self.selected_char = None
        self._timer = None
        self._closed = False

    def setup(self, digit, current_input=""):
        """在 doModal 之前调用，设置要显示的数字键。"""
        self.digit = digit
        self.current_input = current_input
        self.chars = _MULTITAP_MAP.get(digit, [digit])
        self.current_index = 0
        self.selected_char = None

    def onInit(self):
        init_started=time.monotonic()
        self.setProperty('MFG.T9Input', getattr(self, 'current_input', ''))
        self._update_display()
        self._reset_timer()
        # 将焦点设到 T9 面板对应数字键上
        idx = 10 if self.digit == '0' else int(self.digit) - 1
        xbmc.executebuiltin(f'SetFocus(6050,{idx})')

    def _update_display(self):
        """根据当前字符数和选中索引，更新 Home Window 属性。"""
        home = xbmcgui.Window(10000)
        # 布局槽位: [0]=上, [1]=左, [2]=中(数字), [3]=右, [4]=下
        # 规则: 第1个字母=左, 第2个字母=上, 第3个字母=右, 第4个字母(如有)=下
        # 4 chars (3 letters, e.g. 8=TUV): 左=T, 上=U, 中=8, 右=V
        # 5 chars (4 letters, e.g. 9=WXYZ): 左=W, 上=X, 中=9, 右=Y, 下=Z

        if len(self.chars) == 5:
            slots = [self.chars[2], self.chars[1], self.chars[0], self.chars[3], self.chars[4]]
        elif len(self.chars) == 4:
            slots = [self.chars[2], self.chars[1], self.chars[0], self.chars[3], ""]
        else:
            slots = [self.chars[0], "", "", "", ""]

        self._slots = slots

        # 写入属性
        for i, ch in enumerate(slots):
            home.setProperty(f"MFG.CharSel.{i}", ch)

        # 活跃索引：current_index 对应 self.chars 中的位置，需映射到 slot 索引
        char = self.chars[self.current_index]
        active_slot = "2"  # default center
        for si, sc in enumerate(slots):
            if sc == char:
                active_slot = str(si)
                break
        home.setProperty("MFG.CharSel.Active", active_slot)

    def _reset_timer(self):
        if self._timer:
            self._timer.cancel()
        self._timer = threading.Timer(self.CONFIRM_DELAY, self._on_timeout)
        self._timer.daemon = True
        self._timer.start()

    def _on_timeout(self):
        if not self._closed:
            if self.selected_char is None:
                self.selected_char = self.chars[self.current_index]
            self.close()

    def onAction(self, action):
        action_id = action.getId()

        if action_id == 10 or action_id == 92:  # ESC / NavBack
            self._cancel_timer()
            self.selected_char = None
            self.close()
            return

        # 方向键直接选择对应字符: 上=slots[0], 左=slots[1], 右=slots[3], 下=slots[4]
        _DIR_SLOT = {3: 0, 1: 1, 2: 3, 4: 4}  # up=3, left=1, right=2, down=4
        if action_id in _DIR_SLOT:
            slot_idx = _DIR_SLOT[action_id]
            slots = getattr(self, '_slots', [])
            if slots and slot_idx < len(slots) and slots[slot_idx]:
                self._cancel_timer()
                # 显示选中效果
                xbmcgui.Window(10000).setProperty("MFG.CharSel.Active", str(slot_idx))
                self.selected_char = slots[slot_idx]
                # 短暂延迟后关闭，让用户看到选中高亮
                self._timer = threading.Timer(0.15, self._on_timeout)
                self._timer.daemon = True
                self._timer.start()
            return

        # 同一数字键 → 循环
        if 58 <= action_id <= 67:
            pressed_digit = str(action_id - 58)
            if pressed_digit == self.digit:
                self.current_index = (self.current_index + 1) % len(self.chars)
                self._update_display()
                self._reset_timer()
            else:
                # 不同数字键 → 确认当前，让父窗口处理新按键
                self._cancel_timer()
                self.selected_char = self.chars[self.current_index]
                self._pending_digit = pressed_digit
                self.close()
            return

    def _cancel_timer(self):
        if self._timer:
            self._timer.cancel()
            self._timer = None

    def close(self):
        self._closed = True
        self._cancel_timer()
        # 清理属性
        home = xbmcgui.Window(10000)
        for i in range(5):
            home.clearProperty(f"MFG.CharSel.{i}")
        home.clearProperty("MFG.CharSel.Active")
        super().close()


# 定义 Skin String 值到按钮 ID 的映射
# 格式: { 'SkinStringName': { 'Value': ButtonID, 'Default': ButtonID } }
from .genres import GENRE_BUTTONS, GENRE_LABELS, genre_name, genre_label, genre_values

FILTER_MAP = {
    'filter.sort': {
        'mapping': {
            'dateadded': 1013,
            'lastplayed': 1015,
            'random': 1014,
            'latest': 1011,
            'rating': 1012
        },
        'default': 1013
    },
    'filter.mediatype': {
        'mapping': {
            'movie': 6002,
            'set': 6003,
            'tvshow': 6004,
            'concert': 6005,
            'documentary': 6006
        },
        'default': 6001
    },
    'filter.genre': {
        'mapping': dict(GENRE_BUTTONS, **{'其他':2015}),
        'default':2001
    },
    'filter.region': {
        'mapping': {
            'CN': 3002,
            'HK': 3003,
            'TW': 3004,
            'US': 3005,
            'JP': 3006,
            'KR': 3007,
            'TH': 3008,
            'IN': 3009,
            'GB': 3010,
            'FR': 3011,
            'DE': 3012,
            'RU': 3014,
            'CA': 3015,
            '__other__': 3013
        },
        'default': 3001
    },
    'filter.year': {
        'mapping': {
            'this_year': 4002,
            '2020s': 4003,
            '2010s': 4004,
            '2000s': 4005,
            '1990s': 4006,
            '1980s': 4007,
            '1970s': 4008,
            '1960s': 4009,
            'older': 4010
        },
        'default': 4001
    },
    'filter.rating': {
        'mapping': {
            '10-9': 5002,
            '9-8': 5003,
            '8-7': 5004,
            '7-6': 5005,
            '6分以下': 5006
        },
        'default': 5001
    }
}

# 构建 ID -> (组, 值) 的反向查找映射
FILTER_ID_TO_INFO_MAP = {}
for group, data in FILTER_MAP.items():
    # 默认按钮
    FILTER_ID_TO_INFO_MAP[data['default']] = (group, '')
    # 映射按钮
    for val, btn_id in data['mapping'].items():
        FILTER_ID_TO_INFO_MAP[btn_id] = (group, val)

SORT_OPTIONS = (
    ('dateadded', '添加日期', 'descending'),
    ('rating', '评分', 'descending'),
    ('title', '标题', 'ascending'),
    ('premiered', '发行日期', 'descending'),
)

class FilterWindow(xbmcgui.WindowXML):
    def __init__(self, *args, **kwargs):
        self.xml_name = args[0] if args else ""
        self._opening_child = False
        self._background_policy = None
        self._grid_page = self.xml_name in ('Custom_5112_AllMovie.xml', 'Custom_5114_Movie.xml')
        self._grid_bind_queue = queue.Queue()
        self._home_bind_queue = queue.Queue()
        self._grid_initial_focus = self._grid_page
        self._grid_pending_right = None
        self._grid_panel_animations = None
        self._grid_restore_animations = False
        self._grid_force_first_selection = False
        self._has_filter_toolbar = self.xml_name == 'Custom_5112_AllMovie.xml'
        self._filters_expanded = False
        self._sort_open = False
        self._sort_method,self._sort_order = 'dateadded','descending'
        self._facet_queue = queue.Queue()
        self._active_filter_rows = []
        self._facet_row_buttons = {}
        self._filter_layout_signature = None
        self.is_search = self.xml_name == 'Custom_5113_SearchMovie.xml'

    def _sort_scope(self):
        # Navigation assigns initial_mediatype after construction. Keep the
        # entry category stable when temporary media-type filters change.
        if hasattr(self,'collection_id'):
            return 'set'
        return getattr(self,'initial_mediatype',
                       'all' if self.xml_name=='Custom_5112_AllMovie.xml' else 'movie')

    def _restore_sort_preferences(self):
        if self._grid_page:
            from .sort_preferences import load
            self._sort_method,self._sort_order = load(ADDON_DATA_PATH,self._sort_scope())

    def _input_window(self):
        # Search must not overwrite the parent wall's input or filter state.
        return self if (self.is_search or getattr(self, 'reset_filters', False)) else xbmcgui.Window(10000)

    def _set_button_state(self, btn_id, is_selected):
        color_val = 'FFEB9E17' if is_selected else 'FFFFFFFF'
        self.setProperty(f'MFG.FilterColor.{btn_id}', color_val)

    def _load_state_from_skin(self):

        self.filter_state = {}
        
        # 尝试先从单个 blob 加载
        blob = '' if (self.is_search or getattr(self, 'reset_filters', False)) else xbmc.getInfoLabel('Skin.String(MFG.State)')
        if blob:
            try:
                decoded = base64.b64decode(blob).decode('utf-8')
                loaded_state = json.loads(decoded)
                if not isinstance(loaded_state,dict):
                    raise ValueError('Legacy filter state is not an object')
                # Old skins may have obsolete IDs or malformed saved values.
                cleaned={}
                for group,data in FILTER_MAP.items():
                    default_id=data['default']
                    fallback={'id':default_id,'value':FILTER_ID_TO_INFO_MAP[default_id][1]}
                    raw=loaded_state.get(group)
                    def normalize(entry):
                        if not isinstance(entry,dict):return fallback.copy()
                        try:identifier=int(entry.get('id'))
                        except (TypeError,ValueError):return fallback.copy()
                        mapped=FILTER_ID_TO_INFO_MAP.get(identifier)
                        if not mapped or mapped[0]!=group:return fallback.copy()
                        return {'id':identifier,'value':mapped[1]}
                    if group=='filter.rating':
                        entries=raw if isinstance(raw,list) else [raw]
                        cleaned[group]=[normalize(entry) for entry in entries] or [fallback]
                    else:cleaned[group]=normalize(raw)
                loaded_state=cleaned
                
                # 合并加载的状态和默认值，确保所有筛选组都被初始化
                for group, data in FILTER_MAP.items():
                    if group not in loaded_state:
                        default_id = data['default']
                        _, default_val = FILTER_ID_TO_INFO_MAP[default_id]
                        default_obj = {'id': default_id, 'value': default_val}
                        if group == 'filter.rating':
                            loaded_state[group] = [default_obj]
                        else:
                            loaded_state[group] = default_obj
                self.filter_state = loaded_state
                return
            except Exception as e:
                log(f"Error loading state blob: {e}", xbmc.LOGERROR)
        
        # 使用默认值初始化 (ID + 值)
        for group, data in FILTER_MAP.items():
            default_id = data['default']
            _, default_val = FILTER_ID_TO_INFO_MAP[default_id]
            default_obj = {'id': default_id, 'value': default_val}
            
            if group == 'filter.rating':
                self.filter_state[group] = [default_obj]
            else:
                self.filter_state[group] = default_obj

    def _save_state_to_skin(self):
        try:
            # 序列化为 JSON 和 Base64
            json_str = json.dumps(self.filter_state)
            encoded = base64.b64encode(json_str.encode('utf-8')).decode('utf-8')
            
            # 保存到单个 Skin String
            xbmc.executebuiltin(f'Skin.SetString(MFG.State,{encoded})')
            log("Saved state to Skin.String(MFG.State)")
        except Exception as e:
            log(f"Error saving state: {e}", xbmc.LOGERROR)

    def update_highlights(self, target_group=None):
        """
        根据 self.filter_state 设置窗口属性以高亮显示活动按钮。
        如果提供了 target_group，则仅更新该组。
        """
        groups = [target_group] if target_group else self.filter_state.keys()

        for group in groups:
            state = self.filter_state.get(group)
            if state is None: continue

            data = FILTER_MAP.get(group)
            if not data: continue
            
            # 收集该组的所有 ID 以便先重置它们（或仅设置正确的状态）
            all_ids = list(data['mapping'].values())
            all_ids.append(data['default'])
            
            if isinstance(state, list): # 多选 (评分)
                active_ids = [x['id'] for x in state]
                for btn_id in all_ids:
                    self._set_button_state(btn_id, btn_id in active_ids)
            else: # 单选
                active_id = state['id']
                for btn_id in all_ids:
                    self._set_button_state(btn_id, btn_id == active_id)

    def _fav_from_custom_keymaps(self):
        """解析用户的 keymap XML，提取绑定了 toggle_favourite 的自定义按键代码"""
        import os
        import json
        import xml.etree.ElementTree as ET
        
        # 记录全局的键盘映射 button_code -> action_str
        global_keyboard_map = {}
        
        # 加载键盘映射 JSON
        mapping_file = os.path.join(os.path.dirname(__file__), '..', 'resources', 'keyboard_mapping.json')
        name_to_code = {}
        try:
            with open(mapping_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                name_to_code = data.get("name_to_code", {})
        except Exception as e:
            log(f"Failed to load keyboard mapping json: {e}", xbmc.LOGERROR)

        folder = xbmcvfs.translatePath('special://profile/keymaps/')
        if os.path.exists(folder):
            # 获取所有xml并按字母顺序排序，以正确处理Kodi的覆盖加载机制
            xml_files = sorted([f for f in os.listdir(folder) if f.lower().endswith('.xml')])
            
            for file_name in xml_files:
                file_path = os.path.join(folder, file_name)
                try:
                    tree = ET.parse(file_path)
                    root = tree.getroot()
                    
                    # Kodi 的 keymap 加载是不区分大小写的，我们遍历子节点匹配
                    for global_node in root:
                        if global_node.tag.lower() == 'global':
                            for keyboard_node in global_node:
                                if keyboard_node.tag.lower() == 'keyboard':
                                    # 遍历所有的按键标签
                                    for elem in keyboard_node:
                                        tag = elem.tag.lower()
                                        text = elem.text or ''
                                        button_code = None
                                        
                                        # 处理 <key id="xxx">
                                        if tag == 'key' and 'id' in elem.attrib:
                                            try:
                                                button_code = int(elem.attrib['id'])
                                            except ValueError:
                                                pass
                                        else:
                                            # 处理具有命名标签的映射
                                            if tag in name_to_code:
                                                button_code = name_to_code[tag]
                                                
                                        # 将得到的 button_code 映射覆盖，这就是Kodi执行的加载合并逻辑
                                        if button_code is not None:
                                            global_keyboard_map[button_code] = text
                                            
                except Exception as e:
                    log(f"Failed to parse keymap {file_path}: {e}", xbmc.LOGERROR)

        # 从最终合并完成的字典中，筛选出分配给了我们的 toggle_favourite 脚本的按钮代码
        allowed_button_codes = set()
        for code, action_text in global_keyboard_map.items():
            if 'plugin.video.filteredmovies' in action_text and 'toggle_favourite' in action_text:
                allowed_button_codes.add(code)

        log(f"Parsed favourite button codes: {allowed_button_codes}")
        return allowed_button_codes

    def onInit(self):
        init_started=time.monotonic()
        self._window_id = xbmcgui.getCurrentWindowId()
        self._last_reload_id = xbmcgui.Window(10000).getProperty('MFG.ReloadID')
        self._cleaned_up = False
        if (getattr(self, 'running', False)
                and not self._input_stop.is_set()
                and self.worker.is_alive()
                and not self.poster_loader.closed.is_set()):
            # Kodi invokes onInit again after returning from a child window.
            # Reuse workers and Python state; only rebind rebuilt GUI controls.
            if self._grid_page:
                if not self.getControl(9999).size():
                    self._filter_layout_signature = None
                self._update_toolbar()
            # Playback updates the DB/WAL, but does not invalidate this list's
            # selection. The modal guard may already be cleared when onInit
            # arrives; the saved query and surviving controls are authoritative.
            if (self._can_restore_grid() or self.poster_loader.restore_home()):
                self._restore_navigation_state()
                log("WALL resumed from child with navigation state preserved")
            else:
                if self._grid_page:
                    self.setProperty('MFG.ReturnFocus','false')
                self.poster_loader.refresh(self._query_filters())
                log("WALL window resumed with existing workers")
            return
        if self.xml_name.startswith('Custom_5111_'):
            self._background_policy = background_animations(xbmcvfs.translatePath('special://skin/'))
        elif hasattr(self, 'navigation_started'):
            log("WALL navigation %s opened %.3fs" %
                (self.xml_name, time.monotonic() - self.navigation_started))
        if self.xml_name == 'Custom_5112_AllMovie.xml':
            self._grid_panel_animations = panel_animations(
                _ADDON_PATH + '/resources/skins/Default/1080i/' + self.xml_name)
        # Dynamic content controls are owned by the window, not skin invokers.
        from .wall_loader import WallLoader
        self.setProperty('MFG.Home', 'true' if self.xml_name.startswith('Custom_5111_') else 'false')
        self.setProperty('MFG.SearchMode', 'true' if self.is_search else 'false')
        self.setProperty('MFG.InputWindowId', str(self._window_id))
        self.setProperty('MFG.PageTitle', getattr(self, 'page_title', ''))
        if self._grid_page:
            self.setProperty('MFG.ReturnFocus','false')
            self._has_filter_toolbar = getattr(self, 'initial_mediatype', 'all') != 'set'
            self.setProperty('MFG.FilterSupported', 'true' if self._has_filter_toolbar else 'false')
            self.setProperty('MFG.AllPage', 'true' if self.xml_name == 'Custom_5112_AllMovie.xml' else 'false')
            self.setProperty('MFG.FacetsReady', 'false')
            self.setProperty('MFG.FacetMessage', '正在读取筛选条件…')
            if self.xml_name == 'Custom_5112_AllMovie.xml':
                self._active_filter_rows = [1006]
                self._facet_row_buttons[1006] = list(range(6001, 6007))
        # 设置筛选按钮多语言 label
        if not self.xml_name.startswith("Custom_5111_"):
            _set_filter_labels()
        # Set property to indicate window is open
        # Clear T9 input
        self.setProperty("t9_input", "")
        # Load state from Skin into Python memory
        self._load_state_from_skin()
        if hasattr(self,'initial_mediatype'):
            self.filter_state['filter.mediatype']={'id':{'all':6001,'movie':6002,'set':6003,'tvshow':6004,'concert':6005,'documentary':6006}.get(self.initial_mediatype,6001),'value':self.initial_mediatype}
        if self._grid_page:
            self._restore_sort_preferences()
            self._update_toolbar()
        # 立即初始化筛选高亮
        if not self.xml_name.startswith("Custom_5111_"):
            self.update_highlights()
        
        
        # 初始化属性，确保非空
        self._input_window().setProperty("MFG.T9Input", "")
        self.setProperty("MFG.IsRefreshing", "false")
        # self.refresh_container()
        self.input_queue = queue.Queue()
        self.running = True
        self._input_stop = threading.Event()
        self.worker = threading.Thread(target=self._t9_input_worker,
                                       args=(self._input_stop, self.input_queue),
                                       name="PosterWallInput")
        self.worker.daemon = True
        self.worker.start()
        self.poster_loader = WallLoader(self)
        self.poster_loader.refresh(self._query_filters())
        log("WALL window init %.3fs" % (time.monotonic()-init_started))

    def _query_filters(self):
        filters = {}
        for group,item in self.filter_state.items():
            if group == 'filter.rating':
                for entry in item:
                    if entry.get('value'):
                        filters[group+'.'+str(entry['value'])] = True
            elif isinstance(item,dict) and item.get('value') is not None:
                filters[group] = item['value']
        text = self._input_window().getProperty('MFG.T9Input').strip()
        if text:
            filters = {key:value for key,value in filters.items() if key in ('filter.mediatype','filter.sort')}
            filters['filter.t9'] = '|'+text if text.isdigit() else text
        if self._grid_page:
            filters['filter.sort'] = self._sort_method
            filters['filter.sortorder'] = self._sort_order
        if hasattr(self, 'collection_id'):
            filters['filter.mediatype'] = 'movie'
            filters['_collection_id'] = self.collection_id
        return filters

    def close(self):
        if getattr(self, 'poster_loader', None) is not None:
            self.poster_loader.stop()
        if hasattr(self, '_input_stop'):
            self._input_stop.set()
        super().close()

    def _is_active(self):
        return xbmcgui.getCurrentWindowId() == self._window_id

    def _t9_input_worker(self, stop, input_queue):
        # Capture immutable worker resources before Thread.start(), not after
        # scheduling: a returning window must not revive an older worker.
        monitor = xbmc.Monitor()
        last_input = ""
        last_input_time = time.time()
        while not stop.is_set():
            # 检查 Kodi 是否正在关闭
            if monitor.abortRequested():
                log("Abort requested, closing window")
                self.close()
                break

            try:
                events = []
                # 每0.1秒唤醒检查一次
                try:
                    events.append(input_queue.get(timeout=0.1))
                    # 只要队列不为空，就一次性取出所有积压事件
                    while not input_queue.empty():
                        try:
                            events.append(input_queue.get_nowait())
                        except queue.Empty:
                            break
                except queue.Empty:
                    pass
                
                if stop.is_set():
                    return
                if not self._is_active():
                    # Parent windows stay alive behind child windows. Never
                    # read their controls or react to the child's shared input.
                    continue
                reload_id=xbmcgui.Window(10000).getProperty('MFG.ReloadID')
                if reload_id != self._last_reload_id:
                    self._last_reload_id=reload_id
                    self.refresh_container()
                current_input = self._input_window().getProperty("MFG.T9Input") or ""
                # current_input = last_input
                # 批量处理所有事件
                for event in events:
                    if not event: 
                        continue
                    etype, evalue = event
                    if etype == 'input':
                        current_input += evalue
                    elif etype == 'delete':
                        current_input = current_input[:-1] if current_input else ""
                    elif etype == 'clear':
                        current_input = ""
                    elif etype == 'close':
                        log("close worker")
                        return
                if events:
                    self._input_window().setProperty("MFG.T9Input", current_input)
                    last_input_time = time.time()

                    # 系列电影搜索未启用时，拦截输入
                    if current_input and current_input != "000000":
                        mt_state = self.filter_state.get('filter.mediatype', {})
                        if mt_state.get('value') in ('系列电影','set'):
                            enable_set_search = xbmcaddon.Addon().getSetting('enable_set_search') == 'true'
                            if not enable_set_search:
                                notification(_loc(32175))
                                current_input = ""
                                self._input_window().setProperty("MFG.T9Input", current_input)
                                last_input = current_input
                                continue

                if current_input == "000000":
                    t9_helper.helper.ensure_search_index_ready_async(show_progress=True, skip_check=True)
                    notification(_loc(32176), sound=True)
                    log("Magic code 000000 detected. Rebuilding movie C16/tvshow C15 T9 storage.", xbmc.LOGWARNING)
                    current_input = ""
                    self._input_window().setProperty("MFG.T9Input", current_input)
                    last_input = current_input

                if current_input != last_input:
                    if not current_input:
                        # 输入已清空，立即刷新列表
                        self.refresh_container()
                        last_input_time = time.time()
                        last_input = current_input
                    else:
                        if (time.time() - last_input_time > 0.5):
                            self.refresh_container()
                            last_input = current_input
                
            except Exception as e:
                log(f"Worker error: {e}", xbmc.LOGERROR)

    def cleanup(self):
        if getattr(self, '_cleaned_up', False):
            return
        self._cleaned_up = True
        for pending_queue in (self._grid_bind_queue,self._home_bind_queue):
            while True:
                try:
                    job = pending_queue.get_nowait()
                except queue.Empty:
                    break
                job['cancelled'] = True
                job['items'] = None
                if 'rows' in job:job['rows']=None
                job['done'].set()
        if getattr(self,'poster_loader',None) is not None:
            self.poster_loader.stop()
            self.poster_loader.worker.join(timeout=.6)
        if hasattr(self,'_input_stop'):self._input_stop.set()
        self.running = False
        if hasattr(self, 'input_queue'):
            self.input_queue.put(('close', None))
        if getattr(self, 'worker', None) is not None and self.worker.is_alive():
            self.worker.join(timeout=1.0)
        if (hasattr(self, 'filter_state') and not self.is_search
                and not getattr(self, 'reset_filters', False)):
            self._save_state_to_skin()
        # Drop Python ControlList's retained ListItems as well as GUI content.
        ids = (1002,1004,1005,1006,1007,1008) if self.xml_name.startswith('Custom_5111_') else (9999,)
        for cid in ids:
            try:
                self.getControl(cid).reset()
            except RuntimeError:
                pass
        self.poster_loader = None
        self.worker = None
        # 清除全局属性
        self._input_window().clearProperty("MFG.T9Input")

    def bind_grid_items(self, items, generation, append):
        """Worker posts one bind; native selection reads stay on GUI callbacks."""
        loader = self.poster_loader
        if loader is None or not loader.valid(generation):
            return False
        started = time.monotonic()
        job = {'items': items, 'generation': generation, 'append': append,
               'loader': loader, 'done': threading.Event(), 'ok': False}
        self._grid_bind_queue.put(job)
        xbmc.executebuiltin('SendClick(%d,7193)' % self._window_id)
        while not job['done'].wait(.05):
            if (not loader.valid(generation) or not self._is_active()
                    or time.monotonic() - started > 5):
                job['cancelled'] = True
                return False
        return job['ok']

    def queue_playback_state(self, state, generation, position):
        loader = self.poster_loader
        if loader is None or not loader.valid(generation):
            return
        self._grid_bind_queue.put({'items':None, 'playback_state':state,
            'position':position, 'generation':generation, 'loader':loader,
            'done':threading.Event(), 'ok':False})
        xbmc.executebuiltin('SendClick(%d,7193)' % self._window_id)

    def bind_home_rows(self, cid, rows, items, generation):
        loader = self.poster_loader
        if loader is None or not loader.valid(generation) or not self._is_active():
            return False
        job={'cid':cid,'rows':rows,'items':items,'generation':generation,
             'loader':loader,'done':threading.Event(),'ok':False}
        self._home_bind_queue.put(job)
        xbmc.executebuiltin('SendClick(%d,7197)' % self._window_id)
        started=time.monotonic()
        while not job['done'].wait(.05):
            if not loader.valid(generation) or not self._is_active() or time.monotonic()-started>5:
                job['cancelled']=True
                return False
        return job['ok']

    def _apply_home_binds(self):
        while True:
            try:job=self._home_bind_queue.get_nowait()
            except queue.Empty:return
            try:
                loader=job['loader']
                with loader.lock:
                    if (job.get('cancelled') or loader is not self.poster_loader
                            or not loader.valid(job['generation']) or not self._is_active()):
                        continue
                    job['ok']=self._apply_home_row(job['cid'],job['rows'],job['items'],job['generation'])
            except (RuntimeError,AttributeError) as exc:
                log('WALL home bind failed: %s' % type(exc).__name__,xbmc.LOGWARNING)
            finally:
                job['items']=None;job['rows']=None;job['done'].set()

    def _apply_home_row(self, cid, rows, items, generation):
        """Only the active GUI callback owns native recommendation controls."""
        from .home_updates import sync_row
        loader = self.poster_loader
        if loader is None:
            return False
        with loader.lock:
            if not loader.valid(generation) or not self._is_active():
                return False
            panel = self.getControl(cid)
            sync_row(panel, loader.home_rows.get(cid,[]), rows, items)
            loader.home_rows[cid] = list(rows)
            loader.message(cid, 'ready' if rows else 'empty', '' if rows else '暂无推荐内容。')
            if not rows and self.getFocusId() == cid:
                self.setFocusId(1003)
            return True

    def _apply_grid_binds(self):
        while True:
            try:
                job = self._grid_bind_queue.get_nowait()
            except queue.Empty:
                return
            try:
                loader = job['loader']
                with loader.lock:
                    if (job.get('cancelled') or loader is not self.poster_loader
                            or not loader.valid(job['generation']) or not self._is_active()):
                        continue
                    panel = self.getControl(9999)
                    if 'playback_state' in job:
                        position = job['position']
                        if 0 <= position < panel.size():
                            from .playback_state import apply_state
                            job['ok'] = apply_state(panel.getListItem(position), job['playback_state'])
                        continue
                    selected = (max(0, panel.getSelectedPosition())
                                if job['append'] and not self._grid_force_first_selection else 0)
                    if not job['append']:
                        panel.reset()
                    panel.addItems(job['items'])
                    # addItems rebinds the full native list with selection 0;
                    # restore within the same batch before the next GUI frame.
                    target = selected
                    if (self._grid_pending_right == selected + 1
                            and self._grid_pending_right < panel.size()):
                        target = self._grid_pending_right
                        self._grid_pending_right = None
                    if target:
                        panel.selectItem(target)
                    if self._grid_initial_focus and panel.size():
                        if self.getFocusId() != 9999:
                            self.setFocusId(9999)
                        self._grid_initial_focus = False
                    job['ok'] = True
            except (RuntimeError, AttributeError) as exc:
                log('WALL grid bind failed: %s' % exc, xbmc.LOGERROR)
            finally:
                job['items'] = None
                job['done'].set()

    def _grid_edge(self, direction):
        if not self._grid_page or not self._is_active():
            return
        panel = self.getControl(9999)
        selected = panel.getSelectedPosition()
        # SendClick is delivered after Kodi has already handled the key.
        # A delayed row-edge callback must not move a newer selection again.
        columns = 6
        if (direction < 0 and selected % columns != 0 or
                direction > 0 and selected % columns != columns - 1
                and selected != panel.size() - 1):
            return
        target = selected + direction
        if 0 <= target < panel.size():
            self._grid_pending_right = None
            panel.selectItem(target)
        elif direction > 0 and target < self.poster_loader.total:
            # Keep the requested next item while its bounded page arrives.
            self._grid_pending_right = target
            self.poster_loader.next_page()

    def _update_toolbar(self):
        """Keep panel bounds and focusable toolbar in the same GUI callback."""
        self.setProperty('MFG.FiltersExpanded', 'true' if self._filters_expanded else 'false')
        self.setProperty('MFG.SortOpen', 'true' if self._sort_open else 'false')
        for index, (method, label, _default_order) in enumerate(SORT_OPTIONS):
            arrow = ('↑' if self._sort_order == 'ascending' else '↓') if method == self._sort_method else ''
            self.setProperty('MFG.SortOption.%d' % index, label)
            self.setProperty('MFG.SortArrow.%d' % index, arrow)
            if method == self._sort_method:
                self.setProperty('MFG.SortLabel', label + ' ' + arrow)
        self.setProperty('MFG.FilterLabel', '筛选 ▴' if self._filters_expanded else '筛选 ▾')
        panel = self.getControl(9999)
        top = 125 + len(self._active_filter_rows) * 45 if self._has_filter_toolbar and self._filters_expanded else 90
        if panel.getX() != 30 or panel.getY() != top:
            panel.setPosition(30, top)
        if panel.getHeight() != 1080 - top:
            panel.setHeight(1080 - top)
        # The message shares the panel's remaining content area. Recenter
        # after expanding/collapsing facets so it cannot cover filter text.
        self.getControl(7290).setPosition(30, top + (1080 - top - 90) // 2)
        if self._has_filter_toolbar:
            signature = tuple(self._active_filter_rows)
            if signature != self._filter_layout_signature:
                wrappers = {1006:7400, 1002:7401, 1003:7402, 1004:7403, 1005:7404}
                for index, cid in enumerate(self._active_filter_rows):
                    self.getControl(wrappers[cid]).setPosition(0, index * 45)
                    previous = self._facet_row_buttons[self._active_filter_rows[index-1]][0] if index else 7201
                    following = self._facet_row_buttons[self._active_filter_rows[index+1]][0] if index+1 < len(signature) else 9999
                    # grouplists have no Python wrapper; their button controls do.
                    for button_id in self._facet_row_buttons[cid]:
                        button = self.getControl(button_id)
                        button.controlUp(self.getControl(previous))
                        button.controlDown(self.getControl(following))
                self._filter_layout_signature = signature
            first = self._facet_row_buttons[self._active_filter_rows[0]][0] if self._filters_expanded and self._active_filter_rows else 9999
            for button_id in (7200, 7201):
                self.getControl(button_id).controlDown(self.getControl(first))

    def bind_filter_facets(self, facets, generation):
        """Post primitive metadata; native layout/focus changes stay on the GUI thread."""
        self._facet_queue.put((generation, facets))
        xbmc.executebuiltin('SendClick(%d,7195)' % self._window_id)

    def _apply_filter_facets(self):
        while True:
            try:
                generation, facets = self._facet_queue.get_nowait()
            except queue.Empty:
                return
            if not self._is_active() or not self.poster_loader.valid(generation):
                continue
            available_genres = {name for value in facets['genre'] for name in genre_values(value)}
            known_genres = set(GENRE_BUTTONS)
            known_regions = set(FILTER_MAP['filter.region']['mapping']) - {'__other__'}
            rows = [1006] if self.xml_name == 'Custom_5112_AllMovie.xml' else []
            row_buttons = {1006:list(range(6001,6007))} if rows else {}
            groups = [('genre',1002,'HasGenre'), ('region',1003,'HasRegion'),
                      ('year',1004,'HasYear'), ('rating',1005,'HasRating')]
            for field, cid, prefix in groups:
                data = FILTER_MAP['filter.'+field]
                available = []
                for value, button_id in data['mapping'].items():
                    if field == 'genre':
                        visible = bool(available_genres-known_genres) if value == '其他' else True
                    elif field == 'region':
                        visible = bool(facets[field]-known_regions) if value == '__other__' else value in facets[field]
                    elif field == 'year':
                        if value == 'this_year':
                            visible = int(time.strftime('%Y')) in facets[field]
                        elif value == 'older':
                            visible = any(year < 1960 for year in facets[field])
                        else:
                            start = int(value[:4]);visible = any(start <= year <= start+9 for year in facets[field])
                    else:
                        bounds = {'10-9':(9,10),'9-8':(8,9),'8-7':(7,8),'7-6':(6,7),'6分以下':(0,6)}
                        low,high = bounds[value]
                        visible = any(low <= rating <= high if value != '6分以下' else 0 < rating < 6 for rating in facets[field])
                    self.setProperty('MFG.%s.%d' % (prefix,button_id), 'true' if visible else 'false')
                    if visible:
                        available.append(button_id)
                # A single usable option cannot narrow the category.
                state = self.filter_state.get('filter.'+field, {})
                selected_entries = state if isinstance(state, list) else [state]
                has_selection = any(entry.get('value') for entry in selected_entries)
                # Keep a reset button accessible if a category change leaves
                # an active constraint with no matching/alternative options.
                meaningful = (len(available) > 1 and len(facets[field]) > 1) or has_selection
                if field == 'genre':
                    # Keep common genres usable even in an empty library.
                    # Collections have no genre filtering in Kodi.
                    meaningful = self.filter_state.get('filter.mediatype',{}).get('value')!='set'
                self.setProperty('MFG.FacetRow.%d' % cid, 'true' if meaningful else 'false')
                if meaningful:
                    rows.append(cid)
                    row_buttons[cid] = [data['default']] + available
            self._active_filter_rows, self._facet_row_buttons = rows, row_buttons
            self._filter_layout_signature = None
            self.setProperty('MFG.FacetsReady','true')
            self.setProperty('MFG.FacetMessage','' if rows else '当前分类暂无可用的筛选条件。')
            self._update_toolbar()

    def _toggle_sort(self):
        self._sort_open = not self._sort_open
        self._update_toolbar()
        if self._sort_open:
            selected = next(i for i, option in enumerate(SORT_OPTIONS) if option[0] == self._sort_method)
            self.setFocusId(7220 + selected)
        else:
            self.setFocusId(7200)

    def _select_sort(self, index):
        method, _label, default_order = SORT_OPTIONS[index]
        if method == self._sort_method:
            self._sort_order = 'ascending' if self._sort_order == 'descending' else 'descending'
        else:
            self._sort_method, self._sort_order = method, default_order
        from .sort_preferences import save
        if not save(ADDON_DATA_PATH,self._sort_method,self._sort_order,self._sort_scope()):
            log('Unable to save the last selected sort preference',xbmc.LOGWARNING)
        self._sort_open = False
        self._grid_pending_right = None
        self._grid_force_first_selection = True
        self._update_toolbar()
        self.setFocusId(7200)
        self.refresh_container()

    def _grid_up(self):
        # This bridge is called only at the native panel's top boundary.
        self.setFocusId(self._facet_row_buttons[self._active_filter_rows[-1]][0]
                       if self._has_filter_toolbar and self._filters_expanded and self._active_filter_rows else 7200)

    def _grid_back(self):
        if (not self._grid_page or not self._is_active()
                or not xbmc.getCondVisibility('Container(9999).HasPrevious')):
            return False
        self._grid_pending_right = None
        self._grid_force_first_selection = True
        loader = self.poster_loader
        with loader.lock:
            # Rebind existing native items: reset scroll position immediately.
            # No media query, reconstruction, or persistent extra ListItems.
            panel = self.getControl(9999)
            if self._grid_panel_animations is not None:
                panel.setAnimations(self._grid_panel_animations[1])
                self._grid_restore_animations = True
            panel.addItems([])
            self.setFocusId(9999)
        return True

    def onAction(self, action):
        action_id = action.getId()
        button_code = action.getButtonCode()
        if self._grid_page and action_id in (10, 92):
            if self._sort_open:
                self._sort_open = False
                self._update_toolbar()
                self.setFocusId(7200)
                return
            if self._filters_expanded and self.getFocusId() != 9999:
                self._filters_expanded = False
                self._update_toolbar()
                self.setFocusId(7201)
                return
        if self._grid_page and self._sort_open and action_id in (1, 2, 3, 4):
            if self.getFocusId() not in range(7220, 7224):
                self._sort_open = False
                self._update_toolbar()
        if self._grid_restore_animations:
            self.getControl(9999).setAnimations(self._grid_panel_animations[0])
            self._grid_restore_animations = False
        if action_id in (1, 2, 3, 4):
            self._commit_navigation_input()
            self._grid_force_first_selection = False
            if self._grid_page:
                self.setProperty('MFG.ReturnFocus','false')
        if action_id in (1, 3, 4):
            self._grid_pending_right = None
        if action_id in (10, 92) and self._grid_back():
            return
        # Native ControlList reads belong to Kodi's active window callback.
        # The loader worker only queries media and posts ListItem bind messages.
        try:
            loader = getattr(self, 'poster_loader', None)
            if (loader is not None and not loader.closed.is_set()
                    and self.getProperty('MFG.Home') != 'true'
                    and self.getFocusId() == 9999):
                panel = self.getControl(9999)
                size = panel.size()
                if size > 0 and panel.getSelectedPosition() >= size - 40:
                    loader.next_page()
        except (RuntimeError, AttributeError):
            pass
        # High-rate navigation should not generate one INFO log per key.
        
        if action_id == 11:
            try:control=self.getFocusId()
            except RuntimeError:return
            if control in (9999,1002,1004,1005,1006,1007,1008):
                self._show_selected(control,info_only=True)
                return
        # 关闭/返回/ESC等必须立即处理的按键
        if action_id == 10:  # ESC
            self.close()
            return
            
        if action_id == 122:  # HOME / Search / Built-in
            # Kodi's built-in commands (including SetFocus) are not a Back key.
            # JSON-RPC/event-server commands have no physical button code.
            if button_code == 0:
                return
            # 自动放行的全局系统按键列表
            if button_code in [61616, 61622, 61674, 61448, 61467, 61588, 61440]:
                log(f"Auto allowing action 122 for system button_code {button_code}")
                return

            # 延迟加载按键映射
            if not hasattr(self, 'fav_button_codes'):
                log("Parsing custom keymaps for the first time on action 122")
                self.fav_button_codes = self._fav_from_custom_keymaps()

            # 动态检查该 button_code 是否在用户的 keymap中被绑定为 toggle_favourite
            if self.fav_button_codes and button_code in self.fav_button_codes:
                log(f"Ignoring action 122 for button_code {button_code} because it is mapped to toggle_favourite")
                return
            self.close()
            return
        if action_id == 92:  # NavBack
            # 有输入情况输入，没有输入退出页面
            current_text = self._input_window().getProperty("MFG.T9Input")
            if current_text and len(current_text) > 0:
                self.input_queue.put(('clear', None))
            else:
                self.close()
            return

        # 其他输入全部交给worker
        if 58 <= action_id <= 67:  # 0-9
            digit = str(action_id - 58)
            if self._is_digit_letter_mode() and digit in _MULTITAP_MAP and not self.is_search:
                self._open_char_selector(digit)
            else:
                self.input_queue.put(('input', digit))
            return
        if action_id in [110, 112, 80]:  # Backspace, Delete, ItemDelete
            self.input_queue.put(('delete', None))
            return
        if action_id == 13:  # Stop -> 打开虚拟键盘输入
            self._open_keyboard_input()
            return

        pass

    def _is_digit_letter_mode(self):
        try:
            return xbmcaddon.Addon('plugin.video.filteredmovies').getSettingString('input_mode') != 'pure_digit'
        except Exception:
            return True

    def _open_char_selector(self, digit):
        """打开字符选择弹窗，连续按键循环选择。"""
        while digit:
            dlg = CharSelectorDialog(
                'Custom_1117_CharSelector.xml', _ADDON_PATH, 'Default', '1080i')
            dlg.setup(digit, self._input_window().getProperty("MFG.T9Input"))
            dlg.doModal()
            selected = dlg.selected_char
            pending = getattr(dlg, '_pending_digit', None)
            del dlg
            if selected:
                self.input_queue.put(('input', selected))
            # 如果用户按了不同的数字键，继续为新数字打开选择器
            if pending and pending in _MULTITAP_MAP:
                digit = pending
            elif pending:
                # 0 或 1 直接输入
                self.input_queue.put(('input', pending))
                digit = None
            else:
                digit = None

    def _open_keyboard_input(self):
        """打开虚拟键盘，将输入结果（大写字母）设为 T9 输入"""
        current_input = self._input_window().getProperty("MFG.T9Input") or ""
        kb = xbmc.Keyboard(current_input, _loc(32147))
        kb.doModal()
        if kb.isConfirmed():
            text = kb.getText().strip().upper()
            # 清空后设置新值，让 worker 检测到变化并刷新
            self.input_queue.put(('clear', None))
            if text:
                self.input_queue.put(('input', text))

    def _handle_filter_click(self, controlId):
        # 检查 controlId 是否为已知的筛选按钮
        if controlId not in FILTER_ID_TO_INFO_MAP:
            return False
            
        group, val = FILTER_ID_TO_INFO_MAP[controlId]
        click_obj = {'id': controlId, 'value': val}
        
        # 处理评分筛选 (多选逻辑)
        if group == 'filter.rating':
            current_list = self.filter_state.get(group, [])
            default_id = FILTER_MAP[group]['default']
            
            # 检查当前点击的项是否已选中
            exists = False
            for x in current_list:
                if x['id'] == controlId:
                    exists = True
                    break
            
            if controlId == default_id:
                # 点击默认项（如"全部"），清空其他选项并只选中默认项
                _, def_val = FILTER_ID_TO_INFO_MAP[default_id]
                new_list = [{'id': default_id, 'value': def_val}]
            else:
                # 切换逻辑：如果已存在则移除，否则添加
                if exists:
                    new_list = [x for x in current_list if x['id'] != controlId]
                else:
                    new_list = current_list + [click_obj]
                
                # 如果选中了其他项，移除默认项
                has_default = any(x['id'] == default_id for x in new_list)
                if has_default and len(new_list) > 1:
                    new_list = [x for x in new_list if x['id'] != default_id]
                
                # 如果列表为空（取消了所有选择），自动选中默认项
                if not new_list:
                    _, def_val = FILTER_ID_TO_INFO_MAP[default_id]
                    new_list = [{'id': default_id, 'value': def_val}]
            
            self.filter_state[group] = new_list
            
            # 更新评分组所有按钮的UI状态
            # (因为多选会改变多个按钮的状态)
            self.update_highlights(group) # 优化为只更新该组
            
        else:
            # 单选逻辑 (其他筛选组)
            old_obj = self.filter_state.get(group)
            if old_obj and old_obj['id'] == controlId:
                return True # 点击已选中的项，无变化
            
            self.filter_state[group] = click_obj
            
            # 增量更新UI：取消旧的高亮，高亮新的
            if old_obj:
                self._set_button_state(old_obj['id'], False)
            self._set_button_state(controlId, True)
            
        self.refresh_container()
        return True

    def _show_selected(self, control_id, info_only=False):
        try:
            li=self.getControl(control_id).getSelectedItem()
            if li is None:return
            target = li.getProperty('MFG.ViewAll')
            if target:
                if not info_only and self.getProperty('MFG.Home') == 'true':
                    entry = next((cid for cid, (_xml, media) in HOME_ENTRIES.items()
                                  if media == target), None)
                    if entry is not None:
                        self._open_home_entry(entry)
                return
            media_type=li.getVideoInfoTag().getMediaType()
            if media_type == 'episode':
                from .tv_catalog import rpc
                if info_only:
                    from .tv_details import open_tvshow
                    open_tvshow(int(li.getProperty('MFG.TvshowId')),parent=self,
                        seed={'title':li.getProperty('MFG.ShowTitle'),
                              'art':{key:li.getArt(key) for key in ('poster','fanart','clearlogo')}},
                        initial_season=int(li.getProperty('MFG.Season')))
                else:
                    self._capture_navigation_state()
                    rpc('Player.Open',{'item':{'episodeid':li.getVideoInfoTag().getDbId()},
                                       'options':{'resume':True}})
            elif media_type == 'tvshow':
                from .tv_details import open_tvshow
                tag = li.getVideoInfoTag()
                open_tvshow(tag.getDbId(), parent=self, seed={
                    'title': li.getLabel(), 'plot': tag.getPlot(),
                    'art': {key: li.getArt(key) for key in ('poster', 'fanart', 'clearlogo')}})
            elif media_type=='movie' and (info_only or control_id!=1002):
                from .movie_details import open_movie
                tag=li.getVideoInfoTag()
                open_movie(tag.getDbId(),parent=self,seed={'title':li.getLabel(),'plot':tag.getPlot(),
                    'art':{key:li.getArt(key) for key in ('poster','fanart','clearlogo')}})
            elif info_only:
                xbmcgui.Dialog().info(li)
            elif media_type == 'set':
                self._open_home_entry(7102, collection_id=li.getVideoInfoTag().getDbId(),
                                      page_title=li.getLabel())
            elif media_type=='movie':
                xbmc.executebuiltin('PlayMedia("%s",resume)' % li.getPath().replace('"','\\"'))
        except (RuntimeError,AttributeError) as exc:
            log('Selected item action failed: %s' % exc, xbmc.LOGERROR)
            notification('无法打开影片信息，请返回后重试。')

    def _capture_navigation_state(self):
        """Capture primitive indices only; never retain native ListItems."""
        try:
            focus = self.getFocusId()
        except RuntimeError:
            return
        positions = {}
        for cid in (1002, 1003, 1004, 1005, 1006, 1007, 1008, 9999):
            try:
                panel = self.getControl(cid)
                if panel.size():
                    positions[cid] = panel.getSelectedPosition()
            except (RuntimeError, AttributeError):
                pass
        from .catalog_cache import revision
        self._navigation_return_state = (focus, positions, revision())
        self._navigation_return_applied = False
        if self._grid_page:
            self._navigation_return_query = self._query_filters()
            self._navigation_return_applied = False
            self.setProperty('MFG.ReturnFocus','true')
            self._navigation_return_identity = None
            try:
                tag = self.getControl(9999).getSelectedItem().getVideoInfoTag()
                self._navigation_return_identity = (tag.getMediaType(), tag.getDbId())
            except (RuntimeError, AttributeError):
                pass

    def _restore_navigation_state(self):
        """Focus the saved row so Kodi scrolls its group back into view."""
        state = getattr(self, '_navigation_return_state', None)
        if not state or not self._is_active():
            return
        if getattr(self,'_navigation_return_applied',False):
            return
        focus, positions, _revision = state
        if self._grid_page and not self._can_restore_grid():
            return
        if self._grid_page and self.getFocusId() == 9999:
            current = self.getControl(9999).getSelectedPosition()
            saved = positions.get(9999, 0)
            # A rebuilt panel starts at zero. A different nonzero selection
            # means native input has already moved it before Python onInit.
            if current != saved and current > 0:
                self._commit_navigation_input()
                return
        for cid, position in positions.items():
            try:
                panel = self.getControl(cid)
                if panel.size():
                    target = max(0, min(position, panel.size() - 1))
                    if panel.getSelectedPosition() != target:
                        panel.selectItem(target)
            except (RuntimeError, AttributeError):
                pass
        try:
            if self.getFocusId() != focus:
                self.setFocusId(focus)
        except RuntimeError:
            return
        self._navigation_return_applied = True
        if self._grid_page:
            identity = getattr(self,'_navigation_return_identity',None)
            if identity:
                self.poster_loader.refresh_playback_state(identity, positions.get(9999,0))

    def _commit_navigation_input(self):
        """New input owns focus; a delayed return callback cannot rewind it."""
        if (not self._is_active() or not getattr(self,'_navigation_return_state',None)
                or getattr(self,'_navigation_return_applied',False)):
            return
        self._navigation_return_applied = True
        if self._grid_page:
            identity = getattr(self,'_navigation_return_identity',None)
            if identity and self._can_restore_grid():
                self.poster_loader.refresh_playback_state(identity,
                    self._navigation_return_state[1].get(9999,0))

    def _can_restore_grid(self):
        state = getattr(self, '_navigation_return_state', None)
        if not self._grid_page or not state:
            return False
        if getattr(self,'_navigation_return_query',None) != self._query_filters():
            return False
        try:
            return self.getControl(9999).size() > 0
        except RuntimeError:
            return False

    def _open_home_entry(self, control_id, collection_id=None, page_title=None):
        # Called by native button callbacks, including the static category
        # panel's SendClick bridges. No second RunScript interpreter is needed.
        if (control_id not in HOME_ENTRIES or self._opening_child
                or self.getProperty('MFG.ChildWindow') == 'true'
                or not self._is_active()):
            return
        started = time.monotonic()
        self._opening_child = True
        changed_background = False
        try:
            if self._background_policy is not None:
                try:
                    self.getControl(31111).setAnimations(self._background_policy[1])
                    changed_background = True
                except RuntimeError:
                    pass  # An incompatible skin keeps its existing animations.
            xml_name, media = HOME_ENTRIES[control_id]
            log('WALL navigation requested %s (same interpreter, fast-close=%s)' %
                (xml_name, changed_background))
            path = xbmcvfs.translatePath(xbmcaddon.Addon('plugin.video.filteredmovies').getAddonInfo('path'))
            launch_child(xml_name, path, media, parent=self,
                         factory=FilterWindow, started=started,
                         collection_id=collection_id, page_title=page_title)
        except Exception as exc:
            log('WALL navigation failed: %s' % exc, xbmc.LOGERROR)
            notification('页面未能打开，请重试。')
        finally:
            if changed_background:
                try:
                    # Returning from a child may rebuild native controls.
                    self.getControl(31111).setAnimations(self._background_policy[0])
                except RuntimeError:
                    pass
            self._opening_child = False

    def onClick(self, controlId):
        if controlId == 7197:
            self._apply_home_binds()
            return
        if self._grid_page:
            if controlId == 7195:
                self._apply_filter_facets()
                return
            if controlId == 7200:
                self._toggle_sort()
                return
            if 7220 <= controlId <= 7223:
                self._select_sort(controlId - 7220)
                return
            if controlId == 7201 and self._has_filter_toolbar:
                self._filters_expanded = not self._filters_expanded
                self._sort_open = False
                self._update_toolbar()
                return
            if controlId == 7194:
                self._grid_up()
                return
        if controlId == 7193:
            self._apply_grid_binds()
            return
        if controlId in (7191, 7192):
            self._grid_edge(1 if controlId == 7191 else -1)
            return
        if controlId == 6050:
            # Alphabet-panel clicks already run their XML input action. They
            # are not filter buttons and must not force a stale list refresh.
            return
        if controlId in (9999,1002,1004,1005,1006,1007,1008):
            self._show_selected(controlId)
            return
        if self.getProperty("MFG.Home") == "true":
            self._open_home_entry(controlId)
            return
        # 主窗口(5111)导航面板按钮 6001-6006 走 XML onclick，不拦截
        if self.xml_name.startswith('Custom_5111_') and 6001 <= controlId <= 6006:
            return
        
        # 检查是否为筛选按钮 (ID 1000-6999，排除 T9 组 6000/6050)
        if 1000 <= controlId <= 6999:
            if self._handle_filter_click(controlId):
                return
            
            # 稍微延迟一点，以便 Skin.SetString (来自 XML onclick) 完成
            xbmc.sleep(50) 
            # self.update_highlights()
            # 触发容器刷新以更新列表
            self.refresh_container()

    
    def refresh_container(self):
        # Coalesce changes in the same worker; do not clear/reinvoke the plugin.
        if hasattr(self, 'poster_loader'):
            if self._grid_page:
                self.setProperty('MFG.ReturnFocus','false')
            self.poster_loader.refresh(self._query_filters())
