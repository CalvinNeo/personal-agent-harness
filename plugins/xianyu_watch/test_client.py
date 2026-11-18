import base64,importlib.util,io,json,os,sys,tempfile,unittest,urllib.error,urllib.request
from pathlib import Path
from unittest.mock import patch
from PIL import Image
spec=importlib.util.spec_from_file_location('xclient',Path(__file__).with_name('client.py'));c=importlib.util.module_from_spec(spec);spec.loader.exec_module(c)
class Response:
 def __init__(self,value,status=200):self.raw=json.dumps(value).encode();self.status=status
 def __enter__(self):return self
 def __exit__(self,*args):pass
 def read(self,n):return self.raw[:n]
class Tests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.token=Path(self.temp.name)/'token';self.token.write_text('a'*32);self.token.chmod(0o600);self.calls=[]
 def tearDown(self):self.temp.cleanup()
 def client(self,value):
  def opener(req,timeout):self.calls.append((req,timeout));return Response(value)
  return c.XianyuClient(endpoint='http://127.0.0.1:18070',token_file=self.token,opener=opener)
 def test_init_no_shadow_and_endpoint_validation(self):
  self.assertEqual(self.client({}).endpoint,'http://127.0.0.1:18070')
  for url in ['http://127.0.0.1.evil:18070','http://127.0.0.1:18070@evil','http://127.0.0.1:18070/path','http://127.0.0.1:18070?x','https://evil.com','http://' + '.'.join(('10','0','0','1')) + ':80']:
   with self.assertRaises(c.XianyuError,msg=url):c.XianyuClient(endpoint=url)
 def test_token_descriptor_closed_on_invalid_mode(self):
  self.token.chmod(0o644)
  original=os.open;fds=[]
  def opening(*args,**kwargs):fd=original(*args,**kwargs);fds.append(fd);return fd
  with patch.object(c.os,'open',opening):
   with self.assertRaises(c.XianyuError):c.read_token(self.token)
  for fd in fds:
   with self.assertRaises(OSError):os.fstat(fd)
 def test_token_symlink_rejected(self):
  p=self.token.with_name('link');p.symlink_to(self.token)
  with self.assertRaises(c.XianyuError):c.read_token(p)
 def test_default_opener_disables_proxy_and_redirect(self):
  obj=c.XianyuClient(token_file=self.token); owner=obj._opener.__self__
  self.assertFalse(any(isinstance(h,urllib.request.ProxyHandler) and h.proxies for h in owner.handlers))
  redirect=next(h for h in owner.handlers if isinstance(h,c.NoRedirect))
  self.assertIsNone(redirect.redirect_request(None,None,302,'',{},'https://evil.com'))
 def test_fixed_operations_and_payload_validation(self):
  obj=self.client({})
  for method,path,payload in [('POST','/api/v1/login/status',{}),('GET','/other',None),('POST','/api/v1/search',{'keyword':'x','limit':True}),('POST','/api/v1/image',{'url':'https://evil.com/a.jpg'})]:
   with self.assertRaises(c.XianyuError):obj._request(method,path,payload)
  self.assertFalse(self.calls)
 def test_status_exact_bool(self):
  self.assertTrue(self.client({'logged_in':True}).status()['logged_in'])
  with self.assertRaises(c.XianyuError):self.client({'logged_in':'yes'}).status()
 def test_search_normalizes_canonical_and_image(self):
  obj=self.client({'ok':True,'results':[{'item_id':'123','title':'item','price':'88','url':'https://evil.com','image_url':'https://img.alicdn.com/a.jpg'}],'sort':'newest'})
  result=obj.search('camera');self.assertEqual(result['results'][0]['url'],'https://www.goofish.com/item?id=123');self.assertEqual(result['sort'],'newest')
 def test_search_failure_code_sanitized(self):
  self.assertEqual(self.client({'ok':False,'error':'secret-url'}).search('x'),{'ok':False,'error':'service_error'})
 def test_secret_echo_rejected(self):
  with self.assertRaises(c.XianyuError):self.client({'logged_in':True,'extra':'a'*32}).status()
 def test_qr_and_image_validation(self):
  stream=io.BytesIO();Image.new('RGB',(8,8)).save(stream,format='PNG');data=stream.getvalue();encoded=base64.b64encode(data).decode()
  self.assertEqual(self.client({'ok':True,'png_base64':encoded}).qrcode(),data)
  self.assertEqual(self.client({'ok':True,'image_base64':encoded}).download_image('https://img.alicdn.com/a.png'),data)
  self.assertIsNone(self.client({'ok':True,'image_base64':'abc'}).download_image('https://img.alicdn.com/a.png'))
  self.assertIsNone(self.client({}).download_image('https://evil.com/a.png'))
 def test_redirect_http_error_not_followed(self):
  def opening(req,timeout):raise urllib.error.HTTPError(req.full_url,302,'redirect',{},None)
  obj=c.XianyuClient(token_file=self.token,opener=opening)
  with self.assertRaises(c.XianyuError) as ctx:obj.status()
  self.assertEqual(ctx.exception.code,'service_error')
if __name__=='__main__':unittest.main(verbosity=2)
