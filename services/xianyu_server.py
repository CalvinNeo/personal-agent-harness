#!/usr/bin/env python3
"""Loopback-only, authenticated read-only Xianyu search and QR login service."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hmac
import io
import ipaddress
import json
import math
import os
import re
import socket
import stat
import time
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

import aiohttp
from aiohttp import web
from aiohttp.abc import AbstractResolver
from PIL import Image
from playwright.async_api import async_playwright

PROFILE = Path(os.environ.get('XIANYU_PROFILE', '/var/lib/xianyu/data/profile'))
BROWSER = os.environ.get('XIANYU_BROWSER', '/opt/astrbot-douyin-browser/chrome')
PORT = int(os.environ.get('XIANYU_PORT', '18070'))
TOKEN_FILE = Path(os.environ.get('XIANYU_API_TOKEN_FILE', '/etc/xianyu/api-token'))
MAX_BODY = 64 * 1024
MAX_RESULTS = 20
QR_MAX = 512 * 1024
MAX_RESPONSE = 2 * 1024 * 1024
QR_TTL = 240
SEARCH_API = 'mtop.taobao.idlemtopsearch.pc.search'
LOGIN_API = 'mtop.taobao.idlemessage.pc.loginuser.get'
READ_APIS = {SEARCH_API, LOGIN_API, 'mtop.gaia.nodejs.gaia.idle.data.gw.v2.index.get',
             'mtop.idle.web.user.page.nav', 'mtop.taobao.idlemtopsearch.pc.search.shade',
             'mtop.taobao.idlemtopsearch.pc.item.search.activate', 'mtop.taobao.idle.division.all.get'}
DOMAINS = ('goofish.com', 'alicdn.com', 'taobao.com', 'alipay.com', 'aliapp.org', 'mmstat.com', 'aliyun.com')
IMAGE_DOMAIN = 'alicdn.com'


class XError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def token():
    fd = None
    try:
        fd = os.open(TOKEN_FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid not in {0, os.geteuid()}
                or stat.S_IMODE(info.st_mode) not in {0o600, 0o640} or info.st_size > 256):
            raise XError('token_invalid')
        value = os.read(fd, 257).decode('ascii').strip()
        if not re.fullmatch(r'[A-Za-z0-9_-]{16,128}', value):
            raise XError('token_invalid')
        return value
    except XError:
        raise
    except (OSError, UnicodeError):
        raise XError('token_unavailable') from None
    finally:
        if fd is not None:
            os.close(fd)


def auth(request):
    if not hmac.compare_digest(request.headers.get('Authorization', ''), 'Bearer ' + token()):
        raise web.HTTPUnauthorized()


def clean(value, limit=300):
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return ''
    return re.sub(r'[\x00-\x1f\x7f]+', ' ', str(value)).strip()[:limit]


def allowed_host(host):
    host = str(host or '').lower().rstrip('.')
    return bool(host and any(host == domain or host.endswith('.' + domain) for domain in DOMAINS))


def valid_url(value):
    try:
        parts = urlsplit(value)
        if (parts.scheme not in {'https', 'http'} or parts.username or parts.password
                or not allowed_host(parts.hostname) or parts.port not in {None, 80 if parts.scheme == 'http' else 443}
                or re.search(r'[\x00-\x20\x7f\\]', value)):
            raise ValueError
        return parts
    except (ValueError, TypeError):
        raise XError('network_blocked') from None


def allowed_request(url, method, resource_type):
    try:
        parts = valid_url(url)
    except XError:
        return False
    method = method.upper()
    if resource_type in {'websocket', 'eventsource', 'media'} or method not in {'GET', 'HEAD', 'POST', 'OPTIONS'}:
        return False
    path = parts.path.lower()
    host = parts.hostname.lower()
    if '/h5/' in path:
        found = re.match(r'^/h5/([^/]+)/', path)
        return bool(found and found.group(1) in READ_APIS and method in {'GET', 'POST', 'OPTIONS'})
    if host.startswith('passport.') and host.endswith('.goofish.com'):
        # Login state changes happen only through the first-party QR lifecycle.
        return (path.startswith(('/mini_login.htm', '/newlogin/qrcode/', '/newlogin/checklogin.',
                                 '/newlogin/haslogin.', '/newlogin/recommendloginflow.', '/login/'))
                and method in {'GET', 'POST', 'OPTIONS'})
    if host in {'www.goofish.com', 'goofish.com'}:
        return method in {'GET', 'HEAD'} and (path in {'/', '/login', '/search', '/favicon.ico'} or path.startswith('/assets/'))
    # Static login assets, first-party anti-abuse checks and telemetry only.
    return (method in {'GET', 'HEAD'} or method in {'POST', 'OPTIONS'} and
            (host.endswith('.mmstat.com') or host in {'fourier.taobao.com', 'ynuf.alipay.com', 'ynuf.aliapp.org', 'cf.aliyun.com'}))


AUTH_SNAPSHOT_MAX = 512 * 1024
AUTH_ROUTE_TTL = 120
# Exact official post-confirmation exchange. Feed and message APIs remain
# subject to the normal read allowlist, even while a QR login is in progress.
AUTH_COMPLETION_REQUESTS = frozenset({
    ('passport.goofish.com', '/login_token/login.do', 'POST'),
})
AUTH_URL_FIELDS = ('asyncUrls', 'miniVsts', 'miniLogouts', 'redirectUrl',
                   'parentRedirectUrl', 'iframeRedirectUrl', 'replaceUrl')
ACCOUNT_COOKIES = frozenset({'unb', 'cookie17', '_nk_', 'sgcookie', 'lgc'})


def auth_callback_url(value):
    """A QR-confirmed first-party response can grant one exact HTTPS callback."""
    if not isinstance(value, str) or not 1 <= len(value) <= 4096:
        return None
    if value.startswith('//'):
        value = 'https:' + value
    try:
        parts = valid_url(value)
        host = (parts.hostname or '').lower()
        if (parts.scheme != 'https' or parts.fragment
                or not any(host == domain or host.endswith('.' + domain)
                           for domain in ('goofish.com', 'taobao.com', 'alipay.com'))
                or any(segment in parts.path.lower() for segment in
                       ('/publish', '/sendmessage', '/order', '/purchase', '/payment', '/trade/buy'))):
            return None
        return value
    except XError:
        return None


def login_result_callbacks(payload, *, confirmed_exchange=False):
    """Extract exact callback grants from a successful first-party login result."""
    if not isinstance(payload, dict) or payload.get('hasError') is True:
        return []
    content = payload.get('content')
    data = content.get('data') if isinstance(content, dict) else None
    if (not isinstance(content, dict) or content.get('success') is not True
            or not isinstance(data, dict)):
        return []
    if not confirmed_exchange and data.get('qrCodeStatus') not in {'CONFIRMED', 'SUCCESS'}:
        return []
    output = []
    for field in AUTH_URL_FIELDS:
        value = data.get(field)
        values = list(value.values()) if isinstance(value, dict) else value if isinstance(value, list) else [value]
        for raw in values[:32]:
            url = auth_callback_url(raw)
            if url and url not in output:
                output.append(url)
                if len(output) >= 32:
                    return output
    return output


def confirmed_auth_callbacks(payload):
    """Match Havana's real CONFIRMED handler fields; never return them to callers."""
    return login_result_callbacks(payload)


