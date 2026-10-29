"""Bounded QQ attachment transport. Never interpret message text as a URL/path."""
from __future__ import annotations

import asyncio
import base64
import binascii
import inspect
import io
import ipaddress
import os
from pathlib import Path
import re
import socket
import stat
from urllib.parse import unquote, urlsplit
import warnings

MAX_IMAGES = 4
MAX_IMAGE_BYTES = 512 * 1024
MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
MAX_ARTIFACT_EDGE = 4096
MAX_SOURCE_BYTES = 20 * 1024 * 1024
MAX_PIXELS = 20_000_000
MAX_EDGE = 1600
MAX_QUOTED_TEXT = 4000
_QQ_DOMAINS = ("qpic.cn", "qlogo.cn", "gtimg.cn", "gtimg.com")
_QQ_HOSTS = {"multimedia.nt.qq.com", "multimedia.nt.qq.com.cn"}
_UNSUPPORTED = {"File", "Video", "Forward", "Node", "Record"}
_MISSING_REFERENCE = re.compile(r"(?:刚才|刚刚|刚发|前面|上面|之前|最近).{0,24}(?:图片|照片|截图|图|张)|(?:这|那)(?:几|一|两|三|四)?张(?:图|照片)?")
_PLACEHOLDER = re.compile(r"\[(?:图片|图像|照片|image|视频|文件|语音|转发消息)\]", re.I)


class ContextError(ValueError):
    """A user-visible error with no URL, path, credential, or decoded content."""


def _temp_root() -> Path:
    from astrbot.core.utils.astrbot_path import get_astrbot_temp_path
    return Path(get_astrbot_temp_path())


def _read_local(ref: str) -> bytes:
    """Walk directory descriptors with O_NOFOLLOW, including the root ancestors."""
    if ref.startswith("file:"):
        parsed = urlsplit(ref)
        if parsed.scheme != "file" or parsed.netloc or parsed.query or parsed.fragment:
            raise ContextError("图片本地引用无效，请重新发送原图。")
        ref = unquote(parsed.path)
    path, root = Path(ref), _temp_root()
    if not path.is_absolute() or not root.is_absolute() or ".." in path.parts or ".." in root.parts:
        raise ContextError("图片本地引用无效，请重新发送原图。")
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise ContextError("图片不在受信任的临时目录中，请重新发送原图。") from None
    if not relative.parts:
        raise ContextError("图片本地引用无效，请重新发送原图。")
    descriptor = None
    try:
        descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for component in path.parts[1:-1]:
            next_descriptor = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        file_descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        with os.fdopen(file_descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_SOURCE_BYTES:
                raise ContextError("图片文件格式或大小不受支持，请发送不超过 20 MiB 的原图。")
            data = stream.read(MAX_SOURCE_BYTES + 1)
        if not data or len(data) > MAX_SOURCE_BYTES:
            raise ContextError("图片文件过大或为空，请重新发送原图。")
        return data
    except (OSError, ValueError) as error:
        if isinstance(error, ContextError):
            raise
        raise ContextError("图片临时文件不可安全读取，请重新发送原图。") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _validate_url(ref: str) -> str:
    if not isinstance(ref, str) or len(ref) > 8192 or any(c.isspace() or ord(c) < 32 for c in ref) or "\\" in ref:
        raise ContextError("图片下载地址不受支持，请重新发送 QQ 原图。")
    try:
        parsed = urlsplit(ref)
        host = (parsed.hostname or "").lower()
        if (parsed.scheme != "https" or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443) or parsed.fragment or not re.fullmatch(r"[a-z0-9.-]+", host)
                or not (host in _QQ_HOSTS or any(host == domain or host.endswith("." + domain) for domain in _QQ_DOMAINS))):
            raise ValueError
        return host
    except ValueError:
        raise ContextError("仅支持 QQ 图片 CDN 的 HTTPS 附件，请重新发送原图。") from None


