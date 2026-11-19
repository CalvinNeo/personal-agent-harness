"""Offline scheduler/outbox behavior; only the UI framework and transport are fakes."""
import asyncio,copy,importlib.util,json,logging,sys,tempfile,types,unittest
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).parent
PACKAGE='_xianyu_watch_offline'
pkg=types.ModuleType(PACKAGE);pkg.__path__=[str(ROOT)]

class Star:
 def __init__(self,context):self.logger=logging.getLogger('test-watch')
class Plain:
 def __init__(self,text):self.text=text
class Image:
 @staticmethod
 def fromBytes(value):return ('image',value)
class Reply:pass
class Chain:
 def __init__(self,chain):self.chain=chain

def decorate(*args,**kwargs):return lambda fn:fn
modules={name:types.ModuleType(name) for name in ('astrbot','astrbot.api','astrbot.api.event','astrbot.api.message_components','astrbot.core','astrbot.core.star','astrbot.core.star.filter','astrbot.core.star.filter.command','astrbot.core.pipeline','astrbot.core.pipeline.preprocess_stage','astrbot.core.pipeline.preprocess_stage.voice_input')}
modules[PACKAGE]=pkg
modules['astrbot.api'].star=types.SimpleNamespace(Star=Star,Context=object,register=decorate)
modules['astrbot.api.event'].AstrMessageEvent=object
modules['astrbot.api.event'].MessageChain=Chain
modules['astrbot.api.event'].filter=types.SimpleNamespace(command=decorate,event_message_type=decorate,on_llm_request=decorate,EventMessageType=types.SimpleNamespace(GROUP_MESSAGE=1,PRIVATE_MESSAGE=2))
modules['astrbot.api.message_components'].Plain=Plain
modules['astrbot.api.message_components'].Image=Image
modules['astrbot.api.message_components'].Reply=Reply
modules['astrbot.core.star.filter.command'].GreedyStr=str
modules['astrbot.core.pipeline.preprocess_stage.voice_input'].get_voice_input_text=lambda event:''
with patch.dict(sys.modules,modules):
 spec=importlib.util.spec_from_file_location(PACKAGE+'.main',ROOT/'main.py')
 plugin=importlib.util.module_from_spec(spec);sys.modules[spec.name]=plugin;spec.loader.exec_module(plugin)
 Main,TOOL_NAME,MANAGE_NAME,owner=plugin.Main,plugin.TOOL_NAME,plugin.MANAGE_NAME,plugin.owner
SCOPE='synthetic_bot:GroupMessage:synthetic-group'
class Event:
 is_at_or_wake_command=True
 def __init__(self,sender='alice',scope=SCOPE):self.unified_msg_origin=scope;self.sender=sender
 def get_platform_name(self):return 'qq_official'
 def get_sender_id(self):return self.sender
class Context:
 def __init__(self):self.sent=[];self.reject=False;self.enter=None;self.release=None
 async def send_message(self,scope,chain):
  self.sent.append((scope,chain))
  if self.enter:self.enter.set()
  if self.release:await self.release.wait()
  return not self.reject
class FakeClient:
 def __init__(self):self.logged_in=True;self.rows=[];self.status_error=None;self.search_error=None;self.images=True
 def status(self):return {'logged_in':self.logged_in,**({'error':self.status_error} if self.status_error else {})}
 def search(self,k,limit):
  if self.search_error:return {'ok':False,'error':self.search_error}
  return {'ok':True,'results':copy.deepcopy(self.rows)}
 def download_image(self,url):return b'png' if self.images else None

