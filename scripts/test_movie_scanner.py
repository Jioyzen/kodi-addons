import sys,ast,json,types,queue,threading,os,unittest
from pathlib import Path
from unittest.mock import patch
SOURCE=Path(__file__).resolve().parents[1]/'addons/script.ai.movie.scanner/python';sys.path.insert(0,str(SOURCE))
sys.modules['xbmc']=types.SimpleNamespace(LOGINFO=1,LOGERROR=2,LOGWARNING=3,log=lambda *a,**k:None)
import ai_parser,tmdb_api,matching,movie_matcher
namespace={'queue':queue,'threading':threading,'os':os,'time':types.SimpleNamespace(sleep=lambda _:None),'L':lambda _: '', 'log':lambda *a:None,'_art_with_cast':lambda x:{},'xbmc':sys.modules['xbmc'],'ADDON_NAME':'test'}
tree=ast.parse(((SOURCE/'main.py')).read_text(encoding='utf-8'))
exec(compile(ast.Module(body=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='scan_worker'],type_ignores=[]),'worker','exec'),namespace)
class Tests(unittest.TestCase):
 def run_worker(self,filename,resolution,regex=None,force_movie=False):
  abort=threading.Event();jobs=queue.Queue();out=queue.Queue();jobs.put((filename,''));jobs.task_done=lambda:abort.set();writes=[]
  class Database:
   def connect(self):pass
   def close(self):pass
   def find_movie_by_uniqueid(self,*args):return None
   def save_movie(self,details,path,**kwargs):writes.append((details,path));return 10
  mods={'kodi_db':types.SimpleNamespace(KodiDatabase=Database),'tvshow_db':types.SimpleNamespace(TvShowDatabase=Database)}
  patches=[patch.dict(sys.modules,mods),patch.object(movie_matcher,'resolve_movie',return_value=resolution),patch.object(tmdb_api,'get_movie_details',return_value={'info':{'title':'Correct','year':2020}}),patch.object(ai_parser,'parse_filename',side_effect=AssertionError('Not needed'))]
  if regex is not None:patches.append(patch.object(ai_parser,'regex_parse_filename',return_value=regex))
  from contextlib import ExitStack
  with ExitStack() as stack:
   for p in patches:stack.enter_context(p)
   session=types.SimpleNamespace(context=lambda _:None,forced_movies={filename}) if force_movie else None
   namespace['scan_worker'](jobs,out,False,'dummy','dummy','dummy','dummy','zh-CN',0,abort,session)
  return out.get_nowait(),writes
 def test_verified_only_writes_and_records_evidence(self):
  result,writes=self.run_worker('Correct.2020.mkv',{'status':'accepted','candidate':{'id':1,'title':'Correct'},'reason':'source_title_year'})
  self.assertEqual(result[0],'success');self.assertEqual(len(writes),1);self.assertEqual(result[10]['source_year'],2020)
 def test_ambiguity_never_writes(self):
  result,writes=self.run_worker('Correct.2020.mkv',{'status':'ambiguous','candidate':None,'candidates':[{'id':1,'title':'Correct'},{'id':2,'title':'Correct'}],'reason':'same_title_year'})
  self.assertEqual(result[0],'pending');self.assertEqual(writes,[]);self.assertIn('TMDb 1',result[8]);self.assertIn('TMDb 2',result[8])
 def test_unresolved_never_writes_without_ai(self):
  result,writes=self.run_worker('Correct.2020.mkv',{'status':'unresolved','candidate':None,'reason':'year_conflict'})
  self.assertEqual(result[0],'pending');self.assertEqual(writes,[])
 def test_explicit_identifier_needs_no_regex_or_ai(self):
  with patch.object(ai_parser,'regex_parse_filename',return_value=None):
   result,writes=self.run_worker('{tmdbid-1}.mkv',{'status':'accepted','candidate':{'id':1,'title':'Correct'},'reason':'explicit_identifier'})
  self.assertEqual(result[0],'success');self.assertEqual(len(writes),1)
 def test_movie_content_type_overrides_episode_like_filename(self):
  result,writes=self.run_worker('Film.S01E01.2020.mkv',{'status':'accepted','candidate':{'id':1,'title':'Film'},'reason':'explicit_movie_source'},regex={'type':'tvshow','title':'Film','year':2020,'season_number':1,'episode_number':1},force_movie=True)
  self.assertEqual(result[0],'success');self.assertEqual(len(writes),1)
 def test_ai_hallucinated_episode_rejected(self):
  p=ai_parser._normalize_ai_result({'type':'tvshow','title':'Film','year':2020,'season_number':3,'episode_number':9},'Film.2020.2160p.mkv','','auto')
  self.assertEqual((p['type'],p['season_number'],p['episode_number']),('movie',0,0))
 def test_actual_episode_still_tv(self):
  p=ai_parser._normalize_ai_result({'type':'movie','title':'Show'},'Show.S02E03.mkv','','auto')
  self.assertEqual((p['type'],p['season_number'],p['episode_number']),('tvshow',2,3))

 def test_stop_during_metadata_request_prevents_database_write(self):
  abort=threading.Event();jobs=queue.Queue();out=queue.Queue();jobs.put(('Correct.2020.mkv',''))
  class Database:
   def __init__(self):raise AssertionError('Cancelled metadata must not open writer')
  def details(*args):abort.set();return {'info':{'title':'Correct','year':2020}}
  mods={'kodi_db':types.SimpleNamespace(KodiDatabase=Database),'tvshow_db':types.SimpleNamespace(TvShowDatabase=Database)}
  with patch.dict(sys.modules,mods),patch.object(movie_matcher,'resolve_movie',return_value={'status':'accepted','candidate':{'id':1,'title':'Correct'},'reason':'source_title_year'}),patch.object(tmdb_api,'get_movie_details',side_effect=details):
   namespace['scan_worker'](jobs,out,False,'dummy','dummy','dummy','dummy','zh-CN',0,abort)
  self.assertTrue(out.empty());self.assertEqual(jobs.unfinished_tasks,0)

 def test_external_stop_is_seen_before_controller_sets_abort_event(self):
  abort=threading.Event();state={'stop':False};jobs=queue.Queue();out=queue.Queue();jobs.put(('Correct.2020.mkv',''))
  class Database:
   def __init__(self):raise AssertionError('External stop must prevent opening writer')
  def details(*args):state['stop']=True;return {'info':{'title':'Correct','year':2020}}
  mods={'kodi_db':types.SimpleNamespace(KodiDatabase=Database),'tvshow_db':types.SimpleNamespace(TvShowDatabase=Database)}
  with patch.dict(sys.modules,mods),patch.object(movie_matcher,'resolve_movie',return_value={'status':'accepted','candidate':{'id':1,'title':'Correct'},'reason':'source_title_year'}),patch.object(tmdb_api,'get_movie_details',side_effect=details):
   namespace['scan_worker'](jobs,out,False,'dummy','dummy','dummy','dummy','zh-CN',0,abort,stopped=lambda:state['stop'])
  self.assertFalse(abort.is_set());self.assertTrue(out.empty());self.assertEqual(jobs.unfinished_tasks,0)
if __name__=='__main__':unittest.main(verbosity=2)
