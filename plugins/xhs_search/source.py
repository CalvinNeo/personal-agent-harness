from __future__ import annotations

import math
import re
from urllib.parse import parse_qsl, quote, quote_plus, urlencode, urlsplit, urlunsplit

from .core import XHSError


_PRIVATE_KEYS = frozenset({"xsectoken", "cookie", "cookies", "setcookie", "authorization",
                           "accesstoken", "refreshtoken", "apikey", "password", "passwd",
                           "clientsecret", "sessionid", "sessiontoken", "authtoken", "bearertoken",
                           "usertoken", "securitytoken", "credentials"})
_URL = re.compile(r"https?://[^\s\"<>]+")


def _private_key(key):
    return re.sub(r"[^a-z0-9]", "", str(key).lower()) in _PRIVATE_KEYS


def sanitized_source(data: dict, *, private_values=()) -> dict:
    secrets = {value for value in private_values if isinstance(value, str) and value}
    pending = [(data, 0)]
    visited = 0
    while pending:
        value, depth = pending.pop()
        visited += 1
        if depth > 64 or visited > 100000:
            raise XHSError("笔记原始详情超出结构限制，未截断后冒充完整数据。", code="note_invalid")
        if isinstance(value, dict):
            for key, child in value.items():
                if _private_key(key):
                    if isinstance(child, str) and child:
                        secrets.add(child)
                else:
                    pending.append((child, depth + 1))
        elif isinstance(value, list):
            pending.extend((child, depth + 1) for child in value)
        elif isinstance(value, float) and not math.isfinite(value):
            raise XHSError("笔记原始详情包含无效数值。", code="note_invalid")

    if len(secrets) > 256:
        raise XHSError("笔记原始详情包含过多敏感字段，未返回未经脱敏的数据。", code="note_invalid")
    forms = sorted({form for value in secrets for form in (value, quote(value, safe=""), quote_plus(value))},
                   key=len, reverse=True)

    def clean_url(match):
        raw = match.group()
        try:
            parsed = urlsplit(raw)
            if parsed.username is not None or parsed.password is not None:
                return "[已移除含凭据的地址]"
            parameters = parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=128)
            kept = [(key, value) for key, value in parameters if not _private_key(key)]
            if kept != parameters:
                return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(kept), parsed.fragment))
            return raw
        except ValueError:
            return "[无效地址]"

    def clean(value):
        if isinstance(value, dict):
            return {clean(key): clean(child) for key, child in value.items() if not _private_key(key)}
        if isinstance(value, list):
            return [clean(child) for child in value]
        if isinstance(value, str):
            value = _URL.sub(clean_url, value)
            for secret in forms:
                value = value.replace(secret, "[已脱敏]")
            return value
        return value

    return clean(data)
