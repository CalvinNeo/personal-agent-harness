import asyncio,importlib.util,json,os,tempfile,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
spec=importlib.util.spec_from_file_location('server',Path(__file__).with_name('xianyu_server.py'));s=importlib.util.module_from_spec(spec);spec.loader.exec_module(s)
CALLBACK='https://passport.goofish.com/member/vst.htm?st=synthetic-only'
def confirmed(url=CALLBACK):return {'content':{'success':True,'data':{'qrCodeStatus':'CONFIRMED','resultCode':100,'miniVsts':[url]}}}
def cookie(name='unb',value='123'):return {'name':name,'value':value,'domain':'.goofish.com','path':'/','secure':True,'httpOnly':True,'sameSite':'Lax','expires':-1}
class CallbackPolicy(unittest.IsolatedAsyncioTestCase):
 def test_only_confirmed_response_can_register_exact_first_party_urls(self):
  self.assertEqual(s.confirmed_auth_callbacks(confirmed()),[CALLBACK])
  for value in ['https://evil.test/','http://passport.goofish.com/member/vst.htm','https://www.goofish.com/publish','https://www.goofish.com:8443/login','https://u:p' + '@' + 'passport.goofish.com/member/vst.htm']:
   self.assertEqual(s.confirmed_auth_callbacks(confirmed(value)),[])
  raw=confirmed();raw['content']['data']['qrCodeStatus']='NEW';self.assertEqual(s.confirmed_auth_callbacks(raw),[])
 def test_registered_callback_is_exact_get_only_and_expires(self):
  b=s.Browser();b.register_auth_callbacks(confirmed());request=SimpleNamespace(method='GET',url=CALLBACK)
  self.assertTrue(b.auth_route_allowed(request));self.assertFalse(s.allowed_request(CALLBACK,'GET','image'))
  self.assertFalse(b.auth_route_allowed(SimpleNamespace(method='POST',url=CALLBACK)))
  self.assertFalse(b.auth_route_allowed(SimpleNamespace(method='GET',url=CALLBACK+'different')))
  b.auth_routes[CALLBACK]=time.monotonic()-1;self.assertFalse(b.auth_route_allowed(request))
 async def test_response_grant_arrives_before_callback_is_blocked(self):
  b=s.Browser();b.qr_capture_open=True
  request=SimpleNamespace(method='GET',url=CALLBACK,resource_type='image')
  route=SimpleNamespace(request=request,continue_=AsyncMock(),abort=AsyncMock())
  work=asyncio.create_task(b.route(route));await asyncio.sleep(.01);b.register_auth_callbacks(confirmed());await work
  route.continue_.assert_awaited_once();route.abort.assert_not_awaited()
 async def test_confirmed_without_unb_still_verifies_real_identity(self):
  b=s.Browser();b.qr_confirmed_at=time.monotonic();b.context=SimpleNamespace(cookies=AsyncMock(return_value=[cookie('_m_h5_tk','anonymous')]))
  b.mtop=AsyncMock(return_value={'ret':['SUCCESS'],'data':{'userId':'123'}});b.persist_authenticated_session=AsyncMock()
  self.assertTrue(await b.check_auth());b.mtop.assert_awaited_once();b.persist_authenticated_session.assert_awaited_once()
 async def test_success_with_unknown_identity_schema_is_check_failure(self):
  b=s.Browser();b.qr_confirmed_at=time.monotonic();b.context=SimpleNamespace(cookies=AsyncMock(return_value=[]));b.mtop=AsyncMock(return_value={'ret':['SUCCESS'],'data':{}})
  with self.assertRaises(s.XError) as caught:await b.check_auth()
  self.assertEqual(caught.exception.code,'status_unavailable')
 async def test_confirmed_callback_timeout_not_reported_as_lost_login(self):
  b=s.Browser();b.context=object();b.qr_confirmed_at=time.monotonic();b.qr_capture_open=True;b.pending_until=time.monotonic()-1
  b.expire_qr('local_upper_bound');b.check_auth=AsyncMock(side_effect=s.XError('login_required'))
  result=await b.status();self.assertEqual(result['state'],'authorization_incomplete');self.assertEqual(result['error'],'login_callback_incomplete');self.assertFalse(result['login_pending'])
 async def test_confirmed_login_does_not_reissue_scanned_qr(self):
  b=s.Browser();b.context=object();b.qr_state='confirming_login';b.pending_until=time.monotonic()+90;b.login_task=asyncio.create_task(asyncio.sleep(50))
  try:self.assertEqual((await b.qr())['error'],'login_pending')
  finally:b.login_task.cancel();await asyncio.gather(b.login_task,return_exceptions=True)
