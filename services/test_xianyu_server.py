import asyncio, copy, importlib.util, json, os, socket, tempfile, time, unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock
spec=importlib.util.spec_from_file_location('xianyu_server',Path(__file__).with_name('xianyu_server.py'))
s=importlib.util.module_from_spec(spec);spec.loader.exec_module(s)

def payload(rows):return {'ret':['SUCCESS::调用成功'],'data':{'resultList':rows,'resultInfo':{'searchResControlFields':{'numFound':65}}}}
def modern(ident='123456789012',title='二手相机',price=None):
 return {'data':{'item':{'main':{'exContent':{'itemId':ident,'title':title,'price':price if price is not None else [{'text':'¥'},{'text':'1999'}],'picUrl':'https://img.alicdn.com/a.jpg'}}}}}

class Normalize(unittest.TestCase):
 def test_real_modern_pc_schema(self):
  result=s.normalize(payload([modern()]))
  self.assertEqual(result,[{'item_id':'123456789012','title':'二手相机','price':'¥1999','url':'https://www.goofish.com/item?id=123456789012','image_url':'https://img.alicdn.com/a.jpg'}])
 def test_legacy_pc_schema(self):
  self.assertEqual(s.normalize(payload([{'data':{'id':'123','title':'手机','price':'300','picUrl':'//img.alicdn.com/a.jpg'}}]))[0]['price'],'300')
 def test_no_generic_recursive_items(self):
  with self.assertRaises(s.XError):s.normalize({'ret':['SUCCESS'],'data':{'random':[{'id':'123','title':'not item','price':'50'}]}})
 def test_duplicates_bad_ids_untrusted_images(self):
  rows=[modern(),modern(),modern('bad'),modern('456','safe')]
  rows[-1]['data']['item']['main']['exContent']['picUrl']='http://127.0.0.1/secret'
  result=s.normalize(payload(rows));self.assertEqual(len(result),2);self.assertEqual(result[-1]['image_url'],'')
 def test_failure_envelopes_never_empty_success(self):
  for code in ('RGV587_ERROR::SM','FAIL_SYS_SESSION_EXPIRED','CLIENT_ERROR','CAPTCHA_REQUIRED'):
   with self.assertRaises(s.XError):s.normalize({'ret':[code],'data':{}})
 def test_success_requires_resultlist_schema(self):
  self.assertEqual(s.normalize(payload([])),[])
  with self.assertRaises(s.XError):s.normalize({'ret':['SUCCESS'],'data':{}})
 def test_auth_requires_actual_identity(self):
  self.assertTrue(s.authenticated_identity({'ret':['SUCCESS'],'data':{'userId':123}}))
  self.assertFalse(s.authenticated_identity({'ret':['SUCCESS'],'data':{}}))
 def test_network_allowlist(self):
  for url in ('http://127.0.0.1/','http://' + '.'.join(('169','254','169','254')) + '/','https://goofish.com.evil.test/','https://user:pass' + '@' + 'www.goofish.com/','https://www.goofish.com:444/'):
   self.assertFalse(s.allowed_request(url,'GET','document'))
  self.assertFalse(s.allowed_request('https://h5api.m.goofish.com/h5/mtop.taobao.idle.publish/1.0/','POST','xhr'))
  self.assertTrue(s.allowed_request('https://h5api.m.goofish.com/h5/'+s.SEARCH_API+'/1.0/','POST','xhr'))
  self.assertFalse(s.allowed_request('https://www.goofish.com/im','GET','document'))
 def test_image_allowlist(self):
  self.assertEqual(s.image_url('https://img.alicdn.com/x.jpg'),'https://img.alicdn.com/x.jpg')
  self.assertEqual(s.image_url('https://img.alicdn.com/x.jpg?token=x'),'')
 def test_actual_legacy_http_product_image_is_upgraded_to_https(self):
  row=modern();row['data']['item']['main']['exContent']['picUrl']='http://img.alicdn.com/bao/uploaded/product.jpg'
  self.assertEqual(s.normalize(payload([row]))[0]['image_url'],'https://img.alicdn.com/bao/uploaded/product.jpg')
  self.assertEqual(s.image_url('http://img.alicdn.com:80/product.jpg'),'https://img.alicdn.com/product.jpg')
  for url in ('http://127.0.0.1/x.jpg','http://img.alicdn.com.evil.test/x.jpg','http://img.alicdn.com:8080/x.jpg','http://user:pw' + '@' + 'img.alicdn.com/x.jpg','http://img.alicdn.com/x.jpg?sign=private'):
   self.assertEqual(s.image_url(url),'')

