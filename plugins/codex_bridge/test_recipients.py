import copy
import types
import unittest

from recipients import (RECIPIENT_TOOL_NAME, ReplyRecipientContext, parse_recipient_request,
                        recipient_for_followup, resolve_recipient, validate_recipient)


UMO = "qq:GroupMessage:family"
FACTS = ["本群人物关系：<@alice> 是<@wife> 的丈夫，<@father> 是<@alice> 的爸爸"]


def resolve(text, **kwargs):
    return resolve_recipient(text, umo=kwargs.pop("umo", UMO), sender_id=kwargs.pop("sender_id", "alice"),
                             relationship_contents=kwargs.pop("relationship_contents", FACTS), **kwargs)


class Plain:
    def __init__(self, text): self.text = text


class At:
    def __init__(self, qq): self.qq = qq


class Event:
    def __init__(self, text, *, sender="alice", umo=UMO):
        self.parts = [Plain(text)]
        self.sender = sender
        self.unified_msg_origin = umo
        self.is_at_or_wake_command = True
        self.message_obj = types.SimpleNamespace(self_id="bot", group_id="family")
    def get_messages(self): return self.parts
    def get_sender_id(self): return self.sender
    def get_platform_name(self): return "qq_official"
    def get_self_id(self): return "bot"


class RecipientTests(unittest.TestCase):
    def test_exact_sweat_therapy_reproduction_binds_generation_audience(self):
        for text in ("向我老婆讲解一下汗液疗法", "给我老婆介绍下汗液疗法", "请帮我给我的老婆通俗地解释一下汗液疗法"):
            with self.subTest(text=text):
                resolution = resolve(text)
                self.assertEqual(resolution.status, "ready")
                self.assertEqual(resolution.recipient["target_id"], "wife")
                self.assertIn("汗液疗法", resolution.request.task)
                self.assertEqual(resolution.recipient["source"], "relationship")
                self.assertIsNotNone(validate_recipient(resolution.recipient, umo=UMO, sender_id="alice", request_text=text))

    def test_other_generated_phrasings_and_self_audience(self):
        for text in ("跟我老婆讲讲汗液疗法", "对我的妻子科普一下汗液疗法", "为我老婆总结一下刚才的内容", "告诉我老婆汗液疗法是什么"):
            with self.subTest(text=text):
                self.assertEqual(resolve(text).recipient["target_id"], "wife")
        self.assertEqual(resolve("给我解释一下汗液疗法").recipient["target_id"], "alice")

    def test_topic_first_and_explicit_codex_routes_preserve_original_binding(self):
        texts = ("汗液疗法，给我老婆介绍下", "把汗液疗法给我老婆介绍一下",
                 "/codex 向我老婆讲解一下汗液疗法", "/cx write 给我老婆介绍下汗液疗法",
                 "请用 Codex 给我老婆介绍下汗液疗法", "请让 Codex 来向我老婆解释一下汗液疗法")
        for text in texts:
            with self.subTest(text=text):
                resolution = resolve(text)
                self.assertEqual(resolution.status, "ready")
                self.assertEqual(resolution.recipient["target_id"], "wife")
                self.assertIn("汗液疗法", resolution.request.task)
                self.assertIsNotNone(validate_recipient(resolution.recipient, umo=UMO, sender_id="alice", request_text=text))
        self.assertEqual(resolve("/unrelated 给我老婆介绍汗液疗法").status, "none")

    def test_optional_semantic_label_is_grounded_in_current_direct_request(self):
        result = resolve("我想请你和我老婆沟通，给我老婆聊聊汗液疗法", target_label="我老婆")
        self.assertEqual(result.status, "ready")
        self.assertEqual(result.recipient["target_id"], "wife")
        texts = ("请给我老婆介绍汗液疗法", "他说‘给我老婆聊聊汗液疗法’", "研究如何给我老婆聊聊汗液疗法",
                 "别给我老婆聊聊汗液疗法", "给我老婆私聊介绍汗液疗法", "汗液疗法是什么")
        for text in texts:
            label = "我爸爸" if text == texts[0] else "我老婆"
            with self.subTest(text=text):
                self.assertNotEqual(resolve(text, target_label=label).status, "ready")

    def test_explicit_at_generation_is_never_literal_echo(self):
        for text in ("at 我老婆，介绍一下汗液疗法", "@我老婆 解释一下汗液疗法", "艾特我老婆，告诉她汗液疗法是什么"):
            with self.subTest(text=text):
                resolution = resolve(text)
                self.assertEqual(resolution.status, "ready")
                self.assertEqual(resolution.recipient["target_id"], "wife")
                self.assertIn("汗液疗法", resolution.request.task)

    def test_literal_at_tell_stays_with_direct_message_handler(self):
        for text in ("at 我老婆，告诉她胖了", "请@我一下", "艾特我，告诉我准备好了"):
            with self.subTest(text=text):
                self.assertIsNone(parse_recipient_request(text))
                self.assertEqual(resolve(text).status, "none")

    def test_native_recipient_is_resolved_without_model_account_input(self):
        for text in ("介绍一下汗液疗法", '<@!native_member> 解释一下汗液疗法', '给她介绍一下汗液疗法'):
            with self.subTest(text=text):
                resolution = resolve(text, native_targets=("native_member",))
                self.assertEqual(resolution.recipient["target_id"], "native_member")
                self.assertEqual(resolution.recipient["source"], "native")
        self.assertEqual(resolve("向我老婆解释一下汗液疗法", native_targets=("native_member",)).recipient["target_id"], "native_member")

    def test_unknown_and_ambiguous_targets_require_clarification(self):
        self.assertEqual(resolve("给张三介绍一下汗液疗法").reason, "unresolved_target")
        self.assertEqual(resolve("给我老婆介绍一下汗液疗法", native_targets=("a", "b")).reason, "multiple_targets")
        conflict = FACTS + ["<@alice> 是<@other_wife> 的老公"]
        self.assertEqual(resolve("给我老婆介绍下汗液疗法", relationship_contents=conflict).reason, "unresolved_target")
        self.assertEqual(resolve("给我介绍下汗液疗法", native_targets=("wife",)).reason, "conflicting_targets")

    def test_all_unverified_markup_and_injection_never_resolve(self):
        self.assertEqual(resolve("给所有人介绍一下汗液疗法").reason, "all_not_supported")
        self.assertEqual(resolve("介绍一下汗液疗法", native_targets=("all",)).reason, "all_not_supported")
        self.assertEqual(resolve('给我老婆介绍一下<qqbot-at-user id="mallory" />').reason, "unsafe_markup")
        self.assertEqual(resolve('<@mallory> 解释一下汗液疗法', native_targets=("wife",)).reason, "unsafe_markup")

    def test_negation_quoted_examples_research_and_no_audience_are_not_bound(self):
        texts = (
            "不要给我老婆介绍一下汗液疗法", "别向我老婆讲解汗液疗法", "给我老婆介绍一下汗液疗法，不要 at 她",
            '> 向我老婆讲解一下汗液疗法', '“向我老婆讲解一下汗液疗法”',
            '```text\n向我老婆讲解一下汗液疗法\n```', '他说向我老婆讲解一下汗液疗法',
            '解释一下“给我老婆介绍下”的语法', '写一个向我老婆介绍汗液疗法的程序',
            '研究如何给我老婆讲解汗液疗法', '介绍一下汗液疗法', '汗液疗法是什么',
            '例如，给我老婆介绍下汗液疗法', '比如，把汗液疗法给我老婆介绍下',
        )
        for text in texts:
            with self.subTest(text=text):
                self.assertEqual(resolve(text).status, "none")

    def test_group_scope_and_private_denial(self):
        for kwargs in ({"umo": "qq:FriendMessage:alice"}, {"platform_name": "onebot"}, {"umo": "qq:GroupMessage:"}):
            with self.subTest(kwargs=kwargs):
                self.assertEqual(resolve("给我老婆介绍下汗液疗法", **kwargs).reason, "group_only")

    def test_persisted_recipient_is_bound_to_owner_group_sender_and_original_text(self):
        text = "给我老婆介绍下汗液疗法"
        metadata = resolve(text).recipient
        for kwargs in ({"umo": "qq:GroupMessage:other", "sender_id": "alice"},
                       {"umo": UMO, "sender_id": "bob"},
                       {"umo": UMO, "sender_id": "alice", "owner": "a" * 64},
                       {"umo": UMO, "sender_id": "alice", "request_text": "新任务"}):
            with self.subTest(kwargs=kwargs):
                self.assertIsNone(validate_recipient(metadata, **kwargs))
        for key, value in (("target_id", "all"), ("target_id", '<@mallory>'), ("version", True),
                           ("source", "model"), ("request_sha256", "x"), ("target_label", "老婆"), ("inherited", "yes")):
            modified = {**metadata, key: value}
            self.assertIsNone(validate_recipient(modified, umo=UMO, sender_id="alice"))
        self.assertIsNone(validate_recipient({**metadata, "account": "arbitrary"}, umo=UMO, sender_id="alice"))

    def test_same_metadata_is_usable_for_normal_and_codex_delivery(self):
        text = "给我老婆介绍下汗液疗法"
        normal = resolve(text).recipient
        persisted_route = {"umo": UMO, "sender": "alice", "reply_recipient": copy.deepcopy(normal)}
        restored = validate_recipient(persisted_route["reply_recipient"], umo=persisted_route["umo"], sender_id=persisted_route["sender"])
        self.assertEqual(normal["target_id"], restored["target_id"])
        self.assertEqual(restored["target_id"], "wife")

    def test_inherit_only_explicit_related_continuation(self):
        parent = resolve("给我老婆介绍下汗液疗法").recipient
        for text in ("再试试呢", "继续讲", "再详细一点", "解释得通俗一点", "补充一下刚才的内容"):
            with self.subTest(text=text):
                result = recipient_for_followup(parent, text, umo=UMO, sender_id="alice", continuing=True)
                self.assertEqual(result.status, "ready")
                self.assertEqual(result.recipient["target_id"], "wife")
                self.assertTrue(result.recipient["inherited"])
        for text in ("明天南京天气如何", "研究一下机票", "再介绍一下 Java", "不要 at 她，继续讲", "给我爸爸介绍下汗液疗法"):
            with self.subTest(text=text):
                result = recipient_for_followup(parent, text, umo=UMO, sender_id="alice", continuing=True, relationship_contents=FACTS)
                self.assertNotEqual((result.recipient or {}).get("target_id"), "wife")
        self.assertEqual(recipient_for_followup(parent, "继续讲", umo=UMO, sender_id="alice").status, "none")

    def test_explicit_new_recipient_and_new_conversation_clear_previous_binding(self):
        parent = resolve("给我老婆介绍下汗液疗法").recipient
        changed = recipient_for_followup(parent, "给我爸爸介绍下汗液疗法", umo=UMO, sender_id="alice", continuing=True, relationship_contents=FACTS)
        self.assertEqual(changed.recipient["target_id"], "father")
        self.assertFalse(changed.recipient["inherited"])
        unknown = recipient_for_followup(parent, "给张三介绍下汗液疗法", umo=UMO, sender_id="alice", continuing=True)
        self.assertEqual(unknown.status, "clarify")
        self.assertIsNone(unknown.recipient)
        for kwargs in ({"new_conversation": True}, {"sender_id": "bob"}, {"umo": "qq:GroupMessage:other"}):
            result = recipient_for_followup(parent, "继续讲", umo=kwargs.pop("umo", UMO), sender_id=kwargs.pop("sender_id", "alice"), continuing=True, **kwargs)
            self.assertEqual(result.status, "none")
        fresh = recipient_for_followup(parent, "新建一个 Codex 任务，给我爸爸介绍汗液疗法", umo=UMO, sender_id="alice", continuing=True, relationship_contents=FACTS)
        self.assertEqual(fresh.recipient["target_id"], "father")
        self.assertFalse(fresh.recipient["inherited"])
        self.assertEqual(recipient_for_followup(parent, "新建一个 Codex 任务研究上海天气", umo=UMO, sender_id="alice", continuing=True).status, "none")


class RecipientToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_literal_mention_binding_survives_explicit_engine_wrappers(self):
        for text in ("at 我老婆，告诉她胖了", "/codex at 我老婆，告诉她胖了",
                     "/cx write at 我老婆，告诉她胖了", "请用 Codex at 我老婆，告诉她胖了",
                     "请让 Codex 来艾特我老婆，告诉她胖了"):
            with self.subTest(text=text):
                self.assertIsNone(parse_recipient_request(text))
                event = Event(text)
                context = ReplyRecipientContext(event, text, FACTS)
                result = context.prepare()
                self.assertEqual(result["status"], "ready")
                self.assertEqual(result["action"], "literal")
                self.assertEqual(context.metadata()["target_id"], "wife")
                self.assertFalse(result["sent"])
                self.assertTrue((await context.execute(RECIPIENT_TOOL_NAME, {}))["recipient_set"])

    async def test_literal_native_target_and_self_preserve_verified_identity(self):
        event = Event('/codex at <@!native_member>，告诉她准备好了')
        event.parts.append(At("native_member"))
        context = ReplyRecipientContext(event, event.parts[0].text, FACTS)
        self.assertEqual(context.prepare()["action"], "literal")
        self.assertEqual(context.metadata()["target_id"], "native_member")
        self.assertEqual(context.metadata()["source"], "native")
        event = Event("/codex at 我，告诉我准备好了")
        context = ReplyRecipientContext(event, event.parts[0].text, FACTS)
        self.assertEqual(context.prepare()["action"], "literal")
        self.assertEqual(context.metadata()["target_id"], "alice")

    async def test_literal_binding_retains_negation_external_quote_and_markup_guards(self):
        for text in ("/codex 不要 at 我老婆，告诉她胖了", "/codex at 我老婆，私聊告诉她胖了",
                     '“/codex at 我老婆，告诉她胖了”', "> /codex at 我老婆，告诉她胖了",
                     "/codex 例如 at 我老婆，告诉她胖了"):
            with self.subTest(text=text):
                event = Event(text)
                context = ReplyRecipientContext(event, text, FACTS)
                self.assertEqual(context.prepare()["status"], "none")
                self.assertIsNone(context.metadata())
        for text, reason in (("/codex at 张三，告诉他准备好了", "unresolved_target"),
                             ('/codex at 我老婆，告诉她<qqbot-at-user id="mallory"/>', "unsafe_markup")):
            event = Event(text)
            context = ReplyRecipientContext(event, text, FACTS)
            self.assertEqual(context.prepare()["reason"], reason)
            self.assertIsNone(context.metadata())
        event = Event("/codex at 我老婆，告诉她胖了")
        context = ReplyRecipientContext(event, event.parts[0].text, FACTS)
        self.assertFalse((await context.execute(RECIPIENT_TOOL_NAME, {"target_label": "我爸爸"}))["ok"])

    async def test_generation_audience_action_remains_generation(self):
        event = Event("/codex at 我老婆，介绍一下汗液疗法")
        context = ReplyRecipientContext(event, event.parts[0].text, FACTS)
        self.assertEqual(context.prepare()["action"], "generate")

    async def test_semantic_tool_has_no_model_selected_account_and_does_not_send(self):
        event = Event("向我老婆讲解一下汗液疗法")
        context = ReplyRecipientContext(event, event.parts[0].text, FACTS)
        self.assertEqual(set(context.schemas()[0]["function"]["parameters"]["properties"]), {"target_label"})
        result = await context.execute(RECIPIENT_TOOL_NAME, {})
        self.assertTrue(result["recipient_set"])
        self.assertFalse(result["sent"])
        self.assertEqual(context.metadata()["target_id"], "wife")
        self.assertFalse((await context.execute(RECIPIENT_TOOL_NAME, {"target_id": "mallory"}))["ok"])
        self.assertFalse((await context.execute("different_tool", {}))["ok"])

    async def test_semantic_label_can_help_with_unfamiliar_direct_phrasing(self):
        event = Event("我想给我老婆聊聊汗液疗法")
        context = ReplyRecipientContext(event, event.parts[0].text, FACTS)
        self.assertFalse(context.prepare()["ok"])
        result = await context.execute(RECIPIENT_TOOL_NAME, {"target_label": "我老婆"})
        self.assertTrue(result["recipient_set"])
        self.assertEqual(context.metadata()["target_id"], "wife")
        self.assertFalse((await context.execute(RECIPIENT_TOOL_NAME, {"target_label": "我爸爸"}))["ok"])
        self.assertIsNone(context.metadata())

    async def test_prepared_parent_binding_survives_tool_and_prepare_calls(self):
        parent = resolve("给我老婆介绍下汗液疗法").recipient
        event = Event("再详细一点")
        context = ReplyRecipientContext(event, event.parts[0].text, FACTS)
        self.assertTrue(context.prepare(parent=parent, continuing=True)["ok"])
        self.assertTrue(context.metadata()["inherited"])
        self.assertTrue(context.prepare()["ok"])
        self.assertTrue((await context.execute(RECIPIENT_TOOL_NAME, {}))["recipient_set"])
        self.assertEqual(context.metadata()["target_id"], "wife")
        self.assertFalse(context.prepare(parent=None)["ok"])
        self.assertIsNone(context.metadata())
        context = ReplyRecipientContext(event, event.parts[0].text, FACTS, parent=parent, continuing=True)
        self.assertTrue(context.prepare()["ok"])

    async def test_event_binding_is_rechecked_after_owner_or_message_change(self):
        for mutate in (lambda event: setattr(event, "sender", "bob"),
                       lambda event: setattr(event, "unified_msg_origin", "qq:GroupMessage:other"),
                       lambda event: setattr(event.parts[0], "text", "给我爸爸介绍下汗液疗法"),
                       lambda event: setattr(event, "is_at_or_wake_command", False)):
            event = Event("向我老婆讲解一下汗液疗法")
            context = ReplyRecipientContext(event, event.parts[0].text, FACTS)
            self.assertTrue(context.prepare()["ok"])
            mutate(event)
            self.assertIsNone(context.metadata())
            self.assertFalse((await context.execute(RECIPIENT_TOOL_NAME, {}))["ok"])

    async def test_private_and_unresolved_tool_call_fail_without_inventing_identity(self):
        event = Event("向我老婆讲解一下汗液疗法", umo="qq:FriendMessage:alice")
        context = ReplyRecipientContext(event, event.parts[0].text, FACTS)
        self.assertEqual(context.schemas(), [])
        self.assertFalse((await context.execute(RECIPIENT_TOOL_NAME, {}))["ok"])
        event = Event("给张三介绍下汗液疗法")
        context = ReplyRecipientContext(event, event.parts[0].text, FACTS)
        result = await context.execute(RECIPIENT_TOOL_NAME, {})
        self.assertEqual(result["status"], "clarify")
        self.assertIsNone(context.metadata())


if __name__ == "__main__":
    unittest.main()
