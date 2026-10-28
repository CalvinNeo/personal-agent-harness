"""Bounded in-memory image references; no network, filesystem or URL logging."""
from __future__ import annotations

import copy
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from urllib.parse import urlsplit

TTL_SECONDS = 30 * 60
BATCH_GAP_SECONDS = 2 * 60
MAX_IMAGES = 12
MAX_SESSIONS = 64
_QQ_IMAGE_DOMAINS = ("qpic.cn", "qlogo.cn", "gtimg.cn", "gtimg.com")
_QQ_IMAGE_HOSTS = {"multimedia.nt.qq.com", "multimedia.nt.qq.com.cn"}
_COUNT = r"(?:\d{1,3}|一|二|两|三|四|五|六|七|八|九|十|十一|十二|几)"
_REFERENCE = re.compile(
    rf"(?:(?:刚刚|刚才|刚|之前|前面|上面)(?:我)?(?:发(?:送|来|给你)?)?(?:过)?(?:的)?(?:这|那)?(?P<count>{_COUNT})?张?(?:图片|照片|图)|"
    rf"(?:上|前)(?:一|1)张(?:图片|照片|图)|(?:我)?(?:刚刚|刚才|刚)发(?:送|来|给你)?(?:的)?(?:这|那)?(?P<batch_count>{_COUNT})?张(?:图片|照片|图))"
)
_NUMBERS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
            "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12}


class ImageReferenceError(ValueError):
    pass


@dataclass(frozen=True)
class ImageSelection:
    count: int | None = None  # None means the latest contiguous upload batch.


def parse_reference(text: str) -> ImageSelection | None:
    if not isinstance(text, str) or text.lstrip().startswith("/"):
        return None
    matches = list(_REFERENCE.finditer(text))
    if not matches:
        return None
    if len(matches) > 1:
        raise ImageReferenceError("请明确要看最近几张图片，例如“分析刚才三张图”，或直接重新发送要分析的图片。")
    match = matches[0]
    if re.fullmatch(r"(?:上|前)(?:一|1)张(?:图片|照片|图)", match[0]):
        return ImageSelection(1)
    count = match["count"] or match["batch_count"]
    if count is None:
        return ImageSelection(1 if "张" in match[0] else None)
    if count == "几":
        return ImageSelection()
    number = int(count) if count.isascii() and count.isdigit() else _NUMBERS[count]
    if not 1 <= number <= MAX_IMAGES:
        raise ImageReferenceError(f"一次最多分析最近 {MAX_IMAGES} 张图片，请缩小范围或重新发送。")
    return ImageSelection(number)


def is_safe_image_ref(ref: object) -> bool:
    """Only direct QQ attachment HTTPS URLs; never accept user text/file paths."""
    if not isinstance(ref, str) or len(ref) > 8192 or any(c.isspace() for c in ref):
        return False
    try:
        parsed = urlsplit(ref)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443):
            return False
        if parsed.fragment:
            return False
        return host in _QQ_IMAGE_HOSTS or any(host == domain or host.endswith("." + domain) for domain in _QQ_IMAGE_DOMAINS)
    except ValueError:
        return False


@dataclass
class _Entry:
    ref: str = field(repr=False)
    received_at: float
    batch_id: int


class ImageCache:
    """Isolation key is the full (UMO, sender) pair, never group or sender alone."""
    def __init__(self):
        self._sessions: OrderedDict[tuple[str, str], list[_Entry]] = OrderedDict()
        self._batch_id = 0

    def clear(self):
        self._sessions.clear()

    def prune(self, now: float):
        for key in list(self._sessions):
            entries = [entry for entry in self._sessions[key] if now - entry.received_at < TTL_SECONDS]
            if entries:
                self._sessions[key] = entries
            else:
                del self._sessions[key]

    def remember(self, key: tuple[str, str], refs: list[str], now: float):
        self.prune(now)
        refs = list(dict.fromkeys(ref for ref in refs if is_safe_image_ref(ref)))[:MAX_IMAGES]
        if not refs:
            return
        entries = self._sessions.get(key, [])
        if entries and now - entries[-1].received_at <= BATCH_GAP_SECONDS:
            batch_id = entries[-1].batch_id
        else:
            self._batch_id += 1
            batch_id = self._batch_id
        # Keep each URL only once, at its most recent received position.
        entries = [entry for entry in entries if entry.ref not in refs]
        entries.extend(_Entry(ref, now, batch_id) for ref in refs)
        self._sessions[key] = entries[-MAX_IMAGES:]
        self._sessions.move_to_end(key)
        while len(self._sessions) > MAX_SESSIONS:
            self._sessions.popitem(last=False)

    def select(self, key: tuple[str, str], selection: ImageSelection, now: float) -> list[str]:
        self.prune(now)
        entries = self._sessions.get(key, [])
        if not entries:
            raise ImageReferenceError("我没有收到你在本群最近 30 分钟内可用的图片。请重新发送图片并 @我，再告诉我要分析什么。")
        self._sessions.move_to_end(key)
        if selection.count is not None:
            if len(entries) < selection.count:
                raise ImageReferenceError(f"我这里只找到你在本群最近 30 分钟发来的 {len(entries)} 张图片，不足 {selection.count} 张。请重新发送要分析的图片并 @我。")
            entries = entries[-selection.count:]
        else:
            entries = [entry for entry in entries if entry.batch_id == entries[-1].batch_id]
        return [entry.ref for entry in entries]


def without_historical_images(contexts: list[dict]) -> list[dict]:
    """Copy request history and remove visuals without mutating shared storage."""
    result = copy.deepcopy(contexts)
    for message in result:
        if not isinstance(message, dict) or not isinstance(message.get("content"), list):
            continue
        original = message["content"]
        kept = [part for part in original if not (
            isinstance(part, dict) and
            (part.get("type") in {"image_url", "input_image", "image"} or "image_url" in part)
        )]
        if len(kept) != len(original):
            message["content"] = kept or [{"type": "text", "text": "[历史图片已省略；本次仅使用当前发送者明确引用的图片。]"}]
    return result
