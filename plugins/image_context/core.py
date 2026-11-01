"""Bounded image references; no network requests or image/URL logging."""
from __future__ import annotations

import copy
import math
import re
import stat
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

TTL_SECONDS = 30 * 60
BATCH_GAP_SECONDS = 2 * 60
IMPLICIT_REFERENCE_SECONDS = 2 * 60
MAX_IMAGES = 12
MAX_SESSIONS = 64
MAX_LOCAL_IMAGE_BYTES = 20 * 1024 * 1024
_QQ_IMAGE_DOMAINS = ("qpic.cn", "qlogo.cn", "gtimg.cn", "gtimg.com")
_QQ_IMAGE_HOSTS = {"multimedia.nt.qq.com", "multimedia.nt.qq.com.cn"}
_COUNT = r"(?:\d{1,3}|一|二|两|三|四|五|六|七|八|九|十|十一|十二|几)"
_IMAGE_NOUN = r"(?:图片|照片|截图|图)(?![片形表案库标鉴册文书纸])"
_REFERENCE = re.compile(
    rf"(?:(?:刚刚|刚才|刚|才|最近|之前|前面|上面|新)(?:我)?(?:又|新|刚)?"
    rf"(?:发(?:送|来|给你)?)?(?:了|过)?(?:的)?(?:这|那)?(?:个|张)?(?P<count>{_COUNT})?张?{_IMAGE_NOUN}|"
    rf"(?:上|前)(?:一|1)张{_IMAGE_NOUN}|"
    rf"(?:这|那)(?:个|张)?(?P<batch_count>{_COUNT})?张?{_IMAGE_NOUN})"
)
_BARE_DEICTIC_REFERENCE = re.compile(
    r"(?:刚刚|刚才|上面|前面|最近)(?:的)?(?:这|那)?(?:一|1)?张(?![票纸表单图纸])"
)
# Natural descriptions such as “发了个晚饭的图，帮我算热量” refer to the
# upload immediately before the addressed request. Keep this pattern bounded
# to an upload verb and a short label so an arbitrary message containing “图”
# cannot inherit an old image.
_DESCRIPTIVE_UPLOAD_REFERENCE = re.compile(
    rf"(?:我)?(?:刚刚|刚才|刚|最近|之前|前面)?(?:又)?"
    rf"发(?:送|来|给你)?(?:了|过|的)?"
    rf"(?P<upload_count>{_COUNT})?(?:个|张)?"
    rf"[^，。！？?!\n]{{1,24}}?(?:的)?{_IMAGE_NOUN}"
)
# QQ users often put the upload receipt before the action: “图片发你了，帮我
# 看看” or “图发给你了，算一下热量”.  Keep this form deliberately strict:
# the image noun must be immediately followed by an upload verb, and the match
# must begin at the message (apart from “我把/我将”) so questions such as
# “如何把图片发给你” and quoted examples do not inherit an old image.
_UPLOAD_CONFIRMATION_REFERENCE = re.compile(
    rf"^(?:(?:我)?(?:把|将)?{_IMAGE_NOUN}"
    rf"(?:发(?:送|来)?(?:给)?(?:你|您)?|传(?:给(?:你|您))?|上传)(?:了|过)|"
    rf"(?:我)?(?:刚刚|刚才|刚)?发(?:送|来)?(?:了|过)?(?:一|1)?(?:张)?{_IMAGE_NOUN}"
    rf"(?:给(?:你|您)|(?:你|您))?(?:了|过)?)"
)
_UPLOAD_CONFIRMATION_ACTION = re.compile(
    # Keep the action explicit while allowing a short object suffix such as
    # “算一下热量” or “看看里面有什么”。
    r"^(?:(?:帮我|请|麻烦)?(?:看看?|看一下|看下|分析(?:一下|下)?|识别(?:一下|下)?|"
    r"算(?:一下|下)?|估算(?:一下|下)?|判断(?:一下|下)?|描述(?:一下|下)?|"
    r"解释(?:一下|下)?|说说|告诉我|里面(?:有(?:什么|啥)|写了什么)|"
    r"多少(?:热量|卡路里|千卡)|能(?:不能)?看到|收到(?:了)?))"
    r"[^，,。！？?!]{0,24}$"
)
_RECEIPT_REFERENCE = re.compile(
    rf"(?:(?:请|再)?(?:看看|看一下|看下))?(?:你|您)?(?:现在)?"
    rf"(?:能否|能不能|能|是否|有没有|有)?(?:看(?:得到|见|到)|收到)(?:了)?"
    rf"(?:我发(?:送|来)?的)?(?:(?:这|那)(?:些|张))?{_IMAGE_NOUN}(?:了)?(?:吗|么|没|没有)?|"
    rf"(?:(?:这|那)(?:些|张))?{_IMAGE_NOUN}(?:你|您)?"
    rf"(?:能否|能不能|能|是否|有没有|有)?(?:看(?:得到|见|到)|收到)(?:了)?(?:吗|么|没|没有)?"
)
# A short deictic question is meaningful only immediately after this sender's
# upload. Full matching prevents unrelated questions from inheriting images.
_IMPLICIT_REFERENCE = re.compile(
    r"(?:请|麻烦)?(?:你)?(?:帮我)?(?:再)?(?:看看|看一下|看下|分析一下|分析下|识别一下|识别下)?"
    r"(?:这|这个|这些|那|那个|那些)(?:是(?:什么|啥)(?:东西)?|是什么情况|里面有什么|上面写了什么)"
    r"(?:呀|啊|呢|吗|么)?|"
    # Short follow-ups with an image noun are only enabled when the cache has
    # a fresh image for this exact sender/UMO.  Full matching keeps “图书”、
    # “图表”、and “看图说话” out of the implicit path.
    r"(?:请|麻烦)?(?:你)?(?:帮我)?(?:再)?(?:看(?:看|一下|下)|分析(?:一下|下)?|识别(?:一下|下)?|"
    r"算(?:一下|下)?|估算(?:一下|下)?|判断(?:一下|下)?|描述(?:一下|下)?|解释(?:一下|下)?)?"
    rf"(?:这|这个|这些|那|那个|那些)?(?:个|张)?{_IMAGE_NOUN}"
    r"(?:请|麻烦)?(?:你)?(?:帮我)?(?:再)?(?:看(?:看|一下|下)|分析(?:一下|下)?|识别(?:一下|下)?|"
    r"算(?:一下|下)?|估算(?:一下|下)?|判断(?:一下|下)?|描述(?:一下|下)?|解释(?:一下|下)?)?"
    r"(?:呀|啊|呢|吗|么)?"
)
_NUMBERS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
            "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12}
