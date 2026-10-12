"""Validate movie identity against original file/directory names, never AI guesses.

TMDb order/popularity and fuzzy containment alone are not identity proof.
"""
import re,unicodedata,urllib.parse,datetime,difflib
from matching import candidate_titles,year_of
_YEAR=re.compile(r'(?<!\d)(19\d{2}|20\d{2})(?=$|[. _\-\])】(])')
_CJK=re.compile(r'[\u4e00-\u9fff]')
_TECH=re.compile(r'(?i)(?:SGNB|原盘|原盤|字幕|音轨|音軌|国语|國語|国配|中字|2160p|1080p|bluray|blu-ray|web-dl|remux|hdr|x26[45]|h26[45]|杜比|DoVi|Atmos|DTS|AAC|DIY|豆瓣|评分|評分)')
_GENERIC={'4k电影','电影','杜比视界','media','movies','movie','video','videos','4k','uhd','bdmv','stream','纪录片','电视剧','动漫','blu ray'}
_EDITION=re.compile(r'(?i)[ ._-]+(?:the[ ._-]+)?(?:ultimate|theatrical|director[\x27s ._-]*|extended|unrated)[ ._-]+(?:cut|edition)$|[ ._-]+(?:tc|dc|extended|unrated)$')
_EXTRAS=('the making of','making of','behind the scenes','world premiere','幕后特辑','幕後特輯','制作特辑','製作特輯')
_WORD_NUMBERS={'one':'1','two':'2','three':'3','four':'4','five':'5','six':'6','seven':'7','eight':'8','nine':'9','ten':'10'}
# This movie library contains feature films only. TMDb's movie endpoint also
# returns shorts; inspect runtime in minutes after fetching identity details.
# Missing/zero runtime is unknown, never proof that a film is short or long.
FEATURE_MIN_RUNTIME_MINUTES=40

def feature_length_status(candidate):
    runtime=candidate.get('runtime')
    if isinstance(runtime,bool):return 'unknown'
    try:runtime=float(runtime)
    except (TypeError,ValueError,OverflowError):return 'unknown'
    if not 0<runtime<float('inf'):return 'unknown'
    return 'short' if runtime<FEATURE_MIN_RUNTIME_MINUTES else 'feature'

def title_key(value,strip_article=True):
    value=unicodedata.normalize('NFKD',str(value or '')).casefold().replace('&',' and ')
    value=''.join(c for c in value if not unicodedata.combining(c))
    value=value.strip()
    if strip_article:value=re.sub(r'^(?:the|a|an)\s+','',value)
    value=re.sub(r'\b(one|two|three|four|five|six|seven|eight|nine|ten)\b',lambda m:_WORD_NUMBERS[m[1]],value)
    return ''.join(c for c in value if c.isalnum())

def _extract_name(raw):
    raw=urllib.parse.unquote(raw)
    raw=re.sub(r'\.(?:mkv|mp4|iso|m2ts|ts|avi)$','',raw,flags=re.I)
    years=[m for m in _YEAR.finditer(raw) if 1920<=int(m[1])<=datetime.date.today().year+1]
    # A bilingual numeric title can repeat its number: 2001太空漫游.
    # 2001.A.Space.Odyssey. Neither occurrence is a release year.
    years=[m for m in years if not (
        re.match(r'^'+re.escape(m[1])+r'[\u4e00-\u9fff]',raw)
        and re.search(r'[A-Za-z]',_TECH.split(raw[m.end():],maxsplit=1)[0]))]
    if len(years)==1 and not raw[:years[0].start()].strip(' ._-[('):years=[]
    chosen=years[-1] if years else None;year=int(chosen[1]) if chosen else None
    prefix=raw[:chosen.start()] if chosen else raw
    names=[]
    for m in re.finditer(r'[\[【]([^\]】]+)[\]】]',prefix):
        title=m[1].strip()
        if not _TECH.search(title) and title_key(title):names.append(title)
    plain=re.sub(r'[\[【][^\]】]*[\]】]',' ',prefix)
    plain=re.sub(r'[._]+',' ',plain).strip(' -()[]【】')
    if not year:
        hit=_TECH.search(plain)
        if hit and hit.start()>0:plain=plain[:hit.start()].strip()
    plain=_EDITION.sub('',plain).strip()
    if plain and title_key(plain):names.append(plain)
    if _CJK.search(plain) and re.search(r'[A-Za-z]',plain):
        names.append(' '.join(re.findall(r'[\u4e00-\u9fff]+[0-9]*',plain)))
        names.append(re.sub(r'^\d+\s*', '', re.sub(r'[\u4e00-\u9fff]+',' ',plain).strip()))
    return [n for n in names if n],year

