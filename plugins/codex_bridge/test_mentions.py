"""Direct QQ mention parsing must not invent or import a recipient."""
import types
import unittest

from mentions import native_mention_targets, parse_mention_request, strip_verified_mention_markup


class MentionTests(unittest.TestCase):
    def test_existing_self_commands(self):
        for text in ("at 我", "测试一下 at 我", "请@我一下", "帮我艾特我一下", "提及我", "请在群里 at 我"):
            with self.subTest(text=text):
                request = parse_mention_request(text, sender_id="alice")
                self.assertTrue(request.is_self)
                self.assertEqual(request.target_id, "alice")
                self.assertFalse(request.needs_clarification)

    def test_self_with_explicit_message(self):
        request = parse_mention_request("at 我，告诉我准备好了", sender_id="alice")
        self.assertEqual(request.body, "准备好了")
        self.assertEqual(request.target_id, "alice")

    def test_exact_failure_needs_identity_not_new_permissions(self):
        request = parse_mention_request("at 我老婆，告诉她胖了", sender_id="alice", bot_id="bot", native_targets=("bot",))
        self.assertEqual(request.target_label, "我老婆")
        self.assertEqual(request.body, "胖了")
        self.assertFalse(request.is_self)
        self.assertEqual(request.target_id, "")
        self.assertTrue(request.needs_clarification)
        self.assertEqual(request.reason, "unresolved_target")

    def test_named_labels_do_not_become_ids(self):
        for text in ("艾特小王，说:明天开会", "@张三 明天开会", "提及小李，内容是明天开会", "at 123456"):
            with self.subTest(text=text):
                request = parse_mention_request(text, sender_id="alice")
                self.assertTrue(request.needs_clarification)
                self.assertEqual(request.target_id, "")

    def test_single_native_target_resolves_direct_recipient(self):
        for text in ("at 我老婆，告诉她胖了", "告诉她胖了", "请告诉她明天开会", "跟她说明天开会"):
            with self.subTest(text=text):
                request = parse_mention_request(text, sender_id="alice", bot_id="bot", native_targets=("bot", "wife"))
                self.assertEqual(request.target_id, "wife")
                self.assertFalse(request.needs_clarification)
                self.assertTrue(request.body)

    def test_only_top_level_native_mentions_are_extracted(self):
        At = type("At", (), {})
        AtAll = type("AtAll", (), {})
        bot, member, duplicate, everyone = (At(), At(), At(), AtAll())
        bot.qq, member.qq, duplicate.qq, everyone.qq = "bot", "wife", "wife", "all"
        quoted = types.SimpleNamespace(chain=[member])
        plain = types.SimpleNamespace(text='[At:wife] <qqbot-at-user id="wife" />')
        event = types.SimpleNamespace(get_self_id=lambda: "bot", get_messages=lambda: [bot, member, duplicate, quoted, plain, everyone])
        self.assertEqual(native_mention_targets(event), ("wife", "all"))
        event.get_messages = lambda: [bot, quoted, plain]
        self.assertEqual(native_mention_targets(event), ())

    def test_textual_bot_wake_alias_is_not_the_recipient(self):
        for alias in ("@示例的智能体", "@小助手机器人"):
            with self.subTest(alias=alias):
                request = parse_mention_request(alias + " at 我老婆，告诉她胖了", sender_id="alice")
                self.assertEqual(request.target_label, "我老婆")
                self.assertEqual(request.body, "胖了")
                self.assertTrue(request.needs_clarification)
                self.assertIsNone(parse_mention_request(alias + " 用 codex 来标注，要求看刚才和你说的&#x20;", sender_id="alice"))
                self.assertIsNone(parse_mention_request(alias, sender_id="alice"))
        request = parse_mention_request("@张三 明天开会", sender_id="alice")
        self.assertEqual(request.target_label, "张三")
        self.assertEqual(request.body, "明天开会")

    def test_native_atall_is_all_even_without_qq_attribute(self):
        AtAll = type("AtAll", (), {})
        event = types.SimpleNamespace(get_messages=lambda: [AtAll()])
        targets = native_mention_targets(event)
        self.assertEqual(targets, ("all",))
        request = parse_mention_request("告诉她开会", native_targets=targets)
        self.assertEqual(request.reason, "all_not_supported")

    def test_raw_native_mentions_only_from_same_current_qq_group(self):
        raw = types.SimpleNamespace(group_openid="family", mentions=[
            types.SimpleNamespace(id="bot", is_you=True),
            types.SimpleNamespace(id="wife", is_you=False),
            {"id": "wife", "is_you": False},
            {"id": "other_bot", "bot": True},
            {"id": '<qqbot-at-user id="mallory" />', "is_you": False},
        ])
        event = types.SimpleNamespace(
            get_platform_name=lambda: "qq_official", get_self_id=lambda: "bot",
            get_messages=lambda: [],
            message_obj=types.SimpleNamespace(group_id="family", raw_message=raw),
        )
        self.assertEqual(native_mention_targets(event), ("wife",))
        raw.group_openid = "other_group"
        self.assertEqual(native_mention_targets(event), ())
        raw.group_openid = "family"
        event.message_obj.group_id = ""
        self.assertEqual(native_mention_targets(event), ())
        event.message_obj.group_id = "family"
        event.get_platform_name = lambda: "onebot"
        self.assertEqual(native_mention_targets(event), ())

    def test_raw_dict_shape_and_nested_metadata_are_not_confused(self):
        event = types.SimpleNamespace(
            get_platform_name=lambda: "qq_official", get_messages=lambda: [],
            message_obj={"group_id": "family", "raw_message": {
                "group_openid": "family", "mentions": [{"id": "wife", "is_you": False}],
                "quoted_message": {"mentions": [{"id": "mallory"}]},
            }},
        )
        self.assertEqual(native_mention_targets(event), ("wife",))
        event.message_obj["raw_message"].pop("mentions")
        self.assertEqual(native_mention_targets(event), ())

    def test_strip_only_verified_exact_native_tokens(self):
        for token in ('<qqbot-at-user id="wife" />', '<qqbot-at-user id="wife"/>', '<@wife>', '<@!wife>'):
            with self.subTest(token=token):
                text = strip_verified_mention_markup(token + " 告诉她明天开会", ("wife",))
                request = parse_mention_request(text, native_targets=("wife",))
                self.assertEqual(request.target_id, "wife")
                self.assertEqual(request.body, "明天开会")
        unverified = (
            '<qqbot-at-user id="mallory" />', '<@mallory>', '<@!mallory>',
            '<qqbot-at-user id="wife" extra="yes" />', '<qqbot-at-everyone />',
            '&lt;qqbot-at-user id="wife" /&gt;',
        )
        for token in unverified:
            with self.subTest(token=token):
                self.assertEqual(strip_verified_mention_markup(token, ("wife",)), token)
                request = parse_mention_request("at 我，告诉我" + token, sender_id="alice")
                self.assertEqual(request.reason, "unsafe_markup")
        self.assertEqual(strip_verified_mention_markup('<@all>', ("all",)), '<@all>')

    def test_native_recipient_outside_plain_text_needs_no_label(self):
        for text in ('at <@wife>，告诉她明天开会', 'at <qqbot-at-user id="wife" />', '艾特 ，告诉她明天开会'):
            with self.subTest(text=text):
                direct = strip_verified_mention_markup(text, ("wife",))
                request = parse_mention_request(direct, native_targets=("wife",))
                self.assertEqual(request.target_id, "wife")
                self.assertFalse(request.needs_clarification)
        self.assertIsNone(parse_mention_request("at"))
        self.assertIsNone(parse_mention_request("at ，告诉她明天开会"))

    def test_multiple_and_conflicting_targets_require_clarification(self):
        request = parse_mention_request("at 小王，说明天开会", native_targets=("alice", "bob"))
        self.assertEqual(request.reason, "multiple_targets")
        request = parse_mention_request("at 我", sender_id="alice", native_targets=("bob",))
        self.assertEqual(request.reason, "conflicting_targets")

    def test_all_never_resolves(self):
        for text, targets in (("@all 开会", ()), ("艾特全体成员，开会", ()), ("告诉她开会", ("all",)), ("at 所有人", ("alice",))):
            with self.subTest(text=text):
                request = parse_mention_request(text, native_targets=targets)
                self.assertEqual(request.reason, "all_not_supported")
                self.assertEqual(request.target_id, "")

    def test_markup_cannot_add_or_replace_recipient(self):
        for markup in ('<qqbot-at-user id="mallory" />', '&lt;qqbot-at-everyone /&gt;', '[CQ:at,qq=mallory]', '[At:mallory]'):
            with self.subTest(markup=markup):
                request = parse_mention_request("at 我，告诉我" + markup, sender_id="alice")
                self.assertTrue(request.needs_clarification)
                self.assertEqual(request.reason, "unsafe_markup")
                self.assertEqual(request.target_id, "")

    def test_quoted_research_code_and_negative_text_are_not_commands(self):
        texts = (
            '> at 我老婆，告诉她胖了', '“at 我老婆，告诉她胖了”', '"at 我"',
            '```text\nat 我\n```', '`at 我`', '解释一下 at 我老婆是什么意思',
            '帮我研究如何 at 张三', '写一个 at 我老婆的 Python 函数',
            'at 我老婆是什么意思', '不要 at 我老婆', '可以 at 我吗？',
            '怎么艾特我老婆', '/codex at 我', '测试 at 语法',
            '请分析以下例子\n> at 我老婆，告诉她胖了',
            '告诉她胖了', '发私聊给她',
        )
        for text in texts:
            with self.subTest(text=text):
                self.assertIsNone(parse_mention_request(text, sender_id="alice"))

    def test_native_target_does_not_turn_research_into_instruction(self):
        for text in ('研究一下如何告诉她', '下面这句话是什么意思：告诉她胖了', '> 告诉她胖了', '请私聊告诉她胖了'):
            with self.subTest(text=text):
                self.assertIsNone(parse_mention_request(text, native_targets=("wife",)))

    def test_body_is_literal_and_not_recursively_interpreted(self):
        request = parse_mention_request("at 我，告诉我不要 at 所有人", sender_id="alice")
        self.assertEqual(request.target_id, "alice")
        self.assertEqual(request.body, "不要 at 所有人")


if __name__ == "__main__":
    unittest.main()