_NON_REQUEST_PREFIX = re.compile(
    r"^(?:如何|怎么(?:样)?(?:才能|能|让|把|将|使用|用)?|怎样|为什么|例如|比如|举例|示例|"
    r"引用|转发|他说|她说|有人说|我说[‘’\"“”：:])"
)
_REFUSAL = re.compile(r"(?:不要|不用|别|不需要|无需)(?:再)?(?:看|分析|识别|处理|估算|算|用)")


def _is_request_text(text: str) -> bool:
    """Reject meta-discussion/quoted statements before selecting any cache.

    This is intentionally a narrow rejection gate: it does not try to classify
    arbitrary natural language. The parser below still requires a direct image
    reference, upload receipt, or fresh, bounded follow-up question.
    """
    compact = re.sub(r"\s+", "", text)
    if _NON_REQUEST_PREFIX.search(compact) or _REFUSAL.search(compact):
        return False
    if compact.startswith(("“", '"', "‘", "'", "`", ">")):
        return False
    if re.search(r"(?:教程|示例|示范|例子|语法|指令格式)", compact):
        return False
    if re.match(r"^(?:生成|画|绘制|制作)(?!.*(?:根据|按照|参考|基于)).*(?:图|图片|照片)$", compact):
        return False
    return True


class ImageReferenceError(ValueError):
    pass


