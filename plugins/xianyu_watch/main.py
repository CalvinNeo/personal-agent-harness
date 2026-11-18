"""Authorized shared Xianyu watches with price conditions and durable item snapshots."""
from __future__ import annotations
import asyncio,copy,hashlib,json,math,os,re,stat,tempfile,time
from pathlib import Path
from typing import Any
from astrbot.api import star
from astrbot.api.event import AstrMessageEvent,MessageChain,filter
from astrbot.api.message_components import Image,Plain,Reply
from astrbot.core.star.filter.command import GreedyStr
from astrbot.core.pipeline.preprocess_stage.voice_input import get_voice_input_text
from .client import XianyuClient,XianyuError
from .watcher import STATES,normalize_keywords,normalize_item,merge_pending,mark_alert_delivered,compact_record,normalize_max_price,matches_price,baseline_snapshot,baseline_view

DEFAULT_ENDPOINT='http://127.0.0.1:18070';DEFAULT_TOKEN='/etc/xianyu/api-token'
TOOL_NAME='create_xianyu_watch';LIST_NAME='list_xianyu_watches';MANAGE_NAME='manage_xianyu_watch';READ_NAME='read_xianyu_watch'
_UNSET=object()
PRICE_SCHEMA={'type':'number','exclusiveMinimum':0,'maximum':1000000000,'description':'商品标价必须严格小于此上限（不含等于）；未知、面议、范围、定金、运费价不视为合规。'}
TOOL_SCHEMA={'type':'object','properties':{'keywords':{'type':'array','minItems':1,'maxItems':5,'description':'1至5个具体搜索词组，可使用相关同义词扩展。','items':{'type':'string','minLength':1,'maxLength':100}},'interval_minutes':{'type':'integer','minimum':5,'maximum':10080,'default':30},'max_results':{'type':'integer','minimum':1,'maximum':20},'max_price':PRICE_SCHEMA},'required':['keywords'],'additionalProperties':False}
READ_SCHEMA={'type':'object','properties':{'watch_id':{'type':'string','pattern':'^[a-f0-9]{16}$'},'offset':{'type':'integer','minimum':0,'maximum':10000,'default':0},'limit':{'type':'integer','minimum':1,'maximum':50,'default':20},'refresh_metadata':{'type':'boolean','default':False,'description':'仅明确要求补回旧基线详情时启用；当前查询只补原ID缺失字段，不添加新ID、不发提醒、不改变下次执行时间。'}},'required':['watch_id'],'additionalProperties':False}
MANAGE_SCHEMA={'type':'object','properties':{'watch_id':{'type':'string','pattern':'^[a-f0-9]{16}$'},'action':{'type':'string','enum':['pause','resume','cancel','update']},'max_price':{**PRICE_SCHEMA,'type':['number','null'],'description':'update时设置严格价格上限；null明确取消价格限制。'},'interval_minutes':{'type':'integer','minimum':5,'maximum':10080},'max_results':{'type':'integer','minimum':1,'maximum':20}},'required':['watch_id','action'],'additionalProperties':False}

SEARCH_NAME='xianyu_search'
SEARCH_SCHEMA={'type':'object','properties':{'query':{'type':'string','minLength':1,'maxLength':100,'description':'当前要找的闲鱼商品关键词'},'limit':{'type':'integer','minimum':1,'maximum':20,'default':10}},'required':['query'],'additionalProperties':False}
ROUTING='''[闲鱼监控：用户明确要求定期搜索新商品时，使用create_xianyu_watch。你应根据用户的实际购买目标和当前聊天资料构造1至5个具体关键词；缺少购买目标时先读取当前聊天或询问，不把“闲鱼”“提醒”等功能词当关键词。默认每30分钟查询；首次只建立已见基线，之后只提醒首次发现的商品。提醒接收人自动绑定原会话，不填其他UMO。必须登录才能检索；未登录时保存监控草案，使用已授权群聊或私聊的 /闲鱼 login 扫码，再 /闲鱼 status 确认。不得接收密码、Cookie、Token。商品名称、价格和链接由服务端依据实际结果生成，默认不发送商品图片，不能编造。已授权会话共享监控列表、详情和管理，投递目标始终为创建时原会话。列表用list_xianyu_watches，详情和完整首次基线用read_xianyu_watch分页读取，不得把空列表猜成没有监控。预算必须把max_price传给create或manage update，比较为严格小于；未传入时不能承诺已启用预算过滤。暂停/恢复/取消/条件更新使用manage_xianyu_watch。首次基线总数和符合条件数分别汇报；旧版只有ID时明确详情缺失，补回须read的refresh_metadata=true，恢复时间不代表首次价格。新发现表示首次被该监控发现，不保证平台精确发布时间。]'''

def ident(event):return (str(event.unified_msg_origin),str(event.get_sender_id() or ''))
def owner(event):return hashlib.sha256(('\0'.join(ident(event))).encode()).hexdigest()
def private(event):return any(x in str(event.unified_msg_origin).lower() for x in (':friendmessage:',':privatemessage:',':directmessage:'))

def current_text(event):
 try:voice=get_voice_input_text(event)
 except Exception:voice=''
 if voice:return voice.strip()
 return ' '.join(part.text for part in event.get_messages() if isinstance(part,Plain)).strip()

