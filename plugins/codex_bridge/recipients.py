"""Trusted recipient context for generated replies in the current QQ group.

Preparing a recipient never sends text. The normal response and an asynchronous
Codex result can both use the same validated metadata to prepend a native At.
Only current native mentions, the current sender, or scoped explicit relationship
facts can produce an account ID; the model-facing tool accepts no account input.
"""
from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from dataclasses import dataclass

try:
    from .core import new_codex_conversation, owner_key, retry_intent
    from .mentions import native_mention_targets, parse_mention_request, strip_verified_mention_markup
    from .relationships import resolve_relationship
except ImportError:  # Standalone unittest invocation from the plugin directory.
    from core import new_codex_conversation, owner_key, retry_intent
    from mentions import native_mention_targets, parse_mention_request, strip_verified_mention_markup
    from relationships import resolve_relationship


RECIPIENT_TOOL_NAME = "set_current_reply_recipient"
RECIPIENT_TOOL_SCHEMA = {"type": "object", "properties": {
    "target_label": {"type": "string", "maxLength": 40,
                     "description": "可选：当前用户原文里明确要向其讲解的称呼，如我老婆；不能填写账号或引用里的称呼。"}
}, "additionalProperties": False}
RECIPIENT_TOOL_DESCRIPTION = (
    "把当前这条回复的受众绑定为用户在当前群明确指定的成员。"
    "用于‘向我老婆讲解一下’‘给某人介绍/解释’以及当前原生 @ 成员后的内容生成请求。"
    "只从当前消息的原生提及、当前发送者或当前用户已保存且唯一的人物关系解析；"
    "可选称呼必须原样出现在当前用户的直接讲解指令中，不能输入账号、改群、猜人或向私聊发送。"
    "工具只设置本次回复的原生 @，不发送正文；"
    "设置成功后继续生成用户要求的解释/介绍，不能直接复述话题，也不能声称已经发送。"
)

_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_GROUP = re.compile(r"^[^:\s]{1,80}:GroupMessage:[A-Za-z0-9_-]{1,128}$")
_HASH = re.compile(r"^[a-f0-9]{64}$")
_LABEL = re.compile(r"^@?[\w\u3400-\u9fff·.-]{1,40}$")
_RELATION_LABEL = re.compile(r"^我(?:的)?(?:老公|丈夫|老婆|妻子|爸爸|父亲|妈妈|母亲)$")
_SELF = frozenset({"我", "我自己", "本人", "自己", "me", "myself"})
_ALL = frozenset({"all", "everyone", "here", "全体", "全体成员", "所有人", "大家", "全部成员", "所有成员"})
_PREFIX = r"(?:(?:请|麻烦(?:你)?|帮我|帮忙|你|现在|先)\s*)*"
_ACTION = r"(?:讲解|介绍|解释|说明|分析|总结|回答|科普|讲一讲|讲讲|说一说|说说)"
_GENERATING = re.compile(r"^(?:请|再|继续|帮忙|帮我|详细|简单|通俗地?|简短地?|认真地?)*\s*" + _ACTION)
_QUESTION = re.compile(r"(?:是什么|是什么意思|怎么|如何|为什么|是否|有用[吗么]|[?？])")
_DIRECT = re.compile(
    r"^" + _PREFIX + r"(?:向|给|跟|对|为)\s*(?P<label>@?[\w\u3400-\u9fff·.-]{1,40}?)\s*"
    r"(?:(?:详细|简单|通俗|简短|认真)(?:地|的)?\s*)?"
    r"(?P<action>" + _ACTION + r")(?:一下|一遍|下)?\s*[:：,，]?\s*(?P<topic>[\s\S]*)$"
)
_TELL_QUESTION = re.compile(
    r"^" + _PREFIX + r"告诉\s*(?P<label>我(?:的)?(?:老婆|妻子|老公|丈夫|爸爸|父亲|妈妈|母亲))"
    r"\s*[:：,，]?\s*(?P<topic>.+)$"
)
_BOT_WAKE = re.compile(r"^@[\w\u3400-\u9fff·.-]*?(?:的智能体|机器人)(?=\s|[,，:：]|$)\s*[,，:：]?\s*")
_NEGATIVE = re.compile(
    r"^" + _PREFIX + r"(?:不要|不许|禁止|无需|不用|别|不必|取消|停止)(?:再|给我|帮我|替我|去|进行|\s){0,8}"
    r"(?:@|at\b|艾特|提及|向|给|跟|对|为|告诉|发送|发给)|"
    r"(?:[,，;；]|\s)(?:不要|不用|无需|别)\s*(?:@|at\b|艾特|提及)", re.I
)
_EXTERNAL = re.compile(r"(?:私聊|私信)(?:给|向|告诉)|(?:给|向)[^，,。\n]{1,40}(?:私聊|私信)|(?:其他|别的|另一个)群|跨群")
_MARKUP = re.compile(r"<\s*(?:[/!?]?[A-Za-z_]|@)|\[(?:CQ|At|AtAll)\s*[:：]", re.I)
_CONTINUATION = re.compile(
    r"^" + _PREFIX + r"(?:"
    r"(?:再|重新)(?:试试|试一下|试一试|试一次|发一次|发送一次)|"
    r"继续(?:讲|说|解释|介绍|补充)?(?:一下|下)?(?:刚才(?:的)?(?:内容|话题))?|"
    r"(?:再|重新)?(?:讲|说|解释|介绍)(?:一遍|一次|一下|下)?(?:得)?(?:更)?(?:详细|简单|通俗|清楚|简短|具体)(?:一点|一些|点|些)?|"
    r"(?:再|更)(?:详细|简单|通俗|清楚|简短|具体)(?:一点|一些|点|些)|"
    r"补充(?:一下|下)?(?:刚才|上面|这段)(?:的)?(?:内容|解释|介绍)"
    r")(?:呢|吧|啊|呀)?[。!！?？]*$", re.I
)
_FIELDS = {"version", "owner", "umo", "sender", "target_id", "target_label", "source", "request_sha256", "inherited"}
_UNSET = object()
_EXAMPLE_PREFIX = re.compile(r"^(?:例如|比如|假设|如果我说|示例|例子|他说|她说|有人说|下面是(?:引用|示例)|(?:请)?(?:解释|分析).{0,16}(?:语法|句子|这句话|示例))")
_CODEX_COMMAND = re.compile(r"^/(?:codex|cx)\s+(?:write\s+)?", re.I)
_CODEX_PREFIX = re.compile(
    r"^" + _PREFIX + r"(?:用|使用|调用|通过|交给|交由|让)\s*codex\s*(?:来|帮我|帮忙)?\s*[,，:：]?\s*", re.I
)


