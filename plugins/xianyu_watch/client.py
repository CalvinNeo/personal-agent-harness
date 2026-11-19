"""Bounded local Xianyu API client; account credentials never enter chat."""
from __future__ import annotations
import base64
import binascii
import io
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import stat
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_ENDPOINT = "http://127.0.0.1:18070"
DEFAULT_TOKEN = "/etc/xianyu/api-token"
MAX_RESPONSE = 3 * 1024 * 1024
MAX_IMAGE = 2 * 1024 * 1024
MAX_QR = 512 * 1024
OPERATIONS = {("GET", "/api/v1/login/status"), ("GET", "/api/v1/login/qrcode"),
              ("POST", "/api/v1/search"), ("POST", "/api/v1/image")}
ERRORS = {"login_required", "login_pending", "login_expired", "qr_unavailable", "qr_invalid",
          "status_unavailable", "search_unavailable", "search_response_invalid", "verification_required",
          "login_callback_incomplete", "already_logged_in", "qr_expired", "busy", "invalid_query", "request_invalid", "image_unavailable", "image_invalid", "image_not_allowed"}

class XianyuError(ValueError):
    def __init__(self, message="闲鱼服务暂时不可用。", code="service_error"):
        super().__init__(message)
        self.code = code


def read_token(path):
    descriptor = -1
    try:
        descriptor = os.open(Path(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid not in {0, os.geteuid()}
                or stat.S_IMODE(info.st_mode) not in {0o600, 0o640} or info.st_size > 256):
            raise ValueError
        token = os.read(descriptor, 257).decode("ascii").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", token):
            raise ValueError
        return token
    except (OSError, ValueError, UnicodeError):
        raise XianyuError("闲鱼登录服务鉴权配置无效。", "token_invalid") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def validate_endpoint(value):
    if not isinstance(value, str) or len(value) > 2048 or re.search(r"[\s\x00-\x20\x7f\\?#]", value):
        raise XianyuError("闲鱼服务地址无效。", "endpoint_invalid")
    try:
        parts = urllib.parse.urlsplit(value)
        address = ipaddress.ip_address(parts.hostname or "")
        if (parts.scheme != "http" or not address.is_loopback or parts.username is not None
                or parts.password is not None or parts.path not in {"", "/"} or parts.query or parts.fragment
                or not parts.port or not 1 <= parts.port <= 65535):
            raise ValueError
        return urllib.parse.urlunsplit((parts.scheme, parts.netloc, "", "", ""))
    except ValueError:
        raise XianyuError("闲鱼服务地址必须是回环IP的HTTP根地址。", "endpoint_invalid") from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def image_url(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 4096 or re.search(r"[\s\x00-\x1f\x7f\\]", value):
        return None
    try:
        parts = urllib.parse.urlsplit(value)
        host = (parts.hostname or "").lower().rstrip(".")
        if (parts.scheme != "https" or parts.username is not None or parts.password is not None
                or parts.port not in {None, 443} or not (host == "alicdn.com" or host.endswith(".alicdn.com"))
                or parts.fragment):
            return None
        return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))
    except ValueError:
        return None