def build_evidence(filename,directory=''):
    """Read path strings only, never list/stat/open media shares."""
    names,year=_extract_name(filename);source_names=list(names);parent_names=[]
    parts=urllib.parse.unquote(urllib.parse.urlsplit(directory or '').path).rstrip('/').split('/')[-2:]
    for part in reversed(parts):
        if title_key(part) in {title_key(t) for t in _GENERIC}:continue
        extra,other_year=_extract_name(part)
        # A parent is useful only if it looks like a media title, not a root
        # folder. CJK parents and a release-year-bearing parent are anchors.
        if not other_year and not _CJK.search(part):continue
        if year and other_year and abs(year-other_year)>1:continue
        names.extend(extra)
        parent_names.extend(extra)
        if year is None:year=other_year
    out=[];seen=set()
    for name in names:
        key=title_key(name)
        if key and key not in seen and key not in {title_key(t) for t in _GENERIC}:seen.add(key);out.append(name)
    raw=urllib.parse.unquote(filename+' '+str(directory or ''))
    imdb=re.search(r'(?<![A-Za-z0-9])(tt\d{7,10})(?!\d)',raw,re.I)
    tmdb=re.search(r'(?:tmdb(?:id)?)[- =:_]*(\d+)',raw,re.I)
    return {'titles':out[:8],'filename_titles':source_names,'parent_titles':parent_names,'year':year,
            'imdb_id':imdb[1].lower() if imdb else None,'tmdb_id':int(tmdb[1]) if tmdb else None}

def _names(candidate):
    return list(dict.fromkeys([n for n in candidate_titles(candidate)+list(candidate.get('_official_titles') or []) if n]))

def _strength(source,names):
    key=title_key(source)
    if not key:return 0
    best=0
    for name in names:
        nk=title_key(name)
        if key==nk:
            # When Runner and The Runner coexist, preserving the original
            # article is stronger evidence than merely ignoring it.
            exact=title_key(source,False)==title_key(name,False)
            best=max(best,3 if exact else 2)
        # Only an explicit subtitle delimiter permits main-title matching.
        # Arbitrary substring matches are never accepted.
        if ':' in name or '：' in name:
            main=re.split(r'[:：]',name,1)[0]
            if key==title_key(main) and len(key)>=3:best=max(best,2)
        if len(key)>=12 and len(nk)>=12 and re.findall(r'\d+',key)==re.findall(r'\d+',nk):
            if difflib.SequenceMatcher(None,key,nk).ratio()>=.96:best=max(best,1)
    return best

def has_identity_metadata(candidate):
    # Sparse TMDb placeholders are not reliable automatic identity evidence.
    if candidate.get('_identity_checked'):
        return bool(candidate.get('imdb_id') or
                    candidate.get('overview') and candidate.get('genre_ids') or
                    candidate.get('runtime',0)>0 and (candidate.get('directors') or candidate.get('cast')))
    return bool(candidate.get('overview') and candidate.get('genre_ids'))

def evaluate_candidate(candidate,evidence):
    names=_names(candidate);year=evidence.get('year');cy=year_of(candidate)
    if year and cy and abs(year-cy)>1:return {'accepted':False,'reason':'year_conflict','strength':0}
    if year and not cy:return {'accepted':False,'reason':'missing_year','strength':0}
    if feature_length_status(candidate)=='short':
        return {'accepted':False,'reason':'short_film_not_allowed','strength':0}
    source_titles=evidence.get('titles',[])
    for extra in _EXTRAS:
        if any(extra in n.casefold() for n in names) and not any(extra in n.casefold() for n in source_titles):
            # Official titles can include featurettes as aliases in error;
            # use primary/original title for this rejection, not every alias.
            if any(extra in str(candidate.get(k)or'').casefold() for k in ('title','original_title')):
                return {'accepted':False,'reason':'extra_not_feature','strength':0}
    file_chinese=max((_strength(t,names) for t in evidence.get('filename_titles',[]) if _CJK.search(t)),default=0)
    file_strength=max((_strength(t,names) for t in evidence.get('filename_titles',[])),default=0)
    chinese=max((_strength(t,names) for t in source_titles if _CJK.search(t)),default=0)
    other=max((_strength(t,names) for t in source_titles if not _CJK.search(t)),default=0)
    strength=max(chinese,other)
    if not strength:return {'accepted':False,'reason':'title_conflict','strength':0}
    if not has_identity_metadata(candidate):return {'accepted':False,'reason':'sparse_metadata','strength':strength}
    return {'accepted':True,'reason':'source_title_year','strength':strength,
            'chinese':chinese,'other':other,'rank':(file_strength,bool(file_chinese),bool(chinese),strength,bool(chinese and other),bool(year and cy==year))}

def decide(candidates,evidence):
    """A decision carries its evidence; caller never uses result-list order."""
    accepted=[];rejected=[];seen=set()
    for candidate in candidates or []:
        identity=candidate.get('id')
        if identity in seen:continue
        seen.add(identity);assessment=evaluate_candidate(candidate,evidence)
        if assessment['accepted']:accepted.append((assessment,candidate))
        else:rejected.append({'id':identity,'reason':assessment['reason']})
    if not accepted:return {'status':'unresolved','candidate':None,'rejected':rejected,'reason':'no_verified_identity'}
    rank=max(a['rank'] for a,c in accepted);top=[c for a,c in accepted if a['rank']==rank]
    if len(top)!=1:return {'status':'ambiguous','candidate':None,'candidates':top,'rejected':rejected,'reason':'same_title_year'}
    chosen=top[0]
    return {'status':'accepted','candidate':chosen,'rejected':rejected,'reason':'source_title_year'}
