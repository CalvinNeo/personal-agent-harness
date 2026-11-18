"""Shared capability execution, scope binding and restart context regressions."""
import asyncio
import base64
import copy
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import AsyncMock, patch

from test_plugin import Event, Plain, Reply, UMO


package = types.ModuleType("bound_capability_tests")
package.__path__ = [str(Path(__file__).parent)]
sys.modules[package.__name__] = package
capabilities = importlib.import_module(package.__name__ + ".capabilities")
requests = importlib.import_module(package.__name__ + ".request_context")


class ArgumentSchemaTests(unittest.TestCase):
    def test_nullable_price_update_accepts_a_number_or_explicit_removal(self):
        schema = {"type": ["number", "null"], "exclusiveMinimum": 0, "maximum": 1000}
        capabilities._validate(schema, 99.50)
        capabilities._validate(schema, None)

    def test_nullable_price_does_not_accept_booleans_strings_or_invalid_bounds(self):
        schema = {"type": ["number", "null"], "exclusiveMinimum": 0, "maximum": 1000}
        for value in (False, True, "99", 0, -1, 1001, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                capabilities._validate(schema, value)


class At:
    def __init__(self, qq, name=""):
        self.qq, self.name = qq, name


class NativeReply:
    def __init__(self, **kwargs): self.__dict__.update(kwargs)
NativeReply.__name__ = "Reply"


class Plugin:
    def __init__(self):
        self.allowed = True
        self.calls = 0
        self.events = []

    def _allowed(self, event):
        return self.allowed and event.unified_msg_origin == UMO and event.get_sender_id() == "alice"

    async def provide_tools(self, event, request):
        event.set_extra("prepared_for", event.get_sender_id())
        async def tool(source, query):
            self.calls += 1
            self.events.append(source)
            await asyncio.sleep(.01)
            return json.dumps({"ok": self._allowed(source), "query": query, "owner": source.get_sender_id()})
        request.func_tool = types.SimpleNamespace(tools=[types.SimpleNamespace(
            name="travel_test", description="Synthetic scoped query", handler=tool,
            parameters={"type": "object", "properties": {"query": {"type": "string"}},
                        "required": ["query"], "additionalProperties": False})])


class Reminder:
    def __init__(self):
        self._reminders = [{"id": "abcd1234", "umo": UMO, "sender": "alice"}]
    def _allowed(self, event): return event.get_sender_id() == "alice" and event.unified_msg_origin == UMO
    async def provide_schedule_tool(self, event, request): pass
    async def _list(self, event):
        await event.send(types.SimpleNamespace(chain=[Plain("own reminders")]))
    async def _cancel(self, event, text):
        self.cancelled = text
        self._reminders = [item for item in self._reminders if item["id"] != text.split()[-1]]
        await event.send(types.SimpleNamespace(chain=[Plain("cancelled own reminder")]))


class TimedReminder(Reminder):
    def __init__(self): self._reminders = []
    async def _create_reminders(self, event, parsed):
        if any(due < time.time() + 5 for due, _ in parsed):
            await event.send(types.SimpleNamespace(chain=[Plain("提醒时间已过或太近，未创建")]))
            return
        self._reminders.extend({"id": "timed123", "umo": event.unified_msg_origin, "sender": event.get_sender_id(),
                                "due_at": due, "text": content} for due, content in parsed)
        await event.send(types.SimpleNamespace(chain=[Plain("已创建")]))


class NativeResult:
    def __init__(self, content, isError=False):
        self.content, self.isError = content, isError
    def model_dump(self, **kwargs): return {"content": copy.deepcopy(self.content), "isError": self.isError}
    @classmethod
    def model_validate(cls, value): return cls(**copy.deepcopy(value))


class NativeImage:
    def __init__(self, data): self.data = data
    @classmethod
    def fromBytes(cls, value): return cls(value)
NativeImage.__name__ = "Image"


class TrustPlugin(Plugin):
    def __init__(self):
        super().__init__()
        self.callbacks = []

    async def provide_tools(self, event, request):
        request.func_tool = types.SimpleNamespace(tools=[types.SimpleNamespace(
            name="trust_probe", description="Synthetic trust probe", parameters=capabilities._EMPTY,
            handler=self.probe)])

    async def probe(self, event, *, _codex_call=None):
        self.callbacks.append(_codex_call)
        trusted = callable(_codex_call) and _codex_call(event, "trust_probe")
        child_trusted = False
        if callable(_codex_call):
            async def child():
                return _codex_call(event, "trust_probe")
            child_trusted = await asyncio.create_task(child())
        await asyncio.sleep(0)
        return {"ok": True, "trusted": trusted, "child_trusted": child_trusted,
                "other_tool_trusted": bool(callable(_codex_call) and _codex_call(event, "other_tool"))}


class XHSPlugin(Plugin):
    def _login_allowed(self, event): return False
    def _semantic_authorized(self, event):
        return self._allowed(event) and event.get_extra("xhs_search_semantic_authorized") == self.binding(event)
    def _notes_requested(self, event): return True
    @staticmethod
    def binding(event): return {"event": id(event), "owner": (event.unified_msg_origin, event.get_sender_id())}
    async def provide_search_tool(self, event, request):
        event.set_extra("xhs_search_semantic_authorized", self.binding(event))
        async def read(source):
            raw = b"synthetic-verified-image"
            image = types.SimpleNamespace(data=raw, sha256=hashlib.sha256(raw).hexdigest())
            source.set_extra("xhs_verified_images", {"binding": self.binding(source), "items": [
                {"image": image, "title": "Synthetic note", "url": "https://www.xiaohongshu.com/explore/synthetic-note"}]})
            return NativeResult([{"type": "text", "text": '{"ok":true,"image_count":1}'},
                                 {"type": "image", "data": base64.b64encode(raw).decode(), "mimeType": "image/jpeg"}])
        request.func_tool = types.SimpleNamespace(tools=[types.SimpleNamespace(
            name="xhs_read_notes", description="Read verified note image", parameters=capabilities._EMPTY, handler=read)])


class RegistryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.plugin = Plugin()
        self.metadata = types.SimpleNamespace(name="travel_assistant", activated=True, star_cls=self.plugin)
        self.context = types.SimpleNamespace(get_all_stars=lambda: [self.metadata])
        self.event = Event("请查询合适的班次")
        components = sys.modules["astrbot.api.message_components"]
        self.component_patches = [patch.object(components, "At", At, create=True),
                                  patch.object(components, "Reply", NativeReply)]
        for item in self.component_patches:
            item.start()
            self.addCleanup(item.stop)

    async def test_same_schema_and_handler_for_both_engine_adapters(self):
        registry = await capabilities.CapabilityRegistry(self.context, self.event).prepare()
        schema = registry.schemas()[0]["function"]
        tool_module = types.ModuleType("astrbot.core.agent.tool")
        tool_module.FunctionTool = lambda **kwargs: types.SimpleNamespace(**kwargs)
        with patch.dict(sys.modules, {"astrbot.core.agent.tool": tool_module}):
            tool = registry.function_tools()[0]
        self.assertEqual(tool.name, schema["name"])
        self.assertEqual(tool.parameters, schema["parameters"])
        first = json.loads(await tool.handler(self.event, query="train"))
        second = await registry.execute("travel_test", {"query": "flight"}, "worker:1")
        self.assertTrue(first["ok"] and second["ok"])
        self.assertEqual(self.plugin.events, [self.event, self.event])
        self.assertEqual(self.event.sent, [])

    async def test_codex_attestation_is_internal_call_scoped_and_not_shared_by_concurrent_normal_tools(self):
        plugin = TrustPlugin()
        context = types.SimpleNamespace(get_all_stars=lambda: [types.SimpleNamespace(name="travel_assistant", activated=True, star_cls=plugin)])
        source = Event("接着办")
        source.set_extra("codex", True)
        source.set_extra("_codex_call", True)
        registry = await capabilities.CapabilityRegistry(context, source).prepare()
        normal, worker = await asyncio.gather(registry.execute("trust_probe", {}, "normal"),
                                              registry.execute_codex("trust_probe", {}, "codex"))
        self.assertFalse(normal["trusted"])
        self.assertTrue(worker["trusted"])
        self.assertFalse(worker["child_trusted"] or worker["other_tool_trusted"])
        for callback in plugin.callbacks:
            if callable(callback):
                self.assertFalse(callback(source, "trust_probe"))
        for field in ("_codex_call", "codex", "owner", "sender", "event"):
            denied = await registry.execute_codex("trust_probe", {field: True}, "forged-" + field)
            self.assertEqual(denied["code"], "invalid_arguments")
        self.assertEqual(len(plugin.callbacks), 2)

    async def test_normal_call_id_cannot_be_reused_to_change_engine_authority(self):
        registry = await capabilities.CapabilityRegistry(self.context, self.event).prepare()
        first = await registry.execute("travel_test", {"query": "train"}, "one-call")
        self.assertTrue(first["ok"])
        denied = await registry.execute_codex("travel_test", {"query": "train"}, "one-call")
        self.assertEqual(denied["code"], "call_conflict")
        self.assertEqual(self.plugin.calls, 1)

    async def test_codex_execution_rechecks_original_and_normalized_identity_and_plugin_permissions(self):
        for change in (lambda registry: setattr(self.plugin, "allowed", False),
                       lambda registry: setattr(self.metadata, "activated", False),
                       lambda registry: setattr(registry.event, "sender", "foreign"),
                       lambda registry: setattr(registry.event.message_obj, "message_id", "foreign-message"),
                       lambda registry: setattr(registry.event.get_messages()[0], "text", "changed request")):
            self.plugin.allowed, self.metadata.activated = True, True
            source = Event("/codex 请继续")
            registry = await capabilities.CapabilityRegistry(self.context, source).prepare()
            change(registry)
            denied = await registry.execute_codex("travel_test", {"query": "train"}, "changed")
            self.assertFalse(denied["ok"])
        self.assertEqual(self.plugin.calls, 0)

    async def test_codex_cancel_bypasses_phrase_gate_but_not_reminder_ownership(self):
        reminder = Reminder()
        context = types.SimpleNamespace(get_all_stars=lambda: [types.SimpleNamespace(name="reminder", activated=True, star_cls=reminder)])
        registry = await capabilities.CapabilityRegistry(context, Event("按前面说的办")).prepare()
        denied = await registry.execute("cancel_current_reminder", {"reminder_id": "abcd1234"}, "normal")
        self.assertEqual(denied["code"], "intent_required")
        allowed = await registry.execute_codex("cancel_current_reminder", {"reminder_id": "abcd1234"}, "worker")
        self.assertTrue(allowed["ok"])
        self.assertEqual(reminder.cancelled, "取消 abcd1234")
        reminder.cancelled = None
        foreign = await registry.execute_codex("cancel_current_reminder", {"reminder_id": "foreign1"}, "foreign")
        self.assertFalse(foreign["ok"])
        self.assertIsNone(reminder.cancelled)

    async def test_engine_handoff_reuses_completed_mutations_without_reexecuting(self):
        reminder = Reminder()
        reminder._cancel = AsyncMock(wraps=reminder._cancel)
        context = types.SimpleNamespace(get_all_stars=lambda: [types.SimpleNamespace(name="reminder", activated=True, star_cls=reminder)])
        registry = await capabilities.CapabilityRegistry(context, Event("取消提醒 abcd1234")).prepare()
        arguments = {"reminder_id": "abcd1234"}
        normal = await registry.execute("cancel_current_reminder", arguments, "normal")
        worker = await registry.execute_codex("cancel_current_reminder", arguments, "worker")
        self.assertTrue(normal["ok"] and worker["ok"])
        reminder._cancel.assert_awaited_once()

    async def test_handoff_transfers_undrained_delivery_and_does_not_retry_uncertain_failure(self):
        registry = await capabilities.CapabilityRegistry(self.context, self.event).prepare()
        executed = []
        chain = types.SimpleNamespace(chain=[Plain("synthetic pending media")])

        async def pending(event):
            executed.append(True)
            registry._deliveries[asyncio.current_task()] = [chain]
            return {"ok": True}

        registry._register("xhs_login", "synthetic", capabilities._EMPTY, pending)
        await registry.execute("xhs_login", {}, "normal")
        await registry.execute_codex("xhs_login", {}, "codex")
        self.assertEqual(len(executed), 1)
        self.assertEqual(registry.drain_deliveries("normal"), [])
        self.assertEqual(registry.drain_deliveries("codex"), [chain])
        uncertain = AsyncMock(return_value={"ok": False, "code": "tool_failed"})
        registry._register("xhs_logout", "synthetic", capabilities._EMPTY, uncertain)
        await registry.execute("xhs_logout", {}, "normal-failure")
        result = await registry.execute_codex("xhs_logout", {}, "codex-failure")
        self.assertEqual(result["code"], "tool_failed")
        uncertain.assert_awaited_once()

    async def test_concurrent_retry_coalesces_and_call_id_cannot_change_operation(self):
        registry = await capabilities.CapabilityRegistry(self.context, self.event).prepare()
        results = await asyncio.gather(*(registry.execute("travel_test", {"query": "train"}, "same-call") for _ in range(5)))
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(self.plugin.calls, 1)
        changed = await registry.execute("travel_test", {"query": "flight"}, "same-call")
        self.assertEqual(changed["code"], "call_conflict")
        self.assertEqual(self.plugin.calls, 1)

    async def test_permission_deactivation_and_mutated_identity_rechecked(self):
        for mutation in (lambda: setattr(self.plugin, "allowed", False),
                         lambda: setattr(self.metadata, "activated", False),
                         lambda: setattr(self.event, "sender", "bob")):
            self.setUp()
            registry = await capabilities.CapabilityRegistry(self.context, self.event).prepare()
            mutation()
            result = await registry.execute("travel_test", {"query": "train"}, "check")
            self.assertFalse(result["ok"])
            self.assertEqual(self.plugin.calls, 0)

    async def test_model_cannot_choose_context_or_undeclared_arguments(self):
        registry = await capabilities.CapabilityRegistry(self.context, self.event).prepare()
        for arguments in ({"query": "train", "sender": "bob"}, {"query": []}, {}, {"event": self.event}):
            result = await registry.execute("travel_test", arguments, "invalid")
            self.assertFalse(result["ok"])
        self.assertEqual(self.plugin.calls, 0)

    async def test_command_only_reminder_adapters_capture_without_sending(self):
        reminder = Reminder()
        self.context.get_all_stars = lambda: [types.SimpleNamespace(name="reminder", activated=True, star_cls=reminder)]
        self.event = Event("请取消提醒 abcd1234")
        registry = await capabilities.CapabilityRegistry(self.context, self.event).prepare()
        result = await registry.execute("list_current_reminders", {}, "list")
        self.assertEqual(result["messages"], ["own reminders"])
        self.assertEqual(len(registry.drain_deliveries("list")), 1)
        self.assertEqual(registry.drain_deliveries("list"), [])
        denied = await registry.execute("cancel_current_reminder", {"reminder_id": "foreign1"}, "cancel-denied")
        self.assertFalse(denied["ok"])
        result = await registry.execute("cancel_current_reminder", {"reminder_id": "abcd1234"}, "cancel")
        self.assertTrue(result["ok"])
        self.assertEqual(reminder.cancelled, "取消 abcd1234")
        self.assertEqual(self.event.sent, [])

    async def test_cancel_negation_preserves_positive_fenbie_and_tebie(self):
        for phrase, allowed in (("分别取消提醒 abcd1234", True), ("特别取消提醒 abcd1234", True),
                                ("请别取消提醒 abcd1234", False), ("现在不要取消提醒 abcd1234", False),
                                ("例如取消提醒 abcd1234", False), ("如何取消提醒 abcd1234", False)):
            reminder = Reminder()
            context = types.SimpleNamespace(get_all_stars=lambda: [types.SimpleNamespace(name="reminder", activated=True, star_cls=reminder)])
            registry = await capabilities.CapabilityRegistry(context, Event(phrase)).prepare()
            result = await registry.execute("cancel_current_reminder", {"reminder_id": "abcd1234"}, "cancel")
            self.assertEqual(result["ok"], allowed, phrase)

    async def test_explicit_command_task_text_uses_same_owner_and_normalized_request(self):
        source = Event("/codex 请查询班次")
        registry = await capabilities.CapabilityRegistry(self.context, source, task_text="请查询班次").prepare()
        result = await registry.execute("travel_test", {"query": "train"}, "command")
        self.assertTrue(result["ok"])
        self.assertEqual(self.plugin.events[0].get_messages()[0].text, "请查询班次")
        self.assertEqual(self.plugin.events[0].get_sender_id(), source.get_sender_id())
        with self.assertRaises(RuntimeError):
            await self.plugin.events[0].send(None)

    async def test_relative_reminder_keeps_original_time_across_delayed_execution(self):
        spec = importlib.util.spec_from_file_location("timing_reminder_parser", Path(__file__).parents[1] / "reminder" / "reminder_parser.py")
        parser = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(parser)
        module = sys.modules[TimedReminder.__module__]
        for elapsed, expected in ((45, True), (180, False)):
            reminder = TimedReminder()
            context = types.SimpleNamespace(get_all_stars=lambda: [types.SimpleNamespace(name="reminder", activated=True, star_cls=reminder)])
            source = Event("请用Codex两分钟后提醒我喝水")
            received = time.time() - elapsed
            source.message_obj.timestamp = received
            with patch.multiple(module, parse_natural_many=parser.parse_natural_many,
                                ReminderParseError=parser.ReminderParseError, datetime=datetime,
                                LOCAL_TZ=ZoneInfo("Asia/Shanghai"), create=True):
                registry = await capabilities.CapabilityRegistry(context, source).prepare()
                source.message_obj.timestamp = time.time()
                result = await registry.execute("create_current_reminder", {}, "delayed")
            self.assertEqual(result["ok"], expected)
            if expected:
                self.assertAlmostEqual(reminder._reminders[0]["due_at"], received + 120, places=4)
            else:
                self.assertEqual(reminder._reminders, [])

    async def test_recipient_tool_uses_context_schema_without_independent_send(self):
        recipient = types.SimpleNamespace(
            schemas=lambda: [{"type": "function", "function": {"name": "set_current_reply_recipient", "description": "Bind recipient", "parameters": capabilities._EMPTY}}],
            execute=AsyncMock(return_value={"ok": True, "recipient_set": True}))
        registry = await capabilities.CapabilityRegistry(self.context, self.event, recipient=recipient).prepare()
        result = await registry.execute("set_current_reply_recipient", {}, "recipient")
        self.assertTrue(result["recipient_set"])
        recipient.execute.assert_awaited_once_with("set_current_reply_recipient", {})
        self.assertEqual(self.event.sent, [])

    async def test_native_image_result_remains_typed_for_normal_and_json_safe_for_worker(self):
        plugin = XHSPlugin()
        context = types.SimpleNamespace(get_all_stars=lambda: [types.SimpleNamespace(name="xhs_search", activated=True, star_cls=plugin)])
        registry = await capabilities.CapabilityRegistry(context, self.event).prepare()
        tool_module = types.ModuleType("astrbot.core.agent.tool")
        tool_module.FunctionTool = lambda **kwargs: types.SimpleNamespace(**kwargs)
        mcp_types = types.ModuleType("mcp.types")
        mcp_types.CallToolResult = NativeResult
        with patch.dict(sys.modules, {"astrbot.core.agent.tool": tool_module, "mcp.types": mcp_types}):
            tool = registry.function_tools()[0]
            normal = await tool.handler(self.event)
        self.assertIsInstance(normal, NativeResult)
        self.assertEqual(normal.content[1]["type"], "image")
        worker = await registry.execute("xhs_read_notes", {}, "worker-image")
        transported = json.loads(json.dumps(worker))
        self.assertEqual(transported["_mcp_result"]["content"], normal.content)
        self.assertEqual(registry.drain_deliveries("worker-image"), [])
        with patch.object(sys.modules["astrbot.api.message_components"], "Image", NativeImage):
            chains = registry.codex_deliveries("worker-image")
            self.assertEqual(len(chains), 1)
            self.assertEqual(chains[0].chain[1].data, b"synthetic-verified-image")
            self.assertEqual(registry.codex_deliveries("worker-image"), [])
        self.assertEqual(self.event.sent, [])

    async def test_xhs_native_delivery_rechecks_binding_and_does_not_duplicate_normal_attachment(self):
        for mutation in (lambda event: event.set_extra("xhs_search_semantic_authorized", {"owner": "foreign"}),
                         lambda event: event.set_extra("xhs_images_attached", True)):
            plugin = XHSPlugin()
            context = types.SimpleNamespace(get_all_stars=lambda: [types.SimpleNamespace(name="xhs_search", activated=True, star_cls=plugin)])
            source = Event("总结刚才的小红书笔记并带图")
            registry = await capabilities.CapabilityRegistry(context, source).prepare()
            await registry.execute("xhs_read_notes", {}, "image")
            mutation(source)
            self.assertEqual(registry.codex_deliveries("image"), [])


class SnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_received_time_is_captured_once_and_rejects_nonfinite_restore(self):
        source = Event("两分钟后提醒我喝水")
        source.message_obj.timestamp = 1700000000.25
        self.assertEqual(requests.capture_received_at(source, now=1700000001), 1700000000.25)
        source.message_obj.timestamp = 1700000099
        snapshot = requests.snapshot_event(source)
        self.assertEqual(snapshot["received_at"], 1700000000.25)
        with patch.object(sys.modules["astrbot.api.message_components"], "At", At, create=True):
            restored = requests.restore_event(snapshot)
        self.assertEqual(restored.message_obj.timestamp, snapshot["received_at"])
        self.assertEqual(restored.get_extra("current_request_received_at"), snapshot["received_at"])
        for invalid in (float("nan"), float("inf"), -1, True):
            snapshot["received_at"] = invalid
            with self.assertRaises(ValueError): requests.restore_event(snapshot)

    async def test_attachment_only_request_keeps_identity_without_invented_instruction(self):
        source = Event("")
        snapshot = requests.snapshot_event(source)
        with patch.object(sys.modules["astrbot.api.message_components"], "At", At, create=True):
            restored = requests.restore_event(snapshot)
        self.assertEqual(requests.request_text(restored), "")
        self.assertEqual(requests.identity(restored), requests.identity(source))

    async def test_roundtrip_preserves_owner_native_mentions_voice_and_quote(self):
        source = Event("", quoted="quoted task")
        source.set_extra("voice_input_transcribed", True)
        source.set_extra("voice_input_transcript", "给我老婆介绍一下")
        source.set_extra("created_schedule_result", '{"ok":true,"created":[{"id":"abcd"}]}')
        source.message_obj.group_id, source.message_obj.self_id = "group", "bot"
        source.message_obj.raw_message = types.SimpleNamespace(group_openid="group", mentions=[
            types.SimpleNamespace(id="member", is_you=False)])
        source.set_extra("untrusted_owner", "bob")
        snapshot = requests.snapshot_event(source)
        with patch.object(sys.modules["astrbot.api.message_components"], "At", At, create=True), patch.object(
                sys.modules["astrbot.api.message_components"], "Reply", NativeReply):
            restored = requests.restore_event(json.loads(json.dumps(snapshot)))
        self.assertEqual(requests.identity(restored), requests.identity(source))
        self.assertEqual(requests.request_text(restored), "给我老婆介绍一下")
        self.assertEqual([part.qq for part in restored.get_messages() if type(part).__name__ == "At"], ["member"])
        self.assertEqual(next(part.message_str for part in restored.get_messages() if type(part).__name__ == "Reply"), "quoted task")
        self.assertIsNone(restored.get_extra("untrusted_owner"))
        self.assertIn("created", restored.get_extra("created_schedule_result"))
        with self.assertRaises(RuntimeError): await restored.send(None)
        snapshot["sender"] = "bob"
        with self.assertRaises(ValueError): requests.restore_event(snapshot)

    async def test_engine_wrappers_do_not_change_inner_mutation_or_quoted_provenance(self):
        for text in ("/codex 请记住办公室在东侧", "请用 Codex 请记住办公室在东侧", "Codex， 请记住办公室在东侧"):
            self.assertEqual(requests.capability_task_text(Event(text)), "请记住办公室在东侧")
        for text in ('“请用 Codex 请记住别人的事实”', "不要用 Codex 记住这件事", "> /codex 记住引用"):
            self.assertEqual(requests.capability_task_text(Event(text)), text)


if __name__ == "__main__":
    unittest.main()
