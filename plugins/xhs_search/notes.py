from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import io
import ipaddress
import re
import socket
import time
from urllib.parse import urljoin, urlsplit, urlunsplit
import warnings

import httpx
from PIL import Image as PillowImage

from .core import XHSError


IMAGE_HOSTS = frozenset({"sns-webpic-qc.xhscdn.com", "sns-webpic.xhscdn.com", "sns-img-qc.xhscdn.com",
                         "sns-img-bd.xhscdn.com", "sns-img-hw.xhscdn.com"})
MAX_IMAGES = 4
MAX_NOTES = 3
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_PIXELS = 24000000


def image_url(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 2048 or re.search(r"[\x00-\x20\x7f\\]", value):
        raise XHSError("笔记图片地址无效。", code="image_url_invalid")
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or parsed.hostname not in IMAGE_HOSTS
                or parsed.username is not None or parsed.password is not None or parsed.port not in (None, 443)
                or not parsed.path.startswith("/") or parsed.fragment):
            raise ValueError
        return urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, ""))
    except ValueError:
        raise XHSError("笔记图片不在已批准的小红书 CDN。", code="image_url_invalid") from None


def search_references(data: dict, results: list[dict]) -> dict:
    identifiers = {item["url"].rsplit("/", 1)[-1] for item in results}
    references = {}
    for item in data.get("feeds", []):
        if not isinstance(item, dict) or item.get("id") not in identifiers:
            continue
        token = item.get("xsecToken")
        if isinstance(token, str) and re.fullmatch(r"[A-Za-z0-9+/_=-]{1,2048}", token):
            references.setdefault(item["id"], {"feed_id": item["id"], "xsec_token": token})
    return references


def normalize_note(data: dict, expected_id: str) -> dict:
    inner = data.get("data") if isinstance(data, dict) else None
    note = inner.get("note") if isinstance(inner, dict) else None
    if not isinstance(note, dict) or note.get("noteId") != expected_id:
        raise XHSError("笔记详情与请求不一致，未使用其内容。", code="note_invalid")
    title, body = note.get("title"), note.get("desc")
    if not isinstance(title, str) or not isinstance(body, str):
        raise XHSError("笔记正文结构已变化。", code="note_invalid")
    user = note.get("user") or {}
    author = user.get("nickname") or user.get("nickName") or "" if isinstance(user, dict) else ""
    source_images = note.get("imageList") or []
    if not isinstance(source_images, list):
        raise XHSError("笔记图片结构已变化。", code="note_invalid")
    images = []
    for item in source_images[:30]:
        if not isinstance(item, dict):
            continue
        for key in ("urlDefault", "urlPre"):
            try:
                url = image_url(item.get(key))
            except XHSError:
                continue
            if url not in images:
                images.append(url)
            break
    return {"note_id": expected_id, "title": title[:500], "body": body[:12000], "body_truncated": len(body) > 12000,
            "author": author[:200] if isinstance(author, str) else "", "url": "https://www.xiaohongshu.com/explore/" + expected_id,
            "image_urls": images, "image_count": len(source_images), "kind": note.get("type") if note.get("type") in {"normal", "video"} else "unknown"}


class ReferenceStore:
    def __init__(self, *, ttl=1800, clock=time.monotonic):
        self.ttl, self.clock = ttl, clock
        self.records = {}

    def put(self, owner: tuple[str, str], query: str, references: dict):
        now = self.clock()
        self.records = {key: item for key, item in self.records.items() if item["expires_at"] > now}
        self.records.pop(owner, None)
        self.records[owner] = {"query": query, "references": references, "expires_at": now + self.ttl}
        while len(self.records) > 40:
            del self.records[next(iter(self.records))]

    def get(self, owner: tuple[str, str]) -> dict:
        record = self.records.get(owner)
        if record is None or record["expires_at"] <= self.clock():
            self.records.pop(owner, None)
            raise XHSError("当前会话没有属于你的有效笔记结果，请先搜索或提供搜索关键词。", code="notes_expired")
        return record