def auth_snapshot_path():
    return PROFILE.parent / (PROFILE.name + '.login-state.json')


def read_auth_snapshot():
    fd = None
    try:
        path = auth_snapshot_path()
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid not in {0, os.geteuid()}
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > AUTH_SNAPSHOT_MAX):
            return []
        raw = os.read(fd, AUTH_SNAPSHOT_MAX + 1)
        data = json.loads(raw)
        rows = data.get('cookies') if isinstance(data, dict) and data.get('version') == 1 else None
        if not isinstance(rows, list) or len(rows) > 500:
            return []
        cookies = []
        for row in rows:
            if (not isinstance(row, dict) or not isinstance(row.get('name'), str)
                    or not isinstance(row.get('value'), str) or len(row['value']) > 16384
                    or not allowed_host(str(row.get('domain', '')).lstrip('.'))):
                return []
            cookies.append(row)
        return cookies
    except (OSError, ValueError, UnicodeError, TypeError):
        return []
    finally:
        if fd is not None:
            os.close(fd)


def write_auth_snapshot(cookies):
    """Commit validated authenticated cookies privately before reporting durable login."""
    path = auth_snapshot_path()
    data = json.dumps({'version': 1, 'saved_at': time.time(), 'cookies': cookies}, ensure_ascii=False).encode()
    if len(data) > AUTH_SNAPSHOT_MAX or path.is_symlink():
        raise XError('login_persistence_unavailable')
    temporary = path.with_name('.' + path.name + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            fd = None
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if fd is not None:
            os.close(fd)
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def image_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        return ''
    if value.startswith('//'):
        value = 'https:' + value
    try:
        parts = valid_url(value)
        host = parts.hostname.lower()
        if parts.scheme == 'http' and (host == IMAGE_DOMAIN or host.endswith('.' + IMAGE_DOMAIN)):
            # Public product results still contain legacy HTTP CDN links. Fetch
            # those images over HTTPS while retaining the same host and path.
            parts = parts._replace(scheme='https', netloc=host)
            value = parts.geturl()
        if (parts.scheme != 'https' or not (host == IMAGE_DOMAIN or host.endswith('.' + IMAGE_DOMAIN))
                or parts.fragment or re.search(r'(?i)(?:token|secret|password|sign|credential)', parts.query)):
            return ''
        return value
    except XError:
        return ''


def envelope(payload):
    if isinstance(payload, str):
        if len(payload) > MAX_RESPONSE:
            raise XError('search_response_invalid')
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError):
            raise XError('search_response_invalid') from None
    if not isinstance(payload, dict):
        raise XError('search_response_invalid')
    ret = payload.get('ret')
    if not isinstance(ret, list) or not ret or not all(isinstance(x, str) for x in ret):
        raise XError('search_response_invalid')
    codes = ' '.join(ret).upper()
    if any(x in codes for x in ('FAIL_SYS_SESSION_EXPIRED', 'FAIL_SYS_USER_VALIDATE', 'FAIL_SYS_LOGIN', 'FAIL_SYS_ILLEGAL_ACCESS', 'RGV587')):
        raise XError('login_required')
    if any(x in codes for x in ('BAXIA', 'RGV', 'CAPTCHA', 'VALIDATE')):
        raise XError('verification_required')
    if not all(x.startswith('SUCCESS') for x in ret):
        raise XError('search_unavailable')
    data = payload.get('data')
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            raise XError('search_response_invalid') from None
    if not isinstance(data, dict):
        raise XError('search_response_invalid')
    return data


def authenticated_identity(payload):
    data = envelope(payload)
    candidates = [data]
    for key in ('user', 'userInfo', 'loginUser', 'loginUserInfo'):
        if isinstance(data.get(key), dict):
            candidates.append(data[key])
    for row in candidates:
        for key in ('userId', 'uid', 'id', 'userid'):
            value = clean(row.get(key), 32)
            if re.fullmatch(r'[1-9][0-9]{0,31}', value):
                return True
    return False


