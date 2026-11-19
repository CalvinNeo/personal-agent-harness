"""Real AstrBot components with synthetic events and fake service/QQ transports.

Run with the fixed AstrBot dependency environment. No account, model, live
service, production history or external network is used.
"""
from __future__ import annotations

import ast
import base64
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins"))
from PIL import Image as PILImage
from astrbot.api.message_components import Image, Plain, Reply
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from codex_bridge.capabilities import CapabilityRegistry
from xianyu_watch.client import XianyuError
from xianyu_watch.main import Main as Watch

ADMIN = "synthetic-login-admin"
SCANNER = "synthetic-other-group-member"
GROUP = "synthetic-qr-group"
SCOPE = "private_qq_bot:GroupMessage:" + GROUP
OTHER_GROUP = "synthetic-other-authorized-group"
OTHER_SCOPE = "private_qq_bot:GroupMessage:" + OTHER_GROUP
PRIVATE = "private_qq_bot:FriendMessage:" + ADMIN
QR_TEXT = "请打开闲鱼 App，点击右上角“扫一扫”并确认登录。本次扫码会话最多保留213秒。"
CONFIRMING_TEXT = "已收到闲鱼 App 确认，正在完成浏览器授权并验证账号状态。本次扫码会话最多还剩180秒。"
STATUS_QUESTIONS = ("好了么", "好了吗", "现在好了没", "登录好了吗", "成功了么", "好了么？", "那现在登录成功了吗？")


def png():
    stream = io.BytesIO()
    PILImage.new("RGB", (176, 176), (10, 90, 180)).save(stream, format="PNG")
    return stream.getvalue()


PNG = png()


def event(text, *, sender=SCANNER, group=GROUP, parts=None, awake=True, codex=False):
    msg = AstrBotMessage()
    msg.type = MessageType.GROUP_MESSAGE
    msg.message = list(parts) if parts is not None else [Plain(text)]
    msg.message_str = text
    msg.sender = MessageMember(sender)
    msg.group_id = group
    msg.self_id = "synthetic-bot"
    msg.message_id = "synthetic-followup-" + str(id(msg))
    result = AstrMessageEvent(text, msg, PlatformMetadata("qq_official", "private_qq_bot", "private_qq_bot"), group)
    result.is_at_or_wake_command = awake
    result.sent = []
    async def send(chain):
        result.sent.append(chain)
        return True
    result.send = send
    if codex:
        result.set_extra("codex_first_submitted", True)
    return result


def qr_reply():
    return Reply(id="synthetic-bot-qr", sender_id="synthetic-bot", message_str=QR_TEXT,
                 chain=[Plain(QR_TEXT), Image.fromBytes(PNG)])


def status_reply(text=CONFIRMING_TEXT):
    return Reply(id="synthetic-bot-login-status", sender_id="synthetic-bot", message_str=text,
                 chain=[Plain(text)])


def images(chains):
    return [part for chain in chains for part in chain.chain if isinstance(part, Image)]


def texts(chains):
    return "\n".join(part.text for chain in chains for part in chain.chain if isinstance(part, Plain))


def rows(count=2):
    return [{"item_id": str(10001 + i), "title": f"合成测试商品 {i}", "price": f"{35 + 10*i}.00",
             "url": f"https://h5.m.goofish.com/item?id={10001+i}",
             "image_url": f"https://gw.alicdn.com/synthetic/item-{10001+i}.jpg"} for i in range(count)]