class _PublicResolver:
    """Hand the connector verified addresses directly; no second DNS resolution."""
    async def resolve(self, host, port=0, family=socket.AF_UNSPEC):
        _validate_url("https://" + host)
        records = await asyncio.get_running_loop().getaddrinfo(host, port, family=family, type=socket.SOCK_STREAM)
        result, seen = [], set()
        for af, socktype, protocol, canonname, address in records:
            ip = ipaddress.ip_address(address[0])
            if not ip.is_global or ip.is_multicast or ip.is_reserved or ip.is_unspecified or ip.is_loopback or ip.is_link_local:
                raise ContextError("图片下载地址解析异常，请重新发送 QQ 原图。")
            if address[0] not in seen:
                result.append({"hostname": host, "host": address[0], "port": port, "family": af,
                               "proto": protocol, "flags": socket.AI_NUMERICHOST})
                seen.add(address[0])
        if not result:
            raise ContextError("图片地址暂时无法解析，请稍后重试。")
        return result

    async def close(self):
        pass


async def _download(ref: str) -> bytes:
    _validate_url(ref)
    import aiohttp
    try:
        timeout = aiohttp.ClientTimeout(total=15, connect=5, sock_read=10)
        connector = aiohttp.TCPConnector(resolver=_PublicResolver(), use_dns_cache=False, limit=1)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout, trust_env=False,
                                         auto_decompress=False, cookie_jar=aiohttp.DummyCookieJar()) as session:
            async with session.get(ref, allow_redirects=False, headers={"Accept": "image/*", "Accept-Encoding": "identity"}) as response:
                if response.status != 200:
                    raise ContextError("QQ 图片暂时无法下载，请重新发送原图。")
                if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    raise ContextError("图片下载格式不受支持，请重新发送原图。")
                length = response.content_length
                if length is not None and (length <= 0 or length > MAX_SOURCE_BYTES):
                    raise ContextError("图片过大或为空，请发送不超过 20 MiB 的原图。")
                chunks, count = [], 0
                async for chunk in response.content.iter_chunked(64 * 1024):
                    count += len(chunk)
                    if count > MAX_SOURCE_BYTES:
                        raise ContextError("图片过大，请发送不超过 20 MiB 的原图。")
                    chunks.append(chunk)
                if not count:
                    raise ContextError("收到的图片为空，请重新发送原图。")
                return b"".join(chunks)
    except ContextError:
        raise
    except (OSError, TimeoutError, aiohttp.ClientError):
        raise ContextError("图片下载超时或不可用，请重新发送原图后重试。") from None


def _normalize(data: bytes, expected_format: str | None = None) -> bytes:
    from PIL import Image, ImageOps, UnidentifiedImageError
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_SOURCE_BYTES:
        raise ContextError("图片过大或为空，请重新发送原图。")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as source:
                if expected_format is not None and source.format != expected_format:
                    raise ContextError("图片文件类型与声明不符。")
                if source.width <= 0 or source.height <= 0 or source.width * source.height > MAX_PIXELS:
                    raise ContextError("图片像素数过大，请发送不超过 2000 万像素的图片。")
                if getattr(source, "n_frames", 1) != 1:
                    raise ContextError("暂不支持动态或多页图片，请逐张发送静态图片。")
                source.load()
                oriented = ImageOps.exif_transpose(source)
                oriented.thumbnail((MAX_EDGE, MAX_EDGE), Image.Resampling.LANCZOS)
                # Flatten transparency onto white and create a fresh raster without metadata.
                rgba = oriented.convert("RGBA")
                image = Image.new("RGB", rgba.size, "white")
                image.paste(rgba, mask=rgba.getchannel("A"))
                for _ in range(6):
                    for quality in (85, 70, 55, 40):
                        output = io.BytesIO()
                        image.save(output, format="JPEG", quality=quality, optimize=True, exif=b"", icc_profile=None)
                        encoded = output.getvalue()
                        if len(encoded) <= MAX_IMAGE_BYTES:
                            return encoded
                    image.thumbnail((max(1, int(image.width * .8)), max(1, int(image.height * .8))), Image.Resampling.LANCZOS)
    except ContextError:
        raise
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombWarning, Image.DecompressionBombError):
        raise ContextError("图片内容无法解码，请重新发送有效的静态原图。") from None
    raise ContextError("图片无法压缩到安全大小，请降低分辨率后重发。")


