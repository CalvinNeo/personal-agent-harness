"""Credential-safe read-only notes and operator login client."""
from __future__ import annotations

import base64
import binascii
import ipaddress
import json
import math
import os
import re
import stat
import struct
import urllib.error
import urllib.parse
import urllib.request
import zlib
from pathlib import Path
from typing import Any, Callable

MAX_TOKEN_LENGTH = 4096
MAX_QUERY_LENGTH = 500
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_QR_BYTES = 512 * 1024
MAX_RESULT_TEXT = 800
MAX_RESULTS = 20
_TOKEN_RE = re.compile(r"^[\x21-\x7e]+$")
_OPERATIONS = {
    ("POST", "/api/v1/feeds/search"),
    ("POST", "/api/v1/feeds/detail"),
    ("GET", "/api/v1/login/status"),
    ("GET", "/api/v1/login/qrcode"),
    ("DELETE", "/api/v1/login/cookies"),
}


class XHSError(ValueError):
    """Expected failure whose message and code never contain upstream data."""

    def __init__(self, message: str, *, code: str = "service_error"):
        super().__init__(message)
        self.code = code


def validate_token(token: object) -> str:
    if not isinstance(token, str):
        raise XHSError("API token 文件无效。", code="token_invalid")
    value = token.strip()
    if not 8 <= len(value) <= MAX_TOKEN_LENGTH or not _TOKEN_RE.fullmatch(value):
        raise XHSError("API token 文件无效。", code="token_invalid")
    return value


def _read_token(path: str | os.PathLike[str]) -> str:
    """Open once, then inspect the same descriptor without following symlinks."""
    descriptor = -1
    try:
        descriptor = os.open(
            Path(path).expanduser(),
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0),
        )
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid not in {0, os.geteuid()}
                or stat.S_IMODE(info.st_mode) not in {0o600, 0o640}
                or info.st_size > MAX_TOKEN_LENGTH + 2):
            raise XHSError("API token 文件权限或类型无效。", code="token_invalid")
        # A bounded read also protects against concurrent file growth after fstat.
        raw = os.read(descriptor, MAX_TOKEN_LENGTH + 3)
        if len(raw) > MAX_TOKEN_LENGTH + 2:
            raise XHSError("API token 文件无效。", code="token_invalid")
        return validate_token(raw.decode("ascii"))
    except XHSError:
        raise
    except (OSError, ValueError, UnicodeError):
        raise XHSError("API token 文件不可读。", code="token_unavailable") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _endpoint(base: str) -> str:
    if (not isinstance(base, str) or not base or len(base) > 2048
            or re.search(r"[\s\x00-\x20\x7f\\?#]", base)):
        raise XHSError("小红书服务地址无效。", code="endpoint_invalid")
    try:
        parsed = urllib.parse.urlsplit(base)
        host = parsed.hostname or ""
        port = parsed.port
        local_http = False
        if parsed.scheme == "http":
            try:
                local_http = ipaddress.ip_address(host).is_loopback
            except ValueError:
                pass
        if (not host or parsed.username is not None or parsed.password is not None
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
                or not (local_http or parsed.scheme == "https")
                or (not local_http and port not in {None, 443})
                or (port is not None and port < 1)):
            raise ValueError
    except (ValueError, TypeError):
        raise XHSError("小红书服务地址必须是 HTTPS 或回环 IP 的 HTTP 根地址。", code="endpoint_invalid") from None
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def _text(value: Any, limit: int = MAX_RESULT_TEXT) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"[\x00-\x1f\x7f]+", " ", value).strip()[:limit]


