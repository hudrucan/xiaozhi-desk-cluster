"""Pinned-image fixture: synthetic SQLite/read models, no production mounts or network."""
import importlib.util,json,pathlib,sqlite3,sys,time,types
from unittest.mock import patch
sys.path[:0]=['/app/desk_network','/app/server','/app/server/plugins']
for name in ('/data/config','/tmp/api','/tmp/log/plugins'):pathlib.Path(name).mkdir(parents=True,exist_ok=True)
pathlib.Path('/data/config/app.conf').write_text("TIMEZONE='UTC'\nLOG_LEVEL='none'\n")
kind=json.dumps({'dataType':'string','elements':[{'elementType':'input','transformers':[]}]})
pathlib.Path('/tmp/api/table_settings.json').write_text(json.dumps({'data':[{'setKey':k,'setValue':v,'setType':kind} for k,v in [('TIMEZONE','UTC'),('LOG_LEVEL','none')]]}))
import api
from api_read_cache import ReadModelCache
NATIVE=api.api_endpoint_class
QUERY='SELECT * FROM Notifications'
PATH='/tmp/api/table_notifications.json'

class Database:
 def __init__(self,rows):
  self.sql_connection=sqlite3.connect(':memory:',isolation_level=None);self.reads=0
  self.sql_connection.execute('CREATE TABLE Notifications(value TEXT)')
  self.sql_connection.executemany('INSERT INTO Notifications VALUES (?)',rows)
 def get_table_as_json(self,query):
  self.reads+=1;cursor=self.sql_connection.execute(query);names=[v[0] for v in cursor.description]
  return types.SimpleNamespace(json={'data':[dict(zip(names,row)) for row in cursor.fetchall()]})

def correctness():
 api.apiEndpoints.clear();db=Database([('before',)]);clock=[0.]
 try:
  cache=ReadModelCache(NATIVE,lambda:clock[0]);first=cache(db,False,QUERY,PATH)
  assert first.needsUpdate and api.apiEndpoints==[first]
  clock[0]=3600;assert cache(db,False,QUERY,PATH) is first and first.needsUpdate
  first.try_write(True);assert json.loads(pathlib.Path(PATH).read_text())=={'data':[{'value':'before'}]}
  db.sql_connection.execute("UPDATE Notifications SET value='after'")
  changed=cache(db,False,QUERY,PATH);assert changed is not first
  changed.try_write(True);assert json.loads(pathlib.Path(PATH).read_text())=={'data':[{'value':'after'}]}
  assert cache(db,True,QUERY,PATH) is not changed
  db.sql_connection.execute("UPDATE Notifications SET value='UI change'")
  with patch.object(api,'UserEventsQueueInstance') as queue:
   edited=cache(db,False,QUERY,PATH,True);edited.try_write(True)
   queue.return_value.finalize_event.assert_called_once_with('update_api')
  assert json.loads(pathlib.Path(PATH).read_text())=={'data':[{'value':'UI change'}]}
 finally:db.sql_connection.close()
 print('PASS native pending writer, immediate changed rows, force refresh and UI completion')

def benchmark(cache_type):
 api.apiEndpoints.clear();db=Database([('x'*256,)]*5000);clock=[0.]
 try:
  cache=cache_type(NATIVE,lambda:clock[0]);cpu=time.process_time()
  for tick in range(0,121,5):
   clock[0]=tick;cache(db,False,QUERY,PATH)
  return dict(materializations=db.reads,cpu_seconds=round(time.process_time()-cpu,4))
 finally:db.sql_connection.close()

if __name__=='__main__':
 correctness()
 if len(sys.argv)>1:
  spec=importlib.util.spec_from_file_location('baseline_cache',sys.argv[1]);old=importlib.util.module_from_spec(spec);spec.loader.exec_module(old)
  before=benchmark(old.ReadModelCache);after=benchmark(ReadModelCache)
  assert before['materializations']==5 and after['materializations']==1
  print('BENCHMARK '+json.dumps(dict(before=before,after=after)))