@dataclass(frozen=True)
class RecipientRequest:
    target_label: str
    task: str
    action: str = "generate"


@dataclass(frozen=True)
class RecipientResolution:
    status: str
    request: RecipientRequest | None = None
    recipient: dict | None = None
    reason: str = ""


def _hash(text: str) -> str:
    return hashlib.sha256(str(text or "").encode()).hexdigest()


def _direct(text: str, targets=()) -> str:
    value = unicodedata.normalize("NFKC", html.unescape(str(text or ""))).strip()
    if not value or len(value) > 12000 or value.startswith((">", "`", '"', "'", "“", "‘", "「", "『")):
        return ""
    if "```" in value or re.search(r"(?m)^\s*>", value):
        return ""
    value = _BOT_WAKE.sub("", strip_verified_mention_markup(value, targets), count=1).strip()
    value = _CODEX_COMMAND.sub("", value, count=1).strip() if _CODEX_COMMAND.match(value) else value
    value = _CODEX_PREFIX.sub("", value, count=1).strip()
    return "" if value.startswith("/") else value


def _matched_directive(value: str, prior_topic="") -> RecipientRequest | None:
    match = _DIRECT.fullmatch(value)
    if match:
        topic = match.group("topic")
        if topic.startswith(("过", "了吗", "了么")):
            return None
        return RecipientRequest(match.group("label").lstrip("@"),
                                " ".join(part for part in (match.group("action"), prior_topic, topic) if part).strip())
    return None


