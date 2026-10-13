"""Validated, directory-aware TV matching shared by scans and manual review."""
import copy
import re
import threading
from media_identity import title_key
from matching import candidate_titles, year_of, merge_candidates
from tv_context import build_tv_contexts


class TvMatchError(Exception):
    def __init__(self, reason, message, candidates=None):
        super().__init__(message)
        self.reason = reason
        self.candidates = candidates or []


class TvMatchSession:
    def __init__(self, filepaths, api_key, language='zh-CN', expected_type='auto',
                 forced_ids=None, previous_results=None):
        self.contexts = build_tv_contexts(filepaths, expected_type)
        self.api_key, self.language = api_key, language or 'zh-CN'
        self.forced_ids = forced_ids or {}
        self.previous_results = previous_results or {}
        self._locks = {}
        self._cache = {}
        self._lock = threading.Lock()

    def register_source(self, files, expected_type='auto', confirmed_matches=None):
        """Load saved inventory and inherit explicit show-directory confirmations."""
        from path_display import path_token
        from tv_context import _show_directory
        contexts = build_tv_contexts(files,expected_type)
        if expected_type!='movie' and confirmed_matches:
            from collections import defaultdict
            groups = defaultdict(list)
            for path,info in files.items():
                root = info.get('match_evidence',{}).get('show_root') or _show_directory(path)[0]
                groups[root].append(path)
            for root,paths in groups.items():
                confirmed = confirmed_matches.get(path_token(root),{})
                identity = confirmed.get('tmdb_id')
                if not isinstance(identity,int) or isinstance(identity,bool) or identity<=0:
                    continue
                forced = build_tv_contexts(paths,'tvshow')
                for context in forced.values():
                    context['show_root'] = root
                    if not context['title'] and confirmed.get('title'):
                        context.update(title=confirmed['title'],titles=[confirmed['title']])
                        if context.get('reason')=='show_title_missing' and context.get('episodes'):
                            context.update(kind='episode',reason='')
                contexts.update(forced)
                self.forced_ids[root] = identity
        self.contexts.update(contexts)
        self.previous_results.update(files)

    def context(self, filepath):
        return self.contexts.get(filepath)

    def _once(self, key, fetch):
        # A per-key lock provides single-flight, while unrelated shows/seasons
        # can proceed concurrently. Cache failures only for this scan session.
        with self._lock:
            lock = self._locks.setdefault(key,threading.Lock())
        with lock:
            if key not in self._cache:
                try:
                    self._cache[key] = fetch()
                except TvMatchError as error:
                    self._cache[key] = error
            result = self._cache[key]
            if isinstance(result,TvMatchError):
                raise result
            return result

    def resolve_show(self, context):
        key = ('show',context['show_root'],tuple(context.get('directory_titles') or context['titles']),context['year'],
               context['season_number'],context['edition'])
        return self._once(key,lambda:self._resolve_show(context))

    def _resolve_show(self, context):
        from tmdb_api import search_tv_best, get_tv_details
        forced = self.forced_ids.get(context['show_root'])
        if forced:
            details = get_tv_details(forced,self.api_key,self.language)
            if not details:
                raise TvMatchError('show_details_unavailable','无法取得所选电视剧的资料，请稍后重试。')
            return {'id':int(forced),'details':details,'reason':'manual_show_confirmation'}
        results, index = [], {}
        titles = list(context.get('directory_titles') or context['titles'])
        season_aliases = set()
        if context['season_explicit'] and context['season_number'] > 1:
            suffix = str(context['season_number'])
            for title in list(titles):
                if re.fullmatch(r'[\u4e00-\u9fff]+'+suffix,title):
                    alias = title[:-len(suffix)]
                    titles.append(alias);season_aliases.add(title_key(alias,False))
        # Search the enclosing show name, never the episode's individual name.
        for title in titles:
            candidates = search_tv_best(title,context['year'],context['english_title'],self.api_key,self.language) or []
            merge_candidates(results,index,candidates)
            # A year-filtered query can return an unrelated partial-title hit,
            # preventing the API client's no-result fallback. TV release years
            # also refer to later seasons: always verify unfiltered candidates.
            if context['year'] is not None:
                merge_candidates(results,index,search_tv_best(title,None,context['english_title'],self.api_key,self.language) or [])
        verified = []
        source_keys = {title_key(name,False) for name in titles}
        for candidate in results:
            names = candidate_titles(candidate)
            matched_keys = source_keys.intersection(title_key(name,False) for name in names if name)
            if not matched_keys:
                continue
            details = get_tv_details(candidate['id'],self.api_key,self.language)
            if not details:
                continue
            first_year = year_of(candidate)
            source_year = context['year']
            season = next((s for s in details.get('seasons',[]) if s['season_number']==context['season_number']),None)
            season_year = int(season['air_date'][:4]) if season and str(season.get('air_date',''))[:4].isdigit() else None
            if matched_keys.issubset(season_aliases) and (not season or source_year != season_year):
                continue
            if source_year is None:
                strength = 1
            elif source_year == first_year:
                strength = 4
            elif context['season_explicit'] and source_year == season_year:
                strength = 4
            elif first_year is not None and abs(source_year-first_year) <= 1:
                strength = 2
            elif context['edition']=='edited' and first_year is not None and 0 <= source_year-first_year <= 5:
                strength = 1
            else:
                continue
            verified.append((strength,candidate,details))
        if not verified:
            raise TvMatchError('show_identity_unverified',
                               '无法核实电视剧《%s》及其年份，请按剧名手动匹配。' % context['title'],results[:8])
        highest = max(item[0] for item in verified)
        best = [item for item in verified if item[0]==highest]
        if len(best) != 1:
            raise TvMatchError('ambiguous_show_identity',
                               '同名电视剧存在多个符合条件的版本，请手动选择剧集。',
                               [item[1] for item in best])
        _strength,candidate,details = best[0]
        return {'id':candidate['id'],'details':details,'reason':'verified_show_title_and_year'}

    def episode_plan(self, context, resolved=None):
        resolved = resolved or self.resolve_show(context)
        seasons = resolved['details'].get('seasons',[])
        number = context['season_number']
        season = next((s for s in seasons if s['season_number']==number),None)
        if season is None and not context['season_explicit'] and len([s for s in seasons if s['season_number']>0])==1:
            season = next(s for s in seasons if s['season_number']>0)
            number = season['season_number']
        if season is None:
            raise TvMatchError('season_not_found','所选电视剧没有第 %d 季，请确认季号。' % number)
        source = context['episodes']
        if not source:
            raise TvMatchError('episode_number_missing','已识别电视剧，但文件中没有可靠的集号，请手动选择单集。')
        if context['edition']=='edited':
            # A recut has its own physical episode numbering. Original-show
            # episode plots/air dates/IDs cannot be assigned to those cuts.
            return {'show':resolved,'season':number,'episodes':list(source),
                    'mode':'local_edited_version','offset':0}
        count = int(season.get('episode_count',0) or 0)
        offset = 0
        coverage = context['source_episodes']
        if count and max(source)>count:
            if (not context['season_explicit'] and coverage and len(coverage)==count
                    and coverage==list(range(min(coverage),min(coverage)+count))
                    and min(coverage)>count):
                offset = min(coverage)-1
            else:
                raise TvMatchError('episode_number_out_of_range',
                                   '文件集号超过 TMDb 的 %d 集；无法可靠换算，请手动确认编号。' % count)
        numbers = [n-offset for n in source]
        from tmdb_api import get_tv_season_details
        season_details = self._once(('season',resolved['id'],number),
            lambda:get_tv_season_details(resolved['id'],number,self.api_key,self.language))
        if season_details is None:
            raise TvMatchError('season_metadata_unavailable','无法读取这一季的资料，请稍后重试。')
        if any(n not in season_details['episodes'] for n in numbers):
            raise TvMatchError('episode_not_found','TMDb 中没有对应单集；请确认版本和集号，不会生成虚假单集资料。')
        return {'show':resolved,'season':number,'episodes':numbers,
                'mode':'absolute_number_rebased' if offset else 'verified_tmdb_episodes',
                'offset':offset,'season_details':season_details}

    def _local_slots(self):
        from collections import defaultdict
        from directory_listing import natural_key
        groups = defaultdict(list)
        for path,context in self.contexts.items():
            groups[(context['show_root'],context['title'],context['year'])].append(path)
        slots = {}
        for paths in groups.values():
            # Sort the complete inventory, never worker completion order.
            # Adding a file updates natural order while save_episodes retains
            # existing episode/file IDs and playback state.
            def key(path):
                c = self.contexts[path]
                return (c['season_number'], c['episodes'][0] if c['episodes'] else 10000,
                        natural_key(path))
            for ordinal,path in enumerate(sorted(paths,key=key),1):
                slots[path] = ordinal
        return slots

    def _local_plan(self, filepath, resolved, reason):
        context = self.contexts[filepath]
        source_seasons = [other['season_number'] for other in self.contexts.values()
                          if other['show_root']==context['show_root']]
        catalog_seasons = [int(s['season_number']) for s in resolved['details'].get('seasons',[])]
        # A named local season after all actual seasons makes supplements and
        # unmatched cuts sort at the end without impersonating official S00.
        number = max([1]+source_seasons+catalog_seasons)+1
        slot = self._once(('local_slots',),self._local_slots)[filepath]
        return {'show':resolved,'season':number,'episodes':[slot], 'offset':0,
                'mode':'local_unmatched','local_reason':reason}

    def prepare_file(self, filepath):
        import os
        context = self.contexts[filepath]
        if not context['title']:
            raise TvMatchError('show_title_missing','无法从目录或文件名取得剧名，请手动输入剧名。')
        # The show identity is always confirmed first. An episode's absence
        # from TMDb is no longer a failure to match the containing show.
        resolved = self.resolve_show(context)
        if context['kind'] in ('extra','review'):
            plan = self._local_plan(filepath,resolved,'extra_content' if context['kind']=='extra' else context['reason'])
        else:
            try:
                plan = self.episode_plan(context,resolved)
            except TvMatchError as error:
                if error.reason not in ('season_not_found','episode_number_missing',
                                        'episode_number_out_of_range','episode_not_found'):
                    # Network/database failures remain retryable; do not strip
                    # previously verified metadata because of a transient error.
                    raise
                plan = self._local_plan(filepath,resolved,error.reason)
        episodes = []
        for number in plan['episodes']:
            if plan['mode'] in ('local_edited_version','local_unmatched'):
                unknown = plan['mode']=='local_unmatched'
                stem = os.path.splitext(os.path.basename(filepath))[0]
                title = '未知集 · '+stem if unknown else context['episode_title'] or '第 %d 集（精编版）' % number
                art = resolved['details'].get('available_art',{})
                thumbs = art.get('fanart') or art.get('poster') or []
                details = {'info':{'title':title,
                                    'plot':'所属剧目录已匹配；这是未对应到官方单集的本地内容，保留原文件名及顺序。' if unknown else '精编版本的独立集号，未套用原版单集剧情。',
                                    'aired':'','duration':0,'director':[],'credits':[]},
                           'uniqueids':{},'ratings':{},'available_art':{'poster':thumbs[:1]},
                           'cast':copy.deepcopy(resolved['details'].get('cast',[])),
                           'season_name':'未知集' if unknown else ''}
            else:
                details = copy.deepcopy(plan['season_details']['episodes'][number])
            episodes.append((plan['season'],number,details))
        return {'status':'ready','context':context,'plan':plan,'episodes':episodes}

    def process_file(self, filepath):
        import os
        context = self.contexts[filepath]
        evidence = {'type':'tvshow','source_title':context['title'],'source_year':context['year'],
                    'source_episode_numbers':context['episodes']}
        try:
            prepared = self.prepare_file(filepath)
            from tvshow_db import TvShowDatabase
            plan = prepared['plan'];show = plan['show'];details = show['details']
            database = TvShowDatabase()
            try:
                database.connect()
                show_id, episode_ids = database.save_episodes(details,prepared['episodes'],filepath,
                                                               show_root=context['show_root'])
            finally:
                database.close()
            if not show_id or len(episode_ids)!=len(prepared['episodes']):
                raise TvMatchError('episode_write_failed','电视剧写入失败；未保存不完整的单集，请重试。')
            evidence.update(tmdb_id=show['id'],reason=show['reason'],episode_numbers=plan['episodes'],
                            numbering_mode=plan['mode'],episode_ids=episode_ids,show_root=context['show_root'])
            if plan['mode']=='local_unmatched':
                evidence.update(local_reason=plan['local_reason'],local_unknown_episode=plan['episodes'][0])
            art = copy.deepcopy(details.get('available_art',{}))
            for kind,size in (('poster','w780'),('fanart','w1920'),('clearlogo','w500')):
                for image in art.get(kind,[]):
                    image['url'] = image.get('url','').replace('/original/','/%s/'%size)
            art['cast']=[{'url':c['thumbnail']} for c in details.get('cast',[])[:5] if c.get('thumbnail')]
            for _,_,ep in prepared['episodes']:
                art.setdefault('episode.thumb',[]).extend(ep.get('available_art',{}).get('poster',[]))
            return ('success',filepath,os.path.basename(filepath),show_id,details['info']['title'],art,
                    plan['season'],plan['episodes'][0],None,details['info'].get('year',''),evidence)
        except TvMatchError as error:
            evidence['reason']=error.reason
            return ('pending',filepath,os.path.basename(filepath),None,context['title'],None,
                    context['season_number'],context['episodes'][0] if context['episodes'] else 0,
                    str(error),str(context['year'] or ''),evidence)
