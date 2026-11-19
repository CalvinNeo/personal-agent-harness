"""Bounded request snapshots for event-bound tools after a plugin restart."""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from datetime import datetime
from types import SimpleNamespace


_EXTRAS = frozenset({"voice_input_transcribed", "voice_input_transcript", "intent_router_accepted",
                     "intent_router_route", "intent_router_task", "schedule_image_candidate",
                     "created_schedule_result", "created_simple_reminder_result", "long_term_memory_tool_done",
                     "current_request_received_at", "current_request_has_images", "codex_first_submitted", "scheduled_task_id",
                     "codex_subtask_depth", "codex_pure_mode", "codex_execution_profile"})


def event_is_addressed(event) -> bool:
    """Return whether this allowlisted QQ event is an addressed task context.

    QQ FriendMessage events are already a direct conversation with the bot, but
    some adapter versions leave ``is_at_or_wake_command`` false (especially
    when the user quotes a previous message and adds an @). Group messages
    still require the normal @/wake flag; callers must apply their own
    ``_allowed`` check as well.
    """
    if bool(getattr(event, "is_at_or_wake_command", False)):
        return True
    try:
        if event.get_platform_name() != "qq_official":
            return False
        if ":FriendMessage:" in str(event.unified_msg_origin):
            return True
        bot_id = str(event.get_self_id() or "") if callable(getattr(event, "get_self_id", None)) else ""
        if not bot_id:
            bot_id = str(getattr(getattr(event, "message_obj", None), "self_id", "") or "")
        if not bot_id:
            return False
        return any(type(part).__name__ == "At" and str(getattr(part, "qq", "") or "") == bot_id
                   for part in (event.get_messages() or ()))
    except Exception:
        return False


def capture_received_at(event, *, now=None) -> float:
    """Capture host/adapter receipt time once, before engine or queue delays."""
    current = time.time() if now is None else float(now)
    value = event.get_extra("current_request_received_at")
    if type(value) in {int, float} and math.isfinite(value) and value > 0:
        return float(value)
    value = getattr(getattr(event, "message_obj", None), "timestamp", None)
    if isinstance(value, datetime):
        value = value.timestamp() if value.tzinfo is not None else None
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            value = parsed.timestamp() if parsed.tzinfo is not None else None
        except ValueError:
            value = None
    received = float(value) if type(value) in {int, float} and math.isfinite(value) and 0 < value <= current + 300 else current
    event.set_extra("current_request_received_at", received)
    return received


def request_text(event) -> str:
    if event.get_extra("voice_input_transcribed"):
        value = str(getattr(event, "message_str", "") or event.get_extra("voice_input_transcript") or "").strip()
        if value:
            return value
    return " ".join(str(part.text) for part in event.get_messages()
                    if type(part).__name__ == "Plain" and isinstance(getattr(part, "text", None), str)).strip()


def identity(event) -> tuple[str, str, str, str]:
    return (str(event.get_platform_name()), str(event.unified_msg_origin), str(event.get_sender_id() or ""),
            str(getattr(getattr(event, "message_obj", None), "message_id", "") or ""))


def capability_task_text(event, fallback=None) -> str:
    """Remove an explicit engine wrapper, preserving the direct task verbatim."""
    text = request_text(event) if fallback is None else str(fallback).strip()
    command = re.match(r"^/(?:codex|cx)\s+(?:write\s+)?(?P<task>[\s\S]+)$", text, re.I)
    if command:
        return command.group("task").strip()
    named = re.match(
        r"^(?:(?:请|麻烦(?:你)?|帮我|帮忙)\s*)*(?:用|使用|调用|让|交给|交由|转交给|委托|安排)\s*"
        r"codex(?![A-Za-z0-9_])\s*(?:(?:来|帮我|帮忙|给我|一下)\s*)?[,，:：]?\s*(?P<task>[\s\S]+)$",
        text, re.I,
    )
    addressed = re.match(r"^codex(?![A-Za-z0-9_])(?:\s+|[,，:：]\s*)(?P<task>[\s\S]+)$", text, re.I)
    selected = named or addressed
    if selected:
        inner = selected.group("task").strip()
        inner = re.sub(r"^(?:(?:的|以|用|使用|采用)\s*)?(?:只读模式|只读|工作区写入模式|可写(?:工作区)?模式|workspace-write(?:\s+mode)?|read-only(?:\s+mode)?)\s*[,，:：]?\s*", "", inner, count=1, flags=re.I)
        return inner
    return text


def _encode(part, depth=0):
    kind = type(part).__name__
    if kind == "Plain":
        return {"type": kind, "text": str(getattr(part, "text", ""))[:12000]}
    if kind == "At":
        return {"type": kind, "qq": str(getattr(part, "qq", ""))[:128], "name": str(getattr(part, "name", "") or "")[:80]}
    if kind == "Image":
        return {"type": kind, "url": str(getattr(part, "url", "") or "")[:2048],
                "file": str(getattr(part, "file", "") or "")[:2048]}
    if kind == "Reply" and depth == 0:
        return {"type": kind, "id": str(getattr(part, "id", "") or "")[:256],
                "message_str": str(getattr(part, "message_str", "") or "")[:12000],
                "chain": [item for child in list(getattr(part, "chain", None) or [])[:20]
                          if (item := _encode(child, 1)) is not None]}
    return None


