from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain, Reply
from astrbot.core.agent.message import ImageURLPart, TextPart
from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

from .core import ImageCache, ImageReferenceError, is_safe_image_ref, parse_reference, without_historical_images

_WALLCLOCK = time

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
    version="1.0.4",
)
class Main(star.Star):
    def __init__(self, context: star.Context, config: dict[str, Any] | None = None):
        super().__init__(context)
        config = config or {}
        self.allowed_umos = {str(value).strip() for value in config.get("allowed_umos", []) if str(value).strip()}
        self.allowed_senders = {str(value).strip() for value in config.get("allowed_senders", []) if str(value).strip()}
        self._local_image_root = Path(get_astrbot_temp_path()).resolve()
        self._cache = ImageCache(self._local_image_root)
        # Persistence is opt-in.  A missing/blank value keeps local test
        # instances and unconfigured plugins from writing a server-side file.
        # Production should set this to a dedicated file under AstrBot's data
        # directory (the bundled schema uses that path).
        cache_file = config.get("cache_file")
        self._cache_file = self._normalise_cache_file(cache_file)
        self._cleanup_task: asyncio.Task | None = None

    @staticmethod
    def _normalise_cache_file(value: object) -> Path | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        # Require a literal absolute path; do not expand ``~`` from whatever
        # account happens to launch AstrBot.
        path = Path(text)
        if not path.is_absolute() or path.name in {"", ".", ".."}:
            # Relative paths make it too easy to write to an unexpected
            # working directory.  Configure an explicit absolute path.
            return None
        # Do not follow a symlink supplied as the cache file.  Parent
        # directories are checked again immediately before writing.
        try:
            if path.exists() and path.is_symlink():
                return None
        except OSError:
            return None
        # Store the canonical parent so a configured alias such as `/var`
        # cannot be redirected between validation and atomic replacement.
        try:
            return path.parent.resolve(strict=False) / path.name
        except OSError:
            return None

    async def initialize(self):
        self._load_cache()
        self._cleanup_task = asyncio.create_task(self._expire_cache(), name="image-context-expiry")

    async def _expire_cache(self):
        while True:
            await asyncio.sleep(30)
            self._cache.prune(time.monotonic())

    async def terminate(self):
        self._save_cache()
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

    def _refs(self, event: AstrMessageEvent) -> list[str]:
        # Only top-level attachments. Never follow Reply/Forward/Node contents,
        # and never interpret text as an image URL.
        refs = []
        for part in event.get_messages():
            if isinstance(part, Image):
                ref = part.url or part.file
                if is_safe_image_ref(ref, self._local_image_root):
                    refs.append(ref)
        return list(dict.fromkeys(refs))

    def _reply_refs(self, event: AstrMessageEvent) -> list[str]:
        """Read images attached to this message's direct QQ reply only.

        QQ includes quoted attachments in ``Reply.chain``. They are a fresh
        attachment on the current event, so an explicit reference may use them
        even when the original upload is outside the short cache window. We do
        not recurse into nested replies/forward nodes or inspect ordinary
        history, and every reference still passes the same allowlist checks.
        """
        refs: list[str] = []
        for part in event.get_messages():
            if not isinstance(part, Reply):
                continue
            chain = getattr(part, "chain", None)
            if chain is None:
                continue
            for child in chain:
                if isinstance(child, Image):
                    ref = child.url or child.file
                    if is_safe_image_ref(ref, self._local_image_root):
                        refs.append(ref)
        return list(dict.fromkeys(refs))[:12]

    def select_for_task(self, event: AstrMessageEvent) -> list[str]:
        """Select bounded inputs for a delegated task without starting an LLM.

        The caller must already own the user's task. This method still checks
        image-context authorization and never traverses unrelated group history.
        """
        if not self._allowed(event):
            raise ImageReferenceError("当前会话或发送者无权读取图片上下文。")
        current = self._refs(event)
        replied = self._reply_refs(event)
        if current:
            self._remember(event)
        text = " ".join(p.text for p in event.get_messages() if isinstance(p, Plain))
        text = re.sub(r"^\s*/(?:codex|cx)\s+", "", text, flags=re.I)
        now = time.monotonic()
        selection = parse_reference(text, allow_implicit=self._cache.has_recent(self._key(event), now))
        # A new upload owns this task's image scope; a direct reply owns it
        # only when there is no new upload. Never fill a requested count with
        # older cached pictures, which may depict an unrelated subject.
        attached = current or replied
        if attached:
            if selection is not None and selection.count is not None:
                if len(attached) < selection.count:
                    source = "这条消息" if current else "这条引用"
                    raise ImageReferenceError(
                        f"{source}里只有 {len(attached)} 张图片，不足 {selection.count} 张；请重新附上完整图片。"
                    )
                return attached[-selection.count:]
            return attached
        if selection is None:
            return []
        return self._cache.select(self._key(event), selection, now)

    def _load_cache(self):
        if self._cache_file is None:
            return
        try:
            # Refuse symlinks and non-regular files.  The cache contains QQ
            # attachment URLs and should never be redirected to another path.
            info = self._cache_file.lstat()
            if not info or not self._cache_file.is_file() or self._cache_file.is_symlink():
                self.logger.warning("Image cache restore skipped: unsafe cache file")
                return
            if info.st_mode & 0o077:
                # Narrow permissions before reading a file produced by an
                # older plugin version or a manually copied deployment.
                os.chmod(self._cache_file, 0o600)
            with self._cache_file.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            restored = self._cache.restore(data, wall_now=_WALLCLOCK.time(), now=time.monotonic())
            self.logger.info("Image cache restored=%d", restored)
        except FileNotFoundError:
            return
        except Exception:
            self.logger.exception("Image cache restore failed")

    def _save_cache(self):
        if self._cache_file is None:
            return
        temp_name: str | None = None
        try:
            parent = self._cache_file.parent
            parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Resolve parent aliases (for example macOS's /var -> /private/var)
            # before creating the temporary file.  The final cache path itself
            # was checked for symlinks by _normalise_cache_file.
            parent = parent.resolve(strict=True)
            if not parent.is_dir():
                raise OSError(f"unsafe image cache directory: {parent}")
            rows = self._cache.export(wall_now=_WALLCLOCK.time(), now=time.monotonic())
            fd, temp_name = tempfile.mkstemp(prefix=".image-cache.", dir=parent)
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(rows, handle, ensure_ascii=False, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self._cache_file)
            info = self._cache_file.lstat()
            if not self._cache_file.is_file() or self._cache_file.is_symlink():
                raise OSError("unsafe image cache target after replace")
            # Keep the final file private even when a platform's mkstemp or a
            # pre-existing ACL would otherwise widen permissions.
            os.chmod(self._cache_file, 0o600)
            temp_name = None
        except Exception:
            self.logger.exception("Image cache persistence failed")
        finally:
            if temp_name is not None:
                try:
                    os.unlink(temp_name)
                except FileNotFoundError:
                    pass

    def _remember(self, event: AstrMessageEvent):
        if event.get_extra(_CACHED_EXTRA):
            return
        refs = self._refs(event)
        self._cache.remember(self._key(event), refs, time.monotonic(), wall_now=_WALLCLOCK.time())
        self._save_cache()
        count = sum(isinstance(part, Image) for part in event.get_messages())
        if count:
            self.logger.info("Image cache received=%d accepted=%d scope=%s", count, len(refs), self._scope(event))
        event.set_extra(_CACHED_EXTRA, True)

    @staticmethod
    def _scope(event: AstrMessageEvent) -> str:
        # Correlate cache/follow-up without logging images or participant IDs.
        value = event.unified_msg_origin + "\0" + str(event.get_sender_id())
        return hashlib.sha256(value.encode()).hexdigest()[:12]

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
        self._remember(event)
        now = time.monotonic()
        recent = self._cache.has_recent(self._key(event), now)
        mode = "explicit"
        try:
            selection = parse_reference(text)
            if selection is None and recent:
                selection = parse_reference(text, allow_implicit=True)
                mode = "implicit"
            if selection is None:
                if recent:
                    self.logger.info("Image context skipped reason=no_reference scope=%s", self._scope(event))
                return
            current = self._refs(event)
            replied = self._reply_refs(event)
            # A broad question accompanying a new image refers to that image,
            # even if an older upload was close enough to join the same batch.
            if current and selection.count is None:
                refs = current
            elif replied:
                if selection.count is not None and len(replied) < selection.count:
                    raise ImageReferenceError(
                        f"这条引用里只有 {len(replied)} 张图片，不足 {selection.count} 张；请重新引用完整图片。"
                    )
                refs = replied if selection.count is None else replied[-selection.count:]
                mode = "reply"
            else:
                refs = self._cache.select(self._key(event), selection, now)
        except ImageReferenceError as error:
            # Stop before any model call, so missing images cannot be guessed.
            event.stop_event()
            self.logger.info("Image context unavailable scope=%s", self._scope(event))
            await event.send(event.plain_result(str(error)))
            return

        event.set_extra(_INJECTED_EXTRA, True)
        self.logger.info("Image context selected=%d mode=%s scope=%s", len(refs), mode, self._scope(event))
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
