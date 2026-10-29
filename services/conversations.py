"""Owner-scoped durable Codex homes and retained visual inputs."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
from pathlib import Path

RETENTION = 7 * 86400
MAX_CONVERSATIONS = 100
MAX_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_FILES = 4096
THREAD_RE = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")


def private_dir(path: Path, *, create=False) -> Path:
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError("unsafe conversation directory")
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir():
        raise ValueError("conversation directory unavailable")
    return path


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _open_directory(root_fd: int, parts: tuple[str, ...], *, repair=False) -> int:
    """Reopen beneath an anchored root with bounded descriptors and no links."""
    fd = os.dup(root_fd)
    try:
        for name in parts:
            if repair:
                os.chmod(name, 0o700, dir_fd=fd, follow_symlinks=False)
            child = os.open(name, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def remove_tree(path: Path) -> None:
    """Iteratively delete private data, including unreadable or deep trees."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode):
        path.unlink()
        return
    if not stat.S_ISDIR(info.st_mode):
        raise NotADirectoryError(path)
    os.chmod(path, 0o700, follow_symlinks=False)
    root_fd = os.open(path, _DIR_FLAGS)
    try:
        # Reopen relative to the pinned root rather than retaining one FD per
        # level: a valid 1050-level tree exceeds typical process FD limits.
        stack = [((), False)]
        while stack:
            parts, remove_after_children = stack.pop()
            if remove_after_children:
                if parts:
                    parent_fd = _open_directory(root_fd, parts[:-1], repair=True)
                    try:
                        os.rmdir(parts[-1], dir_fd=parent_fd)
                    finally:
                        os.close(parent_fd)
                continue
            fd = _open_directory(root_fd, parts, repair=True)
            try:
                children = []
                with os.scandir(fd) as entries:
                    for entry in entries:
                        info = os.stat(entry.name, dir_fd=fd, follow_symlinks=False)
                        if stat.S_ISDIR(info.st_mode):
                            children.append(parts + (entry.name,))
                        else:
                            os.unlink(entry.name, dir_fd=fd)
                stack.append((parts, True))
                stack.extend((child, False) for child in reversed(children))
            finally:
                os.close(fd)
    finally:
        os.close(root_fd)
    path.rmdir()


def disk_size(path: Path, *, limit=None) -> int:
    limit = MAX_BYTES if limit is None else limit
    total = count = 0
    # os.walk's default onerror=None silently skips chmod-000 directories.
    # Every failed open/stat/scan must fail validation. Count all entries,
    # including directories and links, and stream the scan to stop at the
    # limit without first allocating an arbitrarily large entry list.
    try:
        root_fd = os.open(path, _DIR_FLAGS)
        try:
            pending = [()]
            while pending:
                parts = pending.pop()
                fd = _open_directory(root_fd, parts)
                try:
                    with os.scandir(fd) as entries:
                        for entry in entries:
                            count += 1
                            if count > MAX_FILES:
                                raise ValueError("conversation storage limit exceeded")
                            info = os.stat(entry.name, dir_fd=fd, follow_symlinks=False)
                            if stat.S_ISLNK(info.st_mode):
                                continue
                            if stat.S_ISDIR(info.st_mode):
                                pending.append(parts + (entry.name,))
                                continue
                            if not stat.S_ISREG(info.st_mode):
                                raise ValueError("conversation storage contains unsupported file")
                            total += info.st_size
                            if total > limit:
                                raise ValueError("conversation storage limit exceeded")
                finally:
                    os.close(fd)
        finally:
            os.close(root_fd)
    except OSError as exc:
        raise ValueError("conversation storage cannot be traversed or inspected") from exc
    return total