@dataclass(frozen=True)
class ImageSelection:
    count: int | None = None  # None means the latest contiguous upload batch.


def parse_reference(text: str, *, allow_implicit: bool = False) -> ImageSelection | None:
    if not isinstance(text, str) or text.lstrip().startswith("/") or not _is_request_text(text):
        return None
    matches = list(_REFERENCE.finditer(text))
    # The descriptive form overlaps the normal “刚发的两张图” form. Prefer
    # the established parser whenever it already recognized the reference;
    # use the new form only for labels such as “晚饭的图”.
    if not matches:
        matches = list(_DESCRIPTIVE_UPLOAD_REFERENCE.finditer(text))
    if not matches:
        bare = list(_BARE_DEICTIC_REFERENCE.finditer(text))
        if bare:
            matches = bare
    if matches and matches[0][0].startswith("新") and re.search(
        r"(?:生成|画|绘制|制作)(?:一|几|两|\d+)?(?:张|幅)?$", text[:matches[0].start()]
    ):
        return None
    if not matches:
        compact = re.sub(r"[\s，,。.!！?？]", "", text)
        if _RECEIPT_REFERENCE.fullmatch(compact):
            return ImageSelection()
        # An upload receipt can be phrased before the actual request.  Match
        # only from the beginning and require either no suffix or a bounded
        # analysis action; this rejects “图片发你了吗” and quoted examples.
        receipt = _UPLOAD_CONFIRMATION_REFERENCE.match(compact)
        if receipt:
            suffix = compact[receipt.end():]
            if not suffix or _UPLOAD_CONFIRMATION_ACTION.fullmatch(suffix):
                return ImageSelection(1)
        if allow_implicit and _IMPLICIT_REFERENCE.fullmatch(compact):
            # Deictic questions without an image noun (“看看这是什么”) use
            # the latest contiguous batch.  A singular image noun (“那个图”)
            # selects one image; a bare noun without an action is too vague.
            noun_match = re.search(_IMAGE_NOUN, compact)
            if noun_match:
                if compact == noun_match.group(0):
                    return None
                if re.search(r"(?:这些|那些|几张|多张)", compact):
                    return ImageSelection()
                return ImageSelection(1)
            return ImageSelection()
        return None
    if len(matches) > 1:
        raise ImageReferenceError("请明确要看最近几张图片，例如“分析刚才三张图”，或直接重新发送要分析的图片。")
    match = matches[0]
    prefix = text[:match.start()].rstrip()
    # Requests to create an image do not refer to cached uploads. References
    # used as source material (“根据这张图画…”) remain valid analysis inputs.
    if re.search(r"(?:生成|画|绘制|制作)(?:一|几|两|这|那|个|张|幅|新|的|\d)*$", prefix):
        return None
    if match.re is _BARE_DEICTIC_REFERENCE:
        return ImageSelection(1)
    if match.re is _DESCRIPTIVE_UPLOAD_REFERENCE:
        count = match["upload_count"]
        if count is None:
            return ImageSelection(1)
        if count == "几":
            return ImageSelection()
        number = int(count) if count.isascii() and count.isdigit() else _NUMBERS[count]
        if not 1 <= number <= MAX_IMAGES:
            raise ImageReferenceError(f"一次最多分析最近 {MAX_IMAGES} 张图片，请缩小范围或重新发送。")
        return ImageSelection(number)
    if re.fullmatch(rf"(?:上|前)(?:一|1)张{_IMAGE_NOUN}", match[0]):
        return ImageSelection(1)
    count = match["count"] or match["batch_count"]
    if count is None:
        phrase = match[0]
        # “那个图/上面那个图片” and “我刚发的截图” are singular
        # deictics even though Chinese omits the classifier. “刚才的图”
        # remains a latest-batch reference for backwards compatibility.
        if ("张" in phrase or "个" in phrase or
                re.search(r"刚发(?:的)?(?:图片|照片|截图|图)", phrase)):
            return ImageSelection(1)
        return ImageSelection()
    if count == "几":
        return ImageSelection()
    number = int(count) if count.isascii() and count.isdigit() else _NUMBERS[count]
    if not 1 <= number <= MAX_IMAGES:
        raise ImageReferenceError(f"一次最多分析最近 {MAX_IMAGES} 张图片，请缩小范围或重新发送。")
    return ImageSelection(number)