def decode_artifact(artifact) -> bytes:
    """Verify returned images and strip metadata without shrinking annotation text."""
    from PIL import Image, ImageOps, UnidentifiedImageError
    if not isinstance(artifact, dict) or artifact.get("mime") not in {"image/jpeg", "image/png"}:
        raise ContextError("Codex 返回了不受支持的图片格式。")
    encoded = artifact.get("data")
    if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_ARTIFACT_BYTES + 2) // 3):
        raise ContextError("Codex 返回的图片超过 2 MiB 或无效。")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise ContextError("Codex 返回的图片编码无效。") from None
    if not data or len(data) > MAX_ARTIFACT_BYTES:
        raise ContextError("Codex 返回的图片超过 2 MiB 或为空。")
    expected = "JPEG" if artifact["mime"] == "image/jpeg" else "PNG"
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as source:
                if source.format != expected:
                    raise ContextError("Codex 返回的图片类型与声明不符。")
                if (source.width <= 0 or source.height <= 0 or source.width * source.height > MAX_PIXELS
                        or max(source.size) > MAX_ARTIFACT_EDGE):
                    raise ContextError("Codex 返回的图片超过 4096 像素边长或 2000 万像素限制。")
                if getattr(source, "n_frames", 1) != 1:
                    raise ContextError("Codex 返回了不受支持的动态或多页图片。")
                source.load()
                oriented = ImageOps.exif_transpose(source)
                mode = "RGBA" if expected == "PNG" else "RGB"
                converted = oriented.convert(mode)
                raster = Image.new(mode, converted.size)
                raster.paste(converted)
                output = io.BytesIO()
                if expected == "PNG":
                    raster.save(output, format="PNG", optimize=True)
                else:
                    raster.save(output, format="JPEG", quality=95, optimize=True, exif=b"", icc_profile=None)
                result = output.getvalue()
                if len(result) > MAX_ARTIFACT_BYTES:
                    raise ContextError("Codex 返回的图片安全规范化后超过 2 MiB，请生成较小的结果图。")
                return result
    except ContextError:
        raise
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombWarning, Image.DecompressionBombError):
        raise ContextError("Codex 返回的图片内容无法解码。") from None


def _attachment_ref(part) -> str:
    ref = getattr(part, "url", None) or getattr(part, "file", None)
    if not isinstance(ref, str) or not ref or len(ref) > 8192 or "\x00" in ref:
        raise ContextError("这条消息的图片信息不完整，请重新发送原图。")
    return ref


async def _cache_selector(context):
    getter = getattr(context, "get_all_stars", None)
    stars = getter() if callable(getter) else []
    if inspect.isawaitable(stars):
        stars = await stars
    for metadata in stars or []:
        if getattr(metadata, "name", None) != "image_context" or getattr(metadata, "activated", True) is False:
            continue
        selector = getattr(getattr(metadata, "star_cls", None), "select_for_task", None)
        if callable(selector):
            return selector
    return None


async def _select_cache(event, selector) -> list[str]:
    try:
        refs = selector(event)
        if inspect.isawaitable(refs):
            refs = await refs
        if not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs):
            raise ContextError("图片上下文服务返回无效数据，请重新附上原图。")
        return refs
    except ValueError:
        raise ContextError("未找到本会话中你引用的完整图片，请重新附上原图。") from None


async def _cached_refs(event, context) -> list[str]:
    selector = await _cache_selector(context)
    refs = await _select_cache(event, selector) if selector is not None else []
    if refs:
        return refs
    text = " ".join(str(getattr(part, "text", "")) for part in event.get_messages() if type(part).__name__ == "Plain")
    if _MISSING_REFERENCE.search(text):
        raise ContextError("当前无法读取你之前发的图片，请在这条任务中重新附上原图。")
    return []


