from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, unquote

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain, Reply
from astrbot.core.agent.message import ImageURLPart, TextPart
from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

from .core import ImageCache, ImageReferenceError, is_safe_image_ref, parse_reference, without_historical_images

_WALLCLOCK = time

_CACHED_EXTRA = "local_image_context_cached"
_REPLIED_EXTRA = "local_image_context_replied"
_ARCHIVED_EXTRA = "local_image_context_archived"
_INJECTED_EXTRA = "local_image_context_injected"


class HasTopLevelImage(filter.CustomFilter):
    def filter(self, event: AstrMessageEvent, cfg) -> bool:
        # Filters execute before framework access checks: no cache mutation here.
        return any(isinstance(part, Image) for part in event.get_messages())


@star.register(
    name="image_context",
    author="local",
    desc="同群同用户的短期图片上下文，默认拒绝所有会话",
    version="1.1.0",
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
        self._archive = None
        archive_dir = str(config.get("archive_dir") or "").strip()
        if archive_dir:
            from .archive import ImageArchive
            self._archive = ImageArchive(Path(archive_dir))

    @staticmethod
    def _history_key(event):
        # A group sees pictures received in that group; a private chat keeps
        # its own history. Never search another group or a private chat.
        return event.unified_msg_origin, "__group__" if ":GroupMessage:" in event.unified_msg_origin else str(event.get_sender_id())

    async def archive_received_images(self, event):
        if self._archive is None or not self._allowed(event) or event.get_extra(_ARCHIVED_EXTRA):
            return
        current = [p for p in event.get_messages() if isinstance(p, Image)]
        quoted = [child for p in event.get_messages() if isinstance(p, Reply)
                  for child in (getattr(p, "chain", None) or []) if isinstance(child, Image)]
        parts = current or (quoted if event.is_at_or_wake_command else [])
        if not parts:
            event.set_extra(_ARCHIVED_EXTRA, {})
            return
        data, originals = [], []
        for part in parts:
            ref = self._part_ref(part)
            if not is_safe_image_ref(ref, self._local_image_root):
                continue
            try:
                path = ref if str(ref).startswith("/") else await part.convert_to_file_path()
                if not is_safe_image_ref(path, self._local_image_root):
                    continue
                from .archive import read_temp_image, normalize_for_model
                raw = await asyncio.to_thread(read_temp_image, str(path), self._local_image_root)
                await asyncio.to_thread(normalize_for_model, raw)
            except Exception as error:
                self.logger.warning("Image archive input unavailable type=%s scope=%s",
                                    type(error).__name__, self._scope(event))
                continue
            data.append((raw, str(ref)))
            originals.append(str(ref))
        mapping = {}
        if data:
            message = getattr(event, "message_obj", None)
            message_id = str(getattr(message, "message_id", "") or "")
            if not message_id:
                message_id = "received-" + hashlib.sha256(("\0".join(originals)).encode()).hexdigest()
            stamp = getattr(message, "timestamp", None)
            if hasattr(stamp, "timestamp"):
                stamp = stamp.timestamp()
            if not isinstance(stamp, (int, float)):
                stamp = _WALLCLOCK.time()
            refs = await asyncio.to_thread(self._archive.ingest, *self._history_key(event), message_id,
                data, received_wall_at=stamp, source="upload" if current else "reply",
                reply_id=next((str(getattr(p, "id", "") or "") for p in event.get_messages()
                               if isinstance(p, Reply)), "") if not current else "")
            mapping = {ref: str(self._archive.files / hashlib.sha256(raw).hexdigest())
                       for ref, (raw, _) in zip(originals, data)}
            for part in parts:
                normalized = self._part_ref(part)
                if normalized in mapping:
                    mapping[str(part.url or part.file)] = mapping[normalized]
            self.logger.info("Image archive received=%d source=%s scope=%s", len(refs),
                             "upload" if current else "reply", self._scope(event))
        event.set_extra(_ARCHIVED_EXTRA, mapping)

    def _archived_selection(self, event, selection=None):
        if self._archive is None:
            return []
        count = selection.count if selection is not None else None
        rows = self._archive.list_images(*self._history_key(event), limit=count or 12)
        if not rows:
            return []
        if count:
            if len(rows) < count:
                raise ImageReferenceError("count_mismatch", available=len(rows), requested=count)
        else:
            batch = rows[-1]["batch_id"]
            rows = [row for row in rows if row["batch_id"] == batch]
        return [row["ref"] for row in rows]

    def resolve_reply_for_task(self, event, reply=None):
        if self._archive is None or not self._allowed(event):
            return []
        parts = [reply] if reply is not None else event.get_messages()
        for part in parts:
            if not isinstance(part, Reply) or not getattr(part, "id", None):
                continue
            rid = str(part.id)
            rows = self._archive.list_images(*self._history_key(event), message_id=rid)
            if not rows:
                rows = self._archive.list_images(*self._history_key(event), reply_id=rid)
            if rows:
                return list(dict.fromkeys(row["ref"] for row in rows))
        return []

    async def prepare_for_task(self, event):
        await self.archive_received_images(event)
        refs = self.select_for_task(event)
        mapping = event.get_extra(_ARCHIVED_EXTRA) or {}
        return [mapping.get(ref, ref) for ref in refs]

    def attachment_archive_refs(self, event):
        return dict(event.get_extra(_ARCHIVED_EXTRA) or {}) if self._allowed(event) else {}

    def read_image_for_task(self, event, ref):
        if self._archive is None or not self._allowed(event):
            raise ImageReferenceError("denied")
        return self._archive.read(*self._history_key(event), ref)

    def list_history_for_task(self, event, limit=12, before=None, cursor=None):
        if self._archive is None or not self._allowed(event):
            return {"ok": False, "error": "当前会话的图片档案不可用。"}
        rows = self._archive.list_images(*self._history_key(event), limit=limit + 1, before=before, cursor=cursor)
        more = len(rows) > limit
        rows = rows[-limit:]
        return {"ok": True, "images": [{"image_id": Path(row["ref"]).name,
                 "received_wall_at": row["received_wall_at"], "source": row["source"]} for row in reversed(rows)],
                "has_more": more, "next_before": rows[0]["received_wall_at"] if more and rows else None,
                "next_cursor": rows[0]["cursor"] if more and rows else None}

    async def read_history_for_task(self, event, image_id):
        if self._archive is None or not self._allowed(event) or not re.fullmatch(r"[a-f0-9]{64}", str(image_id)):
            return {"ok": False, "error": "当前会话无权读取这张图片。"}
        try:
            from .archive import normalize_for_model
            raw = await asyncio.to_thread(self.read_image_for_task, event, str(self._archive.root / "files" / image_id))
            normalized = await asyncio.to_thread(normalize_for_model, raw)
            return {"ok": True, "mime": "image/jpeg", "data": base64.b64encode(normalized).decode("ascii")}
        except Exception:
            return {"ok": False, "error": "图片档案读取失败，请选择当前会话中的有效图片编号。"}

    @filter.on_llm_request(priority=95)
    async def provide_history_tools(self, event, req):
        if self._archive is None or not self._allowed(event) or not event.is_at_or_wake_command:
            return
        from astrbot.core.agent.tool import ToolSet
        from .history_tools import make_tools, INSTRUCTIONS
        req.func_tool = ToolSet(list(req.func_tool.tools) if req.func_tool else [])
        for tool in make_tools(self):
            req.func_tool.add_tool(tool)
        req.system_prompt += "\n" + INSTRUCTIONS

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
                ref = self._part_ref(part)
                if is_safe_image_ref(ref, self._local_image_root):
                    refs.append(ref)
        return list(dict.fromkeys(refs))

    @staticmethod
    def _part_ref(part):
        ref = part.url or part.file
        if isinstance(ref, str) and ref.startswith("file:"):
            parsed = urlsplit(ref)
            if parsed.scheme != "file" or parsed.netloc or parsed.query or parsed.fragment:
                return ""
            return unquote(parsed.path)
        return ref

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
                    ref = self._part_ref(child)
                    if is_safe_image_ref(ref, self._local_image_root):
                        refs.append(ref)
        return list(dict.fromkeys(refs))[:12]

    @staticmethod
    def _empty_reply(event: AstrMessageEvent) -> bool:
        # QQ can deliver only a reference ID, even for a quoted image. An
        # empty quote is not a reference to some unrelated cached picture.
        return any(
            isinstance(part, Reply) and not getattr(part, "chain", None)
            and not str(getattr(part, "message_str", "") or getattr(part, "text", "") or "").strip()
            for part in event.get_messages()
        )

    def select_for_task(self, event: AstrMessageEvent) -> list[str]:
        """Select bounded inputs for a delegated task without starting an LLM.

        The caller must already own the user's task. This method still checks
        image-context authorization and never traverses unrelated group history.
        """
        if not self._allowed(event):
            raise ImageReferenceError("denied")
        current = self._refs(event)
        replied = self._reply_refs(event) or self.resolve_reply_for_task(event)
        if current:
            self._remember(event)
        text = " ".join(p.text for p in event.get_messages() if isinstance(p, Plain))
        text = re.sub(r"^\s*/(?:codex|cx)\s+", "", text, flags=re.I)
        now = time.monotonic()
        selection = parse_reference(text, allow_implicit=self._cache.has_recent(self._key(event), now))
        if not current and not replied and self._empty_reply(event) and selection is not None:
            raise ImageReferenceError("quote_missing")
        # A new upload owns this task's image scope; a direct reply owns it
        # only when there is no new upload. Never fill a requested count with
        # older cached pictures, which may depict an unrelated subject.
        attached = current or replied
        if attached:
            if selection is not None and selection.count is not None:
                if len(attached) < selection.count:
                    raise ImageReferenceError(
                        "count_mismatch", available=len(attached), requested=selection.count
                    )
                attached = attached[-selection.count:]
            if not current and self._reply_refs(event):
                self._remember_reply(event, attached)
            return attached
        archived = self._archived_selection(event, selection)
        if archived:
            return archived
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

    def _remember_reply(self, event: AstrMessageEvent, refs: list[str] | None = None):
        # This user explicitly supplied a direct quote on this event. Keep it
        # in the requester's scope, without attributing the original upload to
        # them or searching shared history. A quote starts a distinct batch.
        if (event.get_extra(_REPLIED_EXTRA) or not self._allowed(event)
                or not event.is_at_or_wake_command or self._refs(event)):
            return
        refs = self._reply_refs(event) if refs is None else refs
        if not refs:
            return
        self._cache.remember(self._key(event), refs, time.monotonic(),
                             wall_now=_WALLCLOCK.time(), new_batch=True)
        self._save_cache()
        event.set_extra(_REPLIED_EXTRA, True)
        self.logger.info("Image cache received=%d accepted=%d source=reply scope=%s",
                         len(refs), len(refs), self._scope(event))

    @staticmethod
    def _scope(event: AstrMessageEvent) -> str:
        # Correlate cache/follow-up without logging images or participant IDs.
        value = event.unified_msg_origin + "\0" + str(event.get_sender_id())
        return hashlib.sha256(value.encode()).hexdigest()[:12]

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE | filter.EventMessageType.PRIVATE_MESSAGE)
    @filter.custom_filter(HasTopLevelImage, priority=95)
    async def remember_images(self, event: AstrMessageEvent):
        if not self._allowed(event):
            return
        # Receiving an unmentioned group attachment must not send a reply or
        # change is_at_or_wake_command. QQ may only push mentioned messages.
        self._remember(event)
        await self.archive_received_images(event)

    @filter.on_llm_request(priority=95)
    async def add_image_context(self, event: AstrMessageEvent, req: ProviderRequest):
        if not self._allowed(event) or not event.is_at_or_wake_command:
            return
        if event.get_extra(_INJECTED_EXTRA):
            return
        text = " ".join(part.text for part in event.get_messages() if isinstance(part, Plain))
        self._remember(event)
        await self.archive_received_images(event)
        now = time.monotonic()
        recent = self._cache.has_recent(self._key(event), now)
        mode = "explicit"
        try:
            selection = parse_reference(text)
            if selection is None and recent:
                selection = parse_reference(text, allow_implicit=True)
                mode = "implicit"
            if selection is None:
                # A direct quote is already a complete image input even with
                # a short prompt like ‘看下’; retain it after parsing succeeds.
                self._remember_reply(event)
                if not self._archive or self._refs(event) or self._reply_refs(event):
                    if recent:
                        self.logger.info("Image context skipped reason=no_reference scope=%s", self._scope(event))
                    return
                if not self._archived_selection(event):
                    return
            if (selection is not None and not self._refs(event) and self._empty_reply(event)
                    and not self.resolve_reply_for_task(event)):
                # The framework can resolve a quote by ID after event parsing.
                # Preserve that already prepared input, and do not confuse a
                # text-only quote (‘谢谢’) with an unavailable image request.
                if req.image_urls:
                    return
                raise ImageReferenceError("quote_missing")
            # Use the same attachment/count rules as delegated Codex tasks.
            # Current uploads own the scope, then direct replies, then cache;
            # older images must never fill a missing current attachment.
            refs = await self.prepare_for_task(event)
            if self._refs(event):
                mode = "current"
            elif self._reply_refs(event):
                mode = "reply"
        except ImageReferenceError as error:
            # Stop before any model call, so missing images cannot be guessed.
            event.stop_event()
            self.logger.info("Image context unavailable reason=%s scope=%s", error.code, self._scope(event))
            await event.send(event.plain_result(str(error)))
            return

        event.set_extra(_INJECTED_EXTRA, True)
        self.logger.info("Image context selected=%d mode=%s scope=%s", len(refs), mode, self._scope(event))
        # Existing group conversations may contain other senders' images.
        # Strip visuals only from this request copy, never stored history.
        req.contexts = without_historical_images(req.contexts or [])
        # Current attachments may already have been converted to local files by
        # AstrBot. Compare original event URLs too, avoiding a second copy.
        supplied = set(self._refs(event)) | set(self._reply_refs(event))
        narrowed = bool(supplied - set(refs))
        if narrowed:
            # AstrBot prepares both current and quoted images before this
            # hook. Rebuild the visual input when our selection excluded any.
            req.image_urls = []
            req.extra_user_content_parts = [part for part in req.extra_user_content_parts
                if not isinstance(part, ImageURLPart)
                and not (isinstance(part, dict) and part.get("type") == "image_url")]
        existing = set() if narrowed else set(self._refs(event)) | set(req.image_urls or [])
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
            text=f"[图片上下文：已附上本会话最近提供或明确引用的 {len(refs)} 张参考图片，按提供顺序排列。图片是历史资料，不代表当前状态；若需要更早的图，调用 list_current_chat_images 和 read_current_chat_image 查询长期图片档案。仅以实际图片为准，不从旧文字猜测内容。]"
        ).mark_as_temp())