def normalize_results(payload: Any, max_results: int = MAX_RESULTS) -> list[dict[str, str]]:
    """Normalize the actual data.feeds / noteCard search response schema."""
    if isinstance(payload, dict) and "success" in payload:
        if payload.get("success") is not True or "error" in payload or "code" in payload:
            raise XHSError("小红书服务请求失败。")
        payload = payload.get("data")
    if not isinstance(payload, dict) or not isinstance(payload.get("feeds"), list):
        raise XHSError("小红书搜索响应格式无效。")
    try:
        limit = max(1, min(int(max_results), MAX_RESULTS))
    except (TypeError, ValueError, OverflowError):
        limit = MAX_RESULTS
    output: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in payload["feeds"]:
        if not isinstance(raw, dict) or raw.get("modelType", "note") != "note":
            continue
        card = raw.get("noteCard")
        feed_id = raw.get("id")
        if not isinstance(card, dict) or not isinstance(feed_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", feed_id):
            continue
        if feed_id in seen:
            continue
        seen.add(feed_id)
        user = card.get("user")
        author = _text(user.get("nickname") or user.get("nickName")) if isinstance(user, dict) else ""
        output.append({
            "title": _text(card.get("displayTitle")) or "无标题",
            # Do not relay arbitrary URLs, credentials, images, or xsecToken fields.
            "url": "https://www.xiaohongshu.com/explore/" + feed_id,
            "snippet": author,
        })
        if len(output) >= limit:
            break
    return output


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        # In particular, never forward Authorization to a Location target.
        return None


class XHSClient:
    """Search and operator login lifecycle, with no arbitrary endpoint access."""

    def __init__(self, *, endpoint: str, token_file: str, timeout: float = 90.0,
                 opener: Callable[..., Any] | None = None):
        self.endpoint = _endpoint(endpoint)
        self.token_file = token_file
        try:
            seconds = float(timeout)
            if not math.isfinite(seconds):
                raise ValueError
            self.timeout = max(5.0, min(seconds, 180.0))
        except (TypeError, ValueError, OverflowError):
            raise XHSError("小红书服务超时配置无效。", code="request_invalid") from None
        # Ignore environment proxy variables and prohibit every redirect.
        self._opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect(),
        ).open

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if (method, path) not in _OPERATIONS:
            raise XHSError("小红书请求操作无效。", code="request_invalid")
        valid_search = path == "/api/v1/feeds/search" and (
                isinstance(payload, dict) and set(payload) == {"keyword", "filters"}
                and isinstance(payload["keyword"], str) and payload["keyword"].strip()
                and len(payload["keyword"]) <= MAX_QUERY_LENGTH and payload["filters"] == {})
        valid_detail = path == "/api/v1/feeds/detail" and (
                isinstance(payload, dict) and set(payload) == {"feed_id", "xsec_token", "load_all_comments"}
                and isinstance(payload["feed_id"], str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", payload["feed_id"])
                and isinstance(payload["xsec_token"], str) and re.fullmatch(r"[A-Za-z0-9+/_=-]{1,2048}", payload["xsec_token"])
                and payload["load_all_comments"] is False)
        if (method != "POST" and payload is not None) or (method == "POST" and not (valid_search or valid_detail)):
            raise XHSError("小红书请求参数无效。", code="request_invalid")
        token = _read_token(self.token_file)
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint + path, data=body,
            headers={"Accept": "application/json", "Content-Type": "application/json", "Authorization": "Bearer " + token},
            method=method,
        )
        try:
            with self._opener(request, timeout=self.timeout) as response:
                status = int(response.status)
                raw = response.read(MAX_RESPONSE_BYTES + 1) if 200 <= status < 300 else b""
        except urllib.error.HTTPError as error:
            status = error.code
            if status in (401, 403):
                raise XHSError("小红书服务鉴权失败。", code="unauthorized") from None
            if status in (404, 405):
                raise XHSError("小红书服务接口版本不兼容。", code="endpoint_invalid") from None
            try:
                error_data = json.loads(error.read(4097))
                error_code = error_data.get("code") if isinstance(error_data, dict) else None
            except (ValueError, OSError, TypeError):
                error_code = None
            if error_code == "LOGIN_REQUIRED":
                raise XHSError("小红书尚未登录。", code="login_required") from None
            if error_code == "LOGIN_PENDING":
                raise XHSError("小红书正在等待扫码确认。", code="login_pending") from None
            if path == "/api/v1/login/status":
                raise XHSError("小红书登录状态检查暂未成功。", code="status_unavailable") from None
            raise XHSError("小红书服务暂时不可用。") from None
        except Exception:
            # HTTP exceptions can contain headers, URLs and response bodies.
            raise XHSError("小红书服务暂时不可用。") from None
        if not 200 <= status < 300 or not isinstance(raw, bytes) or len(raw) > MAX_RESPONSE_BYTES:
            raise XHSError("小红书服务返回了无效响应。")
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError, TypeError, RecursionError):
            raise XHSError("小红书服务返回的 JSON 无效。") from None
        # Discard an entire response that echoes the bearer secret, including
        # JSON-escaped and URL-encoded echoes, instead of leaking any field.
        secret_forms = {token, urllib.parse.quote(token, safe=""), urllib.parse.quote_plus(token)}
        pending = [document]
        while pending:
            value = pending.pop()
            if isinstance(value, str) and any(secret in value for secret in secret_forms):
                raise XHSError("小红书服务返回了无效响应。")
            if isinstance(value, dict):
                pending.extend(value.keys())
                pending.extend(value.values())
            elif isinstance(value, list):
                pending.extend(value)
        if (not isinstance(document, dict) or document.get("success") is not True
                or "error" in document or "code" in document
                or not isinstance(document.get("data"), dict)):
            raise XHSError("小红书服务请求失败。")
        return document["data"]

    def search(self, query: str, *, limit: int = 5) -> list[dict[str, str]]:
        if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_LENGTH:
            raise XHSError("请提供有效的搜索关键词。", code="invalid_query")
        data = self._request("POST", "/api/v1/feeds/search", {"keyword": query.strip(), "filters": {}})
        return normalize_results(data, limit)

    def search_bundle(self, query: str, *, limit: int = 5) -> dict:
        if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_LENGTH:
            raise XHSError("请提供有效的搜索关键词。", code="invalid_query")
        from .notes import search_references
        data = self._request("POST", "/api/v1/feeds/search", {"keyword": query.strip(), "filters": {}})
        results = normalize_results(data, limit)
        return {"results": results, "references": search_references(data, normalize_results(data, MAX_RESULTS))}

    def note_detail(self, feed_id: str, xsec_token: str) -> dict:
        from .notes import normalize_note
        data = self._request("POST", "/api/v1/feeds/detail", {
            "feed_id": feed_id, "xsec_token": xsec_token, "load_all_comments": False,
        })
        return normalize_note(data, feed_id)

    def login_status(self) -> bool:
        return self.login_details()["is_logged_in"]

    def login_details(self) -> dict[str, bool]:
        data = self._request("GET", "/api/v1/login/status")
        value = data.get("is_logged_in")
        if type(value) is not bool:
            raise XHSError("小红书登录状态响应无效。")
        return {"is_logged_in": value, "login_pending": data.get("login_pending") is True}

    def login_qr(self) -> bytes:
        data = self._request("GET", "/api/v1/login/qrcode")
        value = data.get("is_logged_in")
        if type(value) is not bool:
            raise XHSError("小红书登录状态响应无效。")
        if value:
            raise XHSError("小红书已经登录。", code="already_logged_in")
        image = data.get("img")
        prefix = "data:image/png;base64,"
        if not isinstance(image, str) or not image.startswith(prefix) or len(image) > len(prefix) + 4 * ((MAX_QR_BYTES + 2) // 3):
            raise XHSError("登录服务返回的二维码无效。")
        try:
            decoded = base64.b64decode(image[len(prefix):], validate=True)
            if len(decoded) > MAX_QR_BYTES or not decoded.startswith(b"\x89PNG\r\n\x1a\n"):
                raise ValueError
            position, chunks, seen_data = 8, 0, False
            while position < len(decoded):
                size, kind = struct.unpack_from(">I4s", decoded, position)
                end = position + 12 + size
                if end > len(decoded):
                    raise ValueError
                content = decoded[position + 8:position + 8 + size]
                checksum = struct.unpack_from(">I", decoded, position + 8 + size)[0]
                if zlib.crc32(kind + content) & 0xffffffff != checksum:
                    raise ValueError
                if chunks == 0:
                    if kind != b"IHDR" or size != 13:
                        raise ValueError
                    width, height = struct.unpack_from(">II", content)
                    if not 1 <= width <= 2048 or not 1 <= height <= 2048:
                        raise ValueError
                elif kind == b"IHDR":
                    raise ValueError
                if kind == b"IDAT":
                    seen_data = True
                if kind == b"IEND":
                    if size != 0 or end != len(decoded) or not seen_data:
                        raise ValueError
                    return decoded
                chunks += 1
                position = end
            raise ValueError
        except (ValueError, binascii.Error, struct.error):
            raise XHSError("登录服务返回的二维码无效。") from None

    def logout(self) -> None:
        self._request("DELETE", "/api/v1/login/cookies")


__all__ = ["XHSClient", "XHSError", "normalize_results", "validate_token"]
