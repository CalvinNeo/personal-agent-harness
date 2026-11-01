import asyncio
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from astrbot.api.message_components import Plain, Reply
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import ToolSet
from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.star.star_handler import star_handlers_registry
from astrbot.core.star.filter.command import CommandFilter, GreedyStr

sys.path.insert(0, os.environ.get("TRAVEL_PLUGIN_ROOT", str(Path(__file__).resolve().parents[1] / "plugins")))
from codex_bridge.main import Main as Codex
from intent_router.main import Main as IntentRouter
from travel_assistant.core import TZ
from travel_assistant.main import Main as Travel, TOOLS, format_tickets


CHECKS = []


def event(text, *, sender="synthetic-owner", group="synthetic-group", private=False, message_id="synthetic-event", reply=None):
    message = AstrBotMessage()
    message.type = MessageType.FRIEND_MESSAGE if private else MessageType.GROUP_MESSAGE
    message.message = [Plain(text)] + ([reply] if reply else [])
    message.message_str = text
    message.sender = MessageMember(sender)
    message.group_id = "" if private else group
    message.self_id = "synthetic-bot"
    message.message_id = message_id
    source = AstrMessageEvent(text, message, PlatformMetadata("qq_official", "test", "test"), sender if private else group)
    source.is_at_or_wake_command = True
    source.send = AsyncMock()
    return source


def passed(name):
    CHECKS.append(name)
    print("PASS " + name, flush=True)


async def invoke(tool, source, **kwargs):
    values = []
    async for value in FunctionToolExecutor.execute(tool, ContextWrapper(SimpleNamespace(event=source)), **kwargs):
        if value is not None:
            values.extend(part.text for part in value.content if part.type == "text")
    assert len(values) == 1, "unexpected framework tool result"
    return json.loads(values[0])