def parse_recipient_request(text: str, *, native_targets=()) -> RecipientRequest | None:
    """Recognize a generation audience, leaving literal at-and-tell alone."""
    value = _direct(text, native_targets)
    if not value or _NEGATIVE.search(value) or _EXTERNAL.search(value) or _EXAMPLE_PREFIX.search(value):
        return None
    request = _matched_directive(value)
    if request:
        return request
    # Explicit topic-first requests still name the same current reply audience.
    pieces = re.split(r"[,，;；]", value)
    if len(pieces) > 1:
        request = _matched_directive(pieces[-1].strip(), "，".join(pieces[:-1]).strip())
        if request:
            return request
    ba = re.match(r"^" + _PREFIX + r"把(?P<topic>.+?)(?P<directive>(?:向|给|跟|对|为).+)$", value)
    if ba:
        request = _matched_directive(ba.group("directive"), ba.group("topic"))
        if request:
            return request
    match = _TELL_QUESTION.fullmatch(value)
    if match and (_QUESTION.search(match.group("topic")) or _GENERATING.match(match.group("topic"))):
        return RecipientRequest(match.group("label"), match.group("topic"))
    mention = parse_mention_request(value, native_targets=native_targets)
    if mention and mention.body and (_GENERATING.match(mention.body) or _QUESTION.search(mention.body)):
        return RecipientRequest(mention.target_label, mention.body)
    if native_targets and (_GENERATING.match(value) or _QUESTION.search(value) or _CONTINUATION.fullmatch(value)):
        return RecipientRequest("被提及的成员", value)
    return None


def _semantic_request(text: str, label: str, targets=()) -> RecipientRequest | None:
    """Ground a model-selected *label* in the user's direct addressing act."""
    value = _direct(text, targets)
    if (not isinstance(label, str) or not _LABEL.fullmatch(label) or label.startswith("@")
            or not value or _NEGATIVE.search(value) or _EXTERNAL.search(value) or _EXAMPLE_PREFIX.search(value)
            or any(mark in value for mark in ('`', '"', "'", "“", "”", "‘", "’", "「", "」", "『", "』"))):
        return None
    addressed = re.search(r"(?:向|给|跟|对|为|告诉)\s*" + re.escape(label) +
                          r"(?P<after>[^，,；;。\n]{0,64})", value)
    if not addressed:
        return None
    before = value[:addressed.start()]
    if re.search(r"(?:如何|怎么|研究|调研|示例|例如|比如|假设|如果|写(?:代码|程序)|编写)", before):
        return None
    if not re.search(r"(?:讲|说|聊|介绍|解释|说明|分析|总结|科普|分享|阐述|回答|告知)", addressed.group("after")):
        return None
    return RecipientRequest(label, value)


def validate_recipient(value, *, umo: str, sender_id: str, owner: str | None = None,
                       request_text: str | None = None) -> dict | None:
    """Recheck persisted metadata against its trusted route before every use."""
    if (not isinstance(value, dict) or set(value) != _FIELDS or type(value.get("version")) is not int
            or value["version"] != 1 or type(value.get("inherited")) is not bool
            or not isinstance(umo, str) or not _GROUP.fullmatch(umo)
            or not isinstance(sender_id, str) or not _ID.fullmatch(sender_id)):
        return None
    expected = owner_key(umo, sender_id)
    if (owner is not None and owner != expected or value.get("owner") != expected
            or value.get("umo") != umo or value.get("sender") != sender_id):
        return None
    target, label, source, digest = (value.get(key) for key in ("target_id", "target_label", "source", "request_sha256"))
    if (not isinstance(target, str) or not _ID.fullmatch(target) or target.lower() in _ALL | {"qq_official"}
            or not isinstance(label, str) or not _LABEL.fullmatch(label) or label.lower() in _ALL
            or source not in {"native", "relationship", "self"}
            or not isinstance(digest, str) or not _HASH.fullmatch(digest)):
        return None
    if source == "self" and (target != sender_id or label.lower() not in _SELF):
        return None
    if source == "relationship" and not _RELATION_LABEL.fullmatch(label):
        return None
    if request_text is not None and digest != _hash(request_text):
        return None
    return dict(value)