class Snapshot(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.profile=Path(self.tmp.name)/'profile';self.profile.mkdir();self.patcher=patch.object(s,'PROFILE',self.profile);self.patcher.start()
 async def asyncTearDown(self):self.patcher.stop();self.tmp.cleanup()
 def test_snapshot_is_private_atomic_and_not_symlink_followed(self):
  s.write_auth_snapshot([cookie()]);p=s.auth_snapshot_path();self.assertEqual(p.stat().st_mode&0o777,0o600);self.assertEqual(s.read_auth_snapshot(),[cookie()])
  p.chmod(0o644);self.assertEqual(s.read_auth_snapshot(),[]);p.unlink();target=Path(self.tmp.name)/'target';target.write_text('do not overwrite');p.symlink_to(target)
  with self.assertRaises(s.XError):s.write_auth_snapshot([cookie()])
  self.assertEqual(target.read_text(),'do not overwrite')
 async def test_restart_restores_snapshot_then_requires_real_api_proof(self):
  s.write_auth_snapshot([cookie()]);b=s.Browser();b.context=SimpleNamespace(cookies=AsyncMock(return_value=[]),add_cookies=AsyncMock())
  await b.restore_authenticated_session();b.context.add_cookies.assert_awaited_once_with([cookie()]);self.assertTrue(b.restored_auth_snapshot)
  b.mtop=AsyncMock(side_effect=s.XError('login_required'))
  with self.assertRaises(s.XError):await b.check_auth()
  self.assertFalse(b.last_login)
 async def test_existing_profile_cookie_is_not_overwritten_by_stale_snapshot(self):
  s.write_auth_snapshot([cookie(value='old')]);b=s.Browser();b.context=SimpleNamespace(cookies=AsyncMock(return_value=[cookie(value='new')]),add_cookies=AsyncMock())
  await b.restore_authenticated_session();b.context.add_cookies.assert_not_awaited()
 async def test_successful_auth_cookie_snapshot_can_be_reloaded(self):
  b=s.Browser();b.context=SimpleNamespace(storage_state=AsyncMock(return_value={'cookies':[cookie()],'origins':[]}))
  await b.persist_authenticated_session();self.assertEqual(s.read_auth_snapshot(),[cookie()]);self.assertIsNone(b.persistence_error)


@unittest.skipUnless(os.environ.get('XIANYU_TEST_BROWSER'), 'Set XIANYU_TEST_BROWSER to run the isolated Chromium bootstrap regression')
class RealChromiumBootstrap(unittest.IsolatedAsyncioTestCase):
 async def test_ready_predicate_does_not_invoke_official_sdk_function(self):
  # Use a real browser and a fully intercepted first-party page. The fixture
  # reproduces the SDK's failure on a no-argument call, without network or login.
  fixture = """<!doctype html><html><body><script>
    window.fixtureCalls = 0;
    window.fixtureInvalidCalls = 0;
    window.lib = {mtop: {request: function(options) {
      window.fixtureCalls += 1;
      if (!options) {
        window.fixtureInvalidCalls += 1;
        throw new Error('SDK requires request options');
      }
      return Promise.resolve({ret: ['SUCCESS'], data: {}});
    }}};
  </script></body></html>"""
  async with s.async_playwright() as pw:
   browser = await pw.chromium.launch(executable_path=os.environ['XIANYU_TEST_BROWSER'], headless=True,
                                     chromium_sandbox=False, args=['--disable-dev-shm-usage', '--disable-gpu'])
   try:
    context = await browser.new_context(service_workers='block')
    async def local_fixture(route):
     if route.request.url == 'https://www.goofish.com/':
      await route.fulfill(status=200, content_type='text/html', body=fixture)
     else:
      await route.abort()
    await context.route('**/*', local_fixture)
    page = await context.new_page()
    b = s.Browser()
    await b.bootstrap(page)
    self.assertEqual(page.url, 'https://www.goofish.com/')
    counters = await page.evaluate('({calls: window.fixtureCalls, invalid: window.fixtureInvalidCalls})')
    self.assertEqual(counters, {'calls': 0, 'invalid': 0})
    # The same function-valued expression used before the fix is itself invoked
    # by Playwright. Prove the fixture catches that original failure mode.
    with self.assertRaises(Exception):
     await page.wait_for_function('window.lib && window.lib.mtop && window.lib.mtop.request', timeout=1000)
    counters = await page.evaluate('({calls: window.fixtureCalls, invalid: window.fixtureInvalidCalls})')
    self.assertEqual(counters, {'calls': 1, 'invalid': 1})
   finally:
    await browser.close()

if __name__=='__main__':unittest.main(verbosity=2)