class OfflineClient:
    def __init__(self):
        self.qr_calls = 0
        self.status_calls = 0
        self.search_calls = []
        self.image_calls = []
        self.logged_in = True
        self.pending = False
        self.status_error = None
        self.search_error = None
        self.search_result = {"ok": True, "results": rows(), "sort": "newest", "page": 1,
                              "coverage": "仅本次平台返回范围", "observed_at": 1000}
        self.image_data = PNG

    def qrcode(self):
        self.qr_calls += 1
        if self.logged_in:
            raise XianyuError("已登录。", "already_logged_in")
        self.pending = True
        return PNG

    def status(self):
        self.status_calls += 1
        if self.status_error:
            raise XianyuError("状态查询暂时失败。", self.status_error)
        return {"logged_in": self.logged_in, "login_pending": self.pending,
                "state": "logged_in" if self.logged_in else "waiting_for_scan" if self.pending else "not_logged_in",
                **({"error": "login_required"} if not self.logged_in and not self.pending else {})}

    def search(self, query, limit=10):
        self.search_calls.append((query, limit))
        if self.search_error:
            raise XianyuError("搜索服务暂时失败。", self.search_error)
        result = copy.deepcopy(self.search_result)
        if isinstance(result.get("results"), list):
            result["results"] = result["results"][:limit]
        return result

    def download_image(self, url):
        self.image_calls.append(url)
        return self.image_data


