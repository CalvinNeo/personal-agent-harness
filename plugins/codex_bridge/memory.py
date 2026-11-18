"""Read scoped memory through the trusted plugin, never through its database."""

import hashlib
from datetime import datetime
from zoneinfo import ZoneInfo


def memory_snapshot(context, event, logger):
    """Read through the active plugin; the worker never receives database access."""
    unavailable = {"status": "unavailable", "code": "plugin_unavailable",
                   "observed_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(), "items": [], "total": None}
    getter = getattr(context, "get_all_stars", None)
    if not callable(getter):
        return unavailable
    try:
        for metadata in getter():
            if getattr(metadata, "name", None) != "long_term_memory" or not getattr(metadata, "activated", True):
                continue
            reader = getattr(getattr(metadata, "star_cls", None), "memory_snapshot_for_task", None)
            if not callable(reader):
                return unavailable
            result = reader(event)
            if not isinstance(result, dict) or result.get("status") not in {"ok", "unavailable"}:
                raise ValueError("invalid memory snapshot")
            return result
    except Exception as error:
        logger.warning("Codex memory snapshot unavailable type=%s", type(error).__name__)
        unavailable["code"] = "read_failed"
    return unavailable


def saved_relationship_facts(context, event, logger):
    """Only the active memory plugin can select this owner's stored facts."""
    getter = getattr(context, "get_all_stars", None)
    if not callable(getter):
        return []
    try:
        for metadata in getter():
            if getattr(metadata, "name", None) != "long_term_memory" or not getattr(metadata, "activated", True):
                continue
            reader = getattr(getattr(metadata, "star_cls", None), "saved_relationship_facts", None)
            if callable(reader):
                values = reader(event)
                return [value for value in values[:100] if isinstance(value, str) and len(value) <= 1000] if isinstance(values, list) else []
    except Exception as error:
        logger.warning("QQ relationship memory unavailable type=%s", type(error).__name__)
    return []


def local_memory_request(context, event):
    parts = [part.text.strip() for part in event.get_messages()
             if type(part).__name__ == "Plain" and isinstance(getattr(part, "text", None), str) and part.text.strip()]
    text = " ".join(parts)
    owner = hashlib.sha256((str(event.unified_msg_origin) + "\0" + str(event.get_sender_id() or "")).encode()).hexdigest()
    binding = {"event": id(event), "owner": owner, "text": hashlib.sha256(text.encode()).hexdigest()}
    if event.get_extra("long_term_memory_request") == binding:
        return True
    getter = getattr(context, "get_all_stars", None)
    if callable(getter):
        for metadata in getter():
            if getattr(metadata, "name", None) != "long_term_memory" or not getattr(metadata, "activated", True):
                continue
            handler = getattr(getattr(metadata, "star_cls", None), "handles_memory_request", None)
            if callable(handler) and handler(event):
                return True
    return False


def with_memory_context(context, event, prompt, logger):
    getter = getattr(context, "get_all_stars", None)
    if not callable(getter):
        return prompt
    try:
        for metadata in getter():
            instance = getattr(metadata, "star_cls", None)
            reader = getattr(instance, "memory_context_for_task", None)
            if not callable(reader):
                continue
            memory = reader(event, prompt)
            if not isinstance(memory, str) or not memory:
                return prompt
            # Never truncate the task or JSON memory mid-record. Selection is
            # already bounded by the memory plugin; skip it if the task is full.
            if len(memory) > 6000 or len(prompt) + len(memory) > 12000:
                logger.info("Codex memory context omitted reason=prompt_budget")
                return prompt
            return prompt + memory
    except Exception as error:
        logger.warning("Codex memory context unavailable type=%s", type(error).__name__)
    return prompt
