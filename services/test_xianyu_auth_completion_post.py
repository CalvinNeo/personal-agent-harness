import asyncio
import importlib.util
import json
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
spec = importlib.util.spec_from_file_location('server', Path(__file__).with_name('xianyu_server.py'))
s = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s)

TOKEN_URL = 'https://passport.goofish.com/login_token/login.do'
FEED_URL = 'https://h5api.m.goofish.com/h5/mtop.taobao.idlehome.home.webpc.feed/1.0/'
REDPOINT_URL = 'https://h5api.m.goofish.com/h5/mtop.taobao.idlemessage.pc.redpoint.query/1.0/'
def req(url=TOKEN_URL, method='POST', page=None):
    return SimpleNamespace(url=url, method=method, resource_type='xhr', frame=SimpleNamespace(page=page))
def confirmed_browser():
    b = s.Browser()
    b.qr_capture_open = True
    b.apply_qr_lifecycle('query', {'platform_status': 'CONFIRMED'})
    return b

class CompletionPostPolicy(unittest.IsolatedAsyncioTestCase):
    async def test_fixed_official_endpoints_only_after_confirmation(self):
        b = s.Browser()
        b.qr_capture_open = True
        for url in [TOKEN_URL, FEED_URL, REDPOINT_URL]:
            self.assertFalse(s.allowed_request(url, 'POST', 'xhr'))
            self.assertFalse(b.auth_completion_allowed(req(url)))
        b.apply_qr_lifecycle('query', {'platform_status': 'CONFIRMED'})
        self.assertTrue(b.auth_completion_allowed(req(TOKEN_URL)))
        for url in [FEED_URL, REDPOINT_URL]:
            self.assertFalse(b.auth_completion_allowed(req(url)))
        self.assertFalse(b.auth_completion_allowed(req(TOKEN_URL, 'DELETE')))
        b.qr_capture_open = False
        self.assertFalse(b.auth_completion_allowed(req()))

    async def test_completion_grant_rejects_unsafe_url_variants(self):
        b = confirmed_browser()
        for url in [TOKEN_URL.replace('https:', 'http:'), TOKEN_URL.replace('.com/', '.com:8443/'),
                    TOKEN_URL.replace('https://', 'https://u:p@'), TOKEN_URL + '#fragment',
                    TOKEN_URL + '/more', TOKEN_URL.replace('passport.goofish.com', 'passport.goofish.com.evil.test')]:
            self.assertFalse(b.auth_completion_allowed(req(url)), url)

    async def test_completion_grant_expires(self):
        b = confirmed_browser()
        b.qr_confirmed_at = time.monotonic() - s.AUTH_ROUTE_TTL - 1
        self.assertFalse(b.auth_completion_allowed(req()))

    async def test_post_waits_for_delayed_confirmation_capture(self):
        b = s.Browser()
        b.qr_capture_open = True
        r = SimpleNamespace(request=req(), continue_=AsyncMock(), abort=AsyncMock())
        work = asyncio.create_task(b.route(r))
        await asyncio.sleep(.01)
        b.apply_qr_lifecycle('query', {'platform_status': 'CONFIRMED'})
        await work
        r.continue_.assert_awaited_once()
        r.abort.assert_not_awaited()

    async def test_old_page_response_cannot_confirm_new_flow(self):
        b = s.Browser()
        old_page = object()
        b.login_page = old_page
        b.qr_capture_open = True
        release = asyncio.Event()
        async def delayed_body():
            await release.wait()
            return json.dumps({'content': {'success': True, 'data': {'qrCodeStatus': 'CONFIRMED'}}}).encode()
        response = SimpleNamespace(url='https://passport.goofish.com/newlogin/qrcode/query.do',
                                   status=200, frame=SimpleNamespace(page=old_page), body=delayed_body)
        work = asyncio.create_task(b.capture_qr_response(response))
        await asyncio.sleep(.01)
        b.login_page = object()
        release.set()
        await work
        self.assertIsNone(b.qr_confirmed_at)

    async def test_foreign_page_cannot_use_current_completion_grant(self):
        b = confirmed_browser()
        b.login_page = object()
        self.assertFalse(b.auth_completion_allowed(req(page=object())))
        self.assertTrue(b.auth_completion_allowed(req(page=b.login_page)))

    async def test_token_response_grants_callbacks_without_changing_confirmation(self):
        b = confirmed_browser()
        page = object()
        b.login_page = page
        callback = 'https://passport.goofish.com/member/vst.htm?synthetic=only'
        payload = {'content': {'success': True, 'data': {'miniVsts': [callback]}}}
        response = SimpleNamespace(url=TOKEN_URL, status=200, frame=SimpleNamespace(page=page),
                                   request=req(page=page), body=AsyncMock(return_value=json.dumps(payload).encode()))
        await b.capture_qr_response(response)
        self.assertTrue(b.auth_route_allowed(req(callback, 'GET', page)))
        self.assertEqual(b.qr_state, 'confirming_login')
        self.assertFalse(b.last_login)

    async def test_token_response_without_current_confirmation_does_not_grant(self):
        b = s.Browser()
        b.qr_capture_open = True
        page = object()
        b.login_page = page
        callback = 'https://passport.goofish.com/member/vst.htm?synthetic=only'
        payload = {'content': {'success': True, 'data': {'miniVsts': [callback]}}}
        response = SimpleNamespace(url=TOKEN_URL, status=200, frame=SimpleNamespace(page=page),
                                   request=req(page=page), body=AsyncMock(return_value=json.dumps(payload).encode()))
        await b.capture_qr_response(response)
        response.body.assert_not_awaited()
        self.assertFalse(b.auth_route_allowed(req(callback, 'GET', page)))

    async def test_callback_waits_for_new_token_grant_despite_prior_event(self):
        b = confirmed_browser()
        b.auth_routes_changed.set()
        callback = 'https://passport.goofish.com/member/vst.htm?synthetic=only'
        r = SimpleNamespace(request=req(callback, 'GET'), continue_=AsyncMock(), abort=AsyncMock())
        work = asyncio.create_task(b.route(r))
        await asyncio.sleep(.01)
        payload = {'content': {'success': True, 'data': {'miniVsts': [callback]}}}
        b.register_auth_callbacks(payload, confirmed_exchange=True)
        await work
        r.continue_.assert_awaited_once()
        r.abort.assert_not_awaited()

    async def test_token_callback_grants_keep_previous_registered_url(self):
        b = confirmed_browser()
        first = 'https://passport.goofish.com/member/vst.htm?first=synthetic'
        second = 'https://passport.goofish.com/member/vst.htm?second=synthetic'
        b.register_auth_callbacks({'content': {'success': True, 'data': {'qrCodeStatus': 'CONFIRMED', 'miniVsts': [first]}}})
        b.register_auth_callbacks({'content': {'success': True, 'data': {'miniVsts': [second]}}}, confirmed_exchange=True)
        self.assertTrue(b.auth_route_allowed(req(first, 'GET')))
        self.assertTrue(b.auth_route_allowed(req(second, 'GET')))

    async def test_old_qr_generation_response_cannot_confirm_refreshed_code(self):
        b = s.Browser()
        page = object()
        b.login_page = page
        b.qr_capture_open = True
        release = asyncio.Event()
        async def delayed_body():
            await release.wait()
            return b'{"content":{"success":true,"data":{"qrCodeStatus":"CONFIRMED"}}}'
        response = SimpleNamespace(url='https://passport.goofish.com/newlogin/qrcode/query.do',
                                   status=200, frame=SimpleNamespace(page=page), body=delayed_body)
        work = asyncio.create_task(b.capture_qr_response(response))
        await asyncio.sleep(.01)
        b.apply_qr_lifecycle('generate', {'generated': True})
        release.set()
        await work
        self.assertIsNone(b.qr_confirmed_at)

    async def test_unsuccessful_token_response_cannot_register_callbacks(self):
        b = confirmed_browser()
        page = object()
        b.login_page = page
        callback = 'https://passport.goofish.com/member/vst.htm?synthetic=only'
        payload = {'content': {'success': False, 'data': {'miniVsts': [callback]}}}
        response = SimpleNamespace(url=TOKEN_URL, status=200, frame=SimpleNamespace(page=page),
                                   request=req(page=page), body=AsyncMock(return_value=json.dumps(payload).encode()))
        await b.capture_qr_response(response)
        self.assertFalse(b.auth_route_allowed(req(callback, 'GET', page)))

class DiagnosticProbe(unittest.TestCase):
    def test_unknown_response_does_not_imply_identity(self):
        self.assertFalse(s.auth_probe_summary({'data': {}})['identity'])
        summary = s.auth_probe_summary({'ret': ['SUCCESS::synthetic-secret'], 'data': {'userId': '123'}})
        self.assertTrue(summary['identity'])
        self.assertEqual(summary['ret'], ['SUCCESS'])
        self.assertNotIn('synthetic-secret', json.dumps(summary))

    def test_failure_summary_never_raises_or_exposes_payload_values(self):
        for ret in ['FAIL_SYS_SESSION_EXPIRED::过期', 'CLIENT_ERROR', 'RGV587_ERROR::SM']:
            payload = {'ret': [ret], 'data': {'token': 'synthetic-secret-not-visible'}}
            summary = s.auth_probe_summary(payload)
            self.assertFalse(summary.get('identity'))
            self.assertNotIn('synthetic-secret-not-visible', json.dumps(summary))

if __name__ == '__main__':
    unittest.main(verbosity=2)
