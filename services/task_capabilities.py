"""Bounded, per-task file transport for the originating chat's local tools.

The runner owns the manifest and checks the caller's owner before exposing a
request to AstrBot.  The worker gets only a private spool, never the runner
socket, QQ credentials, an owner selector, or another task's capabilities.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import secrets
import stat
import time
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

MAX_TOOLS = 32
MAX_CALLS = 32
MAX_MANIFEST = 128 * 1024
MAX_ARGUMENTS = 64 * 1024
MAX_RESULT = 12 * 1024 * 1024
MAX_ENTRIES = MAX_CALLS * 3 + 8
MAX_WAIT = 240.0
POLL_INTERVAL = .1
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
CALL_RE = re.compile(r"^[0-9a-f]{32}$")
JOB_RE = re.compile(r"^[0-9a-f]{16}$")
OWNER_RE = re.compile(r"^[0-9a-f]{64}$")
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class CapabilityError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code

    def result(self) -> dict:
        return {"ok": False, "code": self.code, "error": str(self)}


def _encode(value: Any, limit: int) -> bytes:
    try:
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
    except (TypeError, ValueError, RecursionError) as exc:
        raise CapabilityError("invalid_payload", "Capability payload is not valid JSON") from exc
    if len(raw) > limit:
        raise CapabilityError("payload_too_large", "Capability payload exceeds its size limit")
    return raw


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _schema_tree(value: Any, depth: int = 0) -> None:
    if depth > 24:
        raise CapabilityError("invalid_manifest", "Capability schema is too deeply nested")
    if isinstance(value, dict):
        if any(key in value for key in ("$ref", "$dynamicRef", "$recursiveRef")):
            raise CapabilityError("invalid_manifest", "Capability schemas must be self-contained")
        for child in value.values():
            _schema_tree(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            _schema_tree(child, depth + 1)


def validate_tools(tools: Any) -> list[dict]:
    """Validate a trusted submitter's tool descriptions, without granting tools."""
    if not isinstance(tools, list) or len(tools) > MAX_TOOLS:
        raise CapabilityError("invalid_manifest", "Invalid capability tool list")
    normalized, names = [], set()
    for tool in tools:
        if not isinstance(tool, dict) or set(tool) - {"name", "description", "inputSchema", "parameters"}:
            raise CapabilityError("invalid_manifest", "Invalid capability tool description")
        name, description = tool.get("name"), tool.get("description", "")
        schema = tool.get("inputSchema", tool.get("parameters"))
        if (not isinstance(name, str) or not NAME_RE.fullmatch(name) or name in names
                or not isinstance(description, str) or len(description) > 6000
                or not isinstance(schema, dict) or schema.get("type") != "object"):
            raise CapabilityError("invalid_manifest", "Invalid capability name or schema")
        if "inputSchema" in tool and "parameters" in tool:
            raise CapabilityError("invalid_manifest", "Capability schema was provided twice")
        _schema_tree(schema)
        try:
            Draft202012Validator.check_schema(schema)
        except (SchemaError, RecursionError) as exc:
            raise CapabilityError("invalid_manifest", "Invalid capability JSON schema") from exc
        names.add(name)
        normalized.append({"name": name, "description": description, "inputSchema": schema})
    # Round-trip so callers cannot subsequently mutate the accepted manifest.
    return json.loads(_encode(normalized, MAX_MANIFEST))


def _validate_arguments(tools: dict, name: Any, arguments: Any) -> None:
    if not isinstance(name, str) or name not in tools:
        raise CapabilityError("capability_not_available", "Tool is not available to this task")
    if not isinstance(arguments, dict):
        raise CapabilityError("invalid_arguments", "Tool arguments must be a JSON object")
    _encode(arguments, MAX_ARGUMENTS)
    try:
        Draft202012Validator(tools[name]["inputSchema"]).validate(arguments)
    except (ValidationError, RecursionError) as exc:
        raise CapabilityError("invalid_arguments", "Tool arguments do not match the advertised schema") from exc


def _root_path(root: Path) -> Path:
    root = Path(root).absolute()
    if (root.parent.name != ".tmp" or not re.fullmatch(r"current-chat-[0-9a-f]{16}", root.name)
            or any(path.is_symlink() for path in (root, *root.parents))):
        raise CapabilityError("unsafe_path", "Invalid capability spool directory")
    return root