def resolve_recipient(text: str, *, umo: str, sender_id: str, platform_name="qq_official",
                      native_targets=(), relationship_contents=(), target_label=None, codex=False) -> RecipientResolution:
    """Resolve direct generation audience using only caller-verified evidence."""
    if platform_name != "qq_official" or not _GROUP.fullmatch(str(umo or "")) or not _ID.fullmatch(str(sender_id or "")):
        return RecipientResolution("none", reason="group_only")
    request = parse_recipient_request(text, native_targets=native_targets)
    value = _direct(text, native_targets)
    if codex:
        label = target_label or (request.target_label if request else "被提及的成员" if native_targets else "")
        if not isinstance(label, str) or not _LABEL.fullmatch(label):
            return RecipientResolution("clarify", reason="missing_target_label")
        request = RecipientRequest(label, text)
    elif target_label is not None:
        if request is not None and target_label != request.target_label:
            return RecipientResolution("clarify", request, reason="target_label_mismatch")
        if request is None:
            request = _semantic_request(text, target_label, native_targets)
    if request is None:
        if native_targets and _MARKUP.search(value):
            return RecipientResolution("clarify", RecipientRequest("被提及的成员", value), reason="unsafe_markup")
        return RecipientResolution("none")
    label = request.target_label
    if _MARKUP.search(value):
        return RecipientResolution("clarify", request, reason="unsafe_markup")
    targets = tuple(dict.fromkeys(str(item) for item in native_targets if str(item)))
    if label.lower() in _ALL or any(item.lower() in _ALL for item in targets):
        return RecipientResolution("clarify", request, reason="all_not_supported")
    if any(not _ID.fullmatch(item) for item in targets):
        return RecipientResolution("clarify", request, reason="invalid_native_target")
    if len(targets) > 1:
        return RecipientResolution("clarify", request, reason="multiple_targets")
    if label.lower() in _SELF:
        if targets and targets[0] != sender_id:
            return RecipientResolution("clarify", request, reason="conflicting_targets")
        target, source = sender_id, "self"
    elif targets:
        target, source = targets[0], "native"
    else:
        target = resolve_relationship(list(relationship_contents), sender_id, label)
        if not target:
            return RecipientResolution("clarify", request, reason="unresolved_target")
        source = "relationship"
    metadata = {"version": 1, "owner": owner_key(umo, sender_id), "umo": umo, "sender": sender_id,
                "target_id": target, "target_label": label, "source": source,
                "request_sha256": _hash(text), "inherited": False}
    valid = validate_recipient(metadata, umo=umo, sender_id=sender_id, request_text=text)
    return RecipientResolution("ready", request, valid) if valid else RecipientResolution("clarify", request, reason="invalid_recipient")


def _literal_recipient(text: str, *, umo: str, sender_id: str, native_targets=(),
                       relationship_contents=(), target_label=None) -> RecipientResolution:
    if not _GROUP.fullmatch(umo) or not _ID.fullmatch(sender_id):
        return RecipientResolution("none", reason="group_only")
    direct = _direct(text, native_targets)
    if not direct or _NEGATIVE.search(direct) or _EXTERNAL.search(direct) or _EXAMPLE_PREFIX.search(direct):
        return RecipientResolution("none")
    mention = parse_mention_request(direct, sender_id=sender_id, native_targets=native_targets)
    if mention is None:
        return RecipientResolution("none")
    request = RecipientRequest(mention.target_label, mention.body, "literal")
    if target_label is not None and target_label != mention.target_label:
        return RecipientResolution("clarify", request, reason="target_label_mismatch")
    target, source = mention.target_id, "self" if mention.is_self else "native"
    if mention.needs_clarification:
        if mention.reason != "unresolved_target":
            return RecipientResolution("clarify", request, reason=mention.reason)
        target = resolve_relationship(list(relationship_contents), sender_id, mention.target_label)
        source = "relationship"
        if not target:
            return RecipientResolution("clarify", request, reason="unresolved_target")
    metadata = {"version": 1, "owner": owner_key(umo, sender_id), "umo": umo, "sender": sender_id,
                "target_id": target, "target_label": mention.target_label, "source": source,
                "request_sha256": _hash(text), "inherited": False}
    valid = validate_recipient(metadata, umo=umo, sender_id=sender_id, request_text=text)
    return RecipientResolution("ready", request, valid) if valid else RecipientResolution("clarify", request, reason="invalid_recipient")


def recipient_for_followup(parent, text: str, *, umo: str, sender_id: str,
                          platform_name="qq_official", native_targets=(), relationship_contents=(),
                          continuing=False, new_conversation=False, target_label=None) -> RecipientResolution:
    """Explicit new recipients win; inherit only a verified relevant follow-up.

    ``continuing`` must be supplied only after the caller binds a quoted parent
    to this route's owner. A generic new question never inherits an old At.
    """
    current = resolve_recipient(text, umo=umo, sender_id=sender_id, platform_name=platform_name,
                                native_targets=native_targets, relationship_contents=relationship_contents,
                                target_label=target_label)
    if current.status != "none":
        return current
    if new_conversation or new_codex_conversation(text):
        return RecipientResolution("none", reason="new_conversation")
    direct = _direct(text, native_targets)
    if (platform_name != "qq_official" or not continuing or not direct or _NEGATIVE.search(direct)
            or parse_mention_request(direct, native_targets=native_targets) is not None
            or not (retry_intent(direct) or _CONTINUATION.fullmatch(direct))):
        return RecipientResolution("none")
    inherited = validate_recipient(parent, umo=umo, sender_id=sender_id)
    if inherited is None:
        return RecipientResolution("none", reason="invalid_parent_recipient")
    inherited.update(request_sha256=_hash(text), inherited=True)
    return RecipientResolution("ready", RecipientRequest(inherited["target_label"], direct), inherited)