def is_safe_image_ref(ref: object, local_root: Path | None = None) -> bool:
    """Accept QQ attachments, including files materialized by PreProcessStage.

    Local access is opt-in and confined to the framework temp directory. Callers
    must still obtain references only from received Image components.
    """
    if not isinstance(ref, str) or len(ref) > 8192 or any(c.isspace() for c in ref):
        return False
    if ref.startswith("/"):
        if local_root is None:
            return False
        try:
            root = local_root.resolve(strict=True)
            path = Path(ref)
            relative = path.relative_to(root)
            if ".." in relative.parts or not relative.parts:
                return False
            # Refuse symlinks at every level even when their target is inside.
            cursor = root
            for part in relative.parts:
                cursor = cursor / part
                if cursor.is_symlink():
                    return False
            path.resolve(strict=True).relative_to(root)
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_LOCAL_IMAGE_BYTES:
                return False
            with path.open("rb") as stream:
                head = stream.read(16)
            return (head.startswith(b"\x89PNG\r\n\x1a\n") or head.startswith(b"\xff\xd8\xff")
                    or head.startswith((b"GIF87a", b"GIF89a", b"BM", b"II*\x00", b"MM\x00*"))
                    or (head.startswith(b"RIFF") and head[8:12] == b"WEBP")
                    or (head[4:8] == b"ftyp" and head[8:12] in {b"avif", b"avis"}))
        except (OSError, ValueError):
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
    received_wall_at: float | None = None