async def main():
    with tempfile.TemporaryDirectory(prefix="runtime-", dir=os.environ["TRAVEL_STATE_ROOT"]) as directory:
        root = Path(directory)
        group_umo = event("").unified_msg_origin
        private_umo = event("", private=True).unified_msg_origin
        context = SimpleNamespace(send_message=AsyncMock(), get_all_stars=Mock(return_value=[]))
        travel = Travel(context, {"allowed_umos": [group_umo, private_umo], "data_dir": str(root / "travel")})
        codex = Codex(context, {"allowed_umos": [group_umo, private_umo]})
        codex.routes_file = root / "codex-jobs.json"
        codex._rpc = AsyncMock(return_value={"ok": False, "error": "synthetic-only"})
        tomorrow = (datetime.now(TZ).date() + timedelta(days=1)).isoformat()
        observed = datetime.now(TZ).isoformat(timespec="seconds")
        offer = {"kind": "train", "id": "synthetic-G1", "service": "G1", "origin": "上海虹桥", "destination": "北京南",
                 "departure_date": tomorrow, "departure_time": "09:00", "arrival_date": tomorrow, "arrival_time": "14:00",
                 "duration_minutes": 300, "highspeed": True, "seat_type": "二等座", "available": True,
                 "available_count": 2, "price_cny": 550.0, "observed_at": observed}
        metadata = {"provider": "synthetic-12306", "source": "https://kyfw.12306.cn/otn/leftTicket/init", "observed_at": observed, "notices": []}
        travel.rail.search = AsyncMock(return_value=([offer, {**offer, "id": "synthetic-unknown", "price_cny": None}], metadata))
        try:
            source = event("帮我找明天上海虹桥到北京南高铁，700以内、八点以后出发，优先耗时短")
            request = ProviderRequest(prompt=source.message_str)
            await travel.reserve_travel_request(source)
            assert source.get_extra("travel_local_request")
            await codex.natural_status(source)
            assert not source.is_stopped()
            codex._rpc.assert_not_awaited()
            await travel.provide_tools(source, request)
            await codex.provide_delegation_tool(source, request)
            assert request.func_tool.get_tool("delegate_current_task_to_codex") is None
            assert set(tool.name for tool in request.func_tool.tools) == set(TOOLS)
            await codex.describe_available_capabilities(source, request)
            assert "travel_search_tickets" in request.system_prompt and "当前上海时区时间" in request.system_prompt
            result = await invoke(request.func_tool.get_tool("travel_search_tickets"), source,
                                  kind="train", origin="上海虹桥", destination="北京南", departure_date=tomorrow,
                                  criteria={"max_price": 700, "depart_after": "08:00"}, sort="duration")
            assert result["ok"] and result["matching_count"] == 1 and result["unverified_count"] == 1
            assert result["needs_verification"][0]["unverified_requirements"] == ["max_price"]
            assert "待核实" in format_tickets(result)
            identifier = result["result_set_id"]
            passed("real FunctionToolExecutor invokes tickets; travel does not submit Codex; unknown price is not matching")

            travel.rail.search.reset_mock()
            result = await invoke(request.func_tool.get_tool("travel_search_tickets"), source,
                                  kind="train", origin="上海虹桥", destination="北京南", departure_date="")
            assert not result["ok"] and result["code"] == "invalid_date"
            travel.rail.search.assert_not_awaited()
            passed("missing departure date is rejected before any provider network call")

            followup = event("再筛一下，只要600以内", message_id="followup-one")
            followup_request = ProviderRequest(prompt=followup.message_str)
            await travel.reserve_travel_request(followup)
            assert followup.get_extra("travel_local_request")
            await codex.natural_status(followup)
            codex._rpc.assert_not_awaited()
            await travel.provide_tools(followup, followup_request)
            result = await invoke(followup_request.func_tool.get_tool("travel_filter_tickets"), followup,
                                  criteria={"max_price": 600}, result_set_id=identifier)
            assert result["ok"] and result["cached"] and result["metadata"]["observed_at"] == observed
            assert result["query"]["criteria"]["depart_after"] == "08:00"
            assert result["sort"] == "duration"
            later = event("还要下午六点前到", message_id="followup-two")
            later_request = ProviderRequest(prompt=later.message_str)
            await travel.provide_tools(later, later_request)
            result = await invoke(later_request.func_tool.get_tool("travel_filter_tickets"), later, criteria={"arrive_before": "18:00"})
            assert result["ok"] and result["query"]["criteria"]["max_price"] == 600
            result = await invoke(later_request.func_tool.get_tool("travel_filter_tickets"), later, criteria={"seat_types": ["一等座"]})
            assert not result["ok"] and result["code"] == "new_search_required"
            travel.rail.search.assert_not_awaited()
            passed("two follow-up filters accumulate; original observation time retained; seat changes require new search")

            foreign = event("再筛一下高铁", sender="synthetic-other", message_id="foreign")
            foreign_request = ProviderRequest(prompt=foreign.message_str)
            await travel.provide_tools(foreign, foreign_request)
            result = await invoke(foreign_request.func_tool.get_tool("travel_filter_tickets"), foreign,
                                  criteria={"max_price": 600}, result_set_id=identifier)
            assert not result["ok"] and result["code"] == "unknown_result_set"
            forged = event(source.message_str, message_id="forged")
            forged.set_extra("travel_authorized", source.get_extra("travel_authorized"))
            result = await invoke(request.func_tool.get_tool("travel_search_tickets"), forged,
                                  kind="train", origin="上海虹桥", destination="北京南", departure_date=tomorrow)
            assert result["code"] == "unauthorized"
            passed("foreign sender cannot read results; copied authorization cannot cross event identity")

            shared = ToolSet([])
            fresh = event("南京南站怎么去禄口机场", message_id="map")
            fresh_request = ProviderRequest(prompt=fresh.message_str, func_tool=shared)
            await travel.provide_tools(fresh, fresh_request)
            assert shared.tools == [] and fresh_request.func_tool is not shared
            result = await invoke(fresh_request.func_tool.get_tool("travel_map"), fresh,
                                  action="route", origin="南京南站", destination="南京禄口机场", city="南京", mode="transit")
            assert result["ok"] and result["live_route_metrics"] is False and "当前位置" in result["notice"]
            result = await invoke(fresh_request.func_tool.get_tool("travel_capabilities"), fresh)
            assert result["ok"] and not result["purchasing"] and "入口" in result["maps"]
            passed("map tool produces official links without invented metrics; capability list honest; shared ToolSet unchanged")

            for denied in (event("查机票", group="synthetic-denied"), event("查机票"), event("> 帮我查询机票"), event("例如帮我查机票")):
                if denied.message_str == "查机票" and denied.message_obj.group_id == "synthetic-group":
                    denied.is_at_or_wake_command = False
                denied_request = ProviderRequest(prompt=denied.message_str)
                await travel.provide_tools(denied, denied_request)
                assert denied_request.func_tool is None
            private = event("查询明天上海到北京机票", private=True)
            private.is_at_or_wake_command = False
            private_request = ProviderRequest(prompt=private.message_str)
            await travel.provide_tools(private, private_request)
            assert private_request.func_tool.get_tool("travel_search_tickets") is not None
            passed("unapproved group, unaddressed group and quoted instructions denied; approved private chat works")

            command = event("/travel train " + tomorrow + " 上海虹桥 北京南 max_price=700", message_id="command")
            await travel.travel_command(command, "train " + tomorrow + " 上海虹桥 北京南 max_price=700")
            assert command.is_stopped() and command.send.await_count == 1
            text = command.send.call_args.args[0].chain[0].text
            assert "synthetic-12306" in text and "¥550" in text
            passed("explicit travel command returns deterministic ticket summary without LLM or real QQ send")

            router = IntentRouter(context, {"allowed_umos": [group_umo], "mode": "light", "log_route_details": False})
            for route in ("travel_search", "map_route"):
                routed = event("查明天高铁" if route == "travel_search" else "导航到机场", message_id=route)
                payload = {"route": route, "confidence": 0.99, "task": routed.message_str, "mode": "read-only", "missing_context": []}
                with patch.object(router, "_resolve_provider", new=AsyncMock(return_value=SimpleNamespace(text_chat=AsyncMock(return_value=SimpleNamespace(completion_text=json.dumps(payload)))))):
                    await router.classify_group_message(routed)
                assert routed.get_extra("intent_router_route") == route
                await travel.reserve_travel_request(routed)
                await codex.natural_status(routed)
                codex._rpc.assert_not_awaited()
            passed("real planner route validation accepts travel_search and map_route; natural Codex handler stays idle")

            framework_metadata = list(star_handlers_registry)
            command_metadata = next(item for item in framework_metadata if item.handler_name == "travel_command")
            parser = CommandFilter("travel", alias={"出行"}, handler_md=command_metadata)
            assert parser.handler_params == {"body": GreedyStr}
            assert parser.validate_and_convert_params([], parser.handler_params) == {"body": ""}
            assert parser.validate_and_convert_params(["train", tomorrow, "上海虹桥", "北京南"], parser.handler_params)["body"].startswith("train ")
            passed("AstrBot handler registry imports candidate plugin and explicit command successfully")
        finally:
            await travel.terminate()
        context.send_message.assert_not_awaited()
        report = {"ok": True, "checks": CHECKS, "count": len(CHECKS), "real_framework": True, "provider_network": False, "qq_messages_sent": 0}
        destination = Path(os.environ["TRAVEL_REPORT"])
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        destination.chmod(0o600)
        print(json.dumps(report), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