def login_action(value,pending=False):
 value=(value or '').strip().replace('咸鱼','闲鱼').replace('登陆','登录')
 if not value or value.startswith(('/', '>', '\x60', '“', '‘', '"', "'", '「', '『')):return None
 if re.search(r'不要|不用|无需|不需要|禁止|别|例如|比如|他说|她说|转发|引用|假如|假设|开发|编写|实现.{0,12}(?:登录|代码)',value):return None
 # Short questions refer to login only after a login/status exchange in this
 # exact QQ scope or an explicitly quoted login response. Keep this before the
 # generic question guard: asking whether login finished is a status request.
 if pending and re.fullmatch(r'(?:(?:请问|那|那么|现在|这次|目前|已经)\s*)*(?:(?:闲鱼|xianyu|goofish)\s*)?(?:(?:登录|扫码|浏览器授权|授权|账号状态|状态)\s*)?(?:好(?:了)?|成功(?:了)?|完成(?:了)?|确认(?:了)?|能用了|可以用了)(?:吗|么|嘛|没|没有)[。？！?!\s]*',value,re.I):return 'status'
 if re.match(r'^(?:请问)?(?:怎么|如何|为什么|为啥)',value) or re.search(r'(?:吗|么|嘛)[？?。]*$|[？?]$',value):return None
 explicit=bool(re.search(r'闲鱼|xianyu|goofish',value,re.I))
 if not explicit:
  if not pending:return None
  if re.fullmatch(r'(?:好了|好啦|扫好了|扫好啦|扫完了|扫了|已扫码|已经扫码|已确认|确认了|已登录|已经登录|登录了)[。！!s]*',value):return 'status'
  if re.search(r'(?:检查|确认|查看|看下|看看|查询).{0,20}(?:登录|状态|ready)|(?:已经|已).{0,6}登录|扫码.{0,6}(?:完成|成功)|扫好了',value,re.I):return 'status'
  return None
 if re.search(r'已.{0,6}(?:扫码|登录)|扫码.{0,6}(?:完成|成功|了)|登录(?:状态|态)|(?:检查|查看|查询|确认|看下|看看).{0,20}(?:状态|登录)|多久.{0,8}过期|有效期',value):return 'status'
 if re.search(r'(?:登录|扫码|二维码).{0,12}(?:闲鱼|xianyu|goofish)|(?:闲鱼|xianyu|goofish).{0,12}(?:登录|扫码|二维码)',value,re.I):return 'login'
 return None

def safe(value,limit=300):return re.sub(r'[\x00-\x1f\x7f]+',' ',str(value)).strip()[:limit]

