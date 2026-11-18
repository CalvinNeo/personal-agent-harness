"""Parse direct requests to mention somebody in the current QQ group.

This module never resolves names, looks up members, or sends a message. Only a
current sender ID or a direct native At supplied by the caller can resolve a
recipient. A relation such as ``我老婆`` is a label, never an identity.
"""
from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class MentionRequest:
    target_label: str
    body: str = ""
    target_id: str = ""
    is_self: bool = False
    needs_clarification: bool = False
    reason: str = ""


_PREFIX = (
    r"(?:(?:请|麻烦(?:你)?|帮我|帮忙|你|现在|先)\s*)*"
    r"(?:(?:在)?(?:当前|这个|本)?群(?:聊)?(?:里|内|中)?\s*)?"
    r"(?:测试(?:一下|下)?\s*[,，]?\s*)?"
)
_COMMAND = re.compile(
    r"^" + _PREFIX + r"(?:at(?=\s|[^A-Za-z0-9_]|$)|@|艾特|提及)\s*(?:一下\s*)?(?P<rest>.*)$",
    re.I,
)
_TELL = re.compile(
    r"^(?:(?:并|然后|再|顺便)\s*)?(?:告诉(?:他|她|它|我|对方|这位成员)|"
    r"跟(?:他|她|我|对方)说|对(?:他|她|我|对方)说|说(?:一声)?|"
    r"(?:发送|发)(?:一句|一条)?(?:消息)?|内容(?:是|为)?)\s*[:：,，]?\s*",
    re.I,
)
_BODY_SPLIT = re.compile(
    r"[,，;；:：\n]|\s+(?!一下(?:$|[。!！]))|"
    r"(?=(?:并|然后|再|顺便)?(?:告诉[他她它我]|跟[他她我]说|对[他她我]说|说一声|说[:：]))"
)
_SELF = frozenset({"我", "我自己", "本人", "自己", "me", "myself"})
_ALL = frozenset({"all", "everyone", "here", "全体", "全体成员", "所有人", "大家", "全部成员", "所有成员"})
_LABEL = re.compile(r"^[\w\u3400-\u9fff·.-]{1,40}$", re.UNICODE)
_UNSAFE_MARKUP = re.compile(
    r"<\s*(?:[/!?]?[A-Za-z_]|@)|\[(?:CQ|At|AtAll)\s*[:：]", re.I,
)
_QUESTION = re.compile(r"(?:是什么意思|是什么(?:意思)?|如何|怎么|用法|语法|示例|例子|代码|正则|API|接口|实现|研究|调研|解释|教程|可不可以|能不能|可以吗|行吗|吗|么|\?|？)", re.I)
_BOT_WAKE = re.compile(r"^@[\w\u3400-\u9fff·.-]*?(?:的智能体|机器人)(?=\s|[,，:：]|$)\s*[,，:：]?\s*")
_NATIVE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _field(value, name, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def native_mention_targets(event, *, bot_id: str = "") -> tuple[str, ...]:
    """Read direct At components and current-group QQ native mention metadata.

    AstrBot currently converts only the bot's own mention into an At. Member
    mentions are still in the SDK's GroupMessage.mentions; accept that list
    only when its group_openid equals this event's nonempty group_id. Neither
    raw text, raw_data, Reply, nor Forward content is consulted.
    """
    if not bot_id:
        getter = getattr(event, "get_self_id", None)
        if callable(getter):
            bot_id = str(getter() or "")
        if not bot_id:
            bot_id = str(_field(getattr(event, "message_obj", None), "self_id", "") or "")
    getter = getattr(event, "get_messages", None)
    parts = getter() if callable(getter) else ()
    result = []
    for part in parts or ():
        # Do not accept text, dicts, or nested quoted components as native IDs.
        if type(part).__name__ not in {"At", "AtAll"}:
            continue
        value = "all" if type(part).__name__ == "AtAll" else str(getattr(part, "qq", "") or "").strip()
        if not value or value == bot_id or value == "qq_official":
            continue
        if value not in result:
            result.append(value)
    platform_getter = getattr(event, "get_platform_name", None)
    platform = platform_getter() if callable(platform_getter) else ""
    message = getattr(event, "message_obj", None)
    group_id = str(_field(message, "group_id", "") or "")
    raw = _field(message, "raw_message")
    if platform != "qq_official" or not group_id or str(_field(raw, "group_openid", "") or "") != group_id:
        return tuple(result)
    mentions = _field(raw, "mentions", ())
    if not isinstance(mentions, (list, tuple)):
        return tuple(result)
    for mention in mentions[:100]:
        value = str(_field(mention, "id", "") or "").strip()
        if (_field(mention, "is_you") is True or _field(mention, "bot") is True
                or value in {bot_id, "qq_official"} or not _NATIVE_ID.fullmatch(value)):
            continue
        if value not in result:
            result.append(value)
    return tuple(result)


def strip_verified_mention_markup(text: str, targets: Iterable[str]) -> str:
    """Remove only exact QQ user mention tokens backed by native target IDs.

    Other XML, additional attributes, all-mentions, and unverified IDs remain
    unchanged so the parser's markup rejection still applies. This function
    never derives IDs from text and does not decode HTML entities.
    """
    verified = {str(target) for target in targets
                if _NATIVE_ID.fullmatch(str(target)) and str(target).lower() not in _ALL}
    if not verified:
        return str(text or "")
    ids = "(?:" + "|".join(re.escape(target) for target in sorted(verified)) + ")"
    token = re.compile(r'<qqbot-at-user\s+id="' + ids + r'"\s*/>|<@!?' + ids + r'>')
    return token.sub(" ", str(text or ""))


def _body(value: str) -> str:
    value = value.strip(" \t\r\n,，;；:：")
    value = _TELL.sub("", value, count=1)
    return value.strip()


def parse_mention_request(
    text: str,
    *,
    sender_id: str = "",
    bot_id: str = "",
    native_targets: Iterable[str] = (),
) -> MentionRequest | None:
    """Parse an anchored current-group instruction, without granting authority.

    Unknown names and relations return a clarification request. The caller must
    enforce the QQ platform, group scope, allowlist, and actual send outcome.
    ``native_targets`` must come from direct native At components, never text or
    quoted history. This function does not interpret any instructions in body.
    """
    value = unicodedata.normalize("NFKC", html.unescape(str(text or ""))).strip()
    if not value or len(value) > 2000 or any(ord(c) < 32 and c not in "\t\r\n" for c in value):
        return None
    if value.startswith((">", "`", '"', "'", "“", "‘", "「", "『", "/")) or "```" in value:
        return None
    # A quoted/sample instruction on a later line must not become executable.
    if re.search(r"(?m)^\s*>|`[^`\n]*`", value):
        return None
    # Some QQ clients serialize the bot wake-up as Plain text. A recognized
    # bot alias addresses this handler, and must not become a member recipient.
    # Ordinary member labels such as @张三 remain mention instructions.
    value = _BOT_WAKE.sub("", value, count=1).strip()
    if not value:
        return None

    targets = tuple(dict.fromkeys(
        str(item).strip() for item in native_targets
        if str(item).strip() and str(item).strip() not in {str(bot_id), "qq_official"}
    ))
    match = _COMMAND.match(value)
    if match:
        rest = match.group("rest").strip()
        pieces = _BODY_SPLIT.split(rest, maxsplit=1)
        label = pieces[0].strip().rstrip("。!！")
        body = _body(pieces[1]) if len(pieces) == 2 else ""
        label = re.sub(r"(?:一下|一声|下)$", "", label).strip()
        # English standalone 'at' in explanations and questions is not an act.
        if not label and targets:
            label = "被提及的成员"
        if not label or not _LABEL.fullmatch(label) or _QUESTION.search(label):
            return None
        is_self = label.lower() in _SELF
    else:
        # A real member At may carry the recipient outside Plain text.
        native_tell = re.match(r"^" + _PREFIX + r"(?P<telling>.+)$", value, re.I) if targets else None
        if not native_tell or not _TELL.match(native_tell.group("telling")):
            return None
        label = "被提及的成员"
        body = _body(native_tell.group("telling"))
        is_self = False

    if _UNSAFE_MARKUP.search(value):
        return MentionRequest(label, body, is_self=is_self, needs_clarification=True, reason="unsafe_markup")
    if label.lower() in _ALL or any(target.lower() in _ALL for target in targets):
        return MentionRequest(label, body, is_self=is_self, needs_clarification=True, reason="all_not_supported")
    if is_self:
        if not sender_id:
            return MentionRequest(label, body, is_self=True, needs_clarification=True, reason="missing_sender")
        if targets and any(target != str(sender_id) for target in targets):
            return MentionRequest(label, body, is_self=True, needs_clarification=True, reason="conflicting_targets")
        return MentionRequest(label, body, str(sender_id), is_self=True)
    if len(targets) > 1:
        return MentionRequest(label, body, needs_clarification=True, reason="multiple_targets")
    if len(targets) == 1:
        return MentionRequest(label, body, targets[0])
    return MentionRequest(label, body, needs_clarification=True, reason="unresolved_target")
