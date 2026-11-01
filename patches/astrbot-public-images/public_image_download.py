import asyncio
import os
from pathlib import Path
import ssl
from urllib.parse import parse_qsl, urljoin, urlsplit

import aiohttp
import certifi


PUBLIC_IMAGE_HOSTS = frozenset({'commons.wikimedia.org', 'upload.wikimedia.org'})
MAX_IMAGE_BYTES = 16 * 1024 * 1024


def is_public_image_url(url):
    try:
        parsed = urlsplit(url)
        tracking = parse_qsl(parsed.query, keep_blank_values=True)
        query_allowed = not parsed.query or (
            parsed.hostname == 'upload.wikimedia.org' and len(tracking) == 3
            and dict(tracking) == {'utm_source': 'commons.wikimedia.org',
                                   'utm_campaign': 'index', 'utm_content': 'original'}
        )
        return (
            isinstance(url, str) and len(url) <= 8192
            and not any(character.isspace() or ord(character) < 32 for character in url)
            and parsed.scheme == 'https' and parsed.hostname in PUBLIC_IMAGE_HOSTS
            and parsed.port in (None, 443) and parsed.username is None and parsed.password is None
            and query_allowed and not parsed.fragment
        )
    except (TypeError, ValueError):
        return False


def image_proxy(url):
    if not is_public_image_url(url):
        return None
    proxy = os.environ.get('ASTRBOT_PUBLIC_IMAGE_PROXY', '').strip()
    if not proxy:
        return None
    try:
        parsed = urlsplit(proxy)
        valid = (parsed.scheme == 'http' and parsed.hostname == '127.0.0.1'
                 and parsed.port == 7890 and parsed.path in ('', '/')
                 and parsed.username is None and parsed.password is None
                 and not parsed.query and not parsed.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('public image proxy must be the existing loopback HTTP proxy')
    return proxy


async def download_public_image(url, path):
    proxy = image_proxy(url)
    if proxy is None:
        return False
    destination = Path(path)
    created = False
    try:
        async with asyncio.timeout(45):
            timeout = aiohttp.ClientTimeout(total=45, connect=10, sock_read=20)
            tls = ssl.create_default_context(cafile=certifi.where())
            async with aiohttp.ClientSession(timeout=timeout, trust_env=False,
                                             cookie_jar=aiohttp.DummyCookieJar()) as session:
                current = url
                for redirect in range(5):
                    if not is_public_image_url(current):
                        raise ValueError('public image redirect left the approved hosts')
                    async with session.get(current, proxy=proxy, ssl=tls, allow_redirects=False,
                                           headers={'User-Agent': 'PersonalAgent/1.0 public image fetch'}) as response:
                        if response.status in {301, 302, 303, 307, 308}:
                            location = response.headers.get('Location')
                            if not location or redirect == 4:
                                raise ValueError('public image redirect limit exceeded')
                            current = urljoin(current, location)
                            continue
                        if response.status != 200:
                            raise RuntimeError(f'public image download returned HTTP {response.status}')
                        if not response.headers.get('Content-Type', '').lower().startswith('image/'):
                            raise ValueError('public image response is not an image')
                        if response.content_length is not None and response.content_length > MAX_IMAGE_BYTES:
                            raise ValueError('public image exceeds the download size limit')
                        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                        created = True
                        total = 0
                        with os.fdopen(descriptor, 'wb') as output:
                            async for chunk in response.content.iter_chunked(65536):
                                total += len(chunk)
                                if total > MAX_IMAGE_BYTES:
                                    raise ValueError('public image exceeds the download size limit')
                                output.write(chunk)
                        if total == 0:
                            raise ValueError('public image response is empty')
                        return True
    except BaseException:
        if created:
            destination.unlink(missing_ok=True)
        raise
    raise ValueError('public image download did not complete')