class ImageCache:
    """Isolation key is the full (UMO, sender) pair, never group or sender alone."""
    def __init__(self, local_root: Path | None = None):
        self.local_root = local_root
        self._sessions: OrderedDict[tuple[str, str], list[_Entry]] = OrderedDict()
        self._batch_id = 0

    def clear(self):
        self._sessions.clear()

    def has_recent(self, key: tuple[str, str], now: float) -> bool:
        """Freshness comes from receipt time, never extended by a follow-up."""
        self.prune(now)
        entries = self._sessions.get(key, [])
        return bool(entries and 0 <= now - entries[-1].received_at < IMPLICIT_REFERENCE_SECONDS)

    def prune(self, now: float):
        for key in list(self._sessions):
            entries = [entry for entry in self._sessions[key] if now - entry.received_at < TTL_SECONDS]
            if entries:
                self._sessions[key] = entries
            else:
                del self._sessions[key]

    def remember(self, key: tuple[str, str], refs: list[str], now: float, *, wall_now: float | None = None):
        self.prune(now)
        refs = list(dict.fromkeys(ref for ref in refs if is_safe_image_ref(ref, self.local_root)))[:MAX_IMAGES]
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
        entries.extend(_Entry(ref, now, batch_id, wall_now) for ref in refs)
        self._sessions[key] = entries[-MAX_IMAGES:]
        self._sessions.move_to_end(key)
        while len(self._sessions) > MAX_SESSIONS:
            self._sessions.popitem(last=False)

    def export(self, *, wall_now: float, now: float) -> list[dict]:
        """Return a bounded, privacy-sensitive snapshot for restart recovery."""
        rows: list[dict] = []
        for (umo, sender), entries in self._sessions.items():
            if not self._valid_scope_value(umo) or not self._valid_scope_value(sender):
                continue
            kept = []
            for entry in entries[-MAX_IMAGES:]:
                received_wall = entry.received_wall_at
                if received_wall is None or not 0 <= wall_now - received_wall < TTL_SECONDS:
                    continue
                if not is_safe_image_ref(entry.ref, self.local_root):
                    continue
                kept.append({"ref": entry.ref,
                             # Keep the original wall-clock receipt time so a
                             # restart cannot turn downtime into fresh TTL.
                             "received_wall_at": received_wall,
                             # ``age``/``saved_wall_at`` remain for reading
                             # snapshots produced by 1.0.3 deployments.
                             "age": max(0.0, wall_now - received_wall),
                             "batch_id": entry.batch_id, "saved_wall_at": wall_now})
            if kept:
                rows.append({"umo": umo, "sender": sender, "entries": kept})
        return rows

    @staticmethod
    def _valid_scope_value(value: object) -> bool:
        # Scope values originate from AstrBot's UMO/sender fields.  Keep the
        # snapshot bounded and reject control characters so a malformed cache
        # cannot create surprising keys or log/display injection later.
        return (isinstance(value, str) and 0 < len(value) <= 512 and
                not any(ord(char) < 0x20 for char in value))

    def restore(self, rows: object, *, wall_now: float, now: float) -> int:
        """Load only recent, safe references; malformed rows are ignored."""
        if not isinstance(rows, list):
            return 0
        self.clear()
        count = 0
        for row in rows[:MAX_SESSIONS]:
            if (not isinstance(row, dict) or
                    not self._valid_scope_value(row.get("umo")) or
                    not self._valid_scope_value(row.get("sender"))):
                continue
            raw_entries = row.get("entries", [])
            if not isinstance(raw_entries, list):
                continue
            entries: list[_Entry] = []
            for raw in raw_entries[:MAX_IMAGES]:
                if not isinstance(raw, dict) or not isinstance(raw.get("ref"), str):
                    continue
                try:
                    batch_id = int(raw.get("batch_id"))
                except (TypeError, ValueError):
                    continue
                received_wall_at = raw.get("received_wall_at")
                if received_wall_at is not None:
                    try:
                        received_wall_at = float(received_wall_at)
                    except (TypeError, ValueError):
                        continue
                    if (isinstance(raw.get("received_wall_at"), bool) or
                            not math.isfinite(received_wall_at)):
                        continue
                    age = wall_now - received_wall_at
                else:
                    try:
                        age = float(raw.get("age"))
                    except (TypeError, ValueError):
                        continue
                    # Snapshots from 1.0.3 carry the time at which the age was
                    # measured; add downtime for backwards-compatible reads.
                    saved_wall_at = raw.get("saved_wall_at")
                    if saved_wall_at is not None:
                        try:
                            saved_wall_at = float(saved_wall_at)
                        except (TypeError, ValueError):
                            continue
                        if (isinstance(raw.get("saved_wall_at"), bool) or
                                not math.isfinite(saved_wall_at)):
                            continue
                        age += max(0.0, wall_now - saved_wall_at)
                if (isinstance(raw.get("age"), bool) or isinstance(raw.get("batch_id"), bool) or
                        not math.isfinite(age) or not 0 <= age < TTL_SECONDS or batch_id < 0 or
                        not is_safe_image_ref(raw["ref"], self.local_root)):
                    continue
                received_wall = wall_now - age
                entries.append(_Entry(raw["ref"], now - age, batch_id, received_wall))
            if entries:
                self._sessions[(row["umo"], row["sender"])] = entries
                self._batch_id = max(self._batch_id, max(item.batch_id for item in entries))
                count += len(entries)
        return count

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
        if any(not is_safe_image_ref(entry.ref, self.local_root) for entry in entries):
            raise ImageReferenceError("你引用的图片文件已不可用，请重新发送需要分析的图片并 @我。")
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
