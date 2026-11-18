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
    def __init__(self, chain, use_markdown_=None): self.chain = chain; self.use_markdown = use_markdown_
class FunctionTool:
    def __init__(self, **kwargs): self.__dict__.update(kwargs)
class ToolSet:
    def __init__(self, tools=()): self.tools=list(tools)
    def add_tool(self, tool): self.tools.append(tool)
def deco(*a,**kw): return lambda fn: fn
sys.modules['astrbot.api'].star = types.SimpleNamespace(Star=Star,Context=object,register=deco)
sys.modules['astrbot.api.event'].AstrMessageEvent=object
sys.modules['astrbot.api.event'].MessageChain=MessageChain
sys.modules['astrbot.api.event'].filter=types.SimpleNamespace(platform_adapter_type=deco,event_message_type=deco,on_llm_request=deco,EventMessageType=types.SimpleNamespace(GROUP_MESSAGE=1,PRIVATE_MESSAGE=2))
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
    def test_memory_inventory_questions_are_read_only(self):
        for text in ('告诉我现在你有哪些长期记忆', '我有哪些长期记忆？', '我现在有哪些长期记忆', '告诉我到底有哪些长期记忆',
                     '请告诉我你都记住了什么？', '你记得我什么？', '我的长期记忆有哪些？',
                     '你有哪些长期记忆？', '现在都有哪些长期记忆？', '列出所有长期记忆',
                     '长期记忆里都保存了什么？'):
            with self.subTest(text=text):
                self.assertEqual(m._memory_intent(text), {'action': 'list', 'page': 1})
        for text in ('长期记忆是什么？', '你能保存长期记忆吗？', '你有哪些长期记忆功能？',
                     '告诉我张三有哪些长期记忆', '列出别人的长期记忆', '不要告诉我有哪些长期记忆',
                     '例如：我有哪些长期记忆？', '他说：我有哪些长期记忆？', '“我有哪些长期记忆？”',
                     '查看我的长期记忆，然后清空'):
            with self.subTest(text=text):
                self.assertIsNone(m._memory_intent(text))

    def test_suffix_memory_directive_keeps_only_current_facts(self):
        facts = '办公地点：合成园区甲楼。住处：合成社区乙区。'
        for ending in ('以上要加入长期记忆。', '上述信息都需要保存到我的长期记忆里', '这些内容请写入长期记忆'):
            text = facts + ending
            self.assertEqual(m._memory_intent(text), {'action': 'add', 'content': facts.rstrip('。')}, text)
        for text in ('将合成偏好记入长期记忆', '把合成偏好记到我的长期记忆里'):
            self.assertEqual(m._memory_intent(text), {'action': 'add', 'content': '合成偏好'}, text)
        for text in ('例如办公地点是合成园区。以上要加入长期记忆', '他说：办公地点是合成园区。以上要加入长期记忆',
                     '“办公地点是合成园区”。以上要加入长期记忆', '办公地点是合成园区。以上不要加入长期记忆',
                     '办公地点是合成园区。以上要加入长期记忆吗？', '以上要加入长期记忆'):
            self.assertIsNone(m._memory_intent(text), text)

    def test_report_save_requires_direct_imperative_not_negation_or_reported_speech(self):
        for text in ('不要将该报告单记入长期记忆', '请别把该报告单写入长期记忆', '他让我将该报告单记入长期记忆', '你昨天将该报告单记入长期记忆', '例如将该报告单记入长期记忆', '将该报告单不要记入长期记忆'):
            self.assertIsNone(m._context_memory_intent(text), text)
        for text in ('将当前日期和该报告单记入长期记忆，备查', '请把这份报告保存到我的长期记忆', '麻烦你帮我将这张图片加入长期记忆，供后续参考'):
            self.assertEqual(m._context_memory_intent(text), {'action': 'add', 'from_context': True}, text)
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
          '我有哪些长期记忆？':{'action':'list','page':1},
          '我现在有哪些长期记忆':{'action':'list','page':1},
          '告诉我到底有哪些长期记忆':{'action':'list','page':1},
          '请告诉我有什么长期记忆':{'action':'list','page':1},
          '你记得我什么':{'action':'list','page':1},
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
          '记住我明天上午七点到医院，要吃面。明天中午约咖啡。周六早上九点半打乒乓球。分别创建提醒',
          '记住十秒后提醒我吃饭',
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
    async def save_relationship(self, content, sender='A', group='qq:GroupMessage:family'):
        private = 'qq:FriendMessage:viewer'
        self.plugin.allowed_umos.update({group, private})
        result = await self.op('记住' + content, sender=sender, umo=group)
        self.assertTrue(result['ok'])
        return Event('查看我的长期记忆', sender=sender, umo=private)

    async def test_readable_four_person_graph_from_private_keeps_database_and_raw_snapshot(self):
        raw = '本群人物关系：<@A>是<@B>的老公。<@C>是<@B>的爸爸。<@D>是<@A>的爸爸'
        source = await self.save_relationship(raw)
        before = self.plugin.db.execute('SELECT * FROM memories ORDER BY id').fetchall()
        self.plugin.db.execute('PRAGMA query_only=ON')
        result = await self.plugin._apply(source, 'list')
        self.assertEqual(result['items'], [raw])
        message = result['message']
        self.assertIn('共 1 条', message)
        self.assertNotIn('第 1/1 页', message)
        self.assertIn('\n\n1. 本群人物关系：\n你与妻子是夫妻。\n已记录你的父亲、妻子的父亲。', message)
        self.assertNotIn('<@', message)
        self.assertNotIn('#1.', message)
        exported = self.plugin.memory_snapshot_for_task(source)
        self.assertEqual(exported['status'], 'ok')
        self.assertEqual(exported['items'][0]['content'], raw)
        self.assertEqual(exported['items'][0]['id'], before[0][0])
        self.assertEqual(exported['items'][0]['display_content'], message.split('\n\n1. ', 1)[1])
        self.assertNotIn('<@', exported['items'][0]['display_content'])
        self.assertEqual(self.plugin.db.execute('SELECT * FROM memories ORDER BY id').fetchall(), before)
        await self.plugin.explicit_memory_request(source)
        self.assertIs(source.sent[0].use_markdown, False)
        self.assertTrue(all(isinstance(part, Plain) for part in source.sent[0].chain))
        self.plugin.db.execute('PRAGMA query_only=OFF')

    async def test_reverse_spouse_fact_and_maternal_labels(self):
        raw = '本群人物关系：<@B>是<@A>的老婆。<@C>是<@B>的母亲。<@D>是<@A>的妈妈。'
        source = await self.save_relationship(raw)
        message = (await self.plugin._apply(source, 'list'))['message']
        self.assertIn('你与妻子是夫妻。', message)
        self.assertIn('你的母亲、妻子的母亲', message)
        other = await self.save_relationship('本群人物关系：<@A>是<@B>的老公。', sender='B', group='qq:GroupMessage:other')
        self.assertIn('你与丈夫是夫妻。', (await self.plugin._apply(other, 'list'))['message'])

    async def test_unknown_and_conflicting_relations_keep_distinct_members(self):
        source = await self.save_relationship('本群人物关系：<@X>是<@Y>的父亲。')
        message = (await self.plugin._apply(source, 'list'))['message']
        self.assertIn('成员甲是成员乙的父亲', message)
        self.assertNotIn('你的父亲', message)
        await self.op('记住本群人物关系：<@A>是<@B>的老公。<@A>是<@C>的老公。<@D>是<@B>的爸爸。', sender='A', umo='qq:GroupMessage:family')
        message = (await self.plugin._apply(source, 'list'))['message']
        self.assertIn('存在冲突', message)
        self.assertNotIn('你与妻子', message)
        self.assertNotIn('妻子的父亲', message)
        self.assertNotIn('<@', message)
        self.assertIn('成员甲', message)
        self.assertIn('成员乙', message)

    async def test_conflicting_parents_never_receive_unique_parent_label(self):
        source = await self.save_relationship('本群人物关系：<@C>是<@A>的爸爸。<@D>是<@A>的爸爸。')
        message = (await self.plugin._apply(source, 'list'))['message']
        self.assertIn('存在冲突', message)
        self.assertIn('成员甲', message)
        self.assertIn('成员乙', message)
        self.assertNotIn('已记录你的父亲', message)

    async def test_conflicting_spouse_and_parent_roles_use_member_codes(self):
        source = await self.save_relationship('本群人物关系：<@A>是<@B>的老公。<@B>是<@A>的父亲。')
        message = (await self.plugin._apply(source, 'list'))['message']
        self.assertIn('存在冲突', message)
        self.assertNotIn('你与妻子是夫妻', message)
        self.assertIn('成员甲', message)
        self.assertNotIn('<@', message)

    async def test_parent_directed_cycles_fall_back_without_recursive_family_labels(self):
        for raw in ('本群人物关系：<@B>是<@A>的父亲。<@A>是<@B>的父亲',
                    '本群人物关系：<@B>是<@A>的爸爸。<@C>是<@B>的母亲。<@A>是<@C>的父亲'):
            with self.subTest(raw=raw):
                source = await self.save_relationship(raw)
                result = await self.plugin._apply(source, 'list')
                self.assertIn('存在冲突', result['message'])
                self.assertIn('成员甲', result['message'])
                self.assertNotIn('你是你的父亲的父亲', result['message'])
                self.assertNotIn('已记录你的父亲', result['message'])
                self.assertNotIn('<@', result['message'])
                self.assertIn(raw, result['items'])

    async def test_native_and_legacy_markup_share_readable_display_without_changing_raw(self):
        raw = ('本群人物关系：<qqbot-at-user id="A" />是<@B>的老公。'
               '<@C>是<qqbot-at-user id="B" />的爸爸。'
               '<qqbot-at-user id="D" />是<@A>的爸爸')
        source = await self.save_relationship(raw)
        before = self.plugin.db.execute('SELECT * FROM memories ORDER BY id').fetchall()
        result = await self.plugin._apply(source, 'list')
        self.assertIn('你与妻子是夫妻。', result['message'])
        self.assertIn('已记录你的父亲、妻子的父亲。', result['message'])
        self.assertEqual(result['items'], [raw])
        self.assertNotIn('qqbot-at-', result['message'])
        self.assertNotIn('<@', result['message'])
        snapshot = self.plugin.memory_snapshot_for_task(source)
        self.assertEqual(snapshot['items'][0]['content'], raw)
        self.assertNotIn('qqbot-at-', snapshot['items'][0]['display_content'])
        self.assertEqual(self.plugin.db.execute('SELECT * FROM memories ORDER BY id').fetchall(), before)
        await self.plugin.explicit_memory_request(source)
        self.assertTrue(all(isinstance(part, Plain) for part in source.sent[0].chain))
        self.assertIs(source.sent[0].use_markdown, False)

    async def test_native_markup_in_prose_and_unknown_attributes_remain_plain(self):
        source = await self.save_relationship('本群人物关系：<@A>是<@B>的老公')
        raw = ('例如：<qqbot-at-user id="A" />说<qqbot-at-user id="B" />喜欢茶；'
               '<qqbot-at-user id="B" extra="x" />与<qqbot-at-user id="all" />；<qqbot-at-all />')
        await self.op('记住' + raw, sender='A', umo='qq:GroupMessage:family')
        result = await self.plugin._apply(source, 'list')
        self.assertIn('例如：你说成员甲喜欢茶', result['message'])
        self.assertIn('QQ账号引用与QQ账号引用；QQ账号引用', result['message'])
        self.assertNotIn('qqbot-at-', result['message'])
        self.assertNotIn('id="', result['message'])
        self.assertIn(raw, result['items'])

    async def test_prose_only_replaces_mentions_and_ordinary_text_is_unchanged(self):
        source = await self.save_relationship('本群人物关系：<@A>是<@B>的老公。')
        prose = '例如：<@A>是<@B>的老公；<@C>说“<@B>喜欢茶”'
        await self.op('记住' + prose, sender='A', umo='qq:GroupMessage:family')
        ordinary = '普通偏好：早餐喝温豆浆，回答要简洁'
        await self.op('记住' + ordinary, sender='A', umo='qq:GroupMessage:family')
        result = await self.plugin._apply(source, 'list')
        self.assertIn(ordinary, result['message'])
        self.assertIn('例如：你是成员甲的老公；成员乙说“成员甲喜欢茶”', result['message'])
        self.assertIn(prose, result['items'])
        exported = self.plugin.memory_snapshot_for_task(source)
        self.assertTrue(all('<@' not in item['display_content'] for item in exported['items']))

    async def test_relationship_inference_stays_with_the_source_group(self):
        source = await self.save_relationship('本群人物关系：<@A>是<@B>的老公。')
        await self.save_relationship('本群人物关系：<@C>是<@B>的父亲。', group='qq:GroupMessage:other')
        message = (await self.plugin._apply(source, 'list'))['message']
        self.assertIn('你与妻子是夫妻。', message)
        self.assertNotIn('妻子的父亲', message)
        self.assertIn('成员乙是成员甲的父亲', message)

    async def test_display_order_numbers_continue_across_pages_and_keep_blank_lines(self):
        for index in range(7):
            await self.op(f'记住合成事项{index}')
        first = await self.op('/记忆 list 1')
        second = await self.op('/记忆 list 2')
        self.assertIn('第 1/2 页，共 7 条', first['message'])
        self.assertIn('\n\n1. 合成事项6\n\n2. 合成事项5', first['message'])
        self.assertIn('\n\n5. 合成事项2\n\n下一页：/记忆 list 2', first['message'])
        self.assertIn('\n\n6. 合成事项1\n\n7. 合成事项0', second['message'])
        self.assertNotIn('下一页', second['message'])
        self.assertNotIn('#', first['message'])

    async def test_relationship_reader_is_scoped_and_does_not_modify_memory(self):
        origin = "qq:GroupMessage:family"
        self.plugin.allowed_umos.update({origin, "qq:GroupMessage:foreign", "qq:FriendMessage:alice"})
        source = Event("记住本群人物关系：<@alice> 是<@wife> 的老公", umo=origin)
        await self.plugin.explicit_memory_request(source)
        before = self.plugin._rows(self.plugin._owner(source))
        request = Event("at 我老婆，告诉她准备好了", umo=origin)
        self.assertEqual(self.plugin.saved_relationship_facts(request), [before[0][1]])
        for denied in (Event("at 我老婆", sender="bob", umo=origin),
                       Event("at 我老婆", umo="qq:GroupMessage:foreign"),
                       Event("at 我老婆", umo="qq:FriendMessage:alice"),
                       Event("at 我老婆", umo=origin, addressed=False)):
            self.assertEqual(self.plugin.saved_relationship_facts(denied), [])
        self.assertEqual(self.plugin._rows(self.plugin._owner(source)), before)
        self.assertFalse(request.sent)

    async def test_inventory_lists_every_saved_record_without_relevance_filter_or_write(self):
        for i in range(12):
            await self.op(f'记住合成事项{i}')
        await self.op('记住其他用户的合成事项', sender='bob')
        self.plugin.allowed_umos.add('synthetic-private')
        await self.op('记住其他会话的合成事项', umo='synthetic-private')
        before = self.plugin.db.execute('SELECT * FROM memories ORDER BY id').fetchall()
        self.plugin.db.execute('PRAGMA query_only=ON')
        collected = []
        for page, text in enumerate(('告诉我到底有哪些长期记忆', '/记忆 list 2', '查看我的长期记忆第3页'), 1):
            current = Event(text)
            await self.plugin.explicit_memory_request(current)
            self.assertTrue(current.stopped)
            self.assertTrue(current.get_extra('long_term_memory_tool_done'))
            self.assertEqual(len(current.sent), 1)
            message = current.sent[0].chain[0].text
            self.assertIn(f'第 {page}/3 页，共 13 条', message)
            self.assertNotIn('其他用户', message)
            collected.extend(line for line in message.splitlines() if __import__('re').match(r'^\d+\. ', line))
            if page < 3:
                self.assertIn(f'/记忆 list {page + 1}', message)
        self.assertEqual(len(collected), 13)
        self.assertEqual(len(set(collected)), 13)
        self.assertEqual(self.plugin.db.execute('SELECT * FROM memories ORDER BY id').fetchall(), before)
        self.plugin.db.execute('PRAGMA query_only=OFF')

    async def test_inventory_from_private_session_reads_same_sender_authorized_group_records(self):
        self.plugin.allowed_umos.add('synthetic-private')
        await self.op('记住群里的合成偏好')
        private = Event('告诉我你现在有哪些长期记忆', umo='synthetic-private')
        await self.plugin.explicit_memory_request(private)
        self.assertTrue(private.stopped)
        self.assertIn('共 1 条', private.sent[0].chain[0].text)
        self.assertIn('群里的合成偏好', private.sent[0].chain[0].text)
        self.assertEqual(self.plugin._rows(self.plugin._owner(private)), [])

    async def test_inventory_paginates_over_100_records_across_two_sessions_and_refuses_overflow(self):
        self.plugin.allowed_umos.add('synthetic-private')
        group_owner = self.plugin._owner(Event())
        private_owner = self.plugin._owner(Event(umo='synthetic-private'))
        expected = []
        for owner, prefix in ((group_owner, 'group'), (private_owner, 'private')):
            for index in range(70):
                content = f'{prefix} synthetic memory {index}'
                expected.append(content)
                self.plugin.db.execute(
                    'INSERT INTO memories(owner,content,normalized,created_at,updated_at) VALUES(?,?,?,?,?)',
                    (owner, content, content, index, index))
        self.plugin.db.commit()
        before = self.plugin.db.execute('SELECT * FROM memories ORDER BY id').fetchall()
        self.plugin.db.execute('PRAGMA query_only=ON')
        actual = []
        for page in range(1, 29):
            result = await self.op(f'/记忆 list {page}', umo='synthetic-private')
            self.assertTrue(result['ok'])
            self.assertEqual((result['total'], result['pages'], result['page']), (140, 28, page))
            self.assertEqual(len(result['items']), 5)
            actual.extend(result['items'])
        self.assertCountEqual(actual, expected)
        self.assertEqual(len(set(actual)), 140)
        self.assertEqual(self.plugin.db.execute('SELECT * FROM memories ORDER BY id').fetchall(), before)
        self.plugin.db.execute('PRAGMA query_only=OFF')
        # Simulate legacy/imported rows exceeding the supported snapshot cap.
        # A successful result must never quietly advertise a partial total.
        for index in range(61):
            content = f'overflow synthetic memory {index}'
            self.plugin.db.execute(
                'INSERT INTO memories(owner,content,normalized,created_at,updated_at) VALUES(?,?,?,?,?)',
                (group_owner, content, content, index, index))
        self.plugin.db.commit()
        self.plugin.db.execute('PRAGMA query_only=ON')
        result = await self.op('/记忆 list 1', umo='synthetic-private')
        self.assertFalse(result['ok'])
        self.assertEqual(result['code'], 'too_many_records')
        self.assertEqual(result['items'], [])
        self.assertIsNone(result['total'])
        self.assertIn('无法返回完整清单', result['message'])
        self.assertEqual(self.plugin.db.execute('SELECT COUNT(*) FROM memories').fetchone()[0], 201)
        self.plugin.db.execute('PRAGMA query_only=OFF')


    async def test_location_aliases_retrieve_without_literal_overlap(self):
        content = '公司在合成园区甲楼。家在合成社区乙区'
        await self.op('记住' + content)
        question = '你记得我住在哪里、办公地点在哪里吗？'
        topics = set(m._RETRIEVAL_TOPICS)
        self.assertFalse((m._tokens(content) - topics) & (m._tokens(question) - topics))
        source = Event(question)
        before = self.plugin._rows(self.plugin._owner(source))
        self.assertIn(content, self.plugin.memory_context_for_task(source, question))
        request = types.SimpleNamespace(system_prompt='', func_tool=None)
        await self.plugin.add_memory_context(source, request)
        self.assertIn(content, request.system_prompt)
        self.assertIsNone(request.func_tool)
        self.assertEqual(self.plugin._rows(self.plugin._owner(source)), before)
        self.assertFalse(source.sent)

    async def test_commute_question_retrieves_both_scoped_locations(self):
        home = '住址：合成住宅甲区'
        office = '办公地点：合成园区乙栋'
        await self.op('记住' + home)
        await self.op('记住' + office)
        question = '明天从家出发去公司，怎样走舒服些？'
        self.plugin.allowed_umos.add('synthetic-other-scope')
        context = self.plugin.memory_context_for_task(Event(question), question)
        self.assertIn(home, context)
        self.assertIn(office, context)
        for source in (Event(question, sender='bob'), Event(question, umo='synthetic-other-scope'),
                       Event(question, addressed=False)):
            with self.subTest(sender=source.sender, umo=source.unified_msg_origin, addressed=source.is_at_or_wake_command):
                self.assertEqual(self.plugin.memory_context_for_task(source, question), '')

    async def test_location_aliases_do_not_match_unrelated_words(self):
        await self.op('记住公司在合成园区甲楼，家在合成社区乙区')
        for question in ('解释国家与专家两个词', '国家在哪里体现价值', '上市公司在哪里披露财报', '如何煎蛋', '翻译 hello'):
            with self.subTest(question=question):
                self.assertFalse(set(m._RETRIEVAL_TOPICS) & m._tokens(question))
        for question in ('解释国家与专家两个词', '如何煎蛋', '翻译 hello'):
            with self.subTest(question=question):
                self.assertEqual(self.plugin.memory_context_for_task(Event(question), question), '')

    async def test_private_suffix_request_writes_sqlite_before_any_model(self):
        self.plugin.allowed_umos.add('synthetic-private')
        source = Event('办公地点：合成园区甲楼。住处：合成社区乙区。以上要加入长期记忆。', umo='synthetic-private')
        await self.plugin.explicit_memory_request(source)
        self.assertTrue(source.stopped)
        self.assertTrue(source.get_extra('long_term_memory_tool_done'))
        self.assertTrue(self.plugin.handles_memory_request(source))
        self.assertEqual(source.get_extra('long_term_memory_request')['event'], id(source))
        self.assertEqual(len(self.plugin._rows(self.plugin._owner(source))), 1)
        self.assertIn('合成园区甲楼', self.plugin._rows(self.plugin._owner(source))[0][1])
        await self.plugin.explicit_memory_request(source)
        self.assertEqual(len(source.sent), 1)
        await self.plugin.terminate()
        await self.plugin.initialize()
        self.assertEqual(len(self.plugin._rows(self.plugin._owner(source))), 1)
        self.assertEqual(list(Path(self.tmp.name).glob('*.md')), [])
        self.assertEqual(self.plugin.memory_context_for_task(Event('办公地点', sender='bob', umo='synthetic-private'), '办公地点'), '')

    async def test_report_request_is_reserved_before_planning_and_missing_context_is_honest(self):
        source = Event('将该报告单记入长期记忆')
        await self.plugin.explicit_memory_request(source)
        self.assertFalse(source.stopped)
        self.assertIsNotNone(source.get_extra('long_term_memory_request'))
        request = types.SimpleNamespace(system_prompt='', func_tool=None, image_urls=[])
        await self.plugin.add_memory_context(source, request)
        self.assertIsNone(request.func_tool)
        self.assertIn('SQLite', request.system_prompt)
        self.assertIn('请其附上报告', request.system_prompt)
        self.assertEqual(self.plugin._rows(self.plugin._owner(source)), [])

    async def test_report_context_memory_requires_same_event_and_actual_attachment(self):
        event = Event('将当前日期和该报告单记入长期记忆，备查')
        request = types.SimpleNamespace(system_prompt='',func_tool=None,image_urls=['synthetic-image'])
        await self.plugin.explicit_memory_request(event)
        self.assertFalse(event.stopped)
        await self.plugin.add_memory_context(event,request)
        self.assertTrue(request.func_tool.tools)
        empty = json.loads(await self.plugin.manage_memory_tool(event, action='add', content=''))
        self.assertFalse(empty['ok'])
        self.assertEqual(self.plugin._rows(self.plugin._owner(event)), [])
        result=json.loads(await self.plugin.manage_memory_tool(event,action='add',content='报告日期：2026-10-30；尿白细胞酯酶3+。'))
        self.assertTrue(result['ok'])
        rows=self.plugin._rows(self.plugin._owner(event))
        self.assertIn('记录日期：',rows[0][1])
        self.assertIn('尿白细胞酯酶3+',rows[0][1])
        self.assertFalse(json.loads(await self.plugin.manage_memory_tool(event,action='add',content='again'))['ok'])

    async def test_report_context_memory_missing_image_or_copied_binding_denies_write(self):
        event=Event('将当前日期和该报告单记入长期记忆，备查')
        request=types.SimpleNamespace(system_prompt='',func_tool=None,image_urls=[])
        await self.plugin.add_memory_context(event,request)
        self.assertIsNone(request.func_tool)
        self.assertIn('没有可验证',request.system_prompt)
        request.image_urls=['synthetic-image']
        await self.plugin.add_memory_context(event,request)
        copied=Event('将当前日期和该报告单记入长期记忆，备查')
        copied.extra=dict(event.extra)
        result=json.loads(await self.plugin.manage_memory_tool(copied,action='add',content='报告摘要'))
        self.assertFalse(result['ok'])

    async def test_placeholder_or_unloaded_report_image_cannot_authorize_memory(self):
        reply_type = type('Reply', (), {})
        for quoted in ('[图片]', '[Image Attachment: report.jpg]', '请参考报告', '刚才发过了'):
            reply = reply_type()
            reply.message_str = quoted
            reply.chain = []
            event = Event(parts=[Plain('将当前日期和该报告单记入长期记忆，备查'), reply])
            request = types.SimpleNamespace(system_prompt='', func_tool=None, image_urls=[])
            await self.plugin.add_memory_context(event, request)
            self.assertIsNone(request.func_tool)
            self.assertFalse(json.loads(await self.plugin.manage_memory_tool(event, action='add', content='虚构报告'))['ok'])

    async def test_direct_text_report_is_available_but_quoted_commands_are_not_authority(self):
        reply = type('Reply', (), {})()
        reply.message_str = '检验报告：检测日期2026-10-30，白细胞酯酶3+，参考范围阴性。'
        reply.chain = []
        event = Event(parts=[Plain('将当前日期和该报告单记入长期记忆，备查'), reply])
        request = types.SimpleNamespace(system_prompt='', func_tool=None, image_urls=[])
        await self.plugin.add_memory_context(event, request)
        self.assertIsNotNone(request.func_tool)
        result = json.loads(await self.plugin.manage_memory_tool(event, action='add', content='检验报告：白细胞酯酶3+，参考范围阴性。'))
        self.assertTrue(result['ok'])
        ignored = Event(parts=[Plain('看看这个'), reply])
        request = types.SimpleNamespace(system_prompt='', func_tool=None, image_urls=[])
        await self.plugin.add_memory_context(ignored, request)
        self.assertIsNone(request.func_tool)

    async def test_reminder_request_is_not_saved_as_long_term_memory(self):
        event = Event('记住我明天上午七点到医院，要吃面。明天中午约咖啡。周六早上九点半打乒乓球。分别创建提醒')
        await self.plugin.explicit_memory_request(event)
        self.assertFalse(event.stopped)
        self.assertEqual(self.plugin._rows(self.plugin._owner(event)), [])
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
