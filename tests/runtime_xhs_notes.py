import asyncio
import base64
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from PIL import Image as PillowImage
from astrbot.api.message_components import Image, Plain, Reply
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
from astrbot.core.message.message_event_result import MessageEventResult, ResultContentType
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.platform.sources.qqofficial.qqofficial_message_event import QQOfficialMessageEvent
from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.utils import media_utils

sys.path.insert(0, os.environ.get("XHS_PLUGIN_ROOT", str(Path(__file__).resolve().parents[1] / "plugins")))
from codex_bridge.main import Main as Codex
from xhs_search.main import Main as XHS
from xhs_search.notes import validate_image


CHECKS = []


def event(text, *, sender="synthetic-owner", group="synthetic-group", reply=""):
    message = AstrBotMessage()
    message.type = MessageType.GROUP_MESSAGE
    message.message = [Plain(text)] + ([Reply(id="synthetic-bot-reply", message_str=reply)] if reply else [])
    message.message_str = text
    message.sender = MessageMember(sender)
    message.group_id = group
    message.self_id = "synthetic-bot"
    message.message_id = "synthetic-event"
    source = AstrMessageEvent(text, message, PlatformMetadata("qq_official", "test", "test"), group)
    source.is_at_or_wake_command = True
    source.send = AsyncMock()
    return source


async def invoke(tool, source, **arguments):
    values = []
    async for value in FunctionToolExecutor.execute(tool, ContextWrapper(SimpleNamespace(event=source)), **arguments):
        if value is not None:
            values.append(value)
    assert len(values) == 1
    text = next(item.text for item in values[0].content if item.type == "text")
    return json.loads(text), values[0]


def passed(text):
    CHECKS.append(text)
    print("PASS " + text, flush=True)