class Conversations:
    def __init__(self, records: Path, roots: Path, media):
        # Canonicalize system aliases (macOS /var) once at the trusted roots;
        # paths within a conversation still reject every symlink component.
        records, roots = records.resolve(), roots.resolve()
        self.records, self.roots, self.media = records, roots, media
        private_dir(records, create=True)
        private_dir(roots, create=True)
        self.items = {}
        for path in records.glob("*.json"):
            try:
                if path.is_symlink() or not re.fullmatch(r"[a-f0-9]{16}\.json", path.name) or path.stat().st_size > 65536:
                    continue
                data = json.loads(path.read_text())
                if (data.get("id") != path.stem or not re.fullmatch(r"[a-f0-9]{64}", data.get("owner", ""))
                        or data.get("thread_id") and not THREAD_RE.fullmatch(data["thread_id"])
                        or not isinstance(data.get("images"), list) or len(data["images"]) > 4
                        or not isinstance(data.get("updated"), (int, float))):
                    continue
                self.items[data["id"]] = data
            except (OSError, ValueError, TypeError):
                continue

    def root(self, item: dict) -> Path:
        if not re.fullmatch(r"[a-f0-9]{16}", item["id"]) or not re.fullmatch(r"[a-f0-9]{64}", item["owner"]):
            raise ValueError("invalid conversation identity")
        digest = hashlib.sha256((item["owner"] + "\0" + item["id"]).encode()).hexdigest()[:32]
        result = self.roots / ("conversation-" + digest)
        if result.is_symlink():
            raise ValueError("unsafe conversation directory")
        return result

    def persist(self, item: dict):
        target = self.records / (item["id"] + ".json")
        temporary = target.with_suffix(".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(item, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)

    def create(self, cid: str, owner: str) -> dict:
        if cid in self.items:
            raise ValueError("conversation id collision")
        item = {"id": cid, "owner": owner, "thread_id": None, "images": [],
                "created": time.time(), "updated": time.time(), "unavailable": None}
        root = self.root(item)
        root.mkdir(mode=0o700)  # Never attach to an unrelated existing directory.
        for name in ("home", "workspace"):
            (root / name).mkdir(mode=0o700)
        for name in ("input", "output", ".tmp"):
            (root / "workspace" / name).mkdir(mode=0o700)
        self.persist(item)
        self.items[cid] = item
        return item

    def get(self, cid: str, owner: str, *, active=False) -> dict:
        item = self.items.get(cid)
        if not item or item["owner"] != owner:
            raise ValueError("conversation_unavailable: no retained Codex session")
        if item.get("unavailable"):
            raise ValueError("conversation_unavailable: " + item["unavailable"])
        if not active and time.time() - item["updated"] > RETENTION:
            raise ValueError("conversation_expired: retained Codex session expired")
        private_dir(self.root(item))
        disk_size(self.root(item))
        return item

    def touch(self, item: dict):
        item["updated"] = time.time()
        self.persist(item)

    def set_thread(self, item: dict, tid: str):
        if not isinstance(tid, str) or not THREAD_RE.fullmatch(tid):
            raise ValueError("invalid Codex session id")
        if item.get("thread_id") and item["thread_id"] != tid:
            raise ValueError("Codex resume returned a different session")
        item["thread_id"] = tid
        self.touch(item)

    def add_images(self, item: dict, images: list[dict]) -> list[Path]:
        root = self.root(item)
        workspace = private_dir(root / "workspace")
        folder = private_dir(workspace / "input")
        existing = {entry["digest"] for entry in item["images"]}
        fresh = [image for image in images if image["digest"] not in existing]
        # A multi-image upload can repeat the same image too.
        fresh = list({image["digest"]: image for image in fresh}.values())
        if len(item["images"]) + len(fresh) > 4:
            raise ValueError("conversation_image_limit: at most four retained images")
        for image in fresh:
            name = image["digest"] + self.media.MIMES[image["mime"]][1]
            path = folder / name
            if path.exists():
                # Recover a fully written input whose metadata commit was
                # interrupted, never trust an arbitrary preexisting file.
                data = self.media.read_regular(path, self.media.MAX_INPUT_BYTES)
                if hashlib.sha256(data).hexdigest() != image["digest"]:
                    raise ValueError("retained image digest mismatch")
            else:
                self.media.private_write(path, image["bytes"])
            item["images"].append({"name": name, "mime": image["mime"], "digest": image["digest"]})
        self.touch(item)
        return self.image_paths(item)

    def image_paths(self, item: dict) -> list[Path]:
        folder = private_dir(self.root(item) / "workspace" / "input")
        result = []
        for image in item["images"]:
            if (image.get("mime") not in self.media.MIMES or not re.fullmatch(r"[0-9a-f]{64}", image.get("digest", ""))
                    or image.get("name") != image["digest"] + self.media.MIMES[image["mime"]][1]):
                raise ValueError("invalid retained image")
            path = folder / image["name"]
            data = self.media.read_regular(path, self.media.MAX_INPUT_BYTES)
            if hashlib.sha256(data).hexdigest() != image["digest"]:
                raise ValueError("retained image changed")
            result.append(path)
        return result

    def prepare_output(self, item: dict, job_id: str):
        workspace = private_dir(self.root(item) / "workspace")
        output = private_dir(workspace / "output")
        if any(output.iterdir()):
            history = private_dir(workspace / "previous-output", create=True)
            previous = item.get("last_job_id")
            if not isinstance(previous, str) or not re.fullmatch(r"[0-9a-f]{16}", previous):
                raise ValueError("invalid previous output identity")
            target = history / previous
            if target.exists() or target.is_symlink():
                raise ValueError("previous output collision")
            output.rename(target)
            output.mkdir(mode=0o700)
        item["last_job_id"] = job_id
        self.touch(item)

    def retire(self, item: dict, reason: str):
        remove_tree(self.root(item))
        item.update(unavailable=reason, thread_id=None, images=[])
        self.persist(item)

    def prune(self, active: set[str]):
        retained = [item for item in self.items.values() if not item.get("unavailable")]
        retained.sort(key=lambda item: item["updated"], reverse=True)
        total = 0
        for item in retained:
            if item["id"] in active:
                try:
                    total += disk_size(self.root(item))
                except (OSError, ValueError):
                    # The live worker's storage watchdog stops oversized jobs;
                    # retention must not delete files underneath that process.
                    total += MAX_BYTES
        for index, item in enumerate(retained):
            if item["id"] in active:
                continue
            try:
                size = disk_size(self.root(item))
            except (OSError, ValueError):
                self.retire(item, "conversation storage limit or filesystem validation failed")
                continue
            expired = time.time() - item["updated"] > RETENTION
            if expired or index >= MAX_CONVERSATIONS or total + size > MAX_TOTAL_BYTES:
                self.retire(item, "retention expired")
            else:
                total += size