def _event_text(event) -> str:
    extra = getattr(event, "get_extra", None)
    if callable(extra) and extra("voice_input_transcribed"):
        return str(getattr(event, "message_str", "") or extra("voice_input_transcript") or "").strip()
    getter = getattr(event, "get_messages", None)
    return " ".join(str(part.text) for part in (getter() if callable(getter) else ())
                    if type(part).__name__ == "Plain" and isinstance(getattr(part, "text", None), str)).strip()


class ReplyRecipientContext:
    """One event's grounded semantic tool and shared prepared recipient."""
    def __init__(self, event, text: str, relationship_contents=(), *, parent=None,
                 continuing=False, new_conversation=False):
        self.event = event
        self.text = str(text or "")
        self.relationship_contents = tuple(relationship_contents)
        self._event_id = id(event)
        self._umo = str(getattr(event, "unified_msg_origin", "") or "")
        self._sender = str(event.get_sender_id() or "")
        self._parent = parent
        self._continuing = bool(continuing)
        self._new_conversation = bool(new_conversation)
        self._resolution = RecipientResolution("none")

    def _bound(self):
        return (id(self.event) == self._event_id and self.event.get_platform_name() == "qq_official"
                and bool(getattr(self.event, "is_at_or_wake_command", False))
                and str(self.event.unified_msg_origin) == self._umo
                and str(self.event.get_sender_id() or "") == self._sender
                and _hash(_event_text(self.event)) == _hash(self.text))

    def prepare(self, target_label=None, *, parent=_UNSET, continuing=None, new_conversation=None) -> dict:
        if parent is not _UNSET:
            self._parent = parent
        if continuing is not None:
            self._continuing = bool(continuing)
        if new_conversation is not None:
            self._new_conversation = bool(new_conversation)
        if not self._bound():
            self._resolution = RecipientResolution("none", reason="event_binding_changed")
        else:
            native_targets = native_mention_targets(self.event)
            self._resolution = recipient_for_followup(
                self._parent, self.text, umo=self._umo, sender_id=self._sender,
                native_targets=native_targets, relationship_contents=self.relationship_contents,
                target_label=target_label, continuing=self._continuing, new_conversation=self._new_conversation)
            if self._resolution.status == "none":
                self._resolution = _literal_recipient(
                    self.text, umo=self._umo, sender_id=self._sender, native_targets=native_targets,
                    relationship_contents=self.relationship_contents, target_label=target_label)
        return self._result()

    def _result(self) -> dict:
        resolution = self._resolution
        return {"ok": resolution.status == "ready", "status": resolution.status,
                "recipient_set": resolution.status == "ready", "reason": resolution.reason,
                **({"target_label": resolution.request.target_label, "action": resolution.request.action} if resolution.request else {}),
                "sent": False}

    def metadata(self) -> dict | None:
        if not self._bound() or self._resolution.status != "ready":
            return None
        return validate_recipient(self._resolution.recipient, umo=self._umo, sender_id=self._sender, request_text=self.text)

    def schemas(self) -> list[dict]:
        if not self._bound() or not _GROUP.fullmatch(self._umo):
            return []
        return [{"type": "function", "function": {"name": RECIPIENT_TOOL_NAME,
                "description": RECIPIENT_TOOL_DESCRIPTION, "parameters": RECIPIENT_TOOL_SCHEMA}}]

    async def execute(self, name: str, args: dict, call_id=None, *, codex=False) -> dict:
        if (name != RECIPIENT_TOOL_NAME or not isinstance(args, dict) or set(args) - {"target_label"}
                or "target_label" in args and (not isinstance(args["target_label"], str) or not 0 < len(args["target_label"]) <= 40)):
            return {"ok": False, "error": "此工具只解析当前用户原文，不接受账号、群或改写参数。", "sent": False}
        if codex:
            if not self._bound():
                self._resolution = RecipientResolution("none", reason="event_binding_changed")
            else:
                self._resolution = resolve_recipient(self.text, umo=self._umo, sender_id=self._sender,
                    native_targets=native_mention_targets(self.event), relationship_contents=self.relationship_contents,
                    target_label=args.get("target_label"), codex=True)
            return self._result()
        return self.prepare(args.get("target_label"))