class Lifecycle(unittest.IsolatedAsyncioTestCase):
 async def test_status_and_qr_reuse_pending_without_navigation(self):
  b=s.Browser();b.context=object();b.login_task=asyncio.create_task(asyncio.sleep(50));b.pending_until=time.monotonic()+100;b.qr_png=b'png'
  b.check_auth=AsyncMock(side_effect=AssertionError('must not navigate/probe pending login'))
  result=await b.status();self.assertTrue(result['login_pending']);self.assertFalse(result['logged_in'])
  qr=await b.qr();self.assertTrue(qr['ok']);self.assertLessEqual(qr['expires_seconds'],100)
  b.login_task.cancel();await asyncio.gather(b.login_task,return_exceptions=True)
 async def test_search_mandatory_auth_before_platform_call(self):
  b=s.Browser();b.context=object();b.check_auth=AsyncMock(side_effect=s.XError('login_required'));b.mtop=AsyncMock()
  with self.assertRaises(s.XError):await b.search('相机',5)
  b.mtop.assert_not_called()
 async def test_latest_sort_and_first_page_scope(self):
  b=s.Browser();b.context=object();b.check_auth=AsyncMock(return_value=True);b.mtop=AsyncMock(return_value=payload([modern()]))
  result=await b.search('相机',5)
  args=b.mtop.await_args.args[2];self.assertEqual(args['sortField'],'create');self.assertEqual(args['sortValue'],'desc');self.assertEqual(args['pageNumber'],1)
  self.assertTrue(result['has_more']);self.assertEqual(result['sort'],'newest');self.assertIn('第一页',result['coverage'])
 async def test_dns_private_and_mixed_addresses_rejected(self):
  loop=asyncio.get_running_loop()
  for ips in (['127.0.0.1'],['.'.join(('169','254','169','254'))],['.'.join(('1','1','1','1')),'.'.join(('10','0','0','1'))]):
   rows=[(socket.AF_INET,socket.SOCK_STREAM,6,'',(ip,443)) for ip in ips]
   with patch.object(loop,'getaddrinfo',AsyncMock(return_value=rows)):
    with self.assertRaises(s.XError):await s.resolve_public('www.goofish.com',443)
 async def test_image_requires_prior_search_registration(self):
  b=s.Browser()
  with self.assertRaises(s.XError):await b.image('https://img.alicdn.com/unseen.jpg')


class QRCanvas(unittest.IsolatedAsyncioTestCase):
 async def test_visible_canvas_uses_native_png_without_screenshot(self):
  import io,base64
  from PIL import Image
  out=io.BytesIO();Image.new('RGB',(176,176),'white').save(out,format='PNG');png=out.getvalue()
  element=MagicMock();element.bounding_box=AsyncMock(return_value={'width':176,'height':176,'x':0,'y':0});element.is_visible=AsyncMock(return_value=True)
  element.evaluate=AsyncMock(return_value='data:image/png;base64,'+base64.b64encode(png).decode())
  element.screenshot=AsyncMock(side_effect=AssertionError('screenshot must not run for a readable QR canvas'))
  locator=MagicMock();locator.count=AsyncMock(return_value=1);locator.nth=MagicMock(return_value=element)
  frame=MagicMock();frame.url='https://passport.goofish.com/mini_login.htm';frame.locator=MagicMock(return_value=locator)
  page=MagicMock();page.frames=[frame]
  b=s.Browser();b.login_page=page
  self.assertEqual(await b.qr_image(),png);element.screenshot.assert_not_called()
 async def test_missing_account_marker_fails_without_mtop_or_navigation(self):
  b=s.Browser();b.context=MagicMock();b.context.cookies=AsyncMock(return_value=[{'name':'_m_h5_tk','value':'anonymous-token'}]);b.mtop=AsyncMock()
  with self.assertRaises(s.XError) as raised:await b.check_auth()
  self.assertEqual(raised.exception.code,'login_required');b.mtop.assert_not_called()



