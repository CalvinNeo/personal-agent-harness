"""Resolve exact self-relative family labels from explicit stored QQ facts.

The caller selects authorized memory contents from this same group. This pure
module neither reads memory nor searches people, and never interprets prose as
an instruction. An unknown or conflicting relationship has no resolved target.
"""
from __future__ import annotations

import re
import unicodedata


_ID = r"[A-Za-z0-9_-]{1,128}"
_FACT = re.compile(
    r"^<@!?(?P<subject>" + _ID + r")>\s*是\s*<@!?(?P<object>" + _ID +
    r")>\s*的\s*(?P<relation>老公|丈夫|老婆|妻子|爸爸|父亲|妈妈|母亲)$"
)
_HEADER = re.compile(
    r"^(?:(?:请)?记住\s*)?(?:(?:本群|当前群|群里)(?:的)?\s*)?"
    r"(?:人物关系|成员关系|家庭关系|关系)\s*(?:是|为)?\s*[:：]\s*"
)
_KINDS = {
    "老公": "husband", "丈夫": "husband", "老婆": "wife", "妻子": "wife",
    "爸爸": "father", "父亲": "father", "妈妈": "mother", "母亲": "mother",
}
_LABEL = re.compile(r"^我(?:的)?(老公|丈夫|老婆|妻子|爸爸|父亲|妈妈|母亲)$")
_RESERVED = frozenset({"all", "everyone", "here", "qq_official"})


def _facts(content: str) -> list[tuple[str, str, str]]:
    """Accept only an entire direct fact record, never embedded examples."""
    value = unicodedata.normalize("NFKC", content).strip()
    if not value or len(value) > 6000:
        return []
    # Direct records need no quotes or code delimiters. Reject the whole entry
    # instead of lifting a fact out of a quoted, fenced, or instructional text.
    if any(marker in value for marker in ('`', '"', "'", "“", "”", "‘", "’", "「", "」", "『", "』")):
        return []
    if re.search(r"(?m)^\s*>|(?:如果|假设|例如|比如|示例|例子|教程|引用|忽略|提示词|指令|是否|是不是)", value):
        return []
    value = _HEADER.sub("", value, count=1)
    value = re.sub(r"^(?:请)?记住\s*[:：]?\s*", "", value, count=1)
    clauses = [part.strip() for part in re.split(r"[，,；;。\n]+", value) if part.strip()]
    facts = []
    for clause in clauses:
        match = _FACT.fullmatch(clause)
        if not match:
            return []
        subject, other = match.group("subject"), match.group("object")
        if subject == other or subject.lower() in _RESERVED or other.lower() in _RESERVED:
            return []
        facts.append((subject, other, _KINDS[match.group("relation")]))
    return facts


def resolve_relationship(contents: list[str], sender_id: str, label: str) -> str | None:
    """Return one exact member ID for 我(的)老婆/老公/爸爸/妈妈 synonyms.

    ``<@A> 是 <@B> 的老公`` means B's husband is A and A's wife is B.
    Parent facts resolve only the child's parent; no child/sibling identity is
    inferred. Multiple distinct targets for the requested relation return None.
    """
    normalized = re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(label or "")))
    match = _LABEL.fullmatch(normalized)
    sender = str(sender_id or "")
    if not match or not re.fullmatch(_ID, sender) or sender.lower() in _RESERVED:
        return None
    desired = _KINDS[match.group(1)]
    targets = set()
    for content in contents or []:
        if not isinstance(content, str):
            continue
        for subject, other, relation in _facts(content):
            if other == sender and relation == desired:
                targets.add(subject)
            if subject == sender and (relation, desired) in {("husband", "wife"), ("wife", "husband")}:
                targets.add(other)
    return next(iter(targets)) if len(targets) == 1 else None
