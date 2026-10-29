from __future__ import annotations

import asyncio
import time
from typing import Any

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.core.agent.message import ImageURLPart, TextPart
from astrbot.core.provider.entities import ProviderRequest

from .core import ImageCache, ImageReferenceError, ImageSelection, is_safe_image_ref, parse_reference, without_historical_images

_CACHED_EXTRA = "local_image_context_cached"
_INJECTED_EXTRA = "local_image_context_injected"


class HasTopLevelImage(filter.CustomFilter):
    def filter(self, event: AstrMessageEvent, cfg) -> bool:
        # Filters execute before framework access checks: no cache mutation here.
        return any(isinstance(part, Image) for part in event.get_messages())


@star.register(
    name="image_context",
    author="local",
    desc="同群同用户的短期图片上下文，默认拒绝所有会话",
    version="1.0.0",
)
class Main(star.Star):
    def __init__(self, context: star.Context, config: dict[str, Any] | None = None):
        super().__init__(context)
        config = config or {}
        self.allowed_umos = {str(value).strip() for value in config.get("allowed_umos", []) if str(value).strip()}
        self.allowed_senders = {str(value).strip() for value in config.get("allowed_senders", []) if str(value).strip()}
        self._cache = ImageCache()
        self._cleanup_task: asyncio.Task | None = None

    async def initialize(self):
        self._cleanup_task = asyncio.create_task(self._expire_cache(), name="image-context-expiry")

    async def _expire_cache(self):
        while True:
            await asyncio.sleep(30)
            self._cache.prune(time.monotonic())

    async def terminate(self):
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            await asyncio.gather(self._cleanup_task, return_exceptions=True)
            self._cleanup_task = None
        self._cache.clear()

    def _allowed(self, event: AstrMessageEvent) -> bool:
        if event.get_platform_name() != "qq_official":
            return False
        sender = str(event.get_sender_id() or "").strip()
        return (bool(sender) and event.unified_msg_origin in self.allowed_umos
                and (not self.allowed_senders or sender in self.allowed_senders))

    @staticmethod
    def _key(event: AstrMessageEvent) -> tuple[str, str]:
        return event.unified_msg_origin, str(event.get_sender_id())

    @staticmethod
    def _refs(event: AstrMessageEvent) -> list[str]:
        # Only top-level attachments. Never follow Reply/Forward/Node contents,
        # and never interpret text as an image URL.
        refs = []
        for part in event.get_messages():
            if isinstance(part, Image):
                ref = part.url or part.file
                if is_safe_image_ref(ref):
                    refs.append(ref)
        return list(dict.fromkeys(refs))

    def _remember(self, event: AstrMessageEvent):
        if event.get_extra(_CACHED_EXTRA):
            return
        self._cache.remember(self._key(event), self._refs(event), time.monotonic())
        event.set_extra(_CACHED_EXTRA, True)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.custom_filter(HasTopLevelImage, priority=95)
    async def remember_images(self, event: AstrMessageEvent):
        if not self._allowed(event):
            return
        # Receiving an unmentioned group attachment must not send a reply or
        # change is_at_or_wake_command. QQ may only push mentioned messages.
        self._remember(event)

    @filter.on_llm_request(priority=95)
    async def add_image_context(self, event: AstrMessageEvent, req: ProviderRequest):
        if not self._allowed(event) or not event.is_at_or_wake_command:
            return
        if event.get_extra(_INJECTED_EXTRA):
            return
        text = " ".join(part.text for part in event.get_messages() if isinstance(part, Plain))
        try:
            selection = parse_reference(text)
            light_image = bool(event.get_extra("intent_router_accepted")) and event.get_extra("intent_router_route") == "image"
            if selection is None and light_image:
                # The planner may recognize “看看这张” even when the wording
                # does not match the reference regex. Use only this sender's
                # latest batch; the cache and URL allowlist still apply.
                selection = ImageSelection()
            if selection is None:
                return
            self._remember(event)
            refs = self._cache.select(self._key(event), selection, time.monotonic())
        except ImageReferenceError as error:
            # Stop before any model call, so missing images cannot be guessed.
            event.stop_event()
            await event.send(event.plain_result(str(error)))
            return

        event.set_extra(_INJECTED_EXTRA, True)
        # Existing group conversations may contain other senders' images.
        # Strip visuals only from this request copy, never stored history.
        req.contexts = without_historical_images(req.contexts or [])
        # Current attachments may already have been converted to local files by
        # AstrBot. Compare original event URLs too, avoiding a second copy.
        existing = set(self._refs(event)) | set(req.image_urls or [])
        for part in req.extra_user_content_parts:
            if isinstance(part, ImageURLPart):
                existing.add(part.image_url.url)
            elif isinstance(part, dict) and part.get("type") == "image_url":
                existing.add(part.get("image_url", {}).get("url"))
        for ref in refs:
            if ref not in existing:
                req.extra_user_content_parts.append(
                    ImageURLPart(image_url=ImageURLPart.ImageURL(url=ref)).mark_as_temp()
                )
                existing.add(ref)
        req.extra_user_content_parts.append(TextPart(
            text=f"[图片上下文：已附上本用户在本群最近发送的 {len(refs)} 张参考图片，按发送顺序排列。仅以本次所附图片为准，不使用旧群聊中的图片描述；若图片不可用，请明确请用户重发，不要猜测图片内容。]"
        ).mark_as_temp())