class Followups(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.proactive = []
        async def send_message(scope, chain):
            self.proactive.append((scope, chain))
            return True
        self.context = SimpleNamespace(send_message=send_message, get_all_stars=lambda: [])
        self.watch = Watch(self.context, {"allowed_umos": [SCOPE, OTHER_SCOPE, PRIVATE], "allowed_senders": [],
                                         "login_umos": [SCOPE, PRIVATE], "login_senders": [ADMIN],
                                         "shared_login_members": False, "send_product_images": False,
                                         "state_file": str(Path(self.tmp.name) / "watches.json")})
        self.client = OfflineClient()
        self.watch.client = lambda: self.client
        self.metadata = SimpleNamespace(name="xianyu_watch", activated=True, star_cls=self.watch)
        self.context.get_all_stars = lambda: [self.metadata]

    async def asyncTearDown(self):
        await self.watch.terminate()
        self.tmp.cleanup()

    async def registry(self, e):
        registry = CapabilityRegistry(self.context, e)
        await registry.prepare()
        return registry

    async def test_other_member_can_query_status_without_login_authority(self):
        e = event("闲鱼登录状态", codex=True)
        registry = await self.registry(e)
        names = {s["function"]["name"] for s in registry.schemas(codex=True)}
        self.assertIn("xianyu_login_status", names)
        self.assertIn("xianyu_search", names)
        self.assertNotIn("xianyu_login", names)
        status = await registry.execute_codex("xianyu_login_status", {}, "scanner-status")
        self.assertTrue(status.get("logged_in"), status)
        self.assertGreater(self.client.status_calls, 0)
        self.assertEqual(self.client.qr_calls, 0)
        denied = await registry.execute_codex("xianyu_login", {}, "scanner-mutation")
        self.assertFalse(denied.get("ok"), denied)

    async def test_group_login_can_be_confirmed_by_a_different_member(self):
        self.client.logged_in = False
        await self.watch.natural_login(event("闲鱼登录", sender=ADMIN))
        before = self.client.qr_calls
        for text in ("好了", "扫好了", "已扫码", *STATUS_QUESTIONS):
            e = event(text)
            await self.watch.natural_login(e)
            self.assertTrue(e.sent, text)
            self.assertFalse(images(e.sent), text)
        self.assertEqual(self.client.qr_calls, before)
        self.assertGreaterEqual(self.client.status_calls, 3 + len(STATUS_QUESTIONS))

    async def test_quoted_confirming_status_routes_completion_questions_after_restart(self):
        for text in STATUS_QUESTIONS:
            self.watch._pending_login.clear()
            self.watch._pending_login_scopes.clear()
            self.watch._recent_login_scopes.clear()
            e = event(text, parts=[status_reply(), Plain(text)])
            before = self.client.status_calls
            await self.watch.natural_login(e)
            self.assertTrue(e.sent, text)
            self.assertIn("闲鱼已登录", texts(e.sent), text)
            self.assertEqual(self.client.status_calls, before + 1, text)
            self.assertEqual(self.client.qr_calls, 0, text)
            self.assertFalse(images(e.sent), text)

    async def test_recent_status_reply_keeps_followup_context_in_the_same_scope(self):
        await self.watch.natural_login(event("闲鱼登录状态", sender=ADMIN))
        self.watch._pending_login.clear()
        self.watch._pending_login_scopes.clear()
        before = self.client.status_calls
        e = event("好了么")
        await self.watch.natural_login(e)
        self.assertTrue(e.sent)
        self.assertEqual(self.client.status_calls, before + 1)
        self.assertEqual(self.client.qr_calls, 0)

    async def test_expired_recent_login_context_does_not_capture_a_bare_question(self):
        await self.watch.natural_login(event("闲鱼登录状态", sender=ADMIN))
        self.watch._recent_login_scopes[SCOPE] = time.monotonic() - 1
        before = self.client.status_calls
        e = event("好了么")
        await self.watch.natural_login(e)
        self.assertEqual(e.sent, [])
        self.assertEqual(self.client.status_calls, before)

    async def test_other_questions_do_not_become_login_status_with_recent_context(self):
        await self.watch.natural_login(event("闲鱼登录状态", sender=ADMIN))
        before = self.client.status_calls
        for text in ("商品好了吗", "现在视频能看了吗", "怎么登录", "好了么，顺便搜索一下商品"):
            e = event(text)
            await self.watch.natural_login(e)
            self.assertEqual(e.sent, [], text)
        self.assertEqual(self.client.status_calls, before)

    async def test_exact_quoted_ready_request_checks_status_without_image_task(self):
        text = "现在已经登陆了，请检查登录态是否 ready"
        e = event(text, parts=[qr_reply(), Plain(text)])
        await self.watch.natural_login(e)
        self.assertTrue(e.sent)
        self.assertGreater(self.client.status_calls, 0)
        self.assertEqual(self.client.qr_calls, 0)
        self.assertFalse(images(e.sent))

    async def test_quoted_qr_recovers_scanned_followup_after_plugin_restart(self):
        e = event("扫好了", parts=[qr_reply(), Plain("扫好了")])
        await self.watch.natural_login(e)
        self.assertTrue(e.sent)
        self.assertGreater(self.client.status_calls, 0)
        self.assertEqual(self.client.qr_calls, 0)

    async def test_bare_followup_in_another_scope_does_not_steal_other_login(self):
        self.client.logged_in = False
        await self.watch.natural_login(event("闲鱼登录", sender=ADMIN))
        before = self.client.status_calls
        for value in ("好了", "扫好了", "已扫码", *STATUS_QUESTIONS):
            e = event(value, group=OTHER_GROUP)
            await self.watch.natural_login(e)
            self.assertEqual(e.sent, [])
        self.assertEqual(self.client.status_calls, before)

    async def test_unknown_bare_followup_does_not_claim_a_login(self):
        for value in ("好了", "扫好了", "已扫码", *STATUS_QUESTIONS):
            e = event(value)
            await self.watch.natural_login(e)
            self.assertEqual(e.sent, [])
        self.assertEqual(self.client.status_calls, 0)

    async def test_quoted_instructions_alone_do_not_start_login(self):
        e = event("看一下引用", parts=[qr_reply(), Plain("看一下引用")])
        await self.watch.natural_login(e)
        self.assertEqual(self.client.qr_calls, 0)
        self.assertFalse(images(e.sent))

    async def test_status_service_failure_does_not_invent_an_expired_login(self):
        self.client.status_error = "status_unavailable"
        e = event("现在已经登陆了，请检查登录态是否 ready", parts=[qr_reply(), Plain("现在已经登陆了，请检查登录态是否 ready")])
        await self.watch.natural_login(e)
        self.assertTrue(e.sent)
        self.assertEqual(self.client.qr_calls, 0)
        self.assertNotIn("已过期", texts(e.sent))
        self.assertNotIn("重新扫码", texts(e.sent))

    async def test_direct_search_returns_products_without_product_images(self):
        self.client.search_result["results"] = rows(5)
        e = event("请在闲鱼搜索合成积木套件，看看便宜的", codex=True)
        registry = await self.registry(e)
        result = await registry.execute_codex("xianyu_search", {"query": "合成积木套件", "limit": 5}, "real-query")
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(self.client.search_calls, [("合成积木套件", 5)])
        self.assertEqual([(r["title"], r["price"], r["url"]) for r in result["results"]],
                         [(r["title"], r["price"], r["url"]) for r in rows(5)])
        encoded = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(base64.b64encode(PNG).decode(), encoded)
        self.assertNotIn("base64://", encoded)
        self.assertEqual(e.sent, [], "Codex images must wait for the original-chat broker")
        deliveries = registry.codex_deliveries("real-query")
        self.assertEqual(images(deliveries), [])
        self.assertEqual(self.client.image_calls, [])
        self.assertEqual(result.get("image_count"), 0)
        self.assertTrue(all("image_url" not in row for row in result["results"]))
        self.assertEqual(registry.codex_deliveries("real-query"), [])

    async def test_login_failure_is_not_reported_as_zero_products(self):
        self.client.logged_in = False
        self.client.search_result = {"ok": False, "error": "login_required"}
        registry = await self.registry(event("闲鱼搜索合成积木套件", codex=True))
        result = await registry.execute_codex("xianyu_search", {"query": "合成积木套件"}, "no-login")
        self.assertFalse(result.get("ok"), result)
        self.assertIn("login_required", {result.get("code"), result.get("error")})
        self.assertFalse(result.get("results"))
        self.assertFalse(images(registry.codex_deliveries("no-login")))

    async def test_search_service_failure_is_not_an_empty_success(self):
        self.client.search_error = "service_error"
        registry = await self.registry(event("闲鱼搜索合成积木套件", codex=True))
        result = await registry.execute_codex("xianyu_search", {"query": "合成积木套件"}, "query-error")
        self.assertFalse(result.get("ok"), result)
        self.assertFalse(result.get("results"))
        self.assertFalse(images(registry.codex_deliveries("query-error")))

    async def test_disabled_images_do_not_require_the_image_service(self):
        self.client.image_data = None
        registry = await self.registry(event("闲鱼搜索合成积木套件", codex=True))
        result = await registry.execute_codex("xianyu_search", {"query": "合成积木套件"}, "no-product-image")
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(len(result["results"]), 2)
        self.assertFalse(images(registry.codex_deliveries("no-product-image")))
        self.assertEqual(self.client.image_calls, [])

    async def test_search_cannot_override_the_origin_identity(self):
        registry = await self.registry(event("闲鱼搜索合成积木套件", codex=True))
        result = await registry.execute_codex("xianyu_search", {"query": "合成积木套件", "umo": OTHER_SCOPE}, "identity-override")
        self.assertFalse(result.get("ok"), result)
        self.assertEqual(result.get("code"), "invalid_arguments")
        self.assertEqual(self.client.search_calls, [])

    async def test_unauthorized_group_cannot_get_search_or_status_tools(self):
        registry = await self.registry(event("闲鱼登录状态", group="unknown-group", codex=True))
        names = {s["function"]["name"] for s in registry.schemas(codex=True)}
        self.assertFalse({"xianyu_login", "xianyu_login_status", "xianyu_search"} & names)


class RoutingOrder(unittest.TestCase):
    def test_login_status_handler_precedes_codex_direct_routing(self):
        module = ast.parse((ROOT / "plugins/xianyu_watch/main.py").read_text())
        main = next(node for node in module.body if isinstance(node, ast.ClassDef) and node.name == "Main")
        handler = next(node for node in main.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "natural_login")
        priorities = [keyword.value.value for decorator in handler.decorator_list if isinstance(decorator, ast.Call)
                      for keyword in decorator.keywords if keyword.arg == "priority" and isinstance(keyword.value, ast.Constant)]
        self.assertTrue(priorities, "The direct login/status handler must declare its ordering")
        self.assertTrue(all(priority > 140 for priority in priorities), priorities)



class SharedLogin(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await Followups.asyncSetUp(self)
        self.watch.cfg['shared_login_members'] = True
        self.client.logged_in = False

    async def asyncTearDown(self):
        await Followups.asyncTearDown(self)

    registry = Followups.registry

    async def test_other_member_can_login_in_shared_mode(self):
        source = event('闲鱼登录', sender=SCANNER)
        self.assertTrue(self.watch._login_allowed(source))
        await self.watch.natural_login(source)
        self.assertTrue(images(source.sent))
        self.assertEqual(self.client.qr_calls, 1)
        self.assertEqual(self.client.image_calls, [])

    async def test_codex_shared_member_gets_login_and_status_tools(self):
        source = event('咸鱼登录', sender=SCANNER, codex=True)
        registry = await self.registry(source)
        names = {item['function']['name'] for item in registry.schemas(codex=True)}
        self.assertTrue({'xianyu_login', 'xianyu_login_status', 'xianyu_search'} <= names)
        result = await registry.execute_codex('xianyu_login', {}, 'shared-login')
        self.assertTrue(result['ok'])
        self.assertTrue(images(registry.codex_deliveries('shared-login')))

    async def test_scope_allowlist_still_applies(self):
        source = event('闲鱼登录', sender=SCANNER, group='not-allowed')
        self.assertFalse(self.watch._login_allowed(source))
        await self.watch.natural_login(source)
        self.assertFalse(images(source.sent))
        self.assertEqual(self.client.qr_calls, 0)


class ProductImagePolicy(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await Followups.asyncSetUp(self)

    async def asyncTearDown(self):
        await Followups.asyncTearDown(self)

    async def test_pending_alert_sends_text_without_fetching_product_image(self):
        source = event('监控合成积木套件')
        self.client.search_result['results'] = rows(1)
        created = await self.watch._create(source, ['合成积木套件'])
        self.assertTrue(created['ok'], created)
        self.assertEqual(self.proactive, [])
        watch_id = created['watch']['id']
        self.client.search_result['results'] = rows(2)
        await self.watch._run(watch_id)
        self.assertEqual(len(self.proactive), 1)
        scope, message = self.proactive[0]
        self.assertEqual(scope, SCOPE)
        self.assertFalse(images([message]))
        self.assertIn('合成测试商品 1', texts([message]))
        self.assertIn('https://h5.m.goofish.com/item?id=10002', texts([message]))
        self.assertEqual(self.client.image_calls, [])
        self.assertEqual(self.watch.records[watch_id]['pending_alerts'], [])



class SharedMonitorTools(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await Followups.asyncSetUp(self)

    async def asyncTearDown(self):
        await Followups.asyncTearDown(self)

    registry = Followups.registry

    async def create_monitor(self):
        source = event('按预算监控合成积木套件', sender=ADMIN, codex=True)
        registry = await self.registry(source)
        created = await registry.execute_codex('create_xianyu_watch',
            {'keywords': ['合成积木套件'], 'interval_minutes': 45, 'max_price': 40}, 'create-priced-monitor')
        self.assertTrue(created['ok'], created)
        return created['watch']['id']

    async def test_shared_tools_keep_budget_pagination_and_original_delivery_scope(self):
        watch_id = await self.create_monitor()
        original = copy.deepcopy(self.watch.records[watch_id])
        peer = event('查看和调整已有监控预算', sender=SCANNER, group=OTHER_GROUP, codex=True)
        registry = await self.registry(peer)
        listed = await registry.execute_codex('list_xianyu_watches', {}, 'shared-monitor-list')
        self.assertIn(watch_id, [item['id'] for item in listed['watches']])
        first = await registry.execute_codex('read_xianyu_watch', {'watch_id': watch_id, 'offset': 0, 'limit': 1}, 'shared-monitor-read')
        self.assertTrue(first['ok'], first)
        self.assertEqual(first['baseline']['total'], 2)
        self.assertEqual(first['baseline']['known_matching_count'], 1)
        self.assertTrue(first['baseline']['has_more'])
        self.assertEqual(len(first['baseline']['items']), 1)
        changed = await registry.execute_codex('manage_xianyu_watch', {'watch_id': watch_id, 'action': 'update', 'max_price': 30}, 'shared-monitor-update')
        self.assertTrue(changed['ok'], changed)
        record = self.watch.records[watch_id]
        self.assertEqual(record['max_price'], 30)
        for field in ('umo', 'sender', 'seen_ids', 'baseline_snapshots', 'next_run_at'):
            self.assertEqual(record[field], original[field], field)
        self.assertEqual(self.proactive, [])
        self.assertEqual(self.client.image_calls, [])
        self.assertEqual(registry.codex_deliveries('shared-monitor-read'), [])
        cleared = await registry.execute_codex('manage_xianyu_watch', {'watch_id': watch_id, 'action': 'update', 'max_price': None}, 'shared-monitor-clear-price')
        self.assertTrue(cleared['ok'], cleared)
        self.assertIsNone(self.watch.records[watch_id]['max_price'])
        self.assertEqual(self.watch.records[watch_id]['next_run_at'], original['next_run_at'])

    async def test_legacy_refresh_through_codex_only_backfills_original_ids(self):
        watch_id = await self.create_monitor()
        stored = json.loads(self.watch.file.read_text())
        record = stored['watches'][0]
        for field in list(record):
            if field.startswith('baseline') and field != 'baseline_ready':
                record.pop(field)
        self.watch.file.write_text(json.dumps(stored))
        self.watch._load()
        original = copy.deepcopy(self.watch.records[watch_id])
        self.client.search_result['results'] = [rows(1)[0], {**rows(1)[0], 'item_id': '19999', 'url': 'https://h5.m.goofish.com/item?id=19999'}]
        peer = event('补回旧监控详情', sender=SCANNER, group=OTHER_GROUP, codex=True)
        registry = await self.registry(peer)
        refreshed = await registry.execute_codex('read_xianyu_watch', {'watch_id': watch_id, 'refresh_metadata': True}, 'legacy-monitor-refresh')
        self.assertTrue(refreshed['ok'], refreshed)
        self.assertEqual(refreshed['metadata_refresh']['matched_count'], 1)
        self.assertEqual(refreshed['baseline']['details_unavailable_count'], 1)
        self.assertTrue(refreshed['baseline']['legacy'])
        found = next(item for item in refreshed['baseline']['items'] if item['details_available'])
        self.assertEqual(found['source'], 'recovered_current_query')
        self.assertIsNotNone(found['recovered_at'])
        for field in ('seen_ids', 'pending_alerts', 'next_run_at', 'last_run'):
            self.assertEqual(self.watch.records[watch_id][field], original[field], field)
        self.assertEqual(self.proactive, [])
        self.assertEqual(self.client.image_calls, [])

    async def test_budget_applies_to_scheduled_alerts_and_rejects_equal_unknown_prices(self):
        self.client.search_result['results'] = rows(1)
        watch_id = await self.create_monitor()
        first = rows(1)[0]
        self.client.search_result['results'] = [first,
            {**first, 'item_id': '10002', 'title': '合成低于预算商品', 'price': '39.99'},
            {**first, 'item_id': '10003', 'price': '40'},
            {**first, 'item_id': '10004', 'price': '价格面议'}]
        await self.watch._run(watch_id)
        self.assertEqual(len(self.proactive), 1)
        scope, message = self.proactive[0]
        self.assertEqual(scope, SCOPE)
        self.assertIn('39.99', texts([message]))
        self.assertFalse(images([message]))
        self.assertTrue({'10003', '10004'} <= set(self.watch.records[watch_id]['seen_ids']))
        self.assertEqual(self.client.image_calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
