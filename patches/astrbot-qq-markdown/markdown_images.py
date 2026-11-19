"""Temporary QQ image materials for a bounded, explicitly requested gallery.

Only platform-issued COS upload URLs receive bytes; authorization headers and
preview URLs never enter a Markdown message. Sent messages retain their own
copies, so deleting our temporary materials immediately releases the quota.
"""
from __future__ import annotations

import base64
import hashlib
import io
import re
from urllib.parse import urlsplit

import aiohttp
from botpy.http import Route
from PIL import Image as PillowImage

from astrbot.api import logger
from astrbot.api.message_components import At, Image, Plain, Reply
from astrbot.core.utils.media_utils import MediaResolver

MAX_IMAGES = 3
MAX_BYTES = 2 * 1024 * 1024
MAX_PIXELS = 16_000_000


def eligible(chain):
    images = sum(isinstance(part, Image) for part in chain.chain)
    return (chain.use_markdown_ is True and 2 <= images <= MAX_IMAGES
            and all(isinstance(part, (Plain, At, Reply, Image)) for part in chain.chain))


async def put_image(url, data, mime):
    """Use a fresh unauthenticated session, with redirects disabled."""
    target = urlsplit(url)
    host = (target.hostname or "").lower()
    if (target.scheme != "https" or target.username or target.password
            or target.port not in (None, 443)
            or not host.endswith((".myqcloud.com", ".tencentcos.cn"))):
        raise ValueError("unexpected QQ upload destination")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20), trust_env=False) as session:
        async with session.put(url, data=data, headers={"Content-Type": mime}, allow_redirects=False) as response:
            if not 200 <= response.status < 300:
                raise ValueError("QQ image upload rejected")
            etag = (response.headers.get("ETag") or "").strip('"').lower()
            if etag != hashlib.md5(data).hexdigest():
                raise ValueError("QQ image upload checksum mismatch")
            return etag


async def release(http, image_ids):
    for image_id in image_ids:
        try:
            await http.request(Route("DELETE", "/v2/media-images/{image_id}/delete", image_id=image_id))
        except Exception as error:
            logger.warning("[QQOfficial] Temporary Markdown material cleanup failed type=%s", type(error).__name__)


async def prepare(http, chain):
    """Return an all-text Markdown chain, or the unchanged original on failure."""
    if not eligible(chain):
        return chain, []
    image_ids, parts, number = [], [], 0
    try:
        for part in chain.chain:
            if isinstance(part, Image):
                encoded = await MediaResolver(part.file, media_type="image").to_base64()
                if len(encoded) > 4 * ((MAX_BYTES + 2) // 3):
                    raise ValueError("Markdown image exceeds limit")
                data = base64.b64decode(encoded, validate=True)
                if not 0 < len(data) <= MAX_BYTES:
                    raise ValueError("Markdown image exceeds limit")
                with PillowImage.open(io.BytesIO(data)) as image:
                    width, height, kind = image.width, image.height, image.format
                    if kind not in {"PNG", "JPEG"} or not 0 < width * height <= MAX_PIXELS:
                        raise ValueError("unsupported Markdown image")
                    image.verify()
                number += 1
                mime, extension = ("image/png", "png") if kind == "PNG" else ("image/jpeg", "jpg")
                md5 = hashlib.md5(data).hexdigest()
                task = await http.request(Route("POST", "/v2/media-images/prepare"), json={
                    "file_name": f"result-{number}.{extension}", "file_size": len(data),
                    "content_type": mime, "md5": md5, "sha1": hashlib.sha1(data).hexdigest()})
                if not isinstance(task, dict) or not isinstance(task.get("upload_id"), str) or not isinstance(task.get("presigned_url"), str):
                    raise ValueError("invalid QQ upload task")
                etag = await put_image(task["presigned_url"], data, mime)
                material = await http.request(Route("POST", "/v2/media-images/complete"),
                                              json={"upload_id": task["upload_id"], "md5": etag})
                if not isinstance(material, dict):
                    raise ValueError("invalid QQ image material")
                image_id, url = material.get("image_id"), material.get("url")
                if isinstance(image_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,200}", image_id):
                    image_ids.append(image_id)
                else:
                    raise ValueError("invalid QQ image identifier")
                if not isinstance(url, str) or not re.fullmatch(r"qqbot-media://image/v1/[A-Za-z0-9_-]{1,2048}", url):
                    raise ValueError("invalid QQ Markdown reference")
                scale = min(1, 640 / width)
                parts.append(Plain(f"\n\n![结果图 {number} #{max(1,round(width*scale))}px #{max(1,round(height*scale))}px]({url})\n\n"))
            else:
                parts.append(part)
        return chain.derive(parts), image_ids
    except Exception as error:
        await release(http, image_ids)
        logger.warning("[QQOfficial] Markdown gallery preparation failed; retaining native images type=%s", type(error).__name__)
        return chain, []
