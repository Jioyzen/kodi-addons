"""Conservative movie resolver. AI can propose searches, not rewrite evidence."""
import re
from matching import merge_candidates,match_score,year_of
from media_identity import build_evidence,decide,_strength,feature_length_status

def resolve_movie(filename,parent,api_key,language='zh-CN',parsed=None,initial=None):
    from tmdb_api import (search_movie_best,_movie_search_once,get_movie_identity,
                         find_movie_by_imdb)
    evidence=build_evidence(filename,parent)
    candidates=[];index={}
    merge_candidates(candidates,index,initial)
    explicit=evidence.get('tmdb_id')
    if evidence.get('imdb_id'):
        found=find_movie_by_imdb(evidence['imdb_id'],api_key,language)
        if len(found)!=1:
            return {'status':'ambiguous' if found else 'unresolved','candidate':None,
                    'reason':'explicit_identifier_not_unique','candidates':found,'evidence':evidence}
        if explicit and explicit!=found[0]['id']:
            return {'status':'unresolved','candidate':None,'reason':'explicit_identifier_conflict','evidence':evidence}
        explicit=found[0]['id']
    if explicit:
        identity=get_movie_identity(explicit,api_key,language)
        if not identity or identity.get('unavailable'):
            return {'status':'unresolved','candidate':None,'reason':'explicit_identifier_unavailable','evidence':evidence}
        if evidence.get('imdb_id') and identity.get('imdb_id')!=evidence['imdb_id']:
            return {'status':'unresolved','candidate':None,'reason':'explicit_identifier_conflict','evidence':evidence}
        if feature_length_status(identity)=='short':
            return {'status':'unresolved','candidate':None,'reason':'short_film_not_allowed',
                    'rejected':[{'id':explicit,'reason':'short_film_not_allowed'}],'evidence':evidence}
        return {'status':'accepted','candidate':identity,'reason':'explicit_identifier','evidence':evidence}
    titles=list(evidence['titles'])
    # Bilingual names already have separate anchors. Searching the glued
    # CN+EN parent label creates irrelevant hits and extra empty requests.
    if any(re.search(r'[\u4e00-\u9fff]',t) and not re.search(r'[A-Za-z]',t) for t in titles) and any(not re.search(r'[\u4e00-\u9fff]',t) for t in titles):
        titles=[t for t in titles if not (re.search(r'[\u4e00-\u9fff]',t) and re.search(r'[A-Za-z]',t))]
    # Proposals add candidate recall, but final decisions use original titles
    # and the original year rather than the model's guessed identity/year.
    if parsed:
        for key in ('title','english_title'):
            title=parsed.get(key)
            if title and title not in titles:titles.append(title)
    for title in titles:
        for lang in dict.fromkeys((language,'en-US')):
            found=_movie_search_once(title,evidence['year'],api_key,lang)
            if not found and evidence['year']:
                found=_movie_search_once(title,None,api_key,lang)
            merge_candidates(candidates,index,found)
    initial_decision=decide(candidates,evidence)
    if initial_decision['status']!='accepted' and evidence['year']:
        for title in titles:
            for lang in dict.fromkeys((language,'en-US')):
                merge_candidates(candidates,index,_movie_search_once(title,None,api_key,lang))
    # Check official aliases and actual metadata before committing. Bounded
    # per-file queries prevent generic names from consuming unbounded work.
    def rank(candidate):
        names=[candidate.get('title',''),candidate.get('original_title','')]+list(candidate.get('_match_titles') or [])
        strength=max((_strength(t,names) for t in evidence['titles']),default=0)
        score=max((match_score(candidate,t,evidence['year']) for t in evidence['titles']),default=0)
        return strength,score
    ranked=sorted(candidates,key=rank,reverse=True)
    # A director-prefixed canonical name can have an exact official alias
    # (Lee Cronin's The Mummy -> The Mummy). Check year-coherent candidates
    # even when their search title isn't an exact anchor yet.
    plausible=[c for c in ranked if (rank(c)[0] or rank(c)[1]>=.50) and
               (not evidence['year'] or year_of(c) and abs(evidence['year']-year_of(c))<=1)]
    selected=plausible[:64]
    checked=[]
    def enrich(candidate):
        identity=get_movie_identity(candidate['id'],api_key,language)
        if identity and not identity.get('unavailable'):
            enriched=dict(candidate);enriched.update(identity)
            enriched['_match_titles']=list(dict.fromkeys(list(candidate.get('_match_titles') or [])+
                                        [candidate.get('title',''),candidate.get('original_title','')]))
            checked.append(enriched)
    for candidate in selected:enrich(candidate)
    decision=decide(checked,evidence)
    # If additional equally anchored candidates remain unchecked, there is
    # not enough evidence to pick one safely.
    if decision['status']=='accepted' and len(plausible)>64:
        decision={'status':'ambiguous','candidate':None,'candidates':checked,'reason':'too_many_same_name_candidates'}
    decision['evidence']=evidence
    return decision
