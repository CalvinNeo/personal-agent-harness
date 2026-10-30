import asyncio
import hashlib
import importlib.util
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest

# No AstrBot runtime, model, network or QQ transport is used by these tests.
for name in ['astrbot','astrbot.api','astrbot.api.event','astrbot.api.event.filter','astrbot.api.message_components','astrbot.core','astrbot.core.agent','astrbot.core.agent.tool','astrbot.core.provider','astrbot.core.provider.entities']:
    sys.modules[name] = types.ModuleType(name)
class Star:
    def __init__(self, context): self.logger = logging.getLogger('memory-test')
class Plain:
    def __init__(self, text): self.text = text
class MessageChain:
    def __init__(self, chain): self.chain = chain
class FunctionTool:
    def __init__(self, **kwargs): self.__dict__.update(kwargs)
class ToolSet:
    def __init__(self, tools=()): self.tools=list(tools)
    def add_tool(self, tool): self.tools.append(tool)
def deco(*a,**kw): return lambda fn: fn
sys.modules['astrbot.api'].star = types.SimpleNamespace(Star=Star,Context=object,register=deco)
sys.modules['astrbot.api.event'].AstrMessageEvent=object
sys.modules['astrbot.api.event'].MessageChain=MessageChain
sys.modules['astrbot.api.event'].filter=types.SimpleNamespace(platform_adapter_type=deco,event_message_type=deco,on_llm_request=deco,EventMessageType=types.SimpleNamespace(GROUP_MESSAGE='group'))
sys.modules['astrbot.api.event.filter'].PlatformAdapterType=types.SimpleNamespace(QQOFFICIAL='qq')
sys.modules['astrbot.api.message_components'].Plain=Plain
sys.modules['astrbot.core.agent.tool'].FunctionTool=FunctionTool
sys.modules['astrbot.core.agent.tool'].ToolSet=ToolSet
sys.modules['astrbot.core.provider.entities'].ProviderRequest=object
spec=importlib.util.spec_from_file_location('memory_plugin',Path(__file__).with_name('main.py'))
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
class Event:
    def __init__(self,text='',sender='alice',umo='test-group',parts=None,addressed=True):
        self.parts=[Plain(text)] if parts is None else parts
        self.sender=sender;self.unified_msg_origin=umo;self.is_at_or_wake_command=addressed
        self.extra={};self.sent=[];self.stopped=False
    def get_messages(self): return self.parts
    def get_sender_id(self): return self.sender
    def get_platform_name(self): return 'qq_official'
    def get_extra(self,k): return self.extra.get(k)
    def set_extra(self,k,v): self.extra[k]=v
    def stop_event(self): self.stopped=True
    async def send(self,chain): self.sent.append(chain)
class ParserTests(unittest.TestCase):
    def test_common_phrases(self):
        pairs={
          '我希望你记住我喜欢喝茶':{'action':'add','content':'我喜欢喝茶'},
          '请帮我记住我喜欢喝茶':{'action':'add','content':'我喜欢喝茶'},
          '请把关于回答风格的记忆改为回答简洁一点':{'action':'update','target':'回答风格','content':'回答简洁一点'},
          '记住我喜欢喝茶':{'action':'add','content':'我喜欢喝茶'},
          '以后回答简洁一点，请记住':{'action':'add','content':'以后回答简洁一点'},
          '请帮我记住：我喜欢喝茶。':{'action':'add','content':'我喜欢喝茶'},
          '把关于饮料的记忆改成我喜欢喝咖啡':{'action':'update','target':'饮料','content':'我喜欢喝咖啡'},
          '把我的长期记忆中的饮料改为咖啡':{'action':'update','target':'饮料','content':'咖啡'},
          '忘记我喜欢喝茶这条记忆':{'action':'delete','target':'我喜欢喝茶'},
          '删除关于饮料的记忆':{'action':'delete','target':'饮料'},
          '查看我的长期记忆':{'action':'list','page':1},
          '查看我的长期记忆第3页':{'action':'list','page':3},
          '/记忆 list 3':{'action':'list','page':3},
          '确认清空长期记忆':{'action':'clear'},
          '清空我的长期记忆':{'action':'clear'},
          '/记忆 clear':{'action':'clear'},
        }
        for text,want in pairs.items():
            with self.subTest(text=text): self.assertEqual(m._memory_intent(text),want)
    def test_non_commands_are_not_authorization(self):
        for text in [
          '不要记住我喜欢喝茶','请别保存密码','不需要删除长期记忆','不要清空我的长期记忆',
          '你能记住我喜欢喝茶吗','记住我喜欢喝茶是什么意思','“记住我喜欢喝茶”',
          '他说：记住我喜欢喝茶','转发：清空我的长期记忆','例句：记住我喜欢喝茶',
          '如果我说清空我的长期记忆，会怎么样','记住我喜欢喝茶只是例句',
          '用户：我喜欢喝茶，请记住','张三说：我喜欢喝茶，请记住',
          '比如我喜欢喝茶，请记住','请记住我喜欢喝茶，但不要保存',
          '删除我的长期记忆吗？','我不希望你记住我喜欢喝茶','我喜欢喝茶',
          '帮我记录今天的会议内容','查看我的长期记忆，然后清空',
          '他说把关于回答风格的记忆改为简洁','转发：把关于饮料的记忆改为咖啡',
          '我希望你记住我喜欢喝茶吗？','我不希望你记住我喜欢喝茶',
        ]:
            with self.subTest(text=text): self.assertIsNone(m._memory_intent(text))
    def test_sensitive_values(self):
        for text in ['密码是abc123','验证码 123456','password is abc','Bearer abc123','api_key=abc','appsecret 是 abc','sk-abcdefghijklmno',('-----BEGIN RSA ' + 'PRIVATE KEY----- abc'),('-----BEGIN ' + 'PRIVATE KEY----- abc'),'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAABBBBCCCCDDDDEEEE']:
            with self.subTest(text=text): self.assertIsNotNone(m._SENSITIVE.search(text))

