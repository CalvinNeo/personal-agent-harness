"""Task-scoped download handles for media returned by the Douyin reader."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
from pathlib import Path
import secrets
from urllib.parse import urljoin

import aiohttp

import douyin
from public_web import WebError

MAX_DOWNLOAD = 64 * 1024 * 1024
MAX_SESSION_DOWNLOAD = 128 * 1024 * 1024
MAX_HANDLES = 128
_resources = {}
_downloaded = 0
_busy = False


def register_media(result: dict, *, max_video: int | None = None, max_audio: int | None = None) -> dict:
    if not result.get("ok"):
        return result
    counts = {"video": 0, "audio": 0}
    # Upstream bit_rate order is not a size preference. Reserve bounded handles
    # for the smallest advertised streams instead of the first variants, which
    # may all exceed the downloader's per-resource limit.
    def preference(item):
        size = item.get("size_bytes")
        known = type(size) is int and size > 0
        return (0 if known and size <= MAX_DOWNLOAD else 1 if not known else 2,
                size if known else 0)

    result["media"] = sorted(result.get("media", []), key=preference)
    for item in result["media"]:
        item.pop("resource_id", None)
        kind = item.get("kind")
        urls = tuple(item.get("urls", []))
        if kind not in counts or not urls:
            continue
        maximum = max_video if kind == "video" else max_audio
        size = item.get("size_bytes")
        if (type(size) is int and size > MAX_DOWNLOAD
                or maximum is not None and counts[kind] >= maximum
                or len(_resources) >= MAX_HANDLES):
            item["download_available"] = False
            continue
        handle = secrets.token_hex(12)
        _resources[handle] = {"urls": urls, "kind": kind, "size_bytes": size}
        item["resource_id"] = handle
        item["download_available"] = True
        counts[kind] += 1
    result["notice"] += (" To download a selected stream, call download_douyin_media with its resource_id; "
                         "this bounded task-local transport avoids shell DNS limitations. It does not analyze media.")
    return result


def scratch_directory():
    raw = os.environ.get("TMPDIR", "")
    workspace = Path.cwd().resolve()
    temporary = Path(raw)
    if not raw or not temporary.is_absolute() or temporary.name != ".tmp" or temporary.is_symlink():
        raise WebError("douyin_task_scratch_unavailable")
    if temporary.resolve() != workspace / ".tmp":
        raise WebError("douyin_task_scratch_unavailable")
    try:
        descriptor = os.open(temporary, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        raise WebError("douyin_task_scratch_unavailable") from None
    return temporary, descriptor


async def save_resource(resource, destination):
    global _downloaded
    from douyin_browser import PublicResolver
    connector = aiohttp.TCPConnector(resolver=PublicResolver(), limit=2)
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.douyin.com/", "Accept-Encoding": "identity"}
    async with aiohttp.ClientSession(connector=connector, cookie_jar=aiohttp.DummyCookieJar(), trust_env=False,
                                     timeout=aiohttp.ClientTimeout(total=55, connect=8)) as client:
        code = "douyin_media_unavailable"
        for original in resource["urls"][:3]:
            current = original
            for hop in range(5):
                current = douyin.resource_url(current)
                async with client.get(current, headers=headers, allow_redirects=False) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location")
                        if not location or hop == 4:
                            raise WebError("douyin_media_redirect_limit")
                        current = douyin.resource_url(urljoin(current, location))
                        continue
                    if response.status != 200:
                        code = "douyin_media_http_" + str(response.status)
                        break
                    mime = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                    if not (mime.startswith(("video/", "audio/")) or mime == "application/octet-stream"):
                        raise WebError("douyin_media_content_type")
                    size = response.content_length
                    if size is not None and size > MAX_DOWNLOAD:
                        raise WebError("douyin_media_too_large")
                    digest, total = hashlib.sha256(), 0
                    async for chunk in response.content.iter_chunked(65536):
                        total += len(chunk)
                        _downloaded += len(chunk)
                        if total > MAX_DOWNLOAD or _downloaded > MAX_SESSION_DOWNLOAD:
                            raise WebError("douyin_media_too_large")
                        destination.write(chunk)
                        digest.update(chunk)
                    if not total:
                        raise WebError("douyin_media_empty")
                    return {"bytes": total, "sha256": digest.hexdigest(), "content_type": mime}
        raise WebError(code)


async def download_douyin_media(resource_id: str) -> dict:
    """Download one stream advertised by read_douyin_video into this task's .tmp/.

    Choose a small video/audio variant by its returned resource_id. Max 64 MiB per
    download, no arbitrary URL/path/credentials. Returns a local path for your own
    ffprobe, ffmpeg and image inspection; a successful download is not a viewing result.
    """
    global _busy
    if not isinstance(resource_id, str) or resource_id not in _resources:
        return {"ok": False, "error": "douyin_unknown_media_resource"}
    if _busy:
        return {"ok": False, "error": "douyin_media_download_busy"}
    resource = _resources[resource_id]
    if type(resource.get("size_bytes")) is int and resource["size_bytes"] > MAX_DOWNLOAD:
        return {"ok": False, "error": "douyin_media_too_large"}
    directory_fd, name, created, complete = None, None, False, False
    _busy = True
    try:
        temporary, directory_fd = scratch_directory()
        name = "douyin-" + secrets.token_hex(12) + (".mp4" if resource["kind"] == "video" else ".m4a")
        descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
        created = True
        with os.fdopen(descriptor, "wb") as destination:
            async with asyncio.timeout(60):
                result = await save_resource(resource, destination)
        complete = True
        return {"ok": True, "path": str(temporary / name), "relative_path": ".tmp/" + name,
                "kind": resource["kind"], **result,
                "notice": "Downloaded only; inspect actual frames/audio/subtitles before claiming to have watched it."}
    except WebError as exc:
        return {"ok": False, "error": str(exc)}
    except TimeoutError:
        return {"ok": False, "error": "douyin_media_download_timeout"}
    except (OSError, aiohttp.ClientError):
        return {"ok": False, "error": "douyin_media_download_failed"}
    finally:
        if directory_fd is not None:
            if created and not complete:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(name, dir_fd=directory_fd)
            os.close(directory_fd)
        _busy = False
