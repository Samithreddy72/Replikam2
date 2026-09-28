"""Exercise bounded bridge HTTP workers and rejected bodies through real sockets."""
import importlib.util,pathlib,threading,socket,time,unittest,http.client
from unittest.mock import patch
p=pathlib.Path(__file__).resolve().parents[1]/'pi/scripts/bridge-web.py'
spec=importlib.util.spec_from_file_location('limited_bridge',p);bw=importlib.util.module_from_spec(spec);spec.loader.exec_module(bw)
class LocalServer(bw.Server):
 address_family=socket.AF_INET
 max_workers=2
 socket_timeout=0.5
class Limits(unittest.TestCase):
 def setUp(self):
  self.srv=LocalServer(('127.0.0.1',0),bw.H)
  threading.Thread(target=self.srv.serve_forever,daemon=True).start()
  self.addCleanup(self.srv.server_close);self.addCleanup(self.srv.shutdown)
 def connect(self):
  c=socket.create_connection(self.srv.server_address,timeout=2);self.addCleanup(c.close);return c
 def test_partial_requests_exhaust_only_bounded_slots_and_timeout(self):
  for _ in range(2):self.connect().sendall(b'GET /api/health HTTP/1.1\r\n')
  until=time.monotonic()+0.3
  while self.srv._slots._value and time.monotonic()<until:time.sleep(0.005)
  self.assertEqual(self.srv._slots._value,0)
  extra=self.connect();extra.sendall(b'GET /api/health HTTP/1.1\r\nHost: localhost\r\n\r\n')
  self.assertIn(b'503 Service Unavailable',extra.recv(512))
  until=time.monotonic()+2
  while self.srv._slots._value != 2 and time.monotonic()<until:time.sleep(0.02)
  self.assertEqual(self.srv._slots._value,2)
  c=http.client.HTTPConnection(*self.srv.server_address,timeout=2);self.addCleanup(c.close)
  c.request('GET','/api/health');self.assertEqual(c.getresponse().status,200)
 def test_invalid_or_large_body_never_reaches_pin_handler(self):
  with patch.object(bw,'_audit'),patch.object(bw,'_pin_run',side_effect=AssertionError('PIN handler reached')):
   for body,headers,expected in [(b'',{'Content-Length':'-1'},413),(b'',{'Content-Length':'16385'},413),(b'[]',{},400),(b'{',{},400),(b'',{'Transfer-Encoding':'chunked'},400)]:
    c=http.client.HTTPConnection(*self.srv.server_address,timeout=2);self.addCleanup(c.close)
    c.request('POST','/api/unlock',body=body,headers=headers)
    self.assertEqual(c.getresponse().status,expected);c.close()
if __name__=='__main__':unittest.main()