class QRExpiry(unittest.IsolatedAsyncioTestCase):
 def browser(self):
  b=s.Browser();b.context=object();b.login_task=asyncio.create_task(asyncio.sleep(50));b.qr_window_deadline=time.monotonic()+240;b.pending_until=b.qr_window_deadline;b.qr_capture_open=True;b.qr_png=b'png'
  self.addAsyncCleanup(self.stop,b)
  return b
 async def stop(self,b):
  if b.login_task:b.login_task.cancel();await asyncio.gather(b.login_task,return_exceptions=True)
 def test_actual_generate_schema_does_not_treat_t_as_expiry(self):
  raw={'content':{'data':{'t':1800000000000,'codeContent':'never-log-qr','ck':'never-log-ck','resultCode':100,'processFinished':False},'status':0,'success':True}}
  event=s.qr_lifecycle(raw);self.assertEqual(event,{'generated':True})
  self.assertEqual(s.qr_lifecycle({'content':{'success':False,'data':{'qrCodeStatus':'EXPIRED'}}}),{})
 def test_safe_native_query_schema_and_no_credentials(self):
  raw={'content':{'data':{'qrCodeStatus':'EXPIRED','ck':'must-not-escape','codeContent':'secret-qr'},'success':True}}
  event=s.qr_lifecycle(raw);self.assertEqual(event,{'platform_status':'EXPIRED','state':'expired'});self.assertNotIn('secret',json.dumps(event))
 def test_platform_expiry_timestamp_and_explicit_seconds(self):
  now=1800000000
  a=s.qr_lifecycle({'content':{'data':{'expireTime':(now+90)*1000}}},now=now)
  self.assertEqual(a['remaining_seconds'],90);self.assertEqual(a['expiry_source'],'platform')
  a=s.qr_lifecycle({'content':{'data':{'expiresIn':30}}},now=now)
  self.assertEqual(a['remaining_seconds'],30)
  self.assertNotIn('remaining_seconds',s.qr_lifecycle({'content':{'data':{'timeout':3000,'expireTime':120}}},now=now))
 async def test_platform_expired_stops_old_png_reuse_immediately(self):
  b=self.browser();b.apply_qr_lifecycle('query',s.qr_lifecycle({'content':{'data':{'qrCodeStatus':'EXPIRED'}}}))
  self.assertFalse(b.pending());self.assertIsNone(b.qr_png);self.assertEqual(b.login_error,'login_expired')
  b.check_auth=AsyncMock(side_effect=s.XError('login_required'));result=await b.status()
  self.assertEqual(result['state'],'expired');self.assertEqual(result['error'],'login_expired');self.assertEqual(result['remaining_seconds'],0);self.assertEqual(result['expiry_source'],'platform')
 async def test_local_upper_bound_is_not_platform_lifetime(self):
  b=self.browser();result=await b.qr();self.assertEqual(result['expiry_source'],'local_upper_bound');self.assertLessEqual(result['remaining_seconds'],240)
  b.pending_until=time.monotonic()-1;b.check_auth=AsyncMock(side_effect=s.XError('login_required'));result=await b.status()
  self.assertEqual(result['state'],'expired');self.assertEqual(result['expiry_source'],'local_upper_bound');self.assertIsNone(b.qr_png)
 async def test_platform_ttl_shortens_local_window(self):
  b=self.browser();b.apply_qr_lifecycle('query',{'remaining_seconds':60,'expiry_source':'platform'})
  self.assertLessEqual(b.qr_timing()['remaining_seconds'],60);self.assertEqual(b.qr_timing()['expiry_source'],'platform')
 async def test_scanned_is_pending_confirmation_not_login(self):
  b=self.browser();b.apply_qr_lifecycle('query',{'platform_status':'SCANED','state':'waiting_for_confirmation'})
  result=await b.status();self.assertFalse(result['logged_in']);self.assertTrue(result['login_pending']);self.assertEqual(result['state'],'waiting_for_confirmation')
  b.apply_qr_lifecycle('query',{'platform_status':'CONFIRMED','state':'confirming_login'});result=await b.status();self.assertFalse(result['logged_in']);self.assertEqual(result['state'],'confirming_login')
 async def test_telemetry_untrusted_host_path_or_page_ignored(self):
  b=self.browser();b.login_page=object()
  for url,page in [('https://evil.test/newlogin/qrcode/query.do',b.login_page),('https://passport.goofish.com/elsewhere',b.login_page),('https://passport.goofish.com/newlogin/qrcode/query.do',object())]:
   response=MagicMock();response.url=url;response.status=200;response.frame=MagicMock(page=page);response.body=AsyncMock(return_value=json.dumps({'content':{'data':{'qrCodeStatus':'EXPIRED'}}}).encode())
   await b.capture_qr_response(response);self.assertTrue(b.pending());response.body.assert_not_called()
 async def test_real_query_expired_response_applies(self):
  b=self.browser();b.login_page=object();response=MagicMock();response.url='https://passport.goofish.com/newlogin/qrcode/query.do?do-not-log-secret';response.status=200;response.frame=MagicMock(page=b.login_page);response.body=AsyncMock(return_value=b'{"content":{"data":{"qrCodeStatus":"EXPIRED"}}}')
  await b.capture_qr_response(response);self.assertFalse(b.pending());self.assertEqual(b.qr_state,'expired')
 async def test_native_refresh_invalidates_cached_png(self):
  b=self.browser();b.apply_qr_lifecycle('generate',{'platform_status':'NEW','state':'waiting_for_scan'});self.assertIsNone(b.qr_png)
  b.qr_image=AsyncMock(return_value=b'new-png');result=await b.qr();self.assertTrue(result['ok']);self.assertEqual(b.qr_png,b'new-png');self.assertTrue(b.pending())
 async def test_poll_error_reported_without_forcing_new_qr(self):
  b=self.browser();b.login_error='status_unavailable';result=await b.status();self.assertTrue(result['login_pending']);self.assertEqual(result['error'],'status_unavailable');self.assertIsNotNone(b.qr_png)
 async def test_finished_poll_error_is_not_reported_as_logged_out(self):
  b=self.browser();b.login_task.cancel();await asyncio.gather(b.login_task,return_exceptions=True);b.login_error='status_unavailable';b.check_auth=AsyncMock(side_effect=s.XError('login_required'))
  result=await b.status();self.assertEqual(result['state'],'check_failed');self.assertEqual(result['error'],'status_unavailable')

if __name__=='__main__':unittest.main(verbosity=2)
