import pathlib,sqlite3,sys,tempfile,unittest
from types import SimpleNamespace
from unittest.mock import Mock
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'files'))
from api_read_cache import ReadModelCache,install

class Checks(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.conn=sqlite3.connect(str(pathlib.Path(self.tmp.name)/'db'),isolation_level=None)
  self.conn.execute('create table items(value text)');self.addCleanup(self.conn.close)
  self.db=SimpleNamespace(sql_connection=self.conn);self.time=[0.]
  self.factory=Mock(side_effect=lambda *a:object());self.cache=ReadModelCache(self.factory,lambda:self.time[0])
  self.path='/tmp/api/table_notifications.json'
 def read(self,force=False,query='SELECT * FROM items',path=None,ad_hoc=False):
  return self.cache(self.db,force,query,path or self.path,ad_hoc)
 def test_unchanged_reads_reuse_native_endpoint_and_pending_writer(self):
  endpoint=self.read();self.assertIs(self.read(),endpoint);self.assertEqual(self.factory.call_count,1)
 def test_own_and_external_writes_both_invalidate(self):
  first=self.read();self.conn.execute("insert into items values ('own')")
  second=self.read();self.assertIsNot(first,second)
  with sqlite3.connect(str(pathlib.Path(self.tmp.name)/'db')) as other:
   other.execute("update items set value='external'")
  self.assertIsNot(self.read(),second)
 def test_connection_replacement_force_partial_and_sql_changes(self):
  first=self.read();self.assertIsNot(self.read(force=True),first)
  self.read();self.read(ad_hoc=True);self.assertEqual(self.factory.call_count,4)
  self.read();self.read(query='SELECT value FROM items');self.assertEqual(self.factory.call_count,6)
 def test_new_connection_never_reuses_an_old_connections_version(self):
  a=self.read()
  other=sqlite3.connect(str(pathlib.Path(self.tmp.name)/'db'));self.addCleanup(other.close)
  self.db.sql_connection=other
  self.assertIsNot(self.read(),a)
 def test_time_and_custom_queries_never_cached_and_static_has_max_age(self):
  for path in ('table_devices.json','table_devices_tiles.json','table_resource_history_hour.json','table_custom_endpoint.json'):
   a=self.read(path=path);self.assertIsNot(self.read(path=path),a)
  a=self.read();self.time[0]=31;self.assertIsNot(self.read(),a)
 def test_concurrent_write_during_build_is_not_marked_current(self):
  def build(*args):self.conn.execute("insert into items values ('during')");return object()
  self.factory.side_effect=build;self.read();self.read();self.assertEqual(self.factory.call_count,2)
 def test_metadata_failure_falls_back_and_install_is_idempotent(self):
  self.db.sql_connection=None;self.read();self.read();self.assertEqual(self.factory.call_count,2)
  native=SimpleNamespace(api_endpoint_class=self.factory);install(native);wrapper=native.api_endpoint_class;install(native);self.assertIs(native.api_endpoint_class,wrapper)
if __name__=='__main__':unittest.main()
