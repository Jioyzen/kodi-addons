"""Validated, directory-aware TV matching shared by scans and manual review."""
import copy
import os
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
                root = (contexts.get(path) or {}).get('show_root') or info.get('match_evidence',{}).get('show_root') or _show_directory(path)[0]
                groups[root].append(path)
            for root,paths in groups.items():
                confirmed = confirmed_matches.get(path_token(root),{})
                if not confirmed:
                    # Earlier versions saved season-directory confirmations.
                    # Migrate only unanimous IDs; conflicting confirmations
                    # must not silently override the whole collection.
                    legacy = [confirmed_matches.get(path_token(info.get('match_evidence',{}).get('show_root') or os.path.dirname(path).rstrip('/')+'/'),{})
                              for path in paths for info in [files[path]]]
                    legacy = [match for match in legacy if match.get('tmdb_id')]
                    if len({match['tmdb_id'] for match in legacy})==1:
                        confirmed = legacy[0]
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

    def _filename_consensus(self, context):
        """Use show prefixes only, on failure, without reading video shares.

        Two distinct episode identities must agree. Release duplicates cannot
        supply extra votes; conflicting prefixes keep the directory pending.
        """
        if not context.get('directory_identity'):
            return []
        from tv_context import episode_marker, _titles
        samples = {}
        names = {}
        for path,other in self.contexts.items():
            if other['show_root']!=context['show_root'] or other['kind']!='episode':
                continue
            marker = episode_marker(os.path.basename(path))
            if not marker or marker.get('error') or not marker.get('prefix'):
                continue
            titles,_ = _titles(marker['prefix'])
            keys = set()
            for title in titles:
                if not re.search(r'[A-Za-z\u00c0-\u024f]',title) or re.search(r'[\u4e00-\u9fff]',title):
                    continue
                key = title_key(title,False)
                if len(key)<2:
                    continue
                keys.add(key);names.setdefault(key,title)
            if keys:
                episode = (other['season_number'],tuple(other['episodes']))
                samples.setdefault(episode,[]).append(keys)
        if len(samples)<2:
            return []
        # Every named file must corroborate the alias, including alternate
        # encodes. Neither a single mislabeled file nor a majority vote can
        # silently turn a mixed folder into one show.
        common = set.intersection(*(keys for versions in samples.values() for keys in versions))
        return [names[key] for key in sorted(common)]

    def _resolve_show(self, context):
        try:
            return self._resolve_title_candidates(context)
        except TvMatchError as primary:
            if primary.reason not in ('show_identity_unverified','ambiguous_show_identity'):
                raise
            aliases = self._once(('filename_aliases',context['show_root']),
                lambda:self._filename_consensus(context))
            # Names already searched provide no additional identity evidence.
            primary_keys = {title_key(t,False) for t in context.get('directory_titles') or context['titles']}
            aliases = [t for t in aliases if title_key(t,False) not in primary_keys]
            if not aliases:
                raise
            fallback = dict(context,directory_titles=aliases,english_title=aliases[0])
            try:
                resolved = self._resolve_title_candidates(fallback)
            except TvMatchError as secondary:
                if secondary.reason=='show_identity_unverified':
                    raise primary
                raise
            resolved['reason'] = 'verified_consistent_episode_show_prefix'
            return resolved

    def _resolve_title_candidates(self, context):
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
        metadata_unavailable = False
        source_keys = {title_key(name,False) for name in titles}
        for candidate in results:
            names = candidate_titles(candidate)
            matched_keys = source_keys.intersection(title_key(name,False) for name in names if name)
            if not matched_keys:
                continue
            details = self._once(('show_details', candidate['id']),
                lambda:get_tv_details(candidate['id'],self.api_key,self.language))
            if not details:
                metadata_unavailable = True
                continue
            first_year = year_of(candidate)
            source_year = context['year'] or context.get('season_year')
            season = next((s for s in details.get('seasons',[]) if s['season_number']==context['season_number']),None)
            season_year = int(season['air_date'][:4]) if season and str(season.get('air_date',''))[:4].isdigit() else None
            if matched_keys.issubset(season_aliases) and (not season or source_year != season_year):
                continue
            if source_year is None:
                strength = 1
            elif source_year == first_year:
                strength = 4
            elif context['season_explicit'] and (context.get('season_year') or source_year) == season_year:
                strength = 4
            elif first_year is not None and abs(source_year-first_year) <= 1:
                strength = 2
            elif context['edition']=='edited' and first_year is not None and 0 <= source_year-first_year <= 5:
                strength = 1
            else:
                continue
            # Season availability and actual episode coverage disambiguate
            # same-name shows, without using search order or popularity.
            covered = bool(season and int(season.get('episode_count') or 0)>0 and
                           all(n<=int(season['episode_count']) for n in context.get('source_episodes',[])))
            structure = 2 if covered else 1 if season else 0
            source_count = self._once(('source_count',context['show_root'],tuple(context['titles'])),
                lambda:len({(c['season_number'],n) for c in self.contexts.values()
                            if c['show_root']==context['show_root'] and c['titles']==context['titles']
                            for n in c['episodes']}))
            catalog_count = sum(int(s.get('episode_count') or 0) for s in details.get('seasons',[]) if s['season_number']>0)
            total_supported = int(bool(source_count and catalog_count>=source_count))
            verified.append(((strength,total_supported,structure),candidate,details))
        if not verified:
            if metadata_unavailable:
                raise TvMatchError('show_details_unavailable','电视剧候选资料暂时不可用，请重试。')
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
            identity = (context['show_root'],) if context.get('directory_identity') else (context['show_root'],context['title'],context['year'])
            groups[identity].append(path)
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
        seasons = resolved['details'].get('seasons',[])
        number = context['season_number']
        regular = [s for s in seasons if s['season_number']>0]
        if not context['season_explicit'] and len(regular)==1:
            number = regular[0]['season_number']
        slots = self._once(('season_local_slots',context['show_root'],tuple(context['titles']),number,resolved['id']),
            lambda:self._season_local_slots(context,resolved,number))
        return {'show':resolved,'season':number,'episodes':[slots[filepath]], 'offset':0,
                'mode':'local_unmatched','local_reason':reason}

    def _season_local_slots(self, context, resolved, number):
        from directory_listing import natural_key
        seasons = resolved['details'].get('seasons',[])
        regular = [s for s in seasons if s['season_number']>0]
        season = next((s for s in seasons if s['season_number']==number),None)
        count = int((season or {}).get('episode_count') or 0)
        members = {}
        for path,c in self.contexts.items():
            source_season = c['season_number']
            if not c['season_explicit'] and len(regular)==1:
                source_season = regular[0]['season_number']
            if c['show_root']==context['show_root'] and c['titles']==context['titles'] and source_season==number:
                members[path] = c
        coverage = sorted({n for c in members.values() if c['kind']=='episode' for n in c['episodes']})
        rebase = min(coverage)-1 if (count and coverage and len(coverage)==count and
                 coverage==list(range(min(coverage),min(coverage)+count)) and min(coverage)>count and
                 not any(c['season_explicit'] for c in members.values())) else 0
        def numbers(c):
            return [n-rebase for n in c['episodes']] if c['edition']!='edited' else c['episodes']
        known = {path for path,c in members.items() if c['kind']=='episode' and c['episodes'] and
                 (c['edition']=='edited' or count and all(0<n<=count for n in numbers(c)))}
        official = None
        if known and season and not context['edition']=='edited':
            from tmdb_api import get_tv_season_details
            details = self._once(('season',resolved['id'],number),
                lambda:get_tv_season_details(resolved['id'],number,self.api_key,self.language))
            if details is None:
                raise TvMatchError('season_metadata_unavailable','无法读取这一季的资料，请稍后重试。')
            official = details.get('episodes',{})
        normal, unknown = [], []
        for path,c in members.items():
            valid = path in known and (official is None or all(n in official for n in numbers(c)))
            if valid:
                normal.extend(numbers(c))
            else:
                unknown.append(path)
        # Supplements live in the physical source season after its normal
        # local episodes. Never create or rename a season just for extras.
        base = max(normal,default=0)
        unknown.sort(key=lambda path:(members[path]['episodes'][0] if members[path]['episodes'] else 10000,natural_key(path)))
        return {path:base+i for i,path in enumerate(unknown,1)}

    def _standalone_directory(self, context):
        return bool(context.get('directory_identity') and context['kind']=='review' and
                    not context['season_explicit'] and context['year'] and
                    all(not c['episodes'] and not c['season_explicit'] and c['kind']!='extra'
                        for c in self.contexts.values() if c['show_root']==context['show_root']))

    def _resolve_standalone_movie(self, filepath):
        from movie_matcher import resolve_movie
        from tmdb_api import get_movie_details
        context = self.contexts[filepath]
        decision = resolve_movie(os.path.basename(filepath),os.path.dirname(filepath),self.api_key,self.language)
        if decision['status']!='accepted':
            return None
        details = get_movie_details(decision['candidate']['id'],self.api_key,self.language)
        if not details:
            raise TvMatchError('movie_metadata_unavailable','独立电影已识别，但暂时无法读取资料，请重试。')
        return {'decision':decision,'details':details}

    def _process_standalone_movie(self, filepath, prepared):
        if getattr(self,'cancelled',lambda:False)():
            raise TvMatchError('cancelled','扫描已停止，保留待匹配状态。')
        from kodi_db import KodiDatabase
        movie = prepared['movie'];details = movie['details'];decision = movie['decision']
        identity = decision['candidate']['id']
        database = KodiDatabase()
        try:
            database.connect()
            existing = database.find_movie_by_uniqueid('tmdb',str(identity))
            movie_id = database.save_movie(details,filepath,merge_into_movie_id=existing) if existing else database.save_movie(details,filepath)
        finally:
            database.close()
        if not movie_id:
            raise TvMatchError('movie_write_failed','独立电影写入失败，请重试。')
        evidence = {'type':'movie','tmdb_id':identity,'reason':decision['reason'],
                    'source_titles':decision['evidence'].get('titles',[]),'source_year':decision['evidence'].get('year')}
        art = copy.deepcopy(details.get('available_art',{}))
        art['cast'] = [{'url':c['thumbnail']} for c in details.get('cast',[])[:5] if c.get('thumbnail')]
        return ('merged' if existing else 'success',filepath,os.path.basename(filepath),movie_id,
                details['info']['title'],art,0,0,None,details['info'].get('year',''),evidence)

    def prepare_file(self, filepath):
        import os
        context = self.contexts[filepath]
        if not context['title']:
            raise TvMatchError('show_title_missing','无法从目录或文件名取得剧名，请手动输入剧名。')
        # The show identity is always confirmed first. An episode's absence
        # from TMDb is no longer a failure to match the containing show.
        try:
            resolved = self.resolve_show(context)
        except TvMatchError as error:
            # A TV source can include independent companion feature films.
            # Try the existing strict movie matcher only for a directory
            # with no season/episode evidence, never for failed episodes.
            if error.reason=='show_identity_unverified' and self._standalone_directory(context):
                movie = self._once(('standalone_movie',context['show_root']),
                    lambda:self._resolve_standalone_movie(filepath))
                if movie:
                    return {'status':'movie_ready','context':context,'movie':movie}
            raise
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
                           'season_name':''}
            else:
                details = copy.deepcopy(plan['season_details']['episodes'][number])
            episodes.append((plan['season'],number,details))
        return {'status':'ready','context':context,'plan':plan,'episodes':episodes}

    def process_file(self, filepath):
        import os
        context = self.contexts[filepath]
        evidence = {'type':'tvshow','source_title':context['title'],'source_year':context['year'],
                    'show_root':context['show_root'],
                    'source_episode_numbers':context['episodes']}
        try:
            prepared = self.prepare_file(filepath)
            if getattr(self,'cancelled',lambda:False)():
                raise TvMatchError('cancelled','扫描已停止，保留待匹配状态。')
            if prepared['status']=='movie_ready':
                return self._process_standalone_movie(filepath,prepared)
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