def auth_probe_summary(payload):
    """Return bounded shape diagnostics without response values or credentials."""
    result = {'type': type(payload).__name__, 'identity': False}
    if not isinstance(payload, dict):
        return result
    ret = payload.get('ret')
    # Preserve only code identifiers, never the server's free-form ret message.
    result['ret'] = [str(item).split('::', 1)[0][:80] for item in ret[:4]
                     if isinstance(item, str) and re.fullmatch(r'[A-Za-z0-9_]+(?:::[^\r\n]*)?', item)] if isinstance(ret, list) else []
    data = payload.get('data')
    if isinstance(data, str):
        with contextlib.suppress(ValueError, TypeError):
            data = json.loads(data)
    if isinstance(data, dict):
        result['data_keys'] = sorted(key for key in data if isinstance(key, str)
                                     and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,79}', key))[:64]
    try:
        result['identity'] = authenticated_identity(payload)
    except XError as error:
        result['error'] = error.code
    return result


def price_text(value):
    if isinstance(value, list):
        text = ''.join(clean(x.get('text'), 30) for x in value[:12] if isinstance(x, dict))
        return text[:80] or ''
    if isinstance(value, dict):
        if 'price' in value:
            return price_text(value['price'])
        return clean(value.get('text') or value.get('value'), 80)
    return clean(value, 80)


def normalize(payload, keyword=None):
    """Parse only the two actual pc.search resultList schemas used by Goofish."""
    data = envelope(payload)
    rows = data.get('resultList')
    if not isinstance(rows, list) or len(rows) > 100:
        raise XError('search_response_invalid')
    output, seen = [], set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('data'), dict):
            continue
        source = row['data']
        if isinstance(source.get('item'), dict):
            main = source['item'].get('main')
            source = main.get('exContent') if isinstance(main, dict) else None
            if not isinstance(source, dict):
                continue
            ident = clean(source.get('itemId'), 32)
        else:
            ident = clean(source.get('id') or source.get('itemId'), 32)
        title = clean(source.get('title'), 300)
        price = price_text(source.get('price'))
        if (not re.fullmatch(r'[1-9][0-9]{0,31}', ident) or not title or not price or ident in seen):
            continue
        seen.add(ident)
        output.append({'item_id': ident, 'title': title, 'price': price,
                       'url': 'https://www.goofish.com/item?id=' + ident,
                       'image_url': image_url(source.get('picUrl') or source.get('mainPicUrl'))})
    if rows and not output:
        raise XError('search_response_invalid')
    return output[:MAX_RESULTS]

QR_ENDPOINTS = {'/newlogin/qrcode/generate.do': 'generate', '/newlogin/qrcode/query.do': 'query'}
QR_STATUSES = {'NEW': 'waiting_for_scan', 'INITIAL': 'waiting_for_scan', 'WAITING': 'waiting_for_scan',
               'SCANED': 'waiting_for_confirmation', 'SCANNED': 'waiting_for_confirmation',
               'CONFIRMED': 'confirming_login', 'SUCCESS': 'confirming_login',
               'EXPIRED': 'expired', 'TIMEOUT': 'expired', 'INVALID': 'expired',
               'CANCELED': 'expired', 'CANCELLED': 'expired'}


def qr_lifecycle(payload, *, now=None):
    """Only safe first-party QR lifecycle fields leave this parser; no QR credentials."""
    if not isinstance(payload, dict):
        return {}
    now = time.time() if now is None else now
    content = payload.get('content')
    if isinstance(content, dict) and content.get('success') is False:
        return {}
    nodes = [payload]
    for row in list(nodes):
        for key in ('content', 'data'):
            child = row.get(key)
            if isinstance(child, dict):
                nodes.append(child)
                for nested in ('content', 'data'):
                    if isinstance(child.get(nested), dict):
                        nodes.append(child[nested])
    result = {}
    if isinstance(content, dict):
        generated = content.get('data')
        if (content.get('success') is True and isinstance(generated, dict) and generated.get('resultCode') == 100
                and isinstance(generated.get('codeContent'), str) and generated['codeContent']
                and isinstance(generated.get('ck'), str) and generated['ck']):
            result['generated'] = True
    for row in nodes:
        for key, value in row.items():
            name = re.sub('[^a-z]', '', str(key).lower())
            if name == 'qrcodestatus' and isinstance(value, str):
                platform_status = value.upper()
                if platform_status in QR_STATUSES:
                    result.update(platform_status=platform_status, state=QR_STATUSES[platform_status])
            seconds = None
            if name in {'expiresin', 'expireseconds', 'expiresseconds', 'validseconds', 'qrcodeexpireseconds'}:
                if type(value) in {int, float} or isinstance(value, str) and value.isdecimal():
                    number = float(value)
                    if 0 <= number <= 3600:
                        seconds = number
            elif name in {'expiretime', 'expirytime', 'expiresat', 'expireat', 'qrcodeexpiretime'}:
                if type(value) in {int, float} or isinstance(value, str) and value.isdecimal():
                    number = float(value)
                    epoch = number / 1000 if number >= 10**12 else number
                    if epoch >= 10**9 and -3600 <= epoch - now <= 3600:
                        seconds = max(0, epoch - now)
            if seconds is not None:
                previous = result.get('remaining_seconds')
                result['remaining_seconds'] = seconds if previous is None else min(seconds, previous)
                result['expiry_source'] = 'platform'
    return result


async def resolve_public(host, port):
    if not allowed_host(host) or port not in {80, 443}:
        raise XError('network_blocked')
    try:
        rows = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM), 6)
    except (OSError, TimeoutError):
        raise XError('network_unavailable') from None
    output = []
    for family, socktype, protocol, _, address in rows:
        ip = ipaddress.ip_address(address[0])
        if (not ip.is_global or ip.is_multicast or ip.is_reserved
                or isinstance(ip, ipaddress.IPv6Address) and (ip.ipv4_mapped or ip.sixtofour or ip.teredo)):
            raise XError('network_blocked')
        row = (family, socktype, protocol, address)
        if row not in output:
            output.append(row)
    if not output:
        raise XError('network_unavailable')
    return output[:4]