async def main():
    quoted = "小红书搜索：三个好玩的桌游\n1. 原帖\nhttps://www.xiaohongshu.com/explore/synthetic-note"
    source = event("直接给我总结一下玩法，要带图", reply=quoted)
    context = SimpleNamespace(send_message=AsyncMock(), get_all_stars=Mock(return_value=[]))
    plugin = XHS(context, {"allowed_umos": [source.unified_msg_origin]})
    codex = Codex(context, {"allowed_umos": [source.unified_msg_origin]})
    codex._rpc = AsyncMock()
    note = {"note_id": "synthetic-note", "title": "真实来源合成笔记", "body": "合成正文：七连翻、璀璨宝石；人数需要查规则。",
            "url": "https://www.xiaohongshu.com/explore/synthetic-note", "author": "合成作者", "image_count": 1,
            "image_urls": ["https://sns-webpic-qc.xhscdn.com/synthetic.png"], "kind": "normal", "body_truncated": False}
    calls = []
    async def upstream(method, *arguments, **keywords):
        calls.append((method, arguments, keywords))
        if method == "search_bundle":
            return {"results": [{"title": note["title"], "url": note["url"], "snippet": "合成作者"}],
                    "references": {"synthetic-note": {"feed_id": "synthetic-note", "xsec_token": "synthetic-private-access"}}}
        if method == "note_detail":
            return dict(note)
        raise AssertionError("unexpected operation")
    output = io.BytesIO()
    PillowImage.new("RGB", (32, 20), "#2299dd").save(output, format="PNG")
    image = validate_image(output.getvalue())
    plugin._call = AsyncMock(side_effect=upstream)
    await plugin.natural_search(source)
    assert not source.is_stopped() and source.get_extra("xhs_notes_request")
    await codex.natural_status(source)
    request = ProviderRequest(prompt=source.message_str)
    await plugin.provide_search_tool(source, request)
    await codex.provide_delegation_tool(source, request)
    assert request.func_tool.get_tool("xhs_read_notes") and not request.func_tool.get_tool("delegate_current_task_to_codex")
    codex._rpc.assert_not_awaited()
    passed("historical quoted summary uses local notes tool instead of search-only or Codex")

    with patch("xhs_search.main.download_image", new=AsyncMock(return_value=image)) as downloader:
        prepared, _ = await invoke(request.func_tool.get_tool("xhs_read_notes"), source)
        assert prepared["status"] == "references_ready" and not prepared["notes"] and prepared["image_count"] == 0
        assert [item[0] for item in calls] == ["search_bundle"]
        passed("uncached historical quote refreshes references only; body read continues in a separate bounded tool call")
        result, structured = await invoke(request.func_tool.get_tool("xhs_read_notes"), source, **prepared["next_arguments"])
        assert result["ok"] and result["notes"][0]["body"] == note["body"]
        assert len([item for item in structured.content if item.type == "image"]) == 1
        assert "synthetic-private-access" not in structured.model_dump_json()
        first_calls = len(calls)
        repeated, duplicate = await invoke(request.func_tool.get_tool("xhs_read_notes"), source)
        assert len(calls) == first_calls and repeated["image_count"] == 1
        downloader.assert_awaited_once()
    passed("real FunctionToolExecutor returns source body and image content; no access token; same event idempotent")

    source.set_result(MessageEventResult(chain=[Plain("玩法摘要\n![伪图片](https://cf.geekdo-images.com/fake/filters:strip_icc()/pic.jpg)")])
                      .set_result_content_type(ResultContentType.LLM_RESULT))
    await plugin.attach_note_images(source)
    result = source.get_result()
    assert len([part for part in result.chain if isinstance(part, Image)]) == 1
    assert all("cf.geekdo" not in part.text and "![" not in part.text for part in result.chain if isinstance(part, Plain))
    assert result.use_markdown_ is False and result.use_t2i_ is False
    chunks = QQOfficialMessageEvent._split_message_chain_by_media(result)
    with tempfile.TemporaryDirectory(prefix="qq-runtime-", dir=Path(os.environ["XHS_REPORT"]).parent) as directory:
        with patch.object(media_utils, "get_astrbot_temp_path", return_value=Path(directory)):
            parsed = await QQOfficialMessageEvent._parse_to_qqofficial(chunks[0])
        assert not list(Path(directory).iterdir())
    assert base64.b64decode(parsed[1], validate=True) == image.data
    assert "真实来源合成笔记" in parsed[0]
    await plugin.attach_note_images(source)
    assert len([part for part in result.chain if isinstance(part, Image)]) == 1
    passed("real QQ parser encodes verified image bytes; fabricated parenthesized Markdown removed; output hook idempotent")

    foreign = event("帮我总结刚才的笔记，要带图", sender="synthetic-other")
    foreign_request = ProviderRequest(prompt=foreign.message_str)
    await plugin.provide_search_tool(foreign, foreign_request)
    denied, _ = await invoke(foreign_request.func_tool.get_tool("xhs_read_notes"), foreign)
    assert not denied["ok"]
    copied = event(source.message_str, reply=quoted)
    copied.set_extra("xhs_search_semantic_authorized", source.get_extra("xhs_search_semantic_authorized"))
    denied, _ = await invoke(request.func_tool.get_tool("xhs_read_notes"), copied)
    assert not denied["ok"]
    source.send.assert_not_awaited()
    context.send_message.assert_not_awaited()
    passed("cached references bound to sender and conversation; copied event cannot reuse authorization; zero QQ sends")

    following = event("继续总结玩法，要带图")
    following_request = ProviderRequest(prompt=following.message_str)
    await plugin.provide_search_tool(following, following_request)
    with patch("xhs_search.main.download_image", new=AsyncMock(side_effect=__import__("xhs_search.core", fromlist=["XHSError"]).XHSError("bad", code="image_invalid"))):
        following_result, following_content = await invoke(following_request.func_tool.get_tool("xhs_read_notes"), following)
    assert following_result["ok"] and following_result["image_count"] == 0 and following_result["notes"][0]["images_unavailable"] == 1
    assert not any(item.type == "image" for item in following_content.content)
    passed("bad image does not become a fake attachment; valid note body remains available")

    plain = event("这句引用是什么意思", reply=quoted)
    plain_request = ProviderRequest(prompt=plain.message_str)
    await plugin.provide_search_tool(plain, plain_request)
    denied, _ = await invoke(plain_request.func_tool.get_tool("xhs_read_notes"), plain)
    assert not denied["ok"]
    for text in ("例如帮我总结小红书", "> 总结小红书", "不要总结小红书", "开发小红书插件"):
        assert not plugin._notes_requested(event(text))
    passed("quote-only, example, negative and engineering messages do not authorize note reads")

    budget_event = event("总结小红书笔记，要带图")
    budget_plugin = XHS(context, {"allowed_umos": [budget_event.unified_msg_origin]})
    budget_plugin._note_references.put(budget_plugin._event_identity(budget_event), "桌游", {
        "synthetic-note": {"feed_id": "synthetic-note", "xsec_token": "synthetic-private-access"},
    })
    budget_plugin._call = AsyncMock(return_value={**note, "image_urls": []})
    budget_request = ProviderRequest(prompt=budget_event.message_str)
    await budget_plugin.provide_search_tool(budget_event, budget_request)
    plugin_module = sys.modules[XHS.__module__]
    original_wait_for = asyncio.wait_for
    observed = []

    async def budget_wait(awaitable, *, timeout):
        observed.append(timeout)
        return await original_wait_for(awaitable, timeout=timeout)

    with patch.object(plugin_module, "time", SimpleNamespace(monotonic=Mock(side_effect=[100, 134]))), patch.object(
        plugin_module.asyncio, "wait_for", budget_wait
    ):
        budget_result, _ = await invoke(budget_request.func_tool.get_tool("xhs_read_notes"), budget_event)
    assert budget_result["ok"] and 66 in observed and max(observed) <= 120
    budget_event.send.assert_not_awaited()
    passed("slow note read uses 66 seconds remaining after 34-second search; total 105-second budget remains bounded")

    target = Path(os.environ["XHS_REPORT"])
    report = {"ok": True, "real_framework": True, "provider_network": False, "qq_messages_sent": 0, "checks": CHECKS, "count": len(CHECKS)}
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    target.chmod(0o600)
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
