"""Sign-out releases capture; an uncertain PIN response is never retried blindly."""
import pathlib,sys,unittest,threading,json,urllib.request
from unittest.mock import patch,Mock
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'app/netbridge-source'))
import source_app as a
class Boundaries(unittest.TestCase):
 def test_uncertain_pin_post_is_sent_once_without_mesh_rebuild(self):
  for reply in ({'_error':'timeout'},None):
   with patch.object(a,'bridge_route',return_value={'via':'mesh','base':'http://127.0.0.1:18080'}),patch.object(a,'_bridge_pin_protocol',return_value=(2,{})),patch.object(a,'api',return_value=reply) as api,patch.object(a,'MESH') as mesh,patch.object(a.time,'sleep') as sleep:
    result=a._unlock_bridge('bridge',{},'1234')
    self.assertFalse(result['ok']);self.assertEqual(result['reason'],'unconfirmed');self.assertEqual(result['attempts'],1)
    api.assert_called_once();mesh.stop.assert_not_called();sleep.assert_not_called()
 def test_signout_releases_media_and_clears_identity(self):
  events=[]
  state={'token':'test-token','email':'test@example.invalid','control_url':'https://fleet.invalid'}
  with patch.object(a,'load_state',return_value=state),patch.object(a,'save_state') as save,patch.object(a.SESSION,'stop',side_effect=lambda:events.append('capture stopped')) as stop,patch.object(a,'_end_bridge_session',side_effect=lambda:events.append('session ended')),patch.object(a.MESH,'stop',side_effect=lambda:events.append('mesh stopped')):
   server=a.ThreadingHTTPServer(('127.0.0.1',0),a.Handler)
   threading.Thread(target=server.serve_forever,daemon=True).start()
   try:
    request=urllib.request.Request('http://127.0.0.1:%d/api/signout'%server.server_port,data=b'{}',headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(request) as response:self.assertTrue(json.load(response)['ok'])
    stop.assert_called_once();self.assertEqual(events,['capture stopped','session ended','mesh stopped'])
    self.assertNotIn('token',save.call_args.args[0]);self.assertEqual(save.call_args.args[0]['control_url'],'https://fleet.invalid')
   finally:server.shutdown();server.server_close()
if __name__=='__main__':unittest.main()
