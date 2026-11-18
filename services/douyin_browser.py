"""Ephemeral browser transport with DNS-pinned, bounded platform-only requests."""
from __future__ import annotations

import asyncio
import fcntl
import json
import os
import socket
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver
from playwright.async_api import Error as BrowserError, async_playwright

import douyin
from public_web import WebError, public_addresses
from douyin_egress import PublicEgress


# Cross-process browser serialization is intentionally bounded. Keep this
# separate from the page-read timeout so tests and operators can shorten the
# queue wait without changing the anonymous browser's network budget.
BROWSER_LOCK_WAIT = 90


class PublicResolver(AbstractResolver):
    async def resolve(self, host, port=0, family=socket.AF_UNSPEC):
        records = await asyncio.to_thread(public_addresses, host, port)
        return [{"hostname": host, "host": address[0], "port": port, "family": family,
                 "proto": proto, "flags": socket.AI_NUMERICHOST}
                for family, _, proto, address in records]

    async def close(self):
        pass


class Transport:
    def __init__(self, client, video_id=None):
        self.client = client
        self.video_id = video_id
        self.source = None
        self.ready = asyncio.Event()
        self.requests = 0
        self.transferred = 0
        self.failure = None

    async def fetch(self, url, *, limit=douyin.MAX_DETAIL):
        url = douyin.resource_url(url)
        self.requests += 1
        if self.requests > douyin.MAX_REQUESTS or self.transferred >= douyin.MAX_TRANSFER:
            raise WebError("douyin_transfer_limit")
        headers = {"User-Agent": "Mozilla/5.0", "Accept-Encoding": "identity"}
        async with self.client.request("GET", url, headers=headers, allow_redirects=False) as response:
            if response.content_length is not None and response.content_length > limit:
                raise WebError("douyin_response_too_large")
            chunks, size = [], 0
            async for chunk in response.content.iter_chunked(65536):
                size += len(chunk)
                self.transferred += len(chunk)
                if size > limit or self.transferred > douyin.MAX_TRANSFER:
                    raise WebError("douyin_transfer_limit")
                chunks.append(chunk)
            headers = {"Location": response.headers.get("Location", "")}
            if response.status in {301, 302, 303, 307, 308}:
                douyin.resource_url(urljoin(url, response.headers.get("Location", "")))
            return response.status, headers, b"".join(chunks)

    async def resolve_share(self, url):
        for _ in range(5):
            url, video_id = douyin.page_url(url)
            if video_id:
                return url, video_id
            code, headers, _ = await self.fetch(url)
            location = next((value for key, value in headers.items() if key.lower() == "location"), None)
            if code not in {301, 302, 303, 307, 308} or not location:
                raise WebError("douyin_share_link_unavailable")
            url = urljoin(url, location)
        raise WebError("douyin_redirect_limit")

    def capture(self, url, code, body):
        parts = urlsplit(url)
        if (code != 200 or parts.hostname != "www.douyin.com" or parts.path != "/aweme/v1/web/aweme/detail/"
                or parse_qs(parts.query).get("aweme_id") != [self.video_id]):
            return
        if len(body) > douyin.MAX_DETAIL:
            raise WebError("douyin_response_too_large")
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeError, RecursionError):
            return
        source = payload.get("aweme_detail") if isinstance(payload, dict) else None
        if isinstance(source, dict) and str(source.get("aweme_id")) == self.video_id:
            self.source = source
            self.ready.set()

    async def route(self, route):
        request = route.request
        try:
            body = request.post_data_buffer
            if not douyin.allowed_request(request.url, request.method, request.resource_type, body):
                await route.abort()
                return
            self.requests += 1
            if self.requests > douyin.MAX_REQUESTS:
                self.failure = "douyin_transfer_limit"
                await route.abort()
                return
            await route.continue_()
        except BrowserError:
            pass

    async def response(self, response):
        parts = urlsplit(response.url)
        if parts.hostname != "www.douyin.com" or parts.path != "/aweme/v1/web/aweme/detail/":
            return
        try:
            size = (await response.all_headers()).get("content-length", "")
            if size.isdecimal() and int(size) > douyin.MAX_DETAIL:
                raise WebError("douyin_response_too_large")
            self.capture(response.url, response.status, await response.body())
        except WebError as exc:
            self.failure = str(exc)
        except BrowserError:
            pass


async def _read_source(url, video_id):
    executable = Path(douyin.BROWSER)
    if not executable.is_file():
        raise WebError("douyin_browser_not_installed")
    connector = aiohttp.TCPConnector(resolver=PublicResolver(), limit=12, limit_per_host=6, ttl_dns_cache=60)
    async with aiohttp.ClientSession(connector=connector, cookie_jar=aiohttp.DummyCookieJar(), trust_env=False,
                                     timeout=aiohttp.ClientTimeout(total=18, connect=8)) as client:
        transport = Transport(client, video_id)
        if video_id is None:
            url, video_id = await transport.resolve_share(url)
            transport.video_id = video_id
        try:
            with tempfile.TemporaryDirectory(prefix="douyin-", dir="/tmp") as profile:
                async with PublicEgress() as egress, async_playwright() as playwright:
                    context = await playwright.chromium.launch_persistent_context(profile,
                        executable_path=str(executable), headless=True, chromium_sandbox=True,
                        service_workers="block", accept_downloads=False, timeout=15000, proxy={"server": egress.url},
                        env={**os.environ, "TMPDIR": "/tmp", "HOME": "/tmp"},
                        args=["--disable-dev-shm-usage", "--disable-gpu", "--disable-quic", "--proxy-bypass-list=<-loopback>",
                            "--disable-background-networking", "--disable-extensions", "--dns-prefetch-disable",
                            "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1", "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"])
                    try:
                        await context.route("**/*", transport.route)
                        await context.route_web_socket("**/*", lambda route: route.close())
                        page = await context.new_page()
                        page.on("response", transport.response)
                        try:
                            await page.goto(url, wait_until="domcontentloaded", timeout=25000)
                        except BrowserError:
                            pass
                        try:
                            await asyncio.wait_for(transport.ready.wait(), timeout=40)
                        except TimeoutError:
                            try:
                                text = await page.locator("body").inner_text(timeout=2000)
                            except BrowserError:
                                raise WebError(transport.failure or egress.failure or "douyin_page_unavailable") from None
                            raise WebError(transport.failure or egress.failure or douyin.unavailable_error(text)) from None
                        return transport.source, video_id
                    finally:
                        await context.close()
        except BrowserError:
            raise WebError("douyin_browser_unavailable") from None


async def read_source(url, video_id):
    lock = Path(__file__).open("rb")
    deadline = asyncio.get_running_loop().time() + BROWSER_LOCK_WAIT
    try:
        # Different MCP processes can serve concurrent calls for one task. Queue
        # those calls briefly instead of exposing a transient browser_busy
        # result that the model may mistake for a permanent video failure.
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if asyncio.get_running_loop().time() >= deadline:
                    raise WebError("douyin_browser_busy") from None
                await asyncio.sleep(0.25)
        try:
            return await _read_source(url, video_id)
        except (aiohttp.ClientError, OSError):
            raise WebError("douyin_public_site_unavailable") from None
    finally:
        try:
            fcntl.flock(lock, fcntl.LOCK_UN)
        finally:
            lock.close()
