"""Bounded, normalized image bytes for the private Codex RPC.

No function accepts a client supplied local path or downloads a URL. Pillow is
required in the runner interpreter for actual media requests.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import math
import os
import stat
import subprocess
import warnings
from pathlib import Path

MAX_IMAGES = 4
MAX_INPUT_BYTES = 512 * 1024
MAX_INPUT_TOTAL = 2 * 1024 * 1024
MAX_ARTIFACTS = 3
MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
MAX_ARTIFACT_TOTAL = 6 * 1024 * 1024
MAX_VIDEO_BYTES = 8 * 1024 * 1024
MAX_MEDIA_TOTAL = 24 * 1024 * 1024
MAX_PIXELS = 16_000_000
MAX_EDGE = 4096
MIMES = {"image/png": ("PNG", ".png"), "image/jpeg": ("JPEG", ".jpg")}
ARTIFACT_MIMES = {**MIMES, "video/mp4": ("MP4", ".mp4")}


class ArtifactError(ValueError):
    """Output existed but could not be safely returned as media."""


def validate_video(data: bytes) -> bytes:
    if not 16 <= len(data) <= MAX_VIDEO_BYTES or data[4:8] != b"ftyp":
        raise ValueError("invalid MP4 size or content")
    try:
        result = subprocess.run([
            "ffprobe", "-v", "error", "-protocol_whitelist", "pipe", "-enable_drefs", "0",
            "-f", "mov", "-i", "pipe:0", "-show_entries",
            "format=duration:stream=codec_type,codec_name,width,height", "-of", "json",
        ], input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15, check=True)
        metadata = json.loads(result.stdout)
        streams = metadata.get("streams", [])
        duration = float(metadata.get("format", {}).get("duration", 0))
        video = [stream for stream in streams if stream.get("codec_type") == "video"]
        audio = [stream for stream in streams if stream.get("codec_type") == "audio"]
        if (not math.isfinite(duration) or not 0 < duration <= 600 or len(video) != 1 or len(audio) > 1
                or len(video) + len(audio) != len(streams) or video[0].get("codec_name") != "h264"
                or not 0 < video[0].get("width", 0) <= MAX_EDGE or not 0 < video[0].get("height", 0) <= MAX_EDGE
                or any(stream.get("codec_name") != "aac" for stream in audio)):
            raise ValueError("unsupported MP4 streams or duration")
    except (OSError, subprocess.SubprocessError, AttributeError, TypeError, KeyError, ValueError, OverflowError):
        raise ValueError("MP4 validation failed; H.264/AAC MP4 up to 8 MiB and 10 minutes is required") from None
    return data


def normalize(data: bytes, mime: str, limit: int) -> bytes:
    from PIL import Image, ImageOps, JpegImagePlugin

    if mime not in MIMES or not 0 < len(data) <= limit:
        raise ValueError("invalid image size or type")
    image_format = MIMES[mime][0]
    if (image_format == "PNG" and not data.startswith(b"\x89PNG\r\n\x1a\n")
            or image_format == "JPEG" and not data.startswith(b"\xff\xd8\xff")):
        raise ValueError("image MIME does not match content")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as source:
                if (source.format != image_format or not 0 < source.width * source.height <= MAX_PIXELS
                        or max(source.size) > MAX_EDGE):
                    raise ValueError("invalid image dimensions or type")
                source.verify()
            with Image.open(io.BytesIO(data)) as source:
                if getattr(source, "n_frames", 1) != 1:
                    raise ValueError("animated images are unsupported")
                source.load()
                jpeg_options = ({"qtables": source.quantization, "subsampling": JpegImagePlugin.get_sampling(source),
                                 "optimize": True} if image_format == "JPEG" else {})
                image = ImageOps.exif_transpose(source)
                image = image.convert("RGBA" if image_format == "PNG" and "A" in image.getbands() else "RGB")
                output = io.BytesIO()
                # A fresh image drops EXIF, textual metadata, and embedded
                # profiles. Re-encoding also removes trailing polyglot data.
                clean = Image.new(image.mode, image.size)
                clean.paste(image)
                # Keep JPEG's existing quantization instead of raising its
                # quality: the plugin already compressed inputs to the limit.
                clean.save(output, format=image_format, **jpeg_options)
                normalized = output.getvalue()
                if image_format == "JPEG" and len(normalized) > limit:
                    # Orientation/re-encoding can still grow a JPEG slightly.
                    # Keep its dimensions and retry bounded quality levels;
                    # never make labels unreadable by resizing an artifact.
                    for quality in (85, 70, 55, 40, 25, 10):
                        output = io.BytesIO()
                        clean.save(output, format="JPEG", quality=quality, optimize=True)
                        normalized = output.getvalue()
                        if len(normalized) <= limit:
                            break
    except (OSError, SyntaxError, Warning, Image.DecompressionBombError) as exc:
        raise ValueError("invalid image content") from exc
    if not 0 < len(normalized) <= limit:
        raise ValueError("normalized image is too large")
    return normalized


def decode_inputs(value: object) -> list[dict]:
    if not isinstance(value, list) or len(value) > MAX_IMAGES:
        raise ValueError("invalid image list")
    images, total = [], 0
    for item in value:
        if not isinstance(item, dict) or set(item) != {"mime", "data"}:
            raise ValueError("images require MIME and base64 bytes only")
        mime, encoded = item["mime"], item["data"]
        if (not isinstance(mime, str) or mime not in MIMES or not isinstance(encoded, str)
                or not 0 < len(encoded) <= ((MAX_INPUT_BYTES + 2) // 3) * 4):
            raise ValueError("invalid image encoding or type")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("invalid image encoding") from exc
        total += len(raw)
        if total > MAX_INPUT_TOTAL:
            raise ValueError("images exceed total size limit")
        data = normalize(raw, mime, MAX_INPUT_BYTES)
        images.append({"mime": mime, "bytes": data, "digest": hashlib.sha256(data).hexdigest()})
    return images


def private_write(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)


def read_regular(path: Path, limit: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= limit:
            raise ValueError("unsafe image file")
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError("image file is too large")
    return data


def stage_inputs(workspace: Path, images: list[dict]) -> list[Path]:
    target = workspace / "input"
    target.mkdir(mode=0o700)
    (workspace / "output").mkdir(mode=0o700)
    paths = []
    for index, image in enumerate(images):
        path = target / (str(index) + MIMES[image["mime"]][1])
        private_write(path, image["bytes"])
        paths.append(path)
    return paths


def collect_artifacts(workspace: Path, archive: Path, *, strict: bool = False) -> list[dict]:
    output = workspace / "output"
    if output.is_symlink() or not output.is_dir():
        if strict:
            raise ArtifactError("Media output directory is missing or unsafe; no result media can be returned")
        return []
    artifacts, total = [], 0
    # Only inspect bounded top-level output entries; never recurse.
    candidates = []
    with os.scandir(output) as entries:
        for index, entry in enumerate(entries):
            if index >= 128:
                if strict:
                    raise ArtifactError("Too many output directory entries; keep only final media in output/")
                break
            if Path(entry.name).suffix.lower() in {".png", ".jpg", ".jpeg", ".mp4"}:
                candidates.append(Path(entry.path))
            elif strict and Path(entry.name).suffix.lower() in {".gif", ".webp", ".svg", ".mov", ".webm", ".avi"}:
                raise ArtifactError("Unsupported output media format; export PNG/JPEG images or H.264/AAC MP4 videos")
    if strict and len(candidates) > MAX_ARTIFACTS:
        raise ArtifactError("At most 3 media outputs can be returned per task; no outputs were silently omitted")
    for path in sorted(candidates):
        if len(artifacts) >= MAX_ARTIFACTS:
            break
        mime = "video/mp4" if path.suffix.lower() == ".mp4" else ("image/png" if path.suffix.lower() == ".png" else "image/jpeg")
        try:
            data = (validate_video(read_regular(path, MAX_VIDEO_BYTES)) if mime == "video/mp4"
                    else normalize(read_regular(path, MAX_ARTIFACT_BYTES), mime, MAX_ARTIFACT_BYTES))
        except (OSError, ValueError):
            if strict:
                raise ArtifactError("Generated media failed format, size, or file safety validation; no result media was returned") from None
            continue
        if total + len(data) > MAX_MEDIA_TOTAL:
            if strict:
                raise ArtifactError("Generated media exceed the total size limit; no result media was returned")
            continue
        if not archive.exists():
            archive.mkdir(mode=0o700, parents=True)
        if archive.is_symlink():
            raise ValueError("unsafe artifact archive")
        aid = str(len(artifacts))
        name = "result-" + str(len(artifacts) + 1) + ARTIFACT_MIMES[mime][1]
        private_write(archive / (aid + ARTIFACT_MIMES[mime][1]), data)
        artifacts.append({"id": aid, "mime": mime, "name": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        total += len(data)
    return artifacts


def artifact_payload(archive: Path, metadata: dict) -> dict:
    if archive.is_symlink() or not archive.is_dir():
        raise ValueError("artifact unavailable")
    aid, mime = metadata.get("id"), metadata.get("mime")
    if aid not in {"0", "1", "2"} or mime not in ARTIFACT_MIMES:
        raise ValueError("artifact unavailable")
    data = read_regular(archive / (aid + ARTIFACT_MIMES[mime][1]), MAX_VIDEO_BYTES if mime == "video/mp4" else MAX_ARTIFACT_BYTES)
    digest = hashlib.sha256(data).hexdigest()
    if metadata.get("sha256", digest) != digest or metadata.get("size", len(data)) != len(data):
        raise ValueError("artifact integrity check failed")
    return {"mime": mime, "name": metadata["name"], "size": len(data), "sha256": digest, "data": base64.b64encode(data).decode("ascii")}
