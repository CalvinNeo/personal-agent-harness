from __future__ import annotations

import asyncio
import hashlib
import json

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.event.filter import PlatformAdapterType
from astrbot.api.message_components import Plain
from astrbot.core.star.filter.command import GreedyStr


class Main(star.Star):
    """Explicit QQ command into a separate low-privilege Codex runner."""

    def __init__(self, context: star.Context, config=None):
        super().__init__(context)
        self.config = config or {}
        self._task = None

    def _allowed(self, event):
        allowed = set(self.config.get("allowed_umos") or [])
        senders = set(self.config.get("allowed_senders") or [])
        return event.unified_msg_origin in allowed and (not senders or event.get_sender_id() in senders)

    async def _send(self, event: AstrMessageEvent, text):
        """Send through the original QQ event so the adapter can use msg_id.

        ``context.send_message(session, ...)`` is treated as a proactive group
        message by QQ Official.  That endpoint is rejected for an asynchronous
        Codex result in many groups (40034105).  Keeping the original event
        preserves its message id and lets AstrBot send a normal reply.
        """
        text = str(text).strip()
        if len(text) > 6000:
            text = text[:6000] + "\n[输出已截断]"
        await event.send(MessageChain([Plain(text=text)]))

    async def _run(self, event: AstrMessageEvent, prompt, mode):
        session = event.unified_msg_origin
        writer = None
        try:
            reader, writer = await asyncio.open_unix_connection(str(self.config.get("socket_path") or "/run/astrbot-codex/bridge.sock"))
            payload = {"prompt": prompt, "mode": mode, "workspace": hashlib.sha256(session.encode()).hexdigest()[:32]}
            writer.write((json.dumps(payload, ensure_ascii=False) + "\n").encode())
            await writer.drain()
            raw = await asyncio.wait_for(reader.readline(), 930)
            response = json.loads(raw.decode())
            if response.get("ok"):
                await self._send(event, response.get("result") or "Codex 未返回文本。")
            else:
                await self._send(event, "Codex 任务失败：" + str(response.get("error") or "未知错误")[:1200])
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Codex bridge task failed")
            await self._send(event, "Codex 服务暂时不可用，请稍后再试。")
        finally:
            if writer:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass

    @filter.platform_adapter_type(PlatformAdapterType.QQOFFICIAL)
    @filter.command("codex", alias={"cx"}, priority=100)
    async def codex(self, event: AstrMessageEvent, prompt: GreedyStr):
        """Use /codex <task>; write access is disabled unless explicitly configured."""
        if not self._allowed(event):
            # Consume the explicit command so an unauthorized group cannot fall through
            # to AstrBot's normal LLM handler.
            event.stop_event()
            event.should_call_llm(False)
            return
        event.stop_event()
        event.should_call_llm(False)
        prompt = str(prompt or "").strip()
        mode = "read-only"
        if prompt.lower().startswith("write "):
            if event.get_sender_id() not in set(self.config.get("write_senders") or []):
                await event.send(MessageChain([Plain(text="写入模式当前关闭；请先在服务器配置 write_senders。")]))
                return
            mode = "workspace-write"
            prompt = prompt[6:].strip()
        if not prompt:
            await event.send(MessageChain([Plain(text="用法：@机器人 /codex 任务描述")]))
            return
        if len(prompt) > 12000:
            await event.send(MessageChain([Plain(text="任务过长，最多 12000 个字符。")]))
            return
        if self._task and not self._task.done():
            await event.send(MessageChain([Plain(text="已有一个 Codex 任务在执行，请稍后再试。")]))
            return
        await event.send(MessageChain([Plain(text="已受理 Codex 只读任务，完成后回传结果。" if mode == "read-only" else "已受理 Codex 工作区写入任务，完成后回传结果。")]))
        # Keep the original event alive for the asynchronous result.  Calling
        # context.send_message(session, ...) here becomes a QQ proactive group
        # message and can fail with 40034105 even when the bot permission is on.
        self._task = asyncio.create_task(self._run(event, prompt, mode))

    async def terminate(self):
        if self._task and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