def decode_image(encoded, maximum, *, qr=False):
    if not isinstance(encoded, str) or len(encoded) > 4 * ((maximum + 2) // 3):
        raise XianyuError("闲鱼图片无效。", "qr_invalid" if qr else "image_invalid")
    try:
        data = base64.b64decode(encoded, validate=True)
        if not data or len(data) > maximum:
            raise ValueError
        from PIL import Image
        with Image.open(io.BytesIO(data)) as picture:
            if picture.format not in ({"PNG"} if qr else {"JPEG", "PNG"}):
                raise ValueError
            width, height = picture.size
            if not 1 <= width <= 8192 or not 1 <= height <= 8192 or width * height > 20_000_000:
                raise ValueError
            picture.verify()
        return data
    except Exception:
        raise XianyuError("闲鱼图片无效。", "qr_invalid" if qr else "image_invalid") from None


class XianyuClient:
    def __init__(self, endpoint=DEFAULT_ENDPOINT, token_file=DEFAULT_TOKEN, timeout=90, *, opener=None):
        self.endpoint = validate_endpoint(endpoint)
        self.token_file = token_file
        try:
            seconds = float(timeout)
            if not math.isfinite(seconds):
                raise ValueError
            self.timeout = max(5.0, min(seconds, 180.0))
        except (TypeError, ValueError, OverflowError):
            raise XianyuError("闲鱼请求超时配置无效。", "request_invalid") from None
        self._opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect()).open

    def _request(self, method, path, payload=None):
        if (method, path) not in OPERATIONS:
            raise XianyuError("闲鱼请求操作无效。", "request_invalid")
        if method == "GET" and payload is not None:
            raise XianyuError("闲鱼请求参数无效。", "request_invalid")
        if path == "/api/v1/search":
            if (not isinstance(payload, dict) or set(payload) != {"keyword", "limit"}
                    or not isinstance(payload["keyword"], str) or not 1 <= len(payload["keyword"].strip()) <= 100
                    or type(payload["limit"]) is not int or not 1 <= payload["limit"] <= 20):
                raise XianyuError("闲鱼关键词或数量无效。", "request_invalid")
        if path == "/api/v1/image" and (not isinstance(payload, dict) or set(payload) != {"url"} or image_url(payload["url"]) is None):
            raise XianyuError("闲鱼图片来源无效。", "request_invalid")
        token = read_token(self.token_file)
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
        request = urllib.request.Request(self.endpoint + path, data=body, method=method,
            headers={"Authorization": "Bearer " + token, "Accept": "application/json", "Content-Type": "application/json"})
        try:
            with self._opener(request, timeout=self.timeout) as response:
                status = response.status
                raw = response.read(MAX_RESPONSE + 1) if 200 <= status < 300 else b""
        except urllib.error.HTTPError as error:
            if error.code in {401, 403}:
                raise XianyuError("闲鱼服务鉴权失败。", "unauthorized") from None
            raise XianyuError("闲鱼服务暂时不可用。", "service_error") from None
        except Exception:
            raise XianyuError("闲鱼服务暂时不可用。", "service_error") from None
        if not 200 <= status < 300 or not isinstance(raw, bytes) or len(raw) > MAX_RESPONSE:
            raise XianyuError("闲鱼服务返回无效响应。", "response_invalid")
        try:
            result = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError, RecursionError):
            raise XianyuError("闲鱼服务返回无效响应。", "response_invalid") from None
        if not isinstance(result, dict):
            raise XianyuError("闲鱼服务返回无效响应。", "response_invalid")
        forms = {token, urllib.parse.quote(token, safe=""), urllib.parse.quote_plus(token)}
        pending = [result]
        while pending:
            value = pending.pop()
            if isinstance(value, str) and any(secret in value for secret in forms):
                raise XianyuError("闲鱼服务返回无效响应。", "response_invalid")
            if isinstance(value, dict):
                pending.extend(value.keys()); pending.extend(value.values())
            elif isinstance(value, list):
                pending.extend(value)
        if result.get("ok") is False:
            return {"ok": False, "error": result.get("error") if result.get("error") in ERRORS else "service_error"}
        return result

    def status(self):
        result = self._request("GET", "/api/v1/login/status")
        if type(result.get("logged_in")) is not bool:
            raise XianyuError("闲鱼登录状态无效。", "response_invalid")
        remaining = result.get("remaining_seconds", result.get("expires_seconds"))
        if type(remaining) is not int or not 0 <= remaining <= 240:
            remaining = None
        return {"logged_in": result["logged_in"], "login_pending": result.get("login_pending") is True,
                "state": str(result.get("state") or "")[:40], "remaining_seconds": remaining,
                "expiry_source": result.get("expiry_source") if result.get("expiry_source") in {"platform", "local_upper_bound"} else "local_upper_bound",
                **({"error": result["error"]} if result.get("error") in ERRORS else {})}

    def qrcode_details(self):
        result = self._request("GET", "/api/v1/login/qrcode")
        if result.get("ok") is not True:
            raise XianyuError("闲鱼二维码暂时不可用。", result.get("error", "qr_unavailable"))
        seconds = result.get("expires_seconds", result.get("remaining_seconds", 240))
        if type(seconds) is not int or not 0 < seconds <= 240:
            raise XianyuError("闲鱼二维码已经过期。", "qr_expired")
        return {"image": decode_image(result.get("png_base64"), MAX_QR, qr=True), "expires_seconds": seconds,
                "expiry_source": result.get("expiry_source") if result.get("expiry_source") in {"platform", "local_upper_bound"} else "local_upper_bound"}

    def qrcode(self):
        return self.qrcode_details()["image"]

    def search(self, keyword, limit=10):
        result = self._request("POST", "/api/v1/search", {"keyword": keyword, "limit": limit})
        if result.get("ok") is not True:
            return result
        if not isinstance(result.get("results"), list) or len(result["results"]) > 80:
            raise XianyuError("闲鱼搜索响应格式无效。", "response_invalid")
        items = []
        seen = set()
        for item in result["results"]:
            if not isinstance(item, dict):
                raise XianyuError("闲鱼搜索响应格式无效。", "response_invalid")
            item_id = item.get("item_id")
            title, price = item.get("title"), item.get("price")
            if (not isinstance(item_id, str) or not re.fullmatch(r"[0-9]{1,32}", item_id)
                    or not isinstance(title, str) or not title.strip() or len(title) > 300
                    or not isinstance(price, str) or not 1 <= len(price) <= 80):
                raise XianyuError("闲鱼商品字段无效。", "response_invalid")
            if item_id in seen:
                continue
            seen.add(item_id)
            items.append({"item_id": item_id, "title": re.sub(r"[\x00-\x1f\x7f]", " ", title).strip(),
                          "price": re.sub(r"[\x00-\x1f\x7f]", " ", price).strip(),
                          "url": "https://h5.m.goofish.com/item?id=" + item_id,
                          "image_url": image_url(item.get("image_url"))})
            if len(items) >= limit:
                break
        return {"ok": True, "keyword": keyword, "results": items,
                **{key: result[key] for key in ("observed_at", "sort", "page", "rows_per_page", "coverage", "has_more") if key in result}}

    def download_image(self, url):
        if image_url(url) is None:
            return None
        try:
            result = self._request("POST", "/api/v1/image", {"url": url})
            if result.get("ok") is not True:
                return None
            return decode_image(result.get("image_base64"), MAX_IMAGE)
        except XianyuError:
            return None


def download_image(url, *, endpoint=DEFAULT_ENDPOINT, token_file=DEFAULT_TOKEN, timeout=30):
    try:
        return XianyuClient(endpoint=endpoint, token_file=token_file, timeout=timeout).download_image(url)
    except XianyuError:
        return None
