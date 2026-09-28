"""Oversized/unbounded requests must not reach JSON parsing or endpoint effects."""
import asyncio,importlib.util,pathlib,unittest
p=pathlib.Path(__file__).resolve().parents[1]/'control-plane/backend/app/request_limits.py'
s=importlib.util.spec_from_file_location('limits',p);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
class Limits(unittest.IsolatedAsyncioTestCase):
 async def check(self, events, headers=(), path='/', stall=False):
  received=[];sent=[];calls=[]
  async def app(scope,receive,send):
   calls.append(True);received.append(await receive())
   await send({'type':'http.response.start','status':200,'headers':[]})
  async def receive():
   if stall:await asyncio.sleep(1)
   return events.pop(0)
  async def send(event):sent.append(event)
  await m.RequestLimits(app,default_limit=4,upload_limit=8,timeout=.02)({'type':'http','path':path,'headers':headers},receive,send)
  return sent,calls,received
 async def test_declared_oversize_is_rejected_without_reading(self):
  for length in (b'100000000000',b'9'*5000):
   sent,calls,_=await self.check([],[(b'content-length',length)]);self.assertEqual(sent[0]['status'],413);self.assertFalse(calls)
 async def test_chunked_or_dishonest_length_cannot_bypass_limit(self):
  for headers in ([],[(b'content-length',b'1')]):
   sent,calls,_=await self.check([{'type':'http.request','body':b'abc','more_body':True},{'type':'http.request','body':b'de'}],headers)
   self.assertEqual(sent[0]['status'],413);self.assertFalse(calls)
 async def test_valid_multichunk_body_replayed_exactly(self):
  sent,calls,received=await self.check([{'type':'http.request','body':b'ab','more_body':True},{'type':'http.request','body':b'cd'}])
  self.assertEqual(sent[0]['status'],200);self.assertEqual(received[0]['body'],b'abcd')
 async def test_upload_allowance_and_malformed_lengths(self):
  sent,_,_=await self.check([{'type':'http.request','body':b'12345678'}],path='/v1/diagnostics');self.assertEqual(sent[0]['status'],200)
  for lengths in ([b'-1'],[b'abc'],[b'1',b'2']):
   sent,calls,_=await self.check([],[(b'content-length',v) for v in lengths]);self.assertEqual(sent[0]['status'],400);self.assertFalse(calls)
 async def test_stalled_body_times_out_without_calling_app(self):
  sent,calls,_=await self.check([],stall=True);self.assertEqual(sent[0]['status'],408);self.assertFalse(calls)
 async def test_disconnect_cannot_trigger_handler(self):
  sent,calls,_=await self.check([{'type':'http.disconnect'}]);self.assertFalse(sent);self.assertFalse(calls)
if __name__=='__main__':unittest.main()