class PublicResolver(AbstractResolver):
    async def resolve(self, host, port=0, family=socket.AF_UNSPEC):
        rows = await resolve_public(host, port)
        return [{'hostname': host, 'host': address[0], 'port': port, 'family': fam,
                 'proto': proto, 'flags': socket.AI_NUMERICHOST} for fam, _, proto, address in rows]
    async def close(self):
        pass


class PublicEgress:
    """SOCKS5 pins public DNS results; Chrome cannot bypass via local DNS/QUIC."""
    def __init__(self):
        self.server = None
        self.tasks = set()
    async def start(self):
        self.server = await asyncio.start_server(self.accept, '127.0.0.1', 0, limit=65536)
        self.url = 'socks5://127.0.0.1:' + str(self.server.sockets[0].getsockname()[1])
    async def connect(self, host, port):
        for family, _, protocol, address in await resolve_public(host, port):
            conn = socket.socket(family, socket.SOCK_STREAM, protocol)
            conn.setblocking(False)
            try:
                await asyncio.wait_for(asyncio.get_running_loop().sock_connect(conn, address), 5)
                return await asyncio.open_connection(sock=conn)
            except (OSError, TimeoutError):
                conn.close()
            except BaseException:
                conn.close()
                raise
        raise XError('network_unavailable')
    async def relay(self, reader, writer):
        total = 0
        while data := await reader.read(65536):
            total += len(data)
            if total > 32 * 1024 * 1024:
                raise XError('network_limit')
            writer.write(data)
            await writer.drain()
    async def accept(self, reader, writer):
        current = asyncio.current_task()
        self.tasks.add(current)
        upstream = None
        transfers = []
        try:
            if len(self.tasks) > 96:
                return
            async with asyncio.timeout(12):
                version, count = await reader.readexactly(2)
                methods = await reader.readexactly(count)
                if version != 5 or 0 not in methods:
                    return
                writer.write(b'\x05\x00'); await writer.drain()
                version, command, reserved, kind = await reader.readexactly(4)
                if (version, command, reserved, kind) != (5, 1, 0, 3):
                    return
                count = (await reader.readexactly(1))[0]
                host = (await reader.readexactly(count)).decode('ascii')
                port = int.from_bytes(await reader.readexactly(2), 'big')
                remote, upstream = await self.connect(host, port)
                writer.write(b'\x05\x00\x00\x01' + b'\x00' * 6); await writer.drain()
            transfers = [asyncio.create_task(self.relay(reader, upstream)), asyncio.create_task(self.relay(remote, writer))]
            await asyncio.wait(transfers, timeout=120, return_when=asyncio.FIRST_COMPLETED)
        except (XError, OSError, UnicodeError, TimeoutError, asyncio.IncompleteReadError):
            pass
        finally:
            for task in transfers:
                task.cancel()
            await asyncio.gather(*transfers, return_exceptions=True)
            for stream in (writer, upstream):
                if stream is not None:
                    stream.close()
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(stream.wait_closed(), 1)
            self.tasks.discard(current)
    async def close(self):
        if self.server:
            self.server.close(); await self.server.wait_closed()
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class Browser:
    def __init__(self):
        self.pw = self.context = self.page = self.login_page = self.egress = None
        self.lock = asyncio.Lock()
        self.login_task = None
        self.pending_until = 0
        self.qr_png = None
        self.last_login = False
        self.last_auth_at = 0
        self.login_error = None
        self.images = {}
        self.qr_state = 'idle'
        self.qr_platform_status = None
        self.qr_expiry_source = 'local_upper_bound'
        self.qr_generation = 0
        self.qr_png_generation = None
        self.qr_response_tasks = set()
        self.qr_capture_open = False
        self.qr_window_deadline = 0
        self.qr_platform_deadline = None
        self.qr_confirmed_at = None
        self.auth_routes = {}
        self.auth_routes_changed = asyncio.Event()
        self.auth_completion_changed = asyncio.Event()
        self.blocked_auth_requests = []
        self.restored_auth_snapshot = False
        self.persistence_error = None
        self.last_auth_probe = None
    async def start(self):
        if any(p.is_symlink() for p in (PROFILE, *PROFILE.parents)):
            raise XError('browser_unavailable')
        PROFILE.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(PROFILE, 0o700)
        self.egress = PublicEgress(); await self.egress.start()
        self.pw = await async_playwright().start()
        try:
            self.context = await self.pw.chromium.launch_persistent_context(str(PROFILE), executable_path=BROWSER,
                headless=True, chromium_sandbox=False, accept_downloads=False, service_workers='block',
                proxy={'server': self.egress.url}, args=['--disable-dev-shm-usage', '--disable-gpu', '--disable-quic',
                '--proxy-bypass-list=<-loopback>', '--disable-background-networking', '--disable-extensions',
                '--dns-prefetch-disable', '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1',
                '--force-webrtc-ip-handling-policy=disable_non_proxied_udp'])
            await self.context.route('**/*', self.route)
            self.context.on('response', self.qr_response)
            await self.restore_authenticated_session()
            await self.context.route_web_socket('**/*', lambda route: route.close())
            self.page = await self.context.new_page()
            for page in list(self.context.pages):
                if page is not self.page:
                    await page.close()
        except BaseException:
            await self.close()
            raise
    async def ensure(self):
        if self.context is None:
            await self.start()
    async def restore_authenticated_session(self):
        cookies = await self.context.cookies()
        if any(row.get('name') in ACCOUNT_COOKIES and row.get('value') for row in cookies):
            return
        saved = await asyncio.to_thread(read_auth_snapshot)
        if saved:
            await self.context.add_cookies(saved)
            self.restored_auth_snapshot = True

    async def persist_authenticated_session(self):
        try:
            state = await self.context.storage_state()
            cookies = [row for row in state.get('cookies', [])
                       if allowed_host(str(row.get('domain', '')).lstrip('.'))]
            await asyncio.to_thread(write_auth_snapshot, cookies)
            self.persistence_error = None
        except Exception:
            # A storage problem must not be misreported as invalid account credentials.
            self.persistence_error = 'login_persistence_unavailable'

    def auth_route_allowed(self, request):
        return (request.method.upper() == 'GET' and self.auth_routes.get(request.url, 0) > time.monotonic()
                and auth_callback_url(request.url) is not None)

    def register_auth_callbacks(self, payload, *, confirmed_exchange=False):
        urls = login_result_callbacks(payload, confirmed_exchange=confirmed_exchange)
        if urls:
            now = time.monotonic()
            self.auth_routes = {url: deadline for url, deadline in self.auth_routes.items() if deadline > now}
            self.auth_routes.update({url: now + AUTH_ROUTE_TTL for url in urls})
            self.auth_routes_changed.set()

    def auth_completion_request(self, request):
        try:
            parts = valid_url(request.url)
            return (parts.scheme == 'https' and not parts.fragment
                    and ((parts.hostname or '').lower(), parts.path, request.method.upper()) in AUTH_COMPLETION_REQUESTS
                    and request.frame.page is self.login_page)
        except Exception:
            return False

    def auth_completion_allowed(self, request):
        return (self.qr_capture_open and self.qr_confirmed_at is not None
                and 0 <= time.monotonic() - self.qr_confirmed_at <= AUTH_ROUTE_TTL
                and self.auth_completion_request(request))

    async def wait_for_auth_completion(self, request):
        if self.auth_completion_allowed(request):
            return True
        if not self.qr_capture_open or not self.auth_completion_request(request):
            return False
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self.auth_completion_changed.wait(), .9)
        return self.auth_completion_allowed(request)

    async def route(self, route):
        request = route.request
        try:
            allowed = (allowed_request(request.url, request.method, request.resource_type)
                       or self.auth_route_allowed(request))
            if not allowed:
                allowed = await self.wait_for_auth_completion(request)
            if not allowed and self.qr_capture_open and request.method.upper() == 'GET' and auth_callback_url(request.url):
                # The page can run its result handler before response.body() has
                # reached our listener. Discard a stale notification and wait
                # for a new grant, including grants from the token exchange.
                self.auth_routes_changed.clear()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self.auth_routes_changed.wait(), .9)
                allowed = self.auth_route_allowed(request)
            if allowed:
                await route.continue_()
            else:
                if self.qr_capture_open and auth_callback_url(request.url):
                    parts = urlsplit(request.url)
                    self.blocked_auth_requests.append({'host': parts.hostname, 'path': parts.path[:200],
                                                       'method': request.method.upper(), 'at': time.time()})
                    self.blocked_auth_requests = self.blocked_auth_requests[-16:]
                await route.abort()
        except Exception:
            with contextlib.suppress(Exception):
                await route.abort()

    async def bootstrap(self, page, path='/'):
        try:
            if not page.url.startswith('https://www.goofish.com/'):
                await page.goto('https://www.goofish.com' + path, wait_until='domcontentloaded', timeout=25000)
            # Playwright invokes a function returned by the predicate. Return a
            # boolean so it never calls the site's request SDK with null options.
            await page.wait_for_function("Boolean(window.lib && window.lib.mtop && typeof window.lib.mtop.request === 'function')", timeout=20000)
        except Exception:
            raise XError('browser_unavailable') from None
    async def mtop(self, page, api, data):
        if api not in READ_APIS:
            raise XError('request_invalid')
        await self.bootstrap(page)
        js = '''async ({api, data}) => {
          try {
            if (api === 'mtop.taobao.idlemtopsearch.pc.search' && window.__baxiaReady) {
              await Promise.race([window.__baxiaReady, new Promise(resolve => setTimeout(resolve, 3000))]);
            }
            window.lib.mtop.config.mainDomain='goofish.com';
            window.lib.mtop.config.subDomain='m';
            window.lib.mtop.config.H5Request=true;
            return await window.lib.mtop.request({api, v:'1.0', type:'POST', appKey:'34839810', dataType:'json',
              accountSite:'xianyu', sessionOption:'AutoLoginOnly', data});
          } catch (e) {
            if (e && typeof e === 'object' && e.ret) return {ret:e.ret,data:e.data || {}};
            return {ret:['CLIENT_ERROR'], data:{}};
          }
        }'''
        try:
            result = await asyncio.wait_for(page.evaluate(js, {'api': api, 'data': data}), 30)
        except Exception:
            raise XError('status_unavailable' if api == LOGIN_API else 'search_unavailable') from None
        if len(json.dumps(result, ensure_ascii=False)) > MAX_RESPONSE:
            raise XError('search_response_invalid')
        return result
    async def check_auth(self):
        # Anonymous MTop/bootstrap tokens are not authenticated login proof. A
        # missing account marker fails closed without opening a login popup or
        # disturbing an in-progress QR iframe. The marker alone never succeeds.
        cookies = await self.context.cookies()
        marker = any(x.get('name') in ACCOUNT_COOKIES and x.get('value') for x in cookies)
        confirmed = self.qr_confirmed_at is not None and time.monotonic() - self.qr_confirmed_at <= QR_TTL + 90
        if not (marker or confirmed or self.restored_auth_snapshot or self.last_login):
            self.last_login = False
            raise XError('login_required')
        proof = await self.mtop(self.page, LOGIN_API, {})
        self.last_auth_probe = auth_probe_summary(proof)
        if not authenticated_identity(proof):
            self.last_login = False
            raise XError('status_unavailable')
        self.last_login = True
        self.last_auth_at = time.time()
        await self.persist_authenticated_session()
        return True
    def auth_response_kind(self, response):
        try:
            parts = valid_url(response.url)
            if (not self.qr_capture_open or parts.scheme != 'https' or parts.hostname != 'passport.goofish.com'
                    or parts.fragment or response.status != 200 or response.frame.page is not self.login_page):
                return None
            if parts.path in QR_ENDPOINTS:
                return QR_ENDPOINTS[parts.path]
            if parts.path == '/login_token/login.do' and self.auth_completion_allowed(response.request):
                return 'token'
        except Exception:
            pass
        return None

    def qr_response(self, response):
        """Track QR and its exact first-party completion response without credentials."""
        if len(self.qr_response_tasks) >= 4 or self.auth_response_kind(response) is None:
            return
        task = asyncio.create_task(self.capture_qr_response(response))
        self.qr_response_tasks.add(task)
        task.add_done_callback(self.qr_response_tasks.discard)

    async def capture_qr_response(self, response):
        try:
            kind = self.auth_response_kind(response)
            if kind is None:
                return
            page, generation = self.login_page, self.qr_generation
            raw = await response.body()
            if (len(raw) > 256 * 1024 or not self.qr_capture_open
                    or self.login_page is not page or self.qr_generation != generation):
                return
            payload = json.loads(raw)
            if kind == 'token':
                # This path is reachable only for the same page's exact HTTPS
                # POST during its current, confirmed QR exchange.
                if self.auth_completion_allowed(response.request):
                    self.register_auth_callbacks(payload, confirmed_exchange=True)
                return
            event = qr_lifecycle(payload)
            self.register_auth_callbacks(payload)
            self.apply_qr_lifecycle(kind, event)
        except Exception:
            # Invalid QR telemetry must never change an authenticated state.
            return

    def apply_qr_lifecycle(self, operation, event):
        if not isinstance(event, dict) or not event:
            return
        if operation == 'generate' and (event.get('generated') or event.get('platform_status') in QR_STATUSES):
            self.qr_generation += 1
            self.qr_png = None
            self.qr_png_generation = None
            self.qr_platform_deadline = None
            self.qr_state = 'waiting_for_scan'
            self.qr_platform_status = None
            self.login_error = None
            self.qr_confirmed_at = None
            self.auth_routes = {}
            self.auth_routes_changed.clear()
            self.auth_completion_changed.clear()
        seconds = event.get('remaining_seconds')
        if type(seconds) in {int, float} and 0 <= seconds <= 3600:
            deadline = time.monotonic() + seconds
            self.qr_platform_deadline = deadline if self.qr_platform_deadline is None else min(deadline, self.qr_platform_deadline)
        if self.qr_window_deadline:
            self.pending_until = min(self.qr_window_deadline, self.qr_platform_deadline) if self.qr_platform_deadline is not None else self.qr_window_deadline
            self.qr_expiry_source = 'platform' if self.qr_platform_deadline is not None and self.qr_platform_deadline <= self.qr_window_deadline else 'local_upper_bound'
        if event.get('platform_status') in QR_STATUSES:
            self.qr_platform_status = event['platform_status']
            self.qr_state = QR_STATUSES[self.qr_platform_status]
            if self.qr_platform_status in {'CONFIRMED', 'SUCCESS'}:
                self.qr_confirmed_at = time.monotonic()
                self.auth_completion_changed.set()
                self.qr_png = None
                self.qr_png_generation = None
        if self.qr_state == 'expired':
            self.expire_qr('platform')
        else:
            self.refresh_qr_expiry()

    def expire_qr(self, source):
        self.pending_until = 0
        self.qr_png = None
        self.qr_png_generation = None
        self.qr_state = 'authorization_incomplete' if self.qr_confirmed_at is not None else 'expired'
        self.qr_expiry_source = source
        self.login_error = 'login_callback_incomplete' if self.qr_confirmed_at is not None else 'login_expired'
        self.qr_capture_open = False

    def refresh_qr_expiry(self):
        if self.pending_until and time.monotonic() >= self.pending_until:
            self.expire_qr(self.qr_expiry_source)

    def qr_timing(self):
        self.refresh_qr_expiry()
        remaining = max(0, math.ceil(self.pending_until - time.monotonic())) if self.pending_until else 0
        return {'remaining_seconds': remaining, 'expires_seconds': remaining,
                'expiry_source': self.qr_expiry_source}

    def pending(self):
        self.refresh_qr_expiry()
        return bool(self.login_task and not self.login_task.done() and self.pending_until > time.monotonic())

    async def status(self):
        async with self.lock:
            await self.ensure()
            if self.pending():
                return {'logged_in': False, 'login_pending': True,
                        'state': self.qr_state if self.qr_state in {'waiting_for_confirmation', 'confirming_login'} else 'waiting_for_scan',
                        'observed_at': time.time(), **self.qr_timing(),
                        **({'error': self.login_error} if self.login_error == 'status_unavailable' else {})}
            try:
                await self.check_auth()
                self.login_error = None
                self.qr_state = 'logged_in'
                return {'logged_in': True, 'login_pending': False, 'state': 'logged_in', 'observed_at': time.time()}
            except XError as error:
                self.last_login = False
                if self.login_error == 'login_callback_incomplete' and error.code in {'login_required', 'status_unavailable'}:
                    return {'logged_in': False, 'login_pending': False, 'state': 'authorization_incomplete',
                            'observed_at': time.time(), 'error': 'login_callback_incomplete', **self.qr_timing()}
                if self.login_error == 'login_expired' and error.code == 'login_required':
                    return {'logged_in': False, 'login_pending': False, 'state': 'expired',
                            'observed_at': time.time(), 'error': 'login_expired', **self.qr_timing()}
                code = 'login_required' if error.code == 'login_required' and self.login_error != 'status_unavailable' else 'status_unavailable'
                return {'logged_in': False, 'login_pending': False, 'state': 'not_logged_in' if code == 'login_required' else 'check_failed',
                        'observed_at': time.time(), 'error': code}
    async def qr_image(self):
        page = self.login_page
        for frame in page.frames:
            try:
                parts = urlsplit(frame.url)
                if parts.hostname not in {'www.goofish.com', 'passport.goofish.com'}:
                    continue
                for selector in ('.qrcode-img canvas', '.qrcode-img', '.qrcode-image', '.qr-code-img', '#qrcode canvas',
                                 '[class*="qrcode"] canvas', '[class*="qrcode"] img', '[class*="qr-code"] canvas'):
                    locator = frame.locator(selector)
                    for index in range(min(await locator.count(), 5)):
                        element = locator.nth(index)
                        box = await element.bounding_box()
                        if (not await element.is_visible() or not box or not 100 <= box['width'] <= 500
                                or not 100 <= box['height'] <= 500):
                            continue
                        data = None
                        try:
                            encoded = await element.evaluate("node => node.tagName === 'CANVAS' ? node.toDataURL('image/png') : null")
                            prefix = 'data:image/png;base64,'
                            if isinstance(encoded, str) and encoded.startswith(prefix) and len(encoded) <= 4 * QR_MAX // 3 + 100:
                                data = base64.b64decode(encoded[len(prefix):], validate=True)
                        except Exception:
                            pass
                        if data is None:
                            data = await element.screenshot(type='png', timeout=5000, animations='disabled')
                        if not 100 <= len(data) <= QR_MAX:
                            continue
                        with Image.open(io.BytesIO(data)) as image:
                            if image.format != 'PNG' or min(image.size) < 100 or max(image.size) > 600:
                                continue
                        return data
            except Exception:
                continue
        return None
    def qr_result(self):
        timing = self.qr_timing()
        if not self.qr_png or timing['remaining_seconds'] <= 0:
            raise XError('qr_expired')
        return {'ok': True, 'png_base64': base64.b64encode(self.qr_png).decode(),
                'login_pending': True, 'state': self.qr_state, **timing}

    async def qr(self):
        async with self.lock:
            await self.ensure()
            if self.pending():
                if self.qr_state == 'confirming_login':
                    return {'ok': False, 'error': 'login_pending', 'state': 'confirming_login'}
                if not self.qr_png:
                    self.qr_png = await self.qr_image()
                    self.qr_png_generation = self.qr_generation
                return self.qr_result()
            try:
                await self.check_auth()
            except XError as error:
                if error.code != 'login_required':
                    raise XError('status_unavailable') from None
            else:
                self.login_error = None
                self.qr_state = 'logged_in'
                self.pending_until = 0
                self.qr_png = None
                self.qr_png_generation = None
                self.qr_capture_open = False
                return {'ok': False, 'error': 'already_logged_in'}
            if self.login_task and not self.login_task.done():
                self.login_task.cancel()
                await asyncio.gather(self.login_task, return_exceptions=True)
            self.qr_capture_open = False
            if self.login_page and not self.login_page.is_closed():
                await self.login_page.close()
            self.login_page = await self.context.new_page()
            self.qr_window_deadline = time.monotonic() + QR_TTL
            self.pending_until = self.qr_window_deadline
            self.qr_platform_deadline = None
            self.qr_expiry_source = 'local_upper_bound'
            self.qr_png = None
            self.qr_state = 'waiting_for_scan'
            self.qr_platform_status = None
            self.qr_confirmed_at = None
            self.auth_routes = {}
            self.auth_routes_changed.clear()
            self.auth_completion_changed.clear()
            self.blocked_auth_requests = []
            self.login_error = None
            self.qr_capture_open = True
            await self.login_page.goto('https://www.goofish.com/login', wait_until='domcontentloaded', timeout=25000)
            deadline = min(time.monotonic() + 45, self.qr_window_deadline)
            data = None
            while time.monotonic() < deadline:
                self.refresh_qr_expiry()
                if self.qr_state == 'expired':
                    raise XError('qr_expired')
                data = await self.qr_image()
                if data:
                    break
                # The first-party form defaults to SMS in some regional flows.
                for frame in self.login_page.frames:
                    if urlsplit(frame.url).hostname == 'passport.goofish.com':
                        for selector in ('[class*="icon-qrcode"]', '[class*="qrcode-icon"]'):
                            with contextlib.suppress(Exception):
                                locator = frame.locator(selector).first
                                if await locator.is_visible():
                                    await locator.click(timeout=1000)
                await asyncio.sleep(.5)
            if not data:
                self.qr_capture_open = False
                raise XError('qr_unavailable')
            self.qr_png = data
            self.qr_png_generation = self.qr_generation
            self.last_login = False
            self.login_task = asyncio.create_task(self.poll_login())
            return self.qr_result()

    async def poll_login(self):
        try:
            while self.pending_until and time.monotonic() < self.pending_until:
                await asyncio.sleep(3)
                async with self.lock:
                    self.refresh_qr_expiry()
                    if not self.pending_until:
                        return
                    # The QR iframe remains alive while authentication is tested
                    # in a separate page. SCANED/CONFIRMED alone are not login proof.
                    try:
                        await self.check_auth()
                    except XError as error:
                        if error.code != 'login_required':
                            self.login_error = 'status_unavailable'
                        continue
                    self.pending_until = 0
                    self.qr_png = None
                    self.qr_state = 'logged_in'
                    self.login_error = None
                    self.qr_capture_open = False
                    return
            self.refresh_qr_expiry()
        except asyncio.CancelledError:
            raise
        except Exception:
            self.login_error = 'status_unavailable'
            self.qr_state = 'check_failed'

    async def search(self, keyword, limit):
        async with self.lock:
            await self.ensure()
            if self.pending():
                raise XError('login_pending')
            await self.check_auth()
            payload = await self.mtop(self.page, SEARCH_API, {
                'pageNumber': 1, 'keyword': keyword, 'fromFilter': True, 'rowsPerPage': 30,
                'sortValue': 'desc', 'sortField': 'create', 'customDistance': '', 'gps': '',
                'propValueStr': {'searchFilter': ''}, 'customGps': '', 'searchReqFromPage': 'pcSearch',
                'extraFilterValue': {}, 'userPositionJson': {}})
            results = normalize(payload, keyword)[:limit]
            for item in results:
                if item['image_url']:
                    self.images[item['image_url']] = time.monotonic()
            cutoff = time.monotonic() - 86400
            self.images = dict(sorted(((url, at) for url, at in self.images.items() if at >= cutoff), key=lambda x: x[1], reverse=True)[:500])
            data = envelope(payload)
            count = ((data.get('resultInfo') or {}).get('searchResControlFields') or {}).get('numFound')
            has_more = int(count) > 30 if str(count).isdigit() else None
            return {'ok': True, 'keyword': keyword, 'results': results, 'observed_at': time.time(),
                    'sort': 'newest', 'page': 1, 'rows_per_page': 30, 'has_more': has_more,
                    'coverage': '闲鱼新发布排序的第一页，最多30条；受账号和平台返回范围限制，不保证覆盖全部新上架商品。'}
    async def image(self, url):
        async with self.lock:
            if not image_url(url) or url not in self.images or time.monotonic() - self.images[url] > 86400:
                raise XError('image_unavailable')
            resolver = PublicResolver()
            connector = aiohttp.TCPConnector(resolver=resolver, limit=1, use_dns_cache=False)
            async with aiohttp.ClientSession(connector=connector, trust_env=False, cookie_jar=aiohttp.DummyCookieJar(),
                                            timeout=aiohttp.ClientTimeout(total=20, connect=6)) as client:
                async with client.get(url, allow_redirects=False, headers={'Accept-Encoding': 'identity'}) as response:
                    if response.status != 200 or response.headers.get('Content-Encoding', 'identity') not in {'', 'identity'}:
                        raise XError('image_unavailable')
                    chunks, size = [], 0
                    async for chunk in response.content.iter_chunked(65536):
                        size += len(chunk)
                        if size > MAX_RESPONSE:
                            raise XError('image_unavailable')
                        chunks.append(chunk)
                    raw = b''.join(chunks)
            def encode():
                Image.MAX_IMAGE_PIXELS = 20_000_000
                with Image.open(io.BytesIO(raw)) as image:
                    if image.format not in {'PNG', 'JPEG', 'WEBP'} or image.width * image.height > 20_000_000:
                        raise XError('image_unavailable')
                    image.load(); image.thumbnail((1200, 1200)); image = image.convert('RGB')
                    output = io.BytesIO(); image.save(output, format='JPEG', quality=82)
                    content = output.getvalue()
                    if len(content) > 1024 * 1024:
                        raise XError('image_unavailable')
                    return content
            try:
                data = await asyncio.to_thread(encode)
            except Exception:
                raise XError('image_unavailable') from None
            return {'ok': True, 'image_base64': base64.b64encode(data).decode(), 'mime': 'image/jpeg'}
    async def close(self):
        self.qr_capture_open = False
        tasks = list(self.qr_response_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.qr_response_tasks.clear()
        if self.login_task and self.login_task is not asyncio.current_task():
            self.login_task.cancel()
            await asyncio.gather(self.login_task, return_exceptions=True)
        if self.context:
            await self.context.close()
        if self.pw:
            await self.pw.stop()
        if self.egress:
            await self.egress.close()
        self.context = self.pw = self.page = self.login_page = None


browser = Browser()


async def handle(request):
    auth(request)
    try:
        if request.method == 'GET' and request.path == '/api/v1/login/diagnostics':
            return web.json_response({'state': browser.qr_state, 'platform_status': browser.qr_platform_status,
                                      'confirmation_received': browser.qr_confirmed_at is not None,
                                      'registered_callback_count': len(browser.auth_routes),
                                      'blocked_callbacks': browser.blocked_auth_requests,
                                      'persistence_error': browser.persistence_error,
                                      'restored_snapshot': browser.restored_auth_snapshot,
                                      'last_auth_probe': browser.last_auth_probe})
        if request.method == 'GET' and request.path == '/api/v1/login/status':
            return web.json_response(await browser.status())
        if request.method == 'GET' and request.path == '/api/v1/login/qrcode':
            return web.json_response(await browser.qr())
        if request.method == 'POST' and request.path in {'/api/v1/search', '/api/v1/image'}:
            body = await request.json()
            if not isinstance(body, dict):
                raise XError('request_invalid')
            if request.path == '/api/v1/image':
                if set(body) != {'url'} or not isinstance(body['url'], str):
                    raise XError('request_invalid')
                return web.json_response(await browser.image(body['url']))
            keyword, limit = body.get('keyword'), body.get('limit', 10)
            if (set(body) - {'keyword', 'limit'} or not isinstance(keyword, str)
                    or not 1 <= len(keyword.strip()) <= 100 or re.search(r'[\x00-\x1f\x7f]', keyword)
                    or type(limit) is not int or not 1 <= limit <= MAX_RESULTS):
                raise XError('invalid_query')
            return web.json_response(await browser.search(keyword.strip(), limit))
        raise web.HTTPNotFound()
    except XError as error:
        return web.json_response({'ok': False, 'error': error.code})
    except (ValueError, TypeError, json.JSONDecodeError):
        return web.json_response({'ok': False, 'error': 'request_invalid'}, status=400)
    except web.HTTPException:
        raise
    except Exception:
        return web.json_response({'ok': False, 'error': 'service_unavailable'})


async def main():
    app = web.Application(client_max_size=MAX_BODY)
    app.router.add_route('*', '/{tail:.*}', handle)
    async def cleanup(_):
        await browser.close()
    app.on_cleanup.append(cleanup)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', PORT)
    await site.start()
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


if __name__ == '__main__':
    asyncio.run(main())