async def prepare_images(event, context) -> tuple[list[dict], str]:
    """Resolve current/quoted attachments or the sender-scoped image plugin cache."""
    if event.get_platform_name() != "qq_official" or not event.unified_msg_origin or not str(event.get_sender_id() or ""):
        raise ContextError("图片任务缺少有效的 QQ 会话信息。")
    refs, quotes = [], []
    parts = event.get_messages()
    has_current_image = any(type(part).__name__ == "Image" for part in parts)
    has_reply_image = False
    for part in parts:
        kind = type(part).__name__
        if kind in _UNSUPPORTED:
            raise ContextError("Codex 暂不支持这条消息中的文件、语音、视频或转发内容；请发送原图和文字说明。")
        if kind == "Image":
            refs.append(_attachment_ref(part))
        elif kind == "Reply":
            chain = getattr(part, "chain", None)
            if chain is not None and not isinstance(chain, (list, tuple)):
                raise ContextError("引用消息结构不完整，请重新附上原图和文字。")
            if chain:
                has_content = False
                has_image = any(type(nested).__name__ == "Image" for nested in chain)
                preview = str(getattr(part, "message_str", "") or "")
                if _PLACEHOLDER.search(preview) and not has_image:
                    raise ContextError("引用中缺少原始附件，尚未拿到原图；请重新附上原图和文字。")
                for nested in chain:
                    nested_kind = type(nested).__name__
                    if nested_kind in _UNSUPPORTED or nested_kind == "Reply":
                        raise ContextError("暂不支持嵌套引用或这条引用中的非图片附件，请直接附上原图和文字。")
                    if nested_kind == "Image":
                        has_reply_image = True
                        # An attached new image wins over an older quoted image.
                        if not has_current_image:
                            refs.append(_attachment_ref(nested))
                        has_content = True
                    elif nested_kind == "Plain":
                        text = str(getattr(nested, "text", "") or "")
                        if _PLACEHOLDER.search(text) and not has_image:
                            raise ContextError("引用中只有附件占位信息，请重新附上原图和文字。")
                        if text.strip():
                            quotes.append(text)
                            has_content = True
                if not has_content:
                    raise ContextError("引用消息缺少图片或文字内容，请重新附上原图和文字。")
            else:
                text = str(getattr(part, "message_str", "") or "").strip()
                if not text or _PLACEHOLDER.search(text):
                    raise ContextError("引用中只有图片占位信息，尚未拿到原图；请重新附上原图和文字。")
                quotes.append(text)
    if not refs:
        refs = await _cached_refs(event, context)
    refs = list(dict.fromkeys(refs))
    if len(refs) > MAX_IMAGES:
        raise ContextError(f"一次 Codex 任务最多附带 {MAX_IMAGES} 张图片，请分批发送。")
    # Caller places this string in a dedicated quoted-context field, never the command.
    quoted = "\n\n".join(quotes)
    if len(quoted) > MAX_QUOTED_TEXT:
        raise ContextError("引用文字超过 4000 字，请缩短引用后提交，避免遗漏任务内容。")
    images = []
    for ref in refs:
        if not isinstance(ref, str) or len(ref) > 8192 or "\x00" in ref:
            raise ContextError("图片引用无效，请重新发送原图。")
        if ref.startswith(("/", "file:")):
            data = await asyncio.to_thread(_read_local, ref)
        else:
            data = await _download(ref)
        normalized = await asyncio.to_thread(_normalize, data)
        images.append({"mime": "image/jpeg", "data": base64.b64encode(normalized).decode("ascii")})
    if has_current_image or has_reply_image:
        # Codex consumes the event before the normal image-cache handler runs.
        # The authorized plugin remembers current uploads and validates explicit
        # counts for current/quoted inputs; quoted images are never remembered.
        # A selected subset may narrow/reorder only these direct attachments.
        selector = await _cache_selector(context)
        if selector is not None:
            selected = await _select_cache(event, selector)
            if selected:
                by_ref = dict(zip(refs, images))
                if any(ref not in by_ref for ref in selected):
                    raise ContextError("图片上下文与本次附件不一致，当前没有提交；请重新附上要处理的原图。")
                images = [by_ref[ref] for ref in dict.fromkeys(selected)]
    return images, quoted