class _Spool:
    def _open(self, root: Path) -> None:
        self.root = _root_path(root)
        self.response_fd = -1
        try:
            self.fd = os.open(self.root, DIR_FLAGS)
            info = os.fstat(self.fd)
            if info.st_mode & 0o077:
                os.close(self.fd)
                self.fd = -1
                raise CapabilityError("unsafe_path", "Capability spool must be private")
            self.identity = (info.st_dev, info.st_ino)
        except OSError as exc:
            raise CapabilityError("transport_unavailable", "Capability spool is unavailable") from exc

    def _open_responses(self, *, create=False) -> None:
        if create:
            os.mkdir("responses", 0o700, dir_fd=self.fd)
        try:
            self.response_fd = os.open("responses", DIR_FLAGS, dir_fd=self.fd)
            info = os.fstat(self.response_fd)
            if info.st_mode & 0o077:
                raise CapabilityError("unsafe_path", "Capability responses must be private")
            self.response_identity = (info.st_dev, info.st_ino)
        except OSError as exc:
            raise CapabilityError("transport_unavailable", "Capability response directory is unavailable") from exc

    def _directory(self, response: bool) -> int:
        self._check_root()
        if not response:
            return self.fd
        if self.response_fd < 0:
            raise CapabilityError("task_closed", "Capability responses are no longer active")
        info = os.stat("responses", dir_fd=self.fd, follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != self.response_identity:
            raise CapabilityError("unsafe_path", "Capability response directory was replaced")
        return self.response_fd

    def _check_root(self) -> None:
        if self.fd < 0:
            raise CapabilityError("task_closed", "This task's capabilities are no longer active")
        try:
            info = self.root.lstat()
            if stat.S_ISLNK(info.st_mode) or (info.st_dev, info.st_ino) != self.identity:
                raise CapabilityError("unsafe_path", "Capability spool was replaced")
        except OSError as exc:
            raise CapabilityError("transport_unavailable", "Capability spool is unavailable") from exc

    def _read(self, name: str, limit: int, *, response=False) -> Any:
        directory_fd = self._directory(response)
        fd = -1
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
                raise CapabilityError("unsafe_payload", "Invalid capability transport file")
            raw = bytearray()
            while chunk := os.read(fd, min(65536, limit + 1 - len(raw))):
                raw.extend(chunk)
                if len(raw) > limit:
                    raise CapabilityError("payload_too_large", "Capability payload exceeds its size limit")
            return json.loads(raw, object_pairs_hook=_pairs,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
        except FileNotFoundError:
            raise
        except CapabilityError:
            raise
        except (OSError, ValueError, UnicodeError, RecursionError) as exc:
            raise CapabilityError("invalid_payload", "Could not read capability transport payload") from exc
        finally:
            if fd >= 0:
                os.close(fd)

    def _write(self, name: str, value: Any, limit: int, *, immutable: bool = False, response=False) -> None:
        directory_fd = self._directory(response)
        raw = _encode(value, limit)
        temporary = ".new-" + secrets.token_hex(16)
        fd = -1
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
            view = memoryview(raw)
            while view:
                view = view[os.write(fd, view):]
            if immutable:
                os.fchmod(fd, 0o400)
            os.fsync(fd)
            os.close(fd)
            fd = -1
            # Publish a complete regular file in one rename. Never write
            # through an existing worker-created link. A racing replacement
            # can only replace this spool entry, never its link target.
            try:
                os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise FileExistsError(name)
            os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        except FileExistsError:
            raise
        except OSError as exc:
            raise CapabilityError("transport_unavailable", "Could not publish capability transport payload") from exc
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except OSError:
                pass

    def _names(self) -> list[str]:
        self._check_root()
        names = []
        with os.scandir(self.fd) as entries:
            for entry in entries:
                names.append(entry.name)
                if len(names) > MAX_ENTRIES:
                    raise CapabilityError("call_limit", "Capability spool contains too many entries")
        return names

    def _check_active(self) -> None:
        try:
            active = self._read("active.json", 1024)
            manifest = self._read("manifest.json", MAX_MANIFEST)
        except FileNotFoundError as exc:
            raise CapabilityError("task_closed", "This task's capabilities are no longer active") from exc
        if (active != {"job_id": self.job_id, "generation": self.generation}
                or hashlib.sha256(_encode(manifest, MAX_MANIFEST)).hexdigest() != self.manifest_digest):
            raise CapabilityError("manifest_changed", "This task's capability manifest was changed")

    def release(self) -> None:
        if getattr(self, "response_fd", -1) >= 0:
            os.close(self.response_fd)
            self.response_fd = -1
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


class TaskCapabilities(_Spool):
    """Runner-side lease. Keep this object only while its job is running."""
    def __init__(self, root: Path, tools: list[dict], *, owner: str, job_id: str):
        self.fd = -1
        if not isinstance(owner, str) or not OWNER_RE.fullmatch(owner) or not isinstance(job_id, str) or not JOB_RE.fullmatch(job_id):
            raise CapabilityError("invalid_identity", "Invalid capability task identity")
        self.owner, self.job_id = owner, job_id
        normalized = validate_tools(tools)
        self.tools = {tool["name"]: tool for tool in normalized}
        self.generation = secrets.token_hex(16)
        self.active = False
        self.seen: dict[str, str] = {}
        self.completed: dict[str, str] = {}
        root = _root_path(root)
        if root.name != "current-chat-" + job_id:
            raise CapabilityError("invalid_identity", "Capability spool belongs to a different task")
        try:
            root.mkdir(mode=0o700)
            self._open(root)
            self._open_responses(create=True)
            manifest = {"version": 1, "job_id": job_id, "generation": self.generation, "tools": normalized}
            self.manifest_digest = hashlib.sha256(_encode(manifest, MAX_MANIFEST)).hexdigest()
            self._write("manifest.json", manifest, MAX_MANIFEST, immutable=True)
            self._write("active.json", {"job_id": job_id, "generation": self.generation}, 1024, immutable=True)
            self.active = True
        except BaseException:
            self.close()
            raise

    def _authorize(self, owner: str) -> None:
        if owner != self.owner:
            raise CapabilityError("not_found", "Capability task not found")
        if not self.active:
            raise CapabilityError("task_closed", "This task's capabilities are no longer active")
        self._check_active()

    def pending(self, owner: str) -> list[dict]:
        """Return calls until acknowledged. The trusted consumer deduplicates IDs."""
        self._authorize(owner)
        result = []
        names = self._names()
        requests = sorted(name for name in names if re.fullmatch(r"[0-9a-f]{32}\.request\.json", name))
        for name in requests:
            call_id = name[:32]
            if call_id in self.completed:
                continue
            try:
                request = self._read(name, MAX_ARGUMENTS + 2048)
                if (not isinstance(request, dict) or set(request) != {"call_id", "job_id", "generation", "name", "arguments", "deadline"}
                        or request["call_id"] != call_id or request["job_id"] != self.job_id
                        or request["generation"] != self.generation):
                    raise CapabilityError("invalid_call", "Capability call is not bound to this task")
                digest = hashlib.sha256(_encode(request, MAX_ARGUMENTS + 2048)).hexdigest()
                if call_id not in self.seen:
                    if len(self.seen) >= MAX_CALLS:
                        raise CapabilityError("call_limit", "This task has reached its capability call limit")
                    self.seen[call_id] = digest
                elif self.seen[call_id] != digest:
                    raise CapabilityError("call_changed", "Capability call was changed after submission")
                deadline = request["deadline"]
                if type(deadline) not in {int, float} or not math.isfinite(deadline) or deadline > time.time() + MAX_WAIT + 1:
                    raise CapabilityError("invalid_call", "Invalid capability call deadline")
                if deadline <= time.time() or call_id + ".cancel.json" in names:
                    raise CapabilityError("capability_timeout", "Capability call expired or was cancelled before dispatch")
                _validate_arguments(self.tools, request["name"], request["arguments"])
                result.append({"call_id": call_id, "name": request["name"], "arguments": request["arguments"]})
            except FileNotFoundError:
                continue
            except CapabilityError as exc:
                self._response(call_id, exc.result())
        return result

    def _response(self, call_id: str, result: Any) -> bool:
        raw = _encode(result, MAX_RESULT)
        digest = hashlib.sha256(raw).hexdigest()
        if call_id in self.completed:
            if self.completed[call_id] != digest:
                raise CapabilityError("result_conflict", "Capability call already has a different result")
            return False
        if len(self.completed) >= MAX_CALLS:
            raise CapabilityError("call_limit", "This task has reached its capability call limit")
        payload = {"call_id": call_id, "generation": self.generation, "result": result}
        try:
            self._write(call_id + ".response.json", payload, MAX_RESULT + 1024, immutable=True, response=True)
        except FileExistsError as exc:
            raise CapabilityError("response_changed", "Capability response was pre-created outside the runner") from exc
        self.completed[call_id] = digest
        return True

    def respond(self, owner: str, call_id: str, result: Any) -> bool:
        self._authorize(owner)
        if not isinstance(call_id, str) or not CALL_RE.fullmatch(call_id) or call_id not in self.seen:
            raise CapabilityError("not_found", "Capability call not found")
        return self._response(call_id, result)

    def close(self) -> None:
        # In-memory active is the authority even if a child recreates a file.
        self.active = False
        if self.fd < 0:
            return
        try:
            os.unlink("active.json", dir_fd=self.fd)
        except OSError:
            pass
        try:
            if self.response_fd >= 0:
                with os.scandir(self.response_fd) as entries:
                    for index, entry in enumerate(entries):
                        if index >= MAX_CALLS + 8:
                            break
                        try:
                            os.unlink(entry.name, dir_fd=self.response_fd)
                        except OSError:
                            pass
                try:
                    os.rmdir("responses", dir_fd=self.fd)
                except OSError:
                    pass
            # Never recurse into worker-created directories or follow links.
            for name in self._names():
                try:
                    os.unlink(name, dir_fd=self.fd)
                except OSError:
                    pass
            try:
                self.root.rmdir()
            except OSError:
                pass
        except (OSError, CapabilityError):
            pass
        finally:
            self.release()


class TaskCapabilitiesClient(_Spool):
    """Worker-side MCP transport. No method accepts an owner or QQ identity."""
    def __init__(self, root: Path):
        self.fd = -1
        self._open(root)
        try:
            self._open_responses()
            manifest = self._read("manifest.json", MAX_MANIFEST)
            if (not isinstance(manifest, dict) or set(manifest) != {"version", "job_id", "generation", "tools"}
                    or manifest["version"] != 1 or not isinstance(manifest["job_id"], str)
                    or not JOB_RE.fullmatch(manifest["job_id"]) or not isinstance(manifest["generation"], str)
                    or not CALL_RE.fullmatch(manifest["generation"]) or self.root.name != "current-chat-" + manifest["job_id"]):
                raise CapabilityError("invalid_manifest", "Invalid task capability manifest")
            self.job_id, self.generation = manifest["job_id"], manifest["generation"]
            self.manifest_digest = hashlib.sha256(_encode(manifest, MAX_MANIFEST)).hexdigest()
            self.tools = {tool["name"]: tool for tool in validate_tools(manifest["tools"])}
            self._check_active()
        except BaseException:
            self.release()
            raise

    def list_tools(self) -> list[dict]:
        self._check_active()
        return list(self.tools.values())

    async def call(self, name: str, arguments: dict, *, timeout: float = MAX_WAIT) -> Any:
        _validate_arguments(self.tools, name, arguments)
        if type(timeout) not in {int, float} or not math.isfinite(timeout) or not 0 < timeout <= MAX_WAIT:
            raise CapabilityError("invalid_timeout", "Capability call timeout must be between zero and 240 seconds")
        self._check_active()
        names = self._names()
        if sum(bool(re.fullmatch(r"[0-9a-f]{32}\.request\.json", entry)) for entry in names) >= MAX_CALLS:
            raise CapabilityError("call_limit", "This task has reached its capability call limit")
        call_id = secrets.token_hex(16)
        self._write(call_id + ".request.json", {"call_id": call_id, "job_id": self.job_id,
                    "generation": self.generation, "name": name, "arguments": arguments,
                    "deadline": time.time() + timeout}, MAX_ARGUMENTS + 2048)
        deadline = time.monotonic() + timeout
        try:
            while True:
                self._check_active()
                try:
                    response = self._read(call_id + ".response.json", MAX_RESULT + 1024, response=True)
                    if (not isinstance(response, dict) or set(response) != {"call_id", "generation", "result"}
                            or response["call_id"] != call_id or response["generation"] != self.generation):
                        raise CapabilityError("invalid_response", "Capability response is not bound to this call")
                    return response["result"]
                except FileNotFoundError:
                    pass
                if time.monotonic() >= deadline:
                    raise CapabilityError("capability_timeout", "Local capability call timed out; if execution started its outcome is unknown")
                await asyncio.sleep(min(POLL_INTERVAL, max(0, deadline - time.monotonic())))
        except (CapabilityError, asyncio.CancelledError):
            try:
                self._write(call_id + ".cancel.json", {"cancelled": True}, 1024, immutable=True)
            except (OSError, CapabilityError):
                pass
            raise


def revoke_spools(temporary: Path) -> None:
    """Before a resumed turn, revoke files left by interrupted earlier turns."""
    temporary = Path(temporary).absolute()
    if temporary.name != ".tmp" or any(path.is_symlink() for path in (temporary, *temporary.parents)):
        raise CapabilityError("unsafe_path", "Invalid task temporary directory")
    found = 0
    with os.scandir(temporary) as entries:
        for entry in entries:
            if not re.fullmatch(r"current-chat-[0-9a-f]{16}", entry.name):
                continue
            found += 1
            if found > 64 or entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                raise CapabilityError("unsafe_path", "Invalid previous capability spool")
            lease = TaskCapabilities.__new__(TaskCapabilities)
            lease.fd = -1
            lease.active = False
            try:
                lease._open(temporary / entry.name)
                lease._open_responses()
                lease.close()
            finally:
                lease.release()
