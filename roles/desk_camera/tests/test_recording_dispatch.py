"""Slow root IPC must not block the ordered NATS lease subscription callback."""
import ast,asyncio,pathlib,sys,time,types,unittest
from unittest.mock import AsyncMock,Mock,patch
FILES=pathlib.Path(__file__).resolve().parents[1]/'files'
sys.path.insert(0,str(FILES));sys.path.insert(0,str(FILES.parents[1]/'desk_homeassistant/files'))
from protocol import pack,unpack
from test_recording_contract import query
tree=ast.parse((FILES/'controller.py').read_text());source=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='Controller')
source.body=[n for n in source.body if isinstance(n,ast.AsyncFunctionDef) and n.name in ('received','recording_reply')]
ns={'asyncio':asyncio,'clock':time.monotonic,'time':time,'pack':pack,'unpack':unpack}
exec(compile(ast.Module(body=[source],type_ignores=[]),'controller.py','exec'),ns)
class DispatchTests(unittest.IsolatedAsyncioTestCase):
 async def test_lease_reply_is_immediate_while_recording_ipc_is_stalled(self):
  a=ns['Controller']();a.node='deskb1x';a.prefix='desk.ha.media.tapo_c222_163';a.c={'key':'offline','workload':'tapo_c222_163'};a.nonces={};a.recording_tasks=set();a.rpc=Mock(return_value={'grant':True})
  entered=asyncio.Event();finish=asyncio.Event()
  async def request(*args,**kwargs):entered.set();await finish.wait();return {'state':'queued'}
  def message(action,ident,value):
   return types.SimpleNamespace(data=pack({'sender':'deskb3x','recipient':a.node,'id':ident*32,'at':time.time(),'action':action,'value':value},'offline'),reply=a.prefix+'.reply.deskb3x.'+ident*32,respond=AsyncMock())
  value=query();value['deadline']=time.time()+25
  read=message('recording_v1','c',value);lease=message('lease','d',{})
  with patch.dict(sys.modules,{'media_rpc':types.SimpleNamespace(request=request)}):
   await asyncio.wait_for(a.received(read),.2);await entered.wait();self.assertFalse(read.respond.called)
   await asyncio.wait_for(a.received(lease),.2);lease.respond.assert_awaited_once();a.rpc.assert_called_once_with('lease',{},'deskb3x')
   finish.set();await asyncio.gather(*a.recording_tasks);read.respond.assert_awaited_once()
if __name__=='__main__':unittest.main()
