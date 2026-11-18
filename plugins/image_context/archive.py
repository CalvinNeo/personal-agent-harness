"""Durable, requester-scoped image inputs; no network access or expiry policy."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import io
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
from typing import Iterator
from urllib.parse import unquote, urlsplit
import warnings


MAX_IMAGE_BYTES = 20 * 1024 * 1024
DEFAULT_QUERY_IMAGES = 12
MAX_QUERY_IMAGES = 100
MAX_MODEL_IMAGE_BYTES = 512 * 1024
MAX_MODEL_EDGE = 1600
MAX_PIXELS = 20_000_000
_ERROR_MESSAGES = {
    "invalid_scope": "图片归档缺少有效的会话、发送者或消息标识。",
    "invalid_image": "图片归档仅支持不超过 20 MiB 的原始图片。",
    "invalid_query": "图片归档查询参数无效。",
    "unsafe_storage": "图片归档存储路径不安全，当前未读取或写入图片。",
    "not_found": "当前会话中没有这张已归档图片，或图片已不可用。",
    "storage_unavailable": "图片归档暂时不可用，请稍后重试。",
}
_CURSOR_PATTERN = re.compile(r"([1-9][0-9]{0,18}):((?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:e[+-]?[0-9]+)?)")


class ImageArchiveError(ValueError):
    """Safe public errors: never include URLs, credentials or filesystem paths."""
    def __init__(self, code: str):
        self.code = code if code in _ERROR_MESSAGES else "storage_unavailable"
        self.user_message = _ERROR_MESSAGES[self.code]
        super().__init__(self.user_message)


def _valid_identifier(value: object, *, allow_empty: bool = False) -> bool:
    if (not isinstance(value, str) or not (allow_empty or bool(value))
            or len(value) > 512 or any(ord(char) < 0x20 for char in value)):
        return False
    try:
        value.encode("utf-8")
        return True
    except UnicodeEncodeError:
        return False


def _valid_time(value: object) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def _parse_cursor(value: str) -> tuple[int, float]:
    if not isinstance(value, str) or len(value) > 96:
        raise ImageArchiveError("invalid_query")
    match = _CURSOR_PATTERN.fullmatch(value)
    if match is None:
        raise ImageArchiveError("invalid_query")
    row_id, timestamp = int(match[1]), float(match[2])
    if row_id > 2**63 - 1 or not _valid_time(timestamp):
        raise ImageArchiveError("invalid_query")
    return row_id, timestamp


def _image_magic(data: bytes) -> bool:
    # Decoding and pixel limits belong to the caller's image validation. This
    # boundary still rejects arbitrary files before writing any persistent blob.
    return (data.startswith(b"\x89PNG\r\n\x1a\n") or data.startswith(b"\xff\xd8\xff")
            or data.startswith((b"GIF87a", b"GIF89a", b"BM", b"II*\x00", b"MM\x00*"))
            or data.startswith(b"RIFF") and data[8:12] == b"WEBP"
            or data[4:8] == b"ftyp" and data[8:12] in {b"avif", b"avis"})


def _read_image_at(directory_descriptor: int, name: str, *, digest: str | None = None,
                   make_private: bool = False) -> bytes:
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=directory_descriptor)
    except FileNotFoundError:
        raise ImageArchiveError("not_found") from None
    except OSError:
        raise ImageArchiveError("unsafe_storage") from None
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_IMAGE_BYTES:
            raise ImageArchiveError("unsafe_storage")
        if make_private:
            os.fchmod(handle.fileno(), 0o600)
        data = handle.read(MAX_IMAGE_BYTES + 1)
    if (not 0 < len(data) <= MAX_IMAGE_BYTES or not _image_magic(data)
            or digest is not None and hashlib.sha256(data).hexdigest() != digest):
        raise ImageArchiveError("unsafe_storage")
    return data


def read_temp_image(path: str, temp_root: Path) -> bytes:
    """Read an original only below the framework temp root, without symlinks."""
    descriptor = None
    try:
        if not isinstance(path, str) or not path or "\x00" in path:
            raise ImageArchiveError("unsafe_storage")
        if path.startswith("file:"):
            parsed = urlsplit(path)
            if parsed.scheme != "file" or parsed.netloc or parsed.query or parsed.fragment:
                raise ImageArchiveError("unsafe_storage")
            path = unquote(parsed.path)
        root, candidate = Path(temp_root), Path(path)
        if (not root.is_absolute() or root.parent == root or not candidate.is_absolute()
                or ".." in root.parts or ".." in candidate.parts):
            raise ImageArchiveError("unsafe_storage")
        relative = candidate.relative_to(root)
        if not relative.parts:
            raise ImageArchiveError("unsafe_storage")
        descriptor = ImageArchive._open_directory(root, create=False)
        for component in relative.parts[:-1]:
            try:
                next_descriptor = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                          dir_fd=descriptor)
            except OSError:
                raise ImageArchiveError("unsafe_storage") from None
            os.close(descriptor)
            descriptor = next_descriptor
        return _read_image_at(descriptor, relative.name)
    except ImageArchiveError:
        raise
    except (OSError, ValueError, TypeError):
        raise ImageArchiveError("unsafe_storage") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def normalize_for_model(data: bytes) -> bytes:
    """Return a bounded JPEG view while leaving the archived original intact."""
    from PIL import Image, ImageOps, UnidentifiedImageError
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_IMAGE_BYTES or not _image_magic(data):
        raise ImageArchiveError("invalid_image")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as source:
                if (source.width <= 0 or source.height <= 0 or source.width * source.height > MAX_PIXELS
                        or getattr(source, "n_frames", 1) != 1):
                    raise ImageArchiveError("invalid_image")
                source.load()
                oriented = ImageOps.exif_transpose(source)
                oriented.thumbnail((MAX_MODEL_EDGE, MAX_MODEL_EDGE), Image.Resampling.LANCZOS)
                rgba = oriented.convert("RGBA")
                image = Image.new("RGB", rgba.size, "white")
                image.paste(rgba, mask=rgba.getchannel("A"))
                for _ in range(6):
                    for quality in (85, 70, 55, 40):
                        output = io.BytesIO()
                        image.save(output, format="JPEG", quality=quality, optimize=True,
                                   exif=b"", icc_profile=None)
                        encoded = output.getvalue()
                        if len(encoded) <= MAX_MODEL_IMAGE_BYTES:
                            return encoded
                    image.thumbnail((max(1, int(image.width * .8)), max(1, int(image.height * .8))),
                                    Image.Resampling.LANCZOS)
    except ImageArchiveError:
        raise
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombWarning,
            Image.DecompressionBombError):
        raise ImageArchiveError("invalid_image") from None
    raise ImageArchiveError("invalid_image")


class ImageArchive:
    """Keep every accepted image, with exact (UMO, requester) access checks.

    ``images`` items are ``(validated_original_bytes, original_reference)``.
    Only the original reference's SHA-256 is stored. Identity is the exact
    scope, event message ID and image content hash. Replaying an event never
    replaces its original receipt time. An explicit quote uses the current
    input message ID, ``source='reply'`` and optionally its original reply ID.
    """
    def __init__(self, root: Path):
        self.root = Path(root)
        if not self.root.is_absolute() or self.root.parent == self.root or ".." in self.root.parts:
            raise ImageArchiveError("unsafe_storage")
        self.files = self.root / "files"
        self.database = self.root / "index.sqlite3"
        try:
            with self._database() as (connection, _):
                connection.executescript("""
                    CREATE TABLE IF NOT EXISTS images (
                        id INTEGER PRIMARY KEY,
                        umo TEXT NOT NULL,
                        sender TEXT NOT NULL,
                        message_id TEXT NOT NULL,
                        image_sha256 TEXT NOT NULL,
                        original_ref_sha256 TEXT NOT NULL,
                        received_wall_at REAL NOT NULL,
                        source TEXT NOT NULL CHECK (source IN ('upload', 'reply')),
                        reply_id TEXT NOT NULL,
                        position INTEGER NOT NULL,
                        UNIQUE (umo, sender, message_id, image_sha256)
                    );
                    CREATE INDEX IF NOT EXISTS images_scope_time
                    ON images (umo, sender, received_wall_at DESC, id DESC);
                    CREATE INDEX IF NOT EXISTS images_scope_message
                    ON images (umo, sender, message_id);
                    CREATE INDEX IF NOT EXISTS images_scope_reply
                    ON images (umo, sender, reply_id);
                """)
        except (OSError, sqlite3.Error):
            raise ImageArchiveError("storage_unavailable") from None

    @staticmethod
    def _open_directory(path: Path, *, create: bool) -> int:
        """Walk absolute ancestors with O_NOFOLLOW, including the archive root."""
        descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for component in path.parts[1:]:
                if create:
                    try:
                        os.mkdir(component, 0o700, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                try:
                    next_descriptor = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                              dir_fd=descriptor)
                except OSError:
                    raise ImageArchiveError("unsafe_storage") from None
                os.close(descriptor)
                descriptor = next_descriptor
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    @staticmethod
    def _check_sidecars(root_descriptor: int):
        for name in ("index.sqlite3-journal", "index.sqlite3-wal", "index.sqlite3-shm"):
            try:
                info = os.stat(name, dir_fd=root_descriptor, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ImageArchiveError("unsafe_storage")
            try:
                descriptor = os.open(name, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                                     dir_fd=root_descriptor)
            except FileNotFoundError:
                # A concurrent committed transaction may remove its journal
                # between lstat and open. No file was followed in that case.
                continue
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ImageArchiveError("unsafe_storage")
                os.fchmod(descriptor, 0o600)
            finally:
                os.close(descriptor)

    @contextmanager
    def _database(self) -> Iterator[tuple[sqlite3.Connection, int]]:
        root_descriptor = files_descriptor = None
        connection = None
        try:
            root_descriptor = self._open_directory(self.root, create=True)
            os.fchmod(root_descriptor, 0o700)
            try:
                os.mkdir("files", 0o700, dir_fd=root_descriptor)
            except FileExistsError:
                pass
            try:
                files_descriptor = os.open("files", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                           dir_fd=root_descriptor)
            except OSError:
                raise ImageArchiveError("unsafe_storage") from None
            os.fchmod(files_descriptor, 0o700)
            self._check_sidecars(root_descriptor)
            try:
                descriptor = os.open("index.sqlite3", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                                     0o600, dir_fd=root_descriptor)
            except OSError:
                raise ImageArchiveError("unsafe_storage") from None
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ImageArchiveError("unsafe_storage")
                os.fchmod(descriptor, 0o600)
            finally:
                os.close(descriptor)
            connection = sqlite3.connect(str(self.database), timeout=10, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute("PRAGMA foreign_keys=ON")
            yield connection, files_descriptor
        except (OSError, sqlite3.Error):
            raise ImageArchiveError("storage_unavailable") from None
        finally:
            if connection is not None:
                connection.close()
            if files_descriptor is not None:
                os.close(files_descriptor)
            if root_descriptor is not None:
                os.close(root_descriptor)

    @staticmethod
    def _read_blob(files_descriptor: int, digest: str) -> bytes:
        return _read_image_at(files_descriptor, digest, digest=digest, make_private=True)

    @classmethod
    def _write_blob(cls, files_descriptor: int, digest: str, data: bytes):
        try:
            cls._read_blob(files_descriptor, digest)
            return
        except ImageArchiveError as error:
            if error.code != "not_found":
                raise
        temporary = ".image-" + secrets.token_hex(16)
        descriptor = None
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=files_descriptor)
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = None
                os.fchmod(handle.fileno(), 0o600)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            # Link without replacement: a pre-existing malicious symlink must
            # fail validation rather than be followed or silently overwritten.
            try:
                os.link(temporary, digest, src_dir_fd=files_descriptor, dst_dir_fd=files_descriptor,
                        follow_symlinks=False)
            except FileExistsError:
                cls._read_blob(files_descriptor, digest)
            os.fsync(files_descriptor)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=files_descriptor)
            except FileNotFoundError:
                pass

    def ingest(self, umo: str, sender: str, message_id: str,
               images: list[tuple[bytes, str]], received_wall_at: float,
               source: str = "upload", reply_id: str = "") -> list[str]:
        if (not all(_valid_identifier(value) for value in (umo, sender, message_id))
                or not _valid_identifier(reply_id, allow_empty=True)
                or not isinstance(source, str) or source not in {"upload", "reply"}
                or not _valid_time(received_wall_at)):
            raise ImageArchiveError("invalid_scope")
        if not isinstance(images, list):
            raise ImageArchiveError("invalid_image")
        validated = []
        for image in images:
            if not isinstance(image, (list, tuple)) or len(image) != 2:
                raise ImageArchiveError("invalid_image")
            data, original_ref = image
            if (not isinstance(data, bytes) or not 0 < len(data) <= MAX_IMAGE_BYTES
                    or not _image_magic(data) or not isinstance(original_ref, str) or len(original_ref) > 8192):
                raise ImageArchiveError("invalid_image")
            try:
                original_ref_digest = hashlib.sha256(original_ref.encode()).hexdigest()
            except UnicodeEncodeError:
                raise ImageArchiveError("invalid_image") from None
            validated.append((data, hashlib.sha256(data).hexdigest(), original_ref_digest))
        refs = []
        with self._database() as (connection, files_descriptor):
            connection.execute("BEGIN IMMEDIATE")
            try:
                for position, (data, digest, original_ref_digest) in enumerate(validated):
                    self._write_blob(files_descriptor, digest, data)
                    connection.execute("""
                        INSERT OR IGNORE INTO images
                        (umo, sender, message_id, image_sha256, original_ref_sha256,
                         received_wall_at, source, reply_id, position)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (umo, sender, message_id, digest, original_ref_digest,
                          float(received_wall_at), source, reply_id, position))
                    ref = str(self.files / digest)
                    if ref not in refs:
                        refs.append(ref)
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        return refs

    def list_images(self, umo: str, sender: str, limit: int = DEFAULT_QUERY_IMAGES,
                    before: float | None = None, message_id: str | None = None,
                    cursor: str | None = None, reply_id: str | None = None) -> list[dict]:
        """Return the latest bounded page in oldest-to-newest input order.

        ``before`` is an exclusive receipt timestamp; ``message_id`` addresses
        one exact input event; ``reply_id`` addresses the explicitly quoted
        source recorded on earlier input events in this same scope.
        To retrieve older pages without skipping images
        that share a timestamp, pass the first row's opaque ``cursor`` back on
        the next query. Neither selection nor reading refreshes receipt.
        Storage retains all events regardless of page size or elapsed time.
        """
        if not all(_valid_identifier(value) for value in (umo, sender)):
            raise ImageArchiveError("invalid_scope")
        if (type(limit) is not int or not 1 <= limit <= MAX_QUERY_IMAGES
                or before is not None and not _valid_time(before)
                or message_id is not None and not _valid_identifier(message_id)
                or reply_id is not None and not _valid_identifier(reply_id)):
            raise ImageArchiveError("invalid_query")
        clauses, values = ["umo = ?", "sender = ?"], [umo, sender]
        if before is not None:
            clauses.append("received_wall_at < ?")
            values.append(float(before))
        if cursor is not None:
            row_id, timestamp = _parse_cursor(cursor)
            clauses.append("(received_wall_at < ? OR (received_wall_at = ? AND id < ?))")
            values.extend((timestamp, timestamp, row_id))
        if message_id is not None:
            clauses.append("message_id = ?")
            values.append(message_id)
        if reply_id is not None:
            clauses.append("reply_id = ?")
            values.append(reply_id)
        query = ("SELECT * FROM images WHERE " + " AND ".join(clauses)
                 + " ORDER BY received_wall_at DESC, id DESC LIMIT ?")
        values.append(limit)
        with self._database() as (connection, _):
            rows = connection.execute(query, values).fetchall()
        return [{"ref": str(self.files / row["image_sha256"]),
                 "received_wall_at": row["received_wall_at"], "source": row["source"],
                 "message_id": row["message_id"], "reply_id": row["reply_id"],
                 "batch_id": row["message_id"], "position": row["position"],
                 "cursor": f"{row['id']}:{float(row['received_wall_at'])!r}",
                 "sha256": row["image_sha256"], "original_ref_sha256": row["original_ref_sha256"]}
                for row in reversed(rows)]

    def read(self, umo: str, sender: str, ref: str) -> bytes:
        """Read only a content-addressed image indexed to this exact scope."""
        if not all(_valid_identifier(value) for value in (umo, sender)):
            raise ImageArchiveError("invalid_scope")
        if not isinstance(ref, str):
            raise ImageArchiveError("not_found")
        candidate = Path(ref)
        digest = candidate.name
        if (candidate.parent != self.files or str(candidate) != ref or ".." in candidate.parts
                or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest)):
            raise ImageArchiveError("not_found")
        with self._database() as (connection, files_descriptor):
            row = connection.execute(
                "SELECT 1 FROM images WHERE umo = ? AND sender = ? AND image_sha256 = ? LIMIT 1",
                (umo, sender, digest),
            ).fetchone()
            if row is None:
                raise ImageArchiveError("not_found")
            return self._read_blob(files_descriptor, digest)