@dataclass(frozen=True)
class VerifiedImage:
    data: bytes
    mime_type: str
    width: int
    height: int
    sha256: str


def validate_image(raw: bytes) -> VerifiedImage:
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        raise XHSError("笔记图片为空或过大。", code="image_invalid")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", PillowImage.DecompressionBombWarning)
            with PillowImage.open(io.BytesIO(raw)) as probe:
                if probe.format not in {"JPEG", "PNG", "WEBP", "GIF"} or probe.width * probe.height > MAX_PIXELS:
                    raise ValueError
                probe.verify()
            with PillowImage.open(io.BytesIO(raw)) as original:
                original.seek(0)
                image = original.convert("RGB")
                image.thumbnail((2048, 2048))
                output = io.BytesIO()
                image.save(output, format="JPEG", quality=88, optimize=True)
                data = output.getvalue()
                if len(data) > 2 * 1024 * 1024:
                    raise ValueError
                return VerifiedImage(data, "image/jpeg", image.width, image.height, hashlib.sha256(data).hexdigest())
    except Exception:
        raise XHSError("笔记图片不是可安全解码的图片，未发送。", code="image_invalid") from None


async def download_image(value: str, *, client=None, resolver=socket.getaddrinfo) -> VerifiedImage:
    url = image_url(value)
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=httpx.Timeout(18, connect=6),
                                   headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.xiaohongshu.com/"})
    try:
        async with asyncio.timeout(24):
            for hop in range(3):
                url = image_url(url)
                host = urlsplit(url).hostname
                addresses = await asyncio.to_thread(resolver, host, 443, type=socket.SOCK_STREAM)
                if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
                    raise XHSError("笔记图片解析到非公共地址，已停止。", code="image_url_invalid")
                async with client.stream("GET", url) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location")
                        if not location or hop == 2:
                            raise XHSError("笔记图片跳转异常。", code="image_url_invalid")
                        url = image_url(urljoin(url, location))
                        continue
                    if response.status_code in {403, 429}:
                        raise XHSError("小红书图片被拒绝或限流；未重试或绕过。", code="image_restricted")
                    if response.status_code != 200 or not response.headers.get("Content-Type", "").lower().startswith("image/"):
                        raise XHSError("小红书图片暂时不可读取。", code="image_unavailable")
                    size = response.headers.get("Content-Length", "")
                    if size.isdecimal() and int(size) > MAX_IMAGE_BYTES:
                        raise XHSError("笔记图片过大，已停止读取。", code="image_invalid")
                    chunks, total = [], 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > MAX_IMAGE_BYTES:
                            raise XHSError("笔记图片过大，已停止读取。", code="image_invalid")
                        chunks.append(chunk)
                    return await asyncio.to_thread(validate_image, b"".join(chunks))
    except XHSError:
        raise
    except Exception:
        raise XHSError("笔记图片下载失败或超时，未编造替代图片。", code="image_unavailable") from None
    finally:
        if own_client:
            await client.aclose()
    raise XHSError("笔记图片未读取。", code="image_unavailable")


def strip_markdown_images(text: str) -> str:
    output, cursor = [], 0
    while cursor < len(text):
        start = text.find("![", cursor)
        if start < 0:
            output.append(text[cursor:])
            break
        middle = text.find("](", start + 2)
        if middle < 0 or middle - start > 500:
            output.append(text[cursor:start + 2])
            cursor = start + 2
            continue
        position, depth = middle + 2, 1
        while position < len(text) and depth and position - middle < 4096:
            if text[position] == "\\":
                position += 2
                continue
            if text[position] == "(":
                depth += 1
            elif text[position] == ")":
                depth -= 1
            position += 1
        output.append(text[cursor:start])
        cursor = position if depth == 0 else middle + 2
    return "".join(output)