@star.register(name='xianyu_watch',author='local',desc='登录后闲鱼新商品监控与QQ提醒',version='1.1.0')
class Main(star.Star):
 def __init__(self,context:star.Context,config:dict[str,Any]|None=None):
  super().__init__(context);self.context=context;self.cfg=dict(config or {})
  for key in ('allowed_umos','allowed_senders','login_umos','login_senders'):setattr(self,key,{str(x).strip() for x in self.cfg.get(key,[]) if str(x).strip()})
  self.endpoint=str(self.cfg.get('endpoint') or DEFAULT_ENDPOINT);self.token_file=str(self.cfg.get('api_token_file') or DEFAULT_TOKEN)
  self.file=Path(self.cfg.get('state_file') or '/var/lib/astrbot/data/plugins/xianyu_watch/watches.json')
  self.records={};self._storage_ok=True;self._lock=asyncio.Lock();self._tick_lock=asyncio.Lock();self._watch_locks={};self._task=None;self._pending_login={};self._pending_login_scopes={};self._recent_login_scopes={};self._load()
 def _allowed(self,event):return event.get_platform_name()=='qq_official' and self._route_allowed({'umo':str(event.unified_msg_origin),'sender':str(event.get_sender_id() or '')})
 def allowed(self,event):return self._allowed(event)
 def _route_allowed(self,item):return bool(item.get('sender')) and item.get('umo') in self.allowed_umos and (not self.allowed_senders or item.get('sender') in self.allowed_senders)
 def _watch_visible(self,event,item):return self._allowed(event) and isinstance(item,dict) and item.get('umo') in self.allowed_umos
 def _login_allowed(self,event):return event.get_platform_name()=='qq_official' and str(event.unified_msg_origin) in self.login_umos and (self.cfg.get('shared_login_members',True) is True or str(event.get_sender_id()) in self.login_senders)
 def _login_status_allowed(self,event):return self._allowed(event) or self._login_allowed(event)
 def login_allowed(self,event):return self._login_allowed(event)
 def client(self):return XianyuClient(endpoint=self.endpoint,token_file=self.token_file)
 def _watch_lock(self,wid):return self._watch_locks.setdefault(wid,asyncio.Lock())
 def _semantic_ok(self,event,tool,call):return self._storage_ok and self._allowed(event) and (callable(call) and call(event,tool) or bool(getattr(event,'is_at_or_wake_command',False)))
 def _load(self):
  descriptor=None
  try:
   descriptor=os.open(self.file,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
   info=os.fstat(descriptor)
   if not stat.S_ISREG(info.st_mode) or info.st_uid not in {0,os.geteuid()} or info.st_size>8*1024*1024:raise ValueError('unsafe watch storage')
   raw=os.read(descriptor,8*1024*1024+1)
   if len(raw)>8*1024*1024:raise ValueError('watch storage too large')
   data=json.loads(raw);rows=data.get('watches') if isinstance(data,dict) and data.get('version')==1 else None
   if not isinstance(rows,list) or len(rows)>200:raise ValueError('invalid watch storage')
   records={}
   for x in rows:
    if not isinstance(x,dict) or not re.fullmatch('[a-f0-9]{16}',str(x.get('id',''))) or x.get('id') in records or x.get('state') not in STATES:raise ValueError('invalid watch')
    expected=hashlib.sha256((str(x.get('umo'))+'\0'+str(x.get('sender'))).encode()).hexdigest()
    if not x.get('sender') or x.get('owner')!=expected or normalize_keywords(x.get('keywords'))!=x['keywords']:raise ValueError('invalid watch owner')
    if type(x.get('interval_minutes')) is not int or not 5<=x['interval_minutes']<=10080 or type(x.get('max_results')) is not int or not 1<=x['max_results']<=20:raise ValueError('invalid watch parameters')
    if not isinstance(x.get('seen_ids'),dict) or len(x['seen_ids'])>10000 or not isinstance(x.get('pending_alerts'),list) or len(x['pending_alerts'])>1000:raise ValueError('invalid watch outbox')
    for aid,stamp in x['seen_ids'].items():
     if not isinstance(aid,str) or not isinstance(stamp,(int,float)):raise ValueError('invalid seen id')
    aids=set()
    for alert in x['pending_alerts']:
     row=normalize_item(alert.get('item'));expected_aid=hashlib.sha256((x['id']+'\0'+row['item_id']).encode()).hexdigest()[:24]
     if alert.get('alert_id')!=expected_aid or expected_aid in aids:raise ValueError('invalid alert identity')
     alert['item']=row
     aids.add(expected_aid)
    for field in ('created_at','next_run_at','last_run','next_delivery_at'):
     value=x.get(field)
     if value is not None and (type(value) not in {int,float} or not math.isfinite(value) or value<0):raise ValueError('invalid watch time')
    if type(x.get('generation')) is not int or type(x.get('baseline_ready')) is not bool:raise ValueError('invalid watch state')
    x['max_price']=normalize_max_price(x.get('max_price'))
    if 'baseline_ids' not in x:
     x.update(baseline_ids=list(x['seen_ids']) if x['baseline_ready'] else [],baseline_snapshots={},baseline_at=None,baseline_legacy=x['baseline_ready'])
    ids=x.get('baseline_ids');snapshots=x.get('baseline_snapshots')
    if not isinstance(ids,list) or len(ids)>10000 or len(set(ids))!=len(ids) or any(not isinstance(i,str) or not re.fullmatch(r'[0-9]{1,32}',i) for i in ids):raise ValueError('invalid baseline ids')
    if not isinstance(snapshots,dict) or not set(snapshots)<=set(ids) or type(x.get('baseline_legacy')) is not bool:raise ValueError('invalid baseline details')
    for iid,detail in list(snapshots.items()):
     if not isinstance(detail,dict) or detail.get('item_id')!=iid or detail.get('source') not in {'initial_query','recovered_current_query'}:raise ValueError('invalid baseline snapshot')
     stamp=detail.get('observed_at')
     if type(stamp) not in {int,float} or not math.isfinite(stamp) or stamp<0:raise ValueError('invalid baseline timestamp')
     if detail['source']=='recovered_current_query' and detail.get('recovered_at')!=stamp:raise ValueError('invalid recovery timestamp')
     snapshots[iid]=baseline_snapshot(detail,stamp,recovered=detail['source']=='recovered_current_query')
    for key in ('baseline_at','metadata_refreshed_at'):
     value=x.get(key)
     if value is not None and (type(value) not in {int,float} or not math.isfinite(value) or value<0):raise ValueError('invalid baseline time')
    if x.get('last_query') is not None and not isinstance(x['last_query'],dict):raise ValueError('invalid query coverage')
    records[x['id']]=x
   self.records=records
  except FileNotFoundError:pass
  except Exception as error:
   self._storage_ok=False;self.logger.error('Xianyu watch state unavailable; monitoring disabled type=%s',type(error).__name__)
  finally:
   if descriptor is not None:os.close(descriptor)
 def _save(self):
  if not self._storage_ok:raise RuntimeError('watch storage unavailable')
  tmp=None
  try:
   self.file.parent.mkdir(parents=True,exist_ok=True)
   if self.file.is_symlink():raise ValueError('unsafe watch storage')
   raw=json.dumps({'version':1,'watches':list(self.records.values())},ensure_ascii=False).encode()
   if len(raw)>8*1024*1024:raise ValueError('watch storage too large')
   fd,tmp=tempfile.mkstemp(prefix='.watches.',dir=self.file.parent);os.fchmod(fd,0o600)
   with os.fdopen(fd,'wb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
   os.replace(tmp,self.file);tmp=None
   dfd=os.open(self.file.parent,os.O_RDONLY|os.O_DIRECTORY)
   try:os.fsync(dfd)
   finally:os.close(dfd)
  except Exception:
   self._storage_ok=False;raise
  finally:
   if tmp is not None:
    try:os.unlink(tmp)
    except OSError:pass
 def public(self,item):return compact_record(item)
 def tools(self):
  return [{'name':TOOL_NAME,'description':'创建当前会话的闲鱼新商品监控（最多5个关键词）。预算需传max_price，价格严格小于；默认30分钟。首次保存全部商品快照，后续只提醒新ID中符合条件的商品，默认不发图。','parameters':TOOL_SCHEMA,'handler':self._create},
   {'name':LIST_NAME,'description':'列出所有仍授权共享会话中的闲鱼监控、价格条件、基线总数/已知符合数、下次执行和错误状态。','parameters':{'type':'object','properties':{},'additionalProperties':False},'handler':self._list},
   {'name':READ_NAME,'description':'分页读取授权共享监控的状态和完整首次商品基线（名称、标价、链接、观察时间）。旧ID详情缺失明确返回不可用；显式refresh_metadata只按当前查询补同ID，标恢复时刻且不发通知。','parameters':READ_SCHEMA,'handler':self._read},
   {'name':MANAGE_NAME,'description':'按用户要求暂停、恢复、取消或update授权共享闲鱼监控。update可改max_price、interval_minutes、max_results；不清空已见ID或原基线，不改变原提醒会话。','parameters':MANAGE_SCHEMA,'handler':self._manage}]
 async def _search(self,keyword,limit):return await asyncio.to_thread(self.client().search,keyword,limit)
 async def _create(self,event,keywords,interval_minutes=30,max_results=10,max_price=None,_codex_call=None):
  if not self._semantic_ok(event,TOOL_NAME,_codex_call):return {'ok':False,'code':'storage_unavailable' if not self._storage_ok else 'not_authorized'}
  try:
   keywords=normalize_keywords(keywords);max_price=normalize_max_price(max_price)
   if type(interval_minutes) is not int or not 5<=interval_minutes<=10080:raise ValueError('检查间隔需为5至10080分钟。')
   if type(max_results) is not int or not 1<=max_results<=20:raise ValueError('结果数量需为1至20。')
  except ValueError as error:return {'ok':False,'code':'invalid_watch','error':str(error)}
  identity=[owner(event),keywords,interval_minutes,max_results]
  if max_price is not None:identity.append(max_price)
  wid=hashlib.sha256(json.dumps(identity,ensure_ascii=False,sort_keys=True).encode()).hexdigest()[:16]
  async with self._lock:
   existing=self.records.get(wid)
   if existing and existing['state']!='cancelled':return {'ok':True,'created':False,'watch':self.public(existing)}
   active=[x for x in self.records.values() if x['state']!='cancelled']
   if len(self.records)>=200 or len(active)>=100 or sum(x['owner']==owner(event) for x in active)>=5:return {'ok':False,'code':'watch_quota','error':'闲鱼监控已达上限，请先取消不需要的监控。'}
   now=time.time();item={'id':wid,'owner':owner(event),'umo':ident(event)[0],'sender':ident(event)[1],'keywords':keywords,'interval_minutes':interval_minutes,'max_results':max_results,'max_price':max_price,'state':'awaiting_login','baseline_ready':False,'baseline_ids':[],'baseline_snapshots':{},'baseline_at':None,'baseline_legacy':False,'generation':int((existing or {}).get('generation',0))+1,'created_at':now,'next_run_at':now,'seen_ids':{},'pending_alerts':[],'login_notice_sent':False,'next_delivery_at':0,'last_error':None}
   self.records[wid]=item
   try:self._save()
   except Exception:return {'ok':False,'code':'storage_unavailable','error':'监控草案无法持久化，未确认创建。'}
  await self._run(wid)
  current=self.records[wid]
  return {'ok':True,'created':True,'watch':self.public(current),'baseline_count':len(current['baseline_ids']),'notice':'已保存首次扫描全部商品的基线；符合价格条件数另列，之后只提醒首次发现且符合条件的新商品。' if current['baseline_ready'] else '监控需求已保存，等待登录或首次查询成功后建立基线；不会把旧商品误报为新商品。'}
 async def _list(self,event,_codex_call=None):
  if not self._semantic_ok(event,LIST_NAME,_codex_call):return {'ok':False,'code':'storage_unavailable' if not self._storage_ok else 'not_authorized'}
  async with self._lock:return {'ok':True,'watches':[self.public(x) for x in self.records.values() if self._watch_visible(event,x)]}
 async def _read(self,event,watch_id,offset=0,limit=20,refresh_metadata=False,_codex_call=None):
  if not self._semantic_ok(event,READ_NAME,_codex_call):return {'ok':False,'code':'storage_unavailable' if not self._storage_ok else 'not_authorized'}
  if not isinstance(watch_id,str) or not re.fullmatch(r'[a-f0-9]{16}',watch_id):return {'ok':False,'code':'invalid_arguments'}
  if type(offset) is not int or not 0<=offset<=10000 or type(limit) is not int or not 1<=limit<=50 or type(refresh_metadata) is not bool:return {'ok':False,'code':'invalid_arguments'}
  refresh=None
  async with self._watch_lock(str(watch_id)):
   async with self._lock:
    item=self.records.get(str(watch_id))
    if not self._watch_visible(event,item):return {'ok':False,'code':'not_found'}
    snapshot=copy.deepcopy(item);generation=item['generation']
   if refresh_metadata:
    try:
     fetched=await self._fetch(snapshot);rows=fetched['items'] if isinstance(fetched,dict) else fetched
    except Exception as error:return {'ok':False,'code':getattr(error,'code','search_failed'),'error':'当前详情补回查询失败；原基线和已见记录保留。'}
    async with self._lock:
     item=self.records.get(str(watch_id))
     if not self._watch_visible(event,item) or item['generation']!=generation:return {'ok':False,'code':'watch_changed'}
     now=time.time();count=0;known=set(item['baseline_ids'])
     for row in rows:
      if row['item_id'] in known and row['item_id'] not in item['baseline_snapshots']:
       item['baseline_snapshots'][row['item_id']]=baseline_snapshot(row,now,recovered=True);count+=1
     item['metadata_refreshed_at']=now
     try:self._save()
     except Exception:return {'ok':False,'code':'storage_unavailable'}
     refresh={'matched_count':count,'remaining_unavailable_count':len(known)-len(item['baseline_snapshots']),'recovered_at':now,'notice':'仅补回原ID缺失详情，时间为当前恢复时间；不代表首次扫描价格，不改变下次执行或发送提醒。'}
   async with self._lock:
    item=self.records[str(watch_id)]
    return {'ok':True,'watch':self.public(item),'baseline':baseline_view(item,offset,limit),**({'metadata_refresh':refresh} if refresh is not None else {})}
 async def _manage(self,event,watch_id,action,max_price=_UNSET,interval_minutes=None,max_results=None,_codex_call=None):
  if not self._semantic_ok(event,MANAGE_NAME,_codex_call):return {'ok':False,'code':'not_authorized'}
  if not isinstance(watch_id,str) or not re.fullmatch(r'[a-f0-9]{16}',watch_id):return {'ok':False,'code':'invalid_arguments'}
  if action not in {'pause','resume','cancel','update'}:return {'ok':False,'code':'invalid_action'}
  changes={}
  try:
   if max_price is not _UNSET:changes['max_price']=normalize_max_price(max_price)
   if interval_minutes is not None:
    if type(interval_minutes) is not int or not 5<=interval_minutes<=10080:raise ValueError('invalid interval')
    changes['interval_minutes']=interval_minutes
   if max_results is not None:
    if type(max_results) is not int or not 1<=max_results<=20:raise ValueError('invalid result limit')
    changes['max_results']=max_results
   if action=='update' and not changes or action!='update' and changes:raise ValueError('update requires explicit conditions')
  except ValueError as error:return {'ok':False,'code':'invalid_watch','error':str(error)}
  async with self._watch_lock(str(watch_id)):
   async with self._lock:
    item=self.records.get(str(watch_id))
    if not self._watch_visible(event,item):return {'ok':False,'code':'not_found'}
    if item['state']=='cancelled':return {'ok':False,'code':'watch_cancelled'}
    if action=='update':
     budget=changes.get('max_price',item.get('max_price'));discarded={a['item']['item_id'] for a in item['pending_alerts'] if not matches_price(a['item'],budget)}
     if len(set(item['seen_ids'])|discarded)>10000:return {'ok':False,'code':'watch_capacity'}
    item['generation']+=1
    if action=='cancel':item.update(state='cancelled',next_run_at=None,pending_alerts=[])
    elif action=='pause':item.update(state='paused',next_run_at=None)
    elif action=='resume':item.update(state='active' if item['baseline_ready'] else 'awaiting_login',next_run_at=time.time(),next_delivery_at=0,login_notice_sent=False)
    else:
     item.update(changes)
     if 'interval_minutes' in changes and item.get('next_run_at') is not None:item['next_run_at']=max(time.time(),float(item.get('last_run') or time.time())+item['interval_minutes']*60)
     # Reject queued alerts under the new condition, while retaining their IDs
     # as previously observed. A later price drop cannot turn them into new stock.
     kept=[]
     for alert in item['pending_alerts']:
      if matches_price(alert['item'],item.get('max_price')):kept.append(alert)
      else:item['seen_ids'].setdefault(alert['item']['item_id'],alert.get('created_at',time.time()))
     item['pending_alerts']=kept
    try:self._save()
    except Exception:return {'ok':False,'code':'storage_unavailable','error':'状态无法保存，监控已停止，请检查服务器存储。'}
    return {'ok':True,'watch':self.public(item)}

 async def _fetch(self,item):
  status=await asyncio.to_thread(self.client().status)
  if status.get('logged_in') is not True:raise XianyuError('必须登录。','login_required' if not status.get('error') or status.get('error')=='login_required' else str(status['error']))
  rows=[];coverage=[]
  for keyword in item['keywords']:
   result=await self._search(keyword,item['max_results'])
   if not isinstance(result,dict) or result.get('ok') is not True:raise XianyuError('搜索未成功。',str((result or {}).get('error','search_failed')))
   if not isinstance(result.get('results'),list):raise XianyuError('搜索响应无效。','response_invalid')
   rows.extend(normalize_item(row) for row in result['results'])
   coverage.append({'keyword':keyword,'returned_count':len(result['results']),'limit':item['max_results'],'page':result.get('page',1),'rows_per_page':result.get('rows_per_page',30),'has_more':result.get('has_more') if type(result.get('has_more')) is bool else None,'sort':safe(result.get('sort','newest'),40),'coverage':safe(result.get('coverage','仅本次返回范围，不保证全站覆盖。'),500)})
  return {'items':list({row['item_id']:row for row in rows}.values()),'coverage':coverage}

 async def _run(self,watch):
  wid=watch['id'] if isinstance(watch,dict) else str(watch)
  async with self._lock:
   item=self.records.get(wid)
   if not self._storage_ok or not item or item['state'] not in {'active','awaiting_login'} or not self._route_allowed(item):return
   snapshot=copy.deepcopy(item);generation=item['generation']
  try:
   fetched=await self._fetch(snapshot);rows=fetched['items'] if isinstance(fetched,dict) else fetched
   coverage=fetched.get('coverage',[]) if isinstance(fetched,dict) else []
  except asyncio.CancelledError:raise
  except Exception as error:
   code=getattr(error,'code','search_failed');now=time.time()
   async with self._watch_lock(wid):
    async with self._lock:
     item=self.records.get(wid)
     if not self._storage_ok or not item or item['generation']!=generation or item['state'] not in {'active','awaiting_login'} or not self._route_allowed(item):return
     if code in {'login_required','login_pending','login_expired','session_expired','not_logged_in'}:item['state']='awaiting_login'
     item.update(last_error=code,next_run_at=now+max(30,min(item['interval_minutes']*60,300)))
     try:self._save()
     except Exception:return
    if code in {'login_required','login_pending','login_expired','session_expired','not_logged_in'} and snapshot['baseline_ready'] and not item.get('login_notice_sent'):
     try:
      sent=await asyncio.wait_for(self.context.send_message(item['umo'],MessageChain([Plain('闲鱼登录已失效，监控正在等待重新登录。请在已授权群聊或私聊执行 /闲鱼 login 扫码，再 /闲鱼 status 确认；原监控会自动继续。')])),timeout=60)
      if sent is not False:
       async with self._lock:item['login_notice_sent']=True;self._save()
     except Exception:pass
   return
  async with self._watch_lock(wid):
   async with self._lock:
    item=self.records.get(wid)
    if not self._storage_ok or not item or item['generation']!=generation or item['state'] not in {'active','awaiting_login'} or not self._route_allowed(item):return
    now=time.time()
    try:
     if len(set(item['seen_ids'])|{row['item_id'] for row in rows})>10000:raise ValueError('seen capacity')
     matching=[row for row in rows if matches_price(row,item.get('max_price'))]
     if not item['baseline_ready']:
      item.update(seen_ids={row['item_id']:now for row in rows},baseline_ready=True,baseline_ids=[row['item_id'] for row in rows],baseline_snapshots={row['item_id']:baseline_snapshot(row,now) for row in rows},baseline_at=now,baseline_legacy=False)
     else:
      merge_pending(item,matching,now)
      for row in rows:
       if not matches_price(row,item.get('max_price')):item['seen_ids'].setdefault(row['item_id'],now)
     item['last_query']={'observed_at':now,'returned_count':len(rows),'matching_count':len(matching),'price_excluded_count':len(rows)-len(matching),'coverage':coverage,'notice':'首见商品按ID去重；旧商品后续降价或补价不会视为新上架。'}
     item.update(state='active',last_run=now,last_error=None,next_run_at=now+item['interval_minutes']*60,login_notice_sent=False)
     self._save()
    except Exception:
     if self._storage_ok:
      item.update(state='paused',last_error='watch_capacity',next_run_at=None);self._save()
     return
   await self._flush_locked(wid)
 async def _flush_locked(self,wid):
  # Caller holds the watch lock throughout each bounded QQ send.
  while True:
   async with self._lock:
    item=self.records.get(wid)
    if not self._storage_ok or not item or item['state']!='active' or not self._route_allowed(item) or time.time()<item.get('next_delivery_at',0) or not item['pending_alerts']:return
    if len(item['seen_ids'])>=10000:
     item.update(state='paused',last_error='watch_capacity',next_run_at=None)
     try:self._save()
     except Exception:pass
     return
    alert=copy.deepcopy(item['pending_alerts'][0]);row=alert['item'];target=item['umo'];aid=alert['alert_id']
    if not matches_price(row,item.get('max_price')):
     item['pending_alerts'].pop(0);item['seen_ids'].setdefault(row['item_id'],alert.get('created_at',time.time()))
     try:self._save()
     except Exception:return
     continue
    item['pending_alerts'][0]['attempts']=int(alert.get('attempts',0))+1
    try:self._save()
    except Exception:return
   image=None
   if self.cfg.get('send_product_images',False) is True and row.get('image_url'):
    try:image=await asyncio.to_thread(self.client().download_image,row['image_url'])
    except Exception:pass
   parts=[Plain('闲鱼发现新商品（首次发现）\n'+safe(row['title'])+'\n价格：'+safe(row['price'],120)+'\n'+row['url']+'\n提醒编号：'+aid)]
   if isinstance(image,bytes) and 0<len(image)<=2*1024*1024:parts.append(Image.fromBytes(image))
   elif self.cfg.get('send_product_images',False) is True:parts.append(Plain('\n商品图片暂不可用。'))
   try:
    sent=await asyncio.wait_for(self.context.send_message(target,MessageChain(parts)),timeout=60)
    if sent is False:raise RuntimeError('delivery_rejected')
   except asyncio.CancelledError:raise
   except Exception:
    async with self._lock:
     item=self.records.get(wid)
     if item:
      item['last_error']='delivery_failed';item['next_delivery_at']=time.time()+min(300,30*max(1,int(alert.get('attempts',0))+1))
      for current in item['pending_alerts']:
       if current['alert_id']==aid:current['last_error']='delivery_failed'
      try:self._save()
      except Exception:pass
    return
   async with self._lock:
    item=self.records.get(wid)
    if not item:return
    try:mark_alert_delivered(item,aid,time.time());item['last_error']=None;item['next_delivery_at']=0;self._save()
    except ValueError:
     item.update(state='paused',last_error='watch_capacity',next_run_at=None)
     try:self._save()
     except Exception:pass
     return
    except Exception:return
   return
 async def _tick(self):
  if not self._storage_ok:return
  async with self._tick_lock:
   for wid in list(self.records):
    item=self.records.get(wid)
    if not item or not self._route_allowed(item):continue
    if item['state']=='active' and item.get('pending_alerts') and time.time()>=item.get('next_delivery_at',0):
     async with self._watch_lock(wid):await self._flush_locked(wid)
    item=self.records.get(wid)
    if item and item['state'] in {'active','awaiting_login'} and item.get('next_run_at') is not None and time.time()>=item['next_run_at']:await self._run(wid)
 async def _scheduler(self):
  while True:
   try:await self._tick()
   except asyncio.CancelledError:raise
   except Exception as error:self.logger.error('Xianyu monitor tick failed type=%s',type(error).__name__)
   await asyncio.sleep(30)
 async def initialize(self):
  if self._storage_ok:self._task=asyncio.create_task(self._scheduler())
 async def terminate(self):
  if self._task:self._task.cancel();await asyncio.gather(self._task,return_exceptions=True)
 def _remember_login_context(self,event):
  now=time.monotonic()
  self._recent_login_scopes={scope:until for scope,until in self._recent_login_scopes.items() if until>now}
  self._recent_login_scopes[str(event.unified_msg_origin)]=now+600
 def _has_login_context(self,event):
  if self._pending_login.get(ident(event),0)>time.monotonic() or max(self._pending_login_scopes.get(str(event.unified_msg_origin),0),self._recent_login_scopes.get(str(event.unified_msg_origin),0))>time.monotonic():return True
  for part in event.get_messages():
   if isinstance(part,Reply):
    quote=' '.join(str(getattr(part,k,'') or '') for k in ('message_str','text'))+' '+' '.join(p.text for p in (getattr(part,'chain',None) or []) if isinstance(p,Plain))
    if '闲鱼' in quote and re.search(r'扫一扫|扫码|二维码|登录|浏览器授权|验证账号状态|App\s*确认',quote,re.I):return True
  return False
 def _status_request_allowed(self,event):return self._login_status_allowed(event) and (private(event) or bool(getattr(event,'is_at_or_wake_command',False)))
 async def _search_reply(self,event,query,limit=10):
  if not self._allowed(event) or not (private(event) or getattr(event,'is_at_or_wake_command',False)):return {'ok':False,'code':'not_authorized'}
  if not isinstance(query,str) or not 1<=len(query.strip())<=100 or type(limit) is not int or not 1<=limit<=20:return {'ok':False,'code':'invalid_query'}
  try:result=await asyncio.to_thread(self.client().search,query.strip(),limit)
  except XianyuError as error:return {'ok':False,'code':error.code,'error':'闲鱼查询未成功，请根据真实登录状态或工具错误处理；未取得商品数据。'}
  except Exception:return {'ok':False,'code':'search_unavailable','error':'闲鱼查询暂时失败，未取得商品数据。'}
  if not isinstance(result,dict) or result.get('ok') is not True:return {'ok':False,'code':(result or {}).get('error','search_unavailable') if isinstance(result,dict) else 'search_unavailable','error':'闲鱼查询未成功，不能把失败当作没有商品。'}
  rows=result.get('results')
  if not isinstance(rows,list):return {'ok':False,'code':'response_invalid'}
  if self.cfg.get('send_product_images',False) is not True:
   return {**result,'results':[{key:value for key,value in row.items() if key!='image_url'} for row in rows],
    'ok':True,'query':query.strip(),'image_count':0,
    'notice':'仅返回商品名称、标价和链接；只代表本次查询范围，运费、定金及最终到手价尚需核实。'}
  image_count=0
  for row in rows[:3]:
   if not row.get('image_url'):continue
   try:image=await asyncio.to_thread(self.client().download_image,row['image_url'])
   except Exception:image=None
   if isinstance(image,bytes) and 0<len(image)<=2*1024*1024:
    sent=await event.send(MessageChain([Plain(safe(row.get('title',''))+'\n价格：'+safe(row.get('price',''))+'\n'+str(row.get('url',''))),Image.fromBytes(image)]))
    if sent is not False:image_count+=1
  return {**result,'ok':True,'query':query.strip(),'image_count':image_count,'notice':'只代表闲鱼本次返回范围；价格来自实际标价，运费、定金、缺件和最终到手价尚需核实。不能声称全站最低价。'}
 async def semantic_search(self,event,query,limit=10,_codex_call=None):return await self._search_reply(event,query,limit)
 def _login_request_allowed(self,event):
  return self._login_allowed(event) and (private(event) or bool(getattr(event,'is_at_or_wake_command',False)))
 def _claim(self,event):
  event.stop_event()
  setter=getattr(event,'should_call_llm',None)
  if callable(setter):setter(False)
 async def _wake_waiting_watches(self):
  if not self._storage_ok:return
  try:
   async with self._lock:
    for item in self.records.values():
     if item['state']=='awaiting_login':item['next_run_at']=time.time()
    self._save()
  except Exception as error:self.logger.warning('Xianyu login confirmed but monitor storage update failed type=%s',type(error).__name__)
 async def _login_reply(self,event):
  if not self._login_request_allowed(event):return {'ok':False,'code':'not_authorized'}
  self._remember_login_context(event)
  try:
   client=self.client();reader=getattr(client,'qrcode_details',None)
   if callable(reader):details=await asyncio.to_thread(reader)
   else:details={'image':await asyncio.to_thread(client.qrcode),'expires_seconds':240,'expiry_source':'local_upper_bound'}
   image=details['image'];seconds=details.get('expires_seconds',240)
   if type(seconds) is not int or not 0<seconds<=240:seconds=240
   if not isinstance(image,bytes) or not image:raise XianyuError('二维码无效。','qr_invalid')
   self._pending_login[ident(event)]=time.monotonic()+seconds
   self._pending_login_scopes[str(event.unified_msg_origin)]=time.monotonic()+seconds
   expiry=(f'本次二维码剩余约{seconds}秒。' if details.get('expiry_source')=='platform'
    else f'本次扫码会话最多保留{seconds}秒；二维码可能被闲鱼提前判定过期。')
   await event.send(MessageChain([Plain('请打开闲鱼 App，点击右上角“扫一扫”并确认登录。'+expiry+'过期后再说“闲鱼登录”获取新码。登录态没有固定保证的有效天数，会持续校验。'),Image.fromBytes(image)]))
   return {'ok':True,'state':'waiting_for_scan','expires_seconds':seconds,'expiry_source':details.get('expiry_source','local_upper_bound'),'login_expiry_fixed':False}
  except XianyuError as error:
   if error.code=='already_logged_in':
    await event.send(MessageChain([Plain('闲鱼已登录，无需重新扫码。登录态没有固定保证的有效天数，监控会持续检查。')]))
    await self._wake_waiting_watches();return {'ok':True,'state':'logged_in','login_expiry_fixed':False}
   message=('原二维码已过期，请再说“闲鱼登录”获取新码。' if error.code in {'qr_expired','login_expired'} else '闲鱼二维码暂时生成失败，请稍后重试。')
   await event.send(MessageChain([Plain(message)]));return {'ok':False,'code':error.code}
  except Exception as error:
   self.logger.warning('Xianyu QR request failed type=%s',type(error).__name__)
   await event.send(MessageChain([Plain('闲鱼二维码暂时生成失败，请稍后重试。')]))
   return {'ok':False,'code':'qr_unavailable'}
 async def semantic_login(self,event,_codex_call=None):
  trusted=callable(_codex_call) and _codex_call(event,'xianyu_login')
  if not self._login_request_allowed(event) or not trusted and login_action(current_text(event))!='login':return {'ok':False,'code':'intent_required'}
  return await self._login_reply(event)
 async def semantic_status(self,event,_codex_call=None):
  if not self._status_request_allowed(event):return {'ok':False,'code':'not_authorized'}
  self._remember_login_context(event)
  try:
   result=await asyncio.to_thread(self.client().status)
   if result.get('logged_in') is True:
    self._pending_login.pop(ident(event),None);await self._wake_waiting_watches()
   elif result.get('state') in {'expired','not_logged_in'}:
    self._pending_login.pop(ident(event),None);self._pending_login_scopes.pop(str(event.unified_msg_origin),None)
   return {'ok':result.get('state')!='check_failed',**result,'qr_local_max_seconds':240,'login_expiry_fixed':False}
  except Exception:return {'ok':False,'state':'check_failed','error':'status_unavailable','login_expiry_fixed':False}
 async def _status_reply(self,event):
  result=await self.semantic_status(event)
  if result.get('code')=='not_authorized':return result
  seconds=result.get('remaining_seconds');left=f'本次扫码会话最多还剩{seconds}秒。' if type(seconds) is int else ''
  if result.get('logged_in') is True:message='闲鱼已登录。已保存的监控会自动继续；登录态没有固定保证的有效天数，会实时校验。'
  elif result.get('state')=='authorization_incomplete' or result.get('error')=='login_callback_incomplete':message='闲鱼已收到扫码确认，但浏览器授权回传尚未完成，当前还不能查询商品。请稍后再检查；这不是已登录后自动退出。'
  elif result.get('state')=='expired' or result.get('error') in {'qr_expired','login_expired'}:message='二维码已过期，请说“闲鱼登录”获取新码；上一次二维码不能继续使用。'
  elif result.get('state')=='confirming_login':message='已收到闲鱼 App 确认，正在完成浏览器授权并验证账号状态。'+left
  elif result.get('login_pending') is True:message=('已扫码，等待闲鱼 App 内确认。' if result.get('state')=='waiting_for_confirmation' else '仍在等待扫码或 App 确认。')+left+'平台可能更早使二维码过期，无需反复刷新。'
  elif result.get('state')=='check_failed' or result.get('ok') is False:message='闲鱼登录状态暂时无法检查，这不代表登录已失效。'
  else:message='闲鱼目前未登录。请说“闲鱼登录”获取二维码；本次扫码会话最多4分钟，登录态没有固定保证的有效天数。'
  await event.send(MessageChain([Plain(message)]));return result
 @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE | filter.EventMessageType.PRIVATE_MESSAGE,priority=150)
 async def natural_login(self,event:AstrMessageEvent):
  if not (self._allowed(event) or self._login_allowed(event)) or not (private(event) or getattr(event,'is_at_or_wake_command',False)):return
  action=login_action(current_text(event),pending=self._has_login_context(event))
  if not action:return
  self._claim(event)
  if not (self._login_allowed(event) if action=='login' else self._login_status_allowed(event)):
   await event.send(MessageChain([Plain('当前会话或操作人没有闲鱼登录权限，请使用已授权的操作人和群聊或私聊。')]))
   return
  if action=='login':return await self._login_reply(event)
  return await self._status_reply(event)
 @filter.command('闲鱼',alias={'咸鱼','xianyu'},priority=150)
 async def command(self,event:AstrMessageEvent,body:GreedyStr):
  self._claim(event);raw=str(body or '').strip();cmd,_,arg=raw.partition(' ');cmd=cmd.lower()
  if cmd in {'login','登录','扫码','status','状态'} and not (self._login_status_allowed(event) if cmd in {'status','状态'} else self._login_allowed(event)):
   if self._allowed(event):await event.send(MessageChain([Plain('当前会话或操作人没有闲鱼登录及状态查询权限。')]))
   return {'ok':False,'code':'not_authorized'}
  if cmd in {'login','登录','扫码'}:
   return await self._login_reply(event)
  if cmd in {'status','状态'}:
   return await self._status_reply(event)
  if self._allowed(event):
   if cmd in {'list','列表'}:result=await self._list(event)
   elif cmd in {'read','详情'}:result=await self._read(event,arg.strip())
   elif cmd in {'cancel','取消','pause','暂停','resume','恢复'}:result=await self._manage(event,arg.strip(),{'取消':'cancel','暂停':'pause','恢复':'resume'}.get(cmd,cmd))
   else:result={'使用方法':'直接说购买目标并要求闲鱼监控，默认每30分钟；/闲鱼 list；/闲鱼 read 编号；/闲鱼 pause|resume|cancel 编号。预算修改请给出监控编号和严格价格上限。登录在已授权群聊或私聊说“闲鱼登录”或 /闲鱼 login；查询说“闲鱼登录状态”或 /闲鱼 status。'}
   await event.send(MessageChain([Plain(json.dumps(result,ensure_ascii=False))]))
 @filter.on_llm_request(priority=105)
 async def provide_watch_tools(self,event,req):
  if not (self._allowed(event) or self._login_allowed(event)) or not (private(event) or getattr(event,'is_at_or_wake_command',False)):return
  try:
   from astrbot.core.agent.tool import FunctionTool,ToolSet
   req.func_tool=ToolSet(list(req.func_tool.tools) if req.func_tool else [])
   if self._allowed(event):
    for tool in self.tools():req.func_tool.add_tool(FunctionTool(**tool))
    req.func_tool.add_tool(FunctionTool(name=SEARCH_NAME,description='只读即时搜索闲鱼商品，返回真实名称、标价和链接，默认不发送商品图片。必须登录；失败不等于无商品。价格仅为本次返回范围，便宜需求请比较实际标价并标明运费或定金是否核实。',parameters=SEARCH_SCHEMA,handler=self.semantic_search))
   if self._login_allowed(event):
    req.func_tool.add_tool(FunctionTool(name='xianyu_login',description='在已授权群聊或私聊中按用户请求获取闲鱼扫码登录二维码，支持闲鱼/咸鱼。最多等待4分钟，平台可更早过期；二维码由原生图片回传，不接收密码或Cookie。',parameters={'type':'object','properties':{},'additionalProperties':False},handler=self.semantic_login))
   if self._login_status_allowed(event):
    req.func_tool.add_tool(FunctionTool(name='xianyu_login_status',description='只读检查闲鱼真实登录状态、扫码等待或过期状态；登录态没有固定保证的有效天数。',parameters={'type':'object','properties':{},'additionalProperties':False},handler=self.semantic_status))
   req.system_prompt=(getattr(req,'system_prompt','') or '')+'\n'+ROUTING+'即时闲鱼商品查询使用xianyu_search；群内其他成员也可只读检查共享登录状态。用户说闲鱼登录或咸鱼登录时，可用xianyu_login取得真实二维码；已扫码或问状态时用xianyu_login_status，不能据旧聊天推测登录成功。'
  except Exception as error:self.logger.warning('Xianyu tool injection failed type=%s',type(error).__name__)