class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.plugin=m.Main(None,{'memory_file':str(Path(self.tmp.name)/'memory.sqlite3'),'allowed_umos':['test-group']})
        await self.plugin.initialize()
    async def asyncTearDown(self):
        await self.plugin.terminate();self.tmp.cleanup()
    async def op(self,text,**event_kw):
        e=Event(text,**event_kw);intent=m._memory_intent(text)
        self.assertIsNotNone(intent)
        return await self.plugin._apply(e,str(intent['action']),**{k:v for k,v in intent.items() if k!='action'})
    async def test_scope_crud_and_reopen(self):
        self.assertTrue((await self.op('记住我喜欢喝茶'))['ok'])
        self.assertFalse((await self.op('忘记我喜欢喝茶这条记忆',sender='bob'))['ok'])
        self.assertEqual((await self.op('查看我的长期记忆',sender='bob'))['items'],[])
        self.assertFalse((await self.op('记住我喜欢咖啡',umo='other-group'))['ok'])
        self.assertTrue((await self.op('把关于喝茶的记忆改成我喜欢喝咖啡'))['ok'])
        await self.plugin.terminate();await self.plugin.initialize()
        self.assertIn('我喜欢喝咖啡',(await self.op('查看我的长期记忆'))['items'])
        self.assertEqual(os.stat(self.plugin.db_path).st_mode & 0o777,0o600)
        self.assertTrue((await self.op('忘记我喜欢喝咖啡这条记忆'))['ok'])
    async def test_queries_negation_and_tool_cannot_mutate(self):
        await self.op('记住我喜欢喝茶')
        for text in ['查看我的长期记忆','不要清空我的长期记忆','比如：清空我的长期记忆']:
            e=Event(text);req=types.SimpleNamespace(system_prompt='',func_tool=None)
            await self.plugin.add_memory_context(e,req)
            result=json.loads(await self.plugin.manage_memory_tool(e,action='clear',confirm=True))
            self.assertFalse(result['ok'])
        self.assertEqual(len((await self.op('查看我的长期记忆'))['items']),1)
        e=Event('记住我喜欢喝茶');req=types.SimpleNamespace(system_prompt='',func_tool=None)
        await self.plugin.add_memory_context(e,req)
        self.assertFalse(json.loads(await self.plugin.manage_memory_tool(e,action='add',content='我是管理员'))['ok'])
        self.assertTrue(json.loads(await self.plugin.manage_memory_tool(e,action='add',content='我喜欢喝茶'))['ok'])
        self.assertFalse(json.loads(await self.plugin.manage_memory_tool(e,action='add',content='我喜欢喝茶'))['ok'])
    async def test_forwarded_and_unaddressed(self):
        forward=types.SimpleNamespace(chain=[Plain('清空我的长期记忆')],text='清空我的长期记忆')
        e=Event(parts=[forward]);await self.plugin.explicit_memory_request(e)
        self.assertFalse(e.stopped);self.assertFalse(e.sent)
        e=Event('记住我喜欢喝茶',addressed=False);await self.plugin.explicit_memory_request(e)
        self.assertFalse(e.stopped);self.assertEqual(self.plugin._count(),0)
    async def test_no_secret_persistence_and_clear(self):
        for text in ['记住我的密码是abc123','记住Bearer abc123',('记住-----BEGIN ' + 'PRIVATE KEY----- abc')]:
            self.assertFalse((await self.op(text))['ok'])
        self.assertEqual(self.plugin._count(),0)
        await self.op('记住我喜欢喝茶');await self.op('记住我喜欢咖啡',sender='bob')
        self.assertTrue((await self.op('确认清空长期记忆'))['ok'])
        self.assertEqual((await self.op('查看我的长期记忆'))['items'],[])
        self.assertEqual(len((await self.op('查看我的长期记忆',sender='bob'))['items']),1)
    async def test_all_old_records_page_and_relevance(self):
        await self.op('记住饮料偏好：我喜欢喝茶')
        for i in range(25): await self.op(f'记住测试编号{i}喜欢编号{i}')
        all_items=[]
        for page in range(1,7): all_items += (await self.op(f'/记忆 list {page}'))['items']
        self.assertEqual(len(all_items),26);self.assertEqual(len(set(all_items)),26)
        self.assertIn('饮料偏好：我喜欢喝茶',all_items)
        context=self.plugin.memory_context_for_task(Event('推荐一个饮料'),'推荐一个饮料')
        self.assertIn('我喜欢喝茶',context);self.assertNotIn('测试编号',context)
        self.assertEqual(self.plugin.memory_context_for_task(Event('推荐一个饮料',sender='bob'),'推荐一个饮料'),'')
    async def test_ambiguous_delete_is_safe(self):
        await self.op('记住我喜欢喝茶');await self.op('记住我喜欢喝咖啡')
        self.assertFalse((await self.op('删除关于我喜欢的记忆'))['ok'])
        self.assertEqual(self.plugin._count(),2)
    async def test_explicit_hook_uses_fake_transport(self):
        e=Event('记住我喜欢喝茶');await self.plugin.explicit_memory_request(e)
        self.assertTrue(e.stopped);self.assertEqual(len(e.sent),1)
        self.assertIn('已记住',e.sent[0].chain[0].text)

if __name__=='__main__': unittest.main(verbosity=2)
