import asyncio
import importlib.util
import os
from pathlib import Path
import ssl
import tempfile
import unittest
from unittest.mock import patch


helper = Path(os.environ.get('REPAIR_IMAGE_HELPER', Path(__file__).resolve().parents[1] / 'patches/astrbot-public-images/public_image_download.py'))
spec = importlib.util.spec_from_file_location('public_image_download', helper)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
URL = 'https://commons.wikimedia.org/wiki/Special:FilePath/synthetic.jpg'
PROXY = 'http://127.0.0.1:7890'


class Response:
    def __init__(self, *, status=200, content_type='image/jpeg', location=None, chunks=(b'\xff\xd8\xffsynthetic',), length=None):
        self.status = status
        self.headers = {'Content-Type': content_type}
        if location:
            self.headers['Location'] = location
        self.content = self
        self.chunks = chunks
        self.content_length = length

    async def __aenter__(self):
        return self

    async def __aexit__(self, *arguments):
        return False

    async def iter_chunked(self, limit):
        for chunk in self.chunks:
            yield chunk


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *arguments):
        return False

    def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        return next(self.responses)


class PublicImageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'image.jpg'
        self.environment = patch.dict('os.environ', {'ASTRBOT_PUBLIC_IMAGE_PROXY': PROXY})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_only_exact_public_hosts_without_credentials_or_query_are_eligible(self):
        self.assertEqual(module.image_proxy(URL), PROXY)
        self.assertEqual(module.image_proxy('https://upload.wikimedia.org/synthetic.jpg'), PROXY)
        for url in ('https://multimedia.nt.qq.com.cn/download?fileid=synthetic',
                    'https://www.xiaohongshu.com/', 'http://commons.wikimedia.org/test.jpg',
                    'https://commons.wikimedia.org.example.org/x.jpg',
                    'https://commons.wikimedia.org:8443/x.jpg',
                    'https://user:secret' + '@' + 'commons.wikimedia.org/x.jpg', URL + '?token=synthetic',
                    URL + '#synthetic', URL + '\n', 'https://127.0.0.1/x.jpg'):
            with self.subTest(url=url):
                self.assertIsNone(module.image_proxy(url))

    def test_invalid_proxy_configuration_fails_closed(self):
        for proxy in ('http://example.com:7890', 'http://user:pass@127.0.0.1:7890',
                      'http://127.0.0.1:7891', 'socks5://127.0.0.1:7890', 'http://127.0.0.1:bad'):
            with patch.dict('os.environ', {'ASTRBOT_PUBLIC_IMAGE_PROXY': proxy}):
                with self.assertRaises(ValueError):
                    module.image_proxy(URL)

    def test_only_wikimedia_generated_tracking_query_is_allowed(self):
        image = 'https://upload.wikimedia.org/wikipedia/commons/synthetic.jpg'
        tracking = '?utm_source=commons.wikimedia.org&utm_campaign=index&utm_content=original'
        self.assertEqual(module.image_proxy(image + tracking), PROXY)
        for url in (image + tracking + '&token=synthetic', image + tracking + '&utm_content=other',
                    image + '?utm_source=private', URL + tracking):
            self.assertIsNone(module.image_proxy(url))

    async def test_disabled_and_nonmatching_urls_do_not_start_network(self):
        with patch.object(module.aiohttp, 'ClientSession') as constructor:
            with patch.dict('os.environ', {'ASTRBOT_PUBLIC_IMAGE_PROXY': ''}):
                self.assertFalse(await module.download_public_image(URL, self.path))
            self.assertFalse(await module.download_public_image('https://qq.com/image.jpg', self.path))
        constructor.assert_not_called()

    async def test_approved_redirect_preserves_tls_and_does_not_use_credentials(self):
        session = Session([Response(status=302, location='https://upload.wikimedia.org/synthetic.jpg'), Response()])
        with patch.object(module.aiohttp, 'ClientSession', return_value=session) as constructor:
            self.assertTrue(await module.download_public_image(URL, self.path))
        self.assertEqual(self.path.read_bytes(), b'\xff\xd8\xffsynthetic')
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(constructor.call_args.kwargs['trust_env'])
        self.assertIsInstance(constructor.call_args.kwargs['cookie_jar'], module.aiohttp.DummyCookieJar)
        for url, options in session.requests:
            self.assertEqual(options['proxy'], PROXY)
            self.assertFalse(options['allow_redirects'])
            self.assertEqual(options['ssl'].verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(options['ssl'].check_hostname)
            self.assertNotIn('Authorization', options['headers'])
            self.assertNotIn('Cookie', options['headers'])

    async def test_redirect_outside_allowlist_is_rejected_before_request(self):
        for target in ('https://127.0.0.1/private', 'https://qq.com/attachment?token=synthetic',
                       URL + '?token=synthetic', 'https://user:pass' + '@' + 'upload.wikimedia.org/x.jpg'):
            session = Session([Response(status=302, location=target)])
            with patch.object(module.aiohttp, 'ClientSession', return_value=session):
                with self.assertRaises(ValueError):
                    await module.download_public_image(URL, self.path)
            self.assertEqual(len(session.requests), 1)
            self.assertFalse(self.path.exists())

    async def test_error_status_nonimage_empty_and_excess_size_leave_no_partial_file(self):
        for response in (Response(status=403), Response(content_type='text/html'), Response(chunks=()),
                         Response(length=module.MAX_IMAGE_BYTES + 1), Response(chunks=(b'abcd', b'efgh'))):
            session = Session([response])
            with patch.object(module.aiohttp, 'ClientSession', return_value=session), patch.object(module, 'MAX_IMAGE_BYTES', 5):
                with self.assertRaises((ValueError, RuntimeError)):
                    await module.download_public_image(URL, self.path)
            self.assertFalse(self.path.exists())

    async def test_existing_destination_is_never_overwritten_or_deleted(self):
        self.path.write_bytes(b'keep-existing')
        with patch.object(module.aiohttp, 'ClientSession', return_value=Session([Response()])):
            with self.assertRaises(FileExistsError):
                await module.download_public_image(URL, self.path)
        self.assertEqual(self.path.read_bytes(), b'keep-existing')

    async def test_redirects_are_bounded(self):
        session = Session([Response(status=302, location=URL)] * 5)
        with patch.object(module.aiohttp, 'ClientSession', return_value=session):
            with self.assertRaises(ValueError):
                await module.download_public_image(URL, self.path)
        self.assertEqual(len(session.requests), 5)

    async def test_cancelled_download_removes_partial_file(self):
        class InterruptedResponse(Response):
            async def iter_chunked(self, limit):
                yield b'partial-image'
                raise asyncio.CancelledError()

        session = Session([InterruptedResponse()])
        with patch.object(module.aiohttp, 'ClientSession', return_value=session):
            with self.assertRaises(asyncio.CancelledError):
                await module.download_public_image(URL, self.path)
        self.assertFalse(self.path.exists())

    async def test_rate_limit_is_reported_without_retry(self):
        session = Session([Response(status=429)])
        with patch.object(module.aiohttp, 'ClientSession', return_value=session):
            with self.assertRaisesRegex(RuntimeError, 'HTTP 429'):
                await module.download_public_image(URL, self.path)
        self.assertEqual(len(session.requests), 1)
        self.assertFalse(self.path.exists())


if __name__ == '__main__':
    unittest.main()