def _decode(value):
    from astrbot.api.message_components import At, Image, Plain, Reply
    if not isinstance(value, dict):
        raise ValueError("invalid request component")
    kind = value.get("type")
    if kind == "Plain":
        return Plain(str(value.get("text", ""))[:12000])
    if kind == "At":
        return At(qq=str(value.get("qq", ""))[:128], name=str(value.get("name", ""))[:80])
    if kind == "Image":
        return Image(file=str(value.get("file", ""))[:2048], url=str(value.get("url", ""))[:2048])
    if kind == "Reply":
        return Reply(id=str(value.get("id", ""))[:256], message_str=str(value.get("message_str", ""))[:12000],
                     chain=[_decode(child) for child in value.get("chain", [])[:20]
                            if isinstance(child, dict) and child.get("type") in {"Plain", "At", "Image"}])
    raise ValueError("unsupported request component")


def snapshot_event(event, task_text=None) -> dict:
    platform, umo, sender, message_id = identity(event)
    text = request_text(event) if task_text is None else str(task_text).strip()
    if platform != "qq_official" or not umo or not sender or len(text) > 12000:
        raise ValueError("invalid request identity or text")
    received_at = capture_received_at(event)
    extras = {}
    for key in _EXTRAS:
        value = event.get_extra(key)
        if isinstance(value, (str, bool, int, float)) and len(str(value)) <= 12000:
            extras[key] = value
    original_parts = list(event.get_messages())
    parts = [{"type": "Plain", "text": text}]
    parts.extend(item for part in original_parts[:40] if type(part).__name__ != "Plain"
                 and (item := _encode(part)) is not None)
    from .mentions import native_mention_targets
    present = {part["qq"] for part in parts if part["type"] == "At"}
    for target in native_mention_targets(event):
        if target not in present and target.lower() not in {"all", "everyone", "here"}:
            parts.append({"type": "At", "qq": target, "name": ""})
            present.add(target)
    if len(parts) > 41:
        raise ValueError("too many request components")
    message = getattr(event, "message_obj", None)
    result = {"version": 1, "platform": platform, "umo": umo, "sender": sender, "message_id": message_id,
              "text": text, "awake": event_is_addressed(event), "parts": parts, "extras": extras,
              "received_at": received_at,
              "self_id": str(getattr(message, "self_id", "") or "")[:128],
              "group_id": str(getattr(message, "group_id", "") or "")[:256]}
    result["owner"] = hashlib.sha256((umo + "\0" + sender).encode()).hexdigest()
    if len(json.dumps(result, ensure_ascii=False)) > 64000:
        raise ValueError("request snapshot too large")
    return result


class RestoredEvent:
    """A fixed request identity; tool output is returned to the trusted caller."""
    def __init__(self, snapshot):
        self.unified_msg_origin, self.sender = snapshot["umo"], snapshot["sender"]
        self.platform, self.message_str = snapshot["platform"], snapshot["text"]
        self.is_at_or_wake_command = snapshot["awake"]
        self.parts = [_decode(part) for part in snapshot["parts"]]
        self.extras = dict(snapshot["extras"])
        self.extras["current_request_received_at"] = snapshot["received_at"]
        self.received_at = snapshot["received_at"]
        self.message_obj = SimpleNamespace(message_id=snapshot["message_id"], self_id=snapshot["self_id"],
                                           group_id=snapshot["group_id"], message=self.parts, timestamp=self.received_at)
        self.message_obj.sender = SimpleNamespace(user_id=self.sender)
        self._stopped, self._llm = False, True

    def get_messages(self): return self.parts
    def get_sender_id(self): return self.sender
    def get_platform_name(self): return self.platform
    def get_self_id(self): return self.message_obj.self_id
    def get_group_id(self): return self.message_obj.group_id
    def get_session_id(self): return self.unified_msg_origin.rsplit(":", 1)[-1]
    def is_private_chat(self): return ":FriendMessage:" in self.unified_msg_origin
    def get_extra(self, key, default=None): return self.extras.get(key, default)
    def set_extra(self, key, value): self.extras[key] = value
    def stop_event(self): self._stopped = True
    def is_stopped(self): return self._stopped
    def should_call_llm(self, value): self._llm = value
    async def send(self, chain):
        raise RuntimeError("restored requests return tool output through the trusted bridge")


def restore_event(snapshot):
    if not isinstance(snapshot, dict) or snapshot.get("version") != 1:
        raise ValueError("invalid request snapshot")
    if len(json.dumps(snapshot, ensure_ascii=False)) > 64000:
        raise ValueError("request snapshot too large")
    if (snapshot.get("platform") != "qq_official" or not isinstance(snapshot.get("umo"), str)
            or not isinstance(snapshot.get("sender"), str) or not snapshot["sender"]
            or not isinstance(snapshot.get("text"), str) or len(snapshot["text"]) > 12000
            or type(snapshot.get("awake")) is not bool or not isinstance(snapshot.get("parts"), list)
            or len(snapshot["parts"]) > 41 or not isinstance(snapshot.get("extras"), dict)
            or type(snapshot.get("received_at")) not in {int, float}
            or not math.isfinite(snapshot["received_at"]) or snapshot["received_at"] <= 0):
        raise ValueError("invalid request snapshot fields")
    expected = hashlib.sha256((snapshot["umo"] + "\0" + snapshot["sender"]).encode()).hexdigest()
    if snapshot.get("owner") != expected:
        raise ValueError("request owner mismatch")
    safe = dict(snapshot)
    safe["extras"] = {key: value for key, value in snapshot["extras"].items() if key in _EXTRAS}
    return RestoredEvent(safe)
