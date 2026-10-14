"""Shared genre names for facet visibility and exact library filtering.

TV combination labels supply each of their component filter choices;
original library metadata remains unchanged. Unknown/custom genres remain available through Other.
"""
import unicodedata

GENRE_BUTTONS = {
    '动作':2002, '喜剧':2003, '爱情':2004,
    '科幻':2005, '奇幻':2017, '犯罪':2006,
    '冒险':2007, '剧情':2014, '惊悚':2016, '恐怖':2008,
    '动画':2009, '家庭':2018, '儿童':2024, '战争':2010,
    '政治':2023, '悬疑':2011, '历史':2012, '音乐':2013,
    '西部':2019, '纪录片':2020, '新闻':2025, '真人秀':2026,
    '肥皂剧':2027, '脱口秀':2028, '电视电影':2029,
}

# Display labels only. Persisted filter identities and library metadata retain
# their complete genre names; component filters do not rewrite those records.
GENRE_LABELS = {name:name for name in GENRE_BUTTONS}
GENRE_LABELS.update({'纪录片':'纪录', '真人秀':'真人', '肥皂剧':'肥皂',
                     '脱口秀':'脱口', '电视电影':'电视', '其他':'其他'})


def genre_label(value):
    return GENRE_LABELS.get(value,str(value or ''))

_ALIASES = {
    '动作':('Action','動作'), '动作冒险':('动作与冒险','動作冒險','Action & Adventure'),
    '喜剧':('Comedy','喜劇'), '爱情':('Romance','愛情'),
    '科幻':('Science Fiction','Sci-Fi','科学幻想','科學幻想'),
    '奇幻':('Fantasy','魔幻'), '科幻与奇幻':('Sci-Fi & Fantasy','科幻與奇幻'),
    '犯罪':('Crime',), '冒险':('Adventure','冒險'), '剧情':('Drama','劇情'),
    '惊悚':('Thriller','驚悚'), '恐怖':('Horror',), '动画':('Animation','動畫'),
    '家庭':('Family',), '儿童':('Kids','兒童'), '战争':('War','戰爭'),
    '战争与政治':('War & Politics','战争政治','戰爭與政治'), '政治':('Politics','政治'), '悬疑':('Mystery','懸疑'),
    '历史':('History','歷史'), '音乐':('Music','音樂'), '西部':('Western',),
    '纪录片':('纪录','记录','記錄','紀錄','紀錄片','Documentary'),
    '新闻':('News','新聞'), '真人秀':('Reality',), '肥皂剧':('Soap','肥皂劇'),
    '脱口秀':('Talk','脫口秀'), '电视电影':('TV Movie','電視電影'),
}

def _key(value):
    return ' '.join(unicodedata.normalize('NFKC',str(value or '')).casefold().split())

_NAMES = {_key(name):canonical for canonical in _ALIASES
          for name in (canonical,)+_ALIASES.get(canonical,())}

def genre_name(value):
    value = str(value or '').strip()
    return _NAMES.get(_key(value),value)

_COMBINED = {'动作冒险':('动作','冒险'), '科幻与奇幻':('科幻','奇幻'),
             '战争与政治':('战争','政治')}


def genre_values(value):
    canonical = genre_name(value)
    return _COMBINED.get(canonical,(canonical,)) if canonical else ()


def genre_match(genres,selected):
    values = {name for value in str(genres or '').split('/') if value.strip()
              for name in genre_values(value)}
    if selected=='其他':
        return int(bool(values-set(GENRE_BUTTONS)))
    return int(genre_name(selected) in values)