def row(i,title=None):return {'item_id':str(i),'title':title or 'item '+str(i),'price':'12.50','url':'https://evil.invalid/redirect','image_url':'https://img.alicdn.com/a.jpg'}
class Watches(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.file=Path(self.tmp.name)/'watches.json';self.context=Context();self.client=FakeClient();self.event=Event();self.config={'state_file':str(self.file),'allowed_umos':[SCOPE],'login_umos':['synthetic_bot:FriendMessage:alice'],'login_senders':['alice']};self.bridge=self.new()
 async def asyncTearDown(self):await self.bridge.terminate();self.tmp.cleanup()
 def new(self):
  b=Main(self.context,self.config);b.client=lambda:self.client;return b
 async def create(self):
  r=await self.bridge._create(self.event,['玩具']);self.assertTrue(r['ok'],r);return r['watch']['id']
 async def test_first_baseline_has_no_alert_then_new_items_alert_once(self):
  self.client.rows=[row(1)];wid=await self.create();self.assertFalse(self.context.sent)
  self.client.rows=[row(1),row(2),row(2)]
  await self.bridge._run(wid);await self.bridge._run(wid)
  self.assertEqual(len(self.context.sent),1);self.assertEqual(set(self.bridge.records[wid]['seen_ids']),{'1','2'});self.assertEqual(self.bridge.records[wid]['pending_alerts'],[])
  message=self.context.sent[0][1].chain[0].text;self.assertIn('https://h5.m.goofish.com/item?id=2',message);self.assertNotIn('evil.invalid',message);self.assertIn('12.50',message)
 async def test_delivery_failure_keeps_pending_same_id_after_restart(self):
  wid=await self.create();self.client.rows=[row(2)];self.context.reject=True;await self.bridge._run(wid)
  state=self.bridge.records[wid];aid=state['pending_alerts'][0]['alert_id'];self.assertNotIn('2',state['seen_ids'])
  self.bridge=self.new();state=self.bridge.records[wid];self.assertEqual(state['pending_alerts'][0]['alert_id'],aid);state['next_delivery_at']=0;self.context.reject=False
  await self.bridge._tick();await self.bridge._tick();self.assertEqual(len(self.context.sent),2);self.assertEqual(state['pending_alerts'],[]);self.assertIn('2',state['seen_ids'])
 async def test_create_before_login_preserves_intent_and_later_baselines(self):
  self.client.logged_in=False;wid=await self.create();self.assertEqual(self.bridge.records[wid]['state'],'awaiting_login');self.assertFalse(self.bridge.records[wid]['baseline_ready'])
  self.bridge=self.new();self.client.logged_in=True;self.client.rows=[row(1)];await self.bridge._run(wid);self.assertTrue(self.bridge.records[wid]['baseline_ready']);self.assertFalse(self.context.sent)
 async def test_search_error_is_not_empty_baseline(self):
  self.client.search_error='search_unavailable';wid=await self.create();self.assertFalse(self.bridge.records[wid]['baseline_ready']);self.assertEqual(self.bridge.records[wid]['last_error'],'search_unavailable')
 async def test_pause_and_cancel_preserve_scope_and_suppress_pending(self):
  wid=await self.create();self.client.rows=[row(2)];self.context.reject=True;await self.bridge._run(wid)
  r=await self.bridge._manage(self.event,wid,'pause');self.assertTrue(r['ok']);self.context.reject=False;self.bridge.records[wid]['next_delivery_at']=0;await self.bridge._tick();self.assertEqual(len(self.context.sent),1)
  r=await self.bridge._manage(Event('bob'),wid,'resume');self.assertTrue(r['ok'])
  r=await self.bridge._manage(self.event,wid,'cancel');self.assertEqual(r['watch']['state'],'cancelled');await self.bridge._tick();self.assertEqual(len(self.context.sent),1)
 async def test_cancel_during_search_cannot_commit_or_send(self):
  wid=await self.create();entered=asyncio.Event();release=asyncio.Event()
  async def fetch(_):entered.set();await release.wait();return [row(2)]
  self.bridge._fetch=fetch;running=asyncio.create_task(self.bridge._run(wid));await entered.wait();await self.bridge._manage(self.event,wid,'cancel');release.set();await running
  self.assertFalse(self.context.sent);self.assertEqual(self.bridge.records[wid]['state'],'cancelled')
 async def test_pause_waits_for_inflight_delivery_and_then_blocks_later_sends(self):
  wid=await self.create();self.client.rows=[row(2),row(3)];self.context.enter=asyncio.Event();self.context.release=asyncio.Event();running=asyncio.create_task(self.bridge._run(wid));await self.context.enter.wait()
  pause=asyncio.create_task(self.bridge._manage(self.event,wid,'pause'));await asyncio.sleep(.01);self.assertFalse(pause.done());self.context.release.set();await running;await pause
  count=len(self.context.sent);await self.bridge._run(wid);self.assertEqual(len(self.context.sent),count)
 async def test_corrupt_storage_fails_closed_without_overwrite(self):
  self.file.write_text('{bad');b=self.new();self.assertFalse(b._storage_ok);r=await b._create(self.event,['玩具']);self.assertEqual(r['code'],'storage_unavailable');self.assertEqual(self.file.read_text(),'{bad')
 async def test_wrong_owner_in_storage_fails_closed(self):
  wid=await self.create();d=json.loads(self.file.read_text());d['watches'][0]['sender']='mallory';self.file.write_text(json.dumps(d));self.assertFalse(self.new()._storage_ok)
 async def test_revoked_scope_does_not_search_or_send(self):
  wid=await self.create();self.client.rows=[row(2)];self.bridge.allowed_umos=set();await self.bridge._run(wid);self.assertFalse(self.context.sent)
 async def test_login_required_notice_only_once_and_auto_recovery(self):
  wid=await self.create();self.client.logged_in=False;await self.bridge._run(wid);await self.bridge._run(wid);self.assertEqual(len(self.context.sent),1);self.assertEqual(self.bridge.records[wid]['state'],'awaiting_login')
  self.client.logged_in=True;await self.bridge._run(wid);self.assertEqual(self.bridge.records[wid]['state'],'active')
 async def test_product_images_disabled_preserves_text_delivery(self):
  wid=await self.create();self.client.rows=[row(2)];self.client.images=False;await self.bridge._run(wid);self.assertEqual(len(self.context.sent[0][1].chain),1);self.assertIn('item 2',self.context.sent[0][1].chain[0].text)
 async def test_duplicate_create_does_not_reset_baseline(self):
  self.client.rows=[row(1)];wid=await self.create();self.client.rows=[row(2)];r=await self.bridge._create(self.event,['玩具']);self.assertFalse(r['created']);self.assertEqual(set(self.bridge.records[wid]['seen_ids']),{'1'})
 async def test_no_send_if_storage_save_fails(self):
  wid=await self.create();self.client.rows=[row(2)]
  def fail():self.bridge._storage_ok=False;raise OSError('disk full')
  self.bridge._save=fail;await self.bridge._run(wid);self.assertFalse(self.context.sent)
 async def test_function_words_cannot_be_watch_keywords(self):
  r=await self.bridge._create(self.event,['闲鱼']);self.assertEqual(r['code'],'invalid_watch');self.assertFalse(self.bridge.records)
 async def test_corrupt_scheduled_time_fails_closed(self):
  wid=await self.create();d=json.loads(self.file.read_text());d['watches'][0]['next_run_at']='soon';self.file.write_text(json.dumps(d));self.assertFalse(self.new()._storage_ok)
 async def test_tool_defaults_and_foreign_chat_denied(self):
  wid=await self.create();self.assertEqual(self.bridge.records[wid]['interval_minutes'],30);r=await self.bridge._manage(Event(scope='synthetic_bot:GroupMessage:other'),wid,'cancel');self.assertFalse(r['ok'])

 async def test_price_only_update_keeps_next_run_and_blocks_stale_fetch_commit(self):
  wid=await self.create();original_due=self.bridge.records[wid]['next_run_at'];entered=asyncio.Event();release=asyncio.Event()
  async def fetch(_):entered.set();await release.wait();return [dict(row(2),price='50')]
  self.bridge._fetch=fetch;running=asyncio.create_task(self.bridge._run(wid));await entered.wait()
  changed=await self.bridge._manage(Event('bob'),wid,'update',max_price=10);self.assertTrue(changed['ok']);self.assertEqual(self.bridge.records[wid]['next_run_at'],original_due)
  release.set();await running
  self.assertEqual(self.context.sent,[]);self.assertEqual(self.bridge.records[wid]['seen_ids'],{});self.assertEqual(self.bridge.records[wid]['pending_alerts'],[])

 async def test_current_price_condition_is_checked_again_before_pending_delivery(self):
  wid=await self.create();self.client.rows=[row(2)];self.context.reject=True;await self.bridge._run(wid)
  self.assertTrue(self.bridge.records[wid]['pending_alerts']);self.context.sent.clear();self.context.reject=False
  # Simulate a persisted condition arriving before an old queued alert flush.
  self.bridge.records[wid]['max_price']=10;self.bridge.records[wid]['next_delivery_at']=0
  async with self.bridge._watch_lock(wid):await self.bridge._flush_locked(wid)
  self.assertEqual(self.context.sent,[]);self.assertEqual(self.bridge.records[wid]['pending_alerts'],[]);self.assertIn('2',self.bridge.records[wid]['seen_ids'])

if __name__=='__main__':unittest.main(verbosity=2)
