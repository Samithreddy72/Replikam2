"""The idempotence fast path must compare whole fields, never address prefixes."""
import importlib.util,pathlib,json,types,unittest,io
from unittest.mock import patch
ROOT=pathlib.Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('bw',ROOT/'pi/scripts/bridge-web.py');bw=importlib.util.module_from_spec(spec);spec.loader.exec_module(bw)
class Peer(unittest.TestCase):
 def request(self,conf,ip='100.88.12.3',port=5004):
  h=object.__new__(bw.H);h.path='/api/set-peer';h.client_address=(ip,1000)
  body=json.dumps({'ip':ip,'port':port}).encode();h.rfile=io.BytesIO(body);h.headers={'Content-Length':str(len(body))};h._session_ok=lambda *a:True
  responses=[];h._send=lambda body,*a,**kw:responses.append(json.loads(body))
  with patch.object(bw,'_audit'),patch.object(bw,'_mesh_or_local',return_value=True),patch.object(bw,'read',return_value=conf),patch.object(bw.subprocess,'run',return_value=types.SimpleNamespace(returncode=0,stdout='',stderr='')) as run:
   h.do_POST();return responses,run.call_count
 def test_prefix_address_is_not_current_peer(self):
  _,calls=self.request('RETURN_DEST_IP=100.88.12.34\nRETURN_DEST_PORT=5004\n');self.assertEqual(calls,1)
 def test_prefix_port_is_not_current_peer(self):
  _,calls=self.request('RETURN_DEST_IP=100.88.12.3\nRETURN_DEST_PORT=50040\n');self.assertEqual(calls,1)
 def test_exact_peer_and_quoted_peer_skip_restart(self):
  for conf in ['RETURN_DEST_IP=100.88.12.3\nRETURN_DEST_PORT=5004\n','RETURN_DEST_IP="100.88.12.3"\nRETURN_DEST_PORT="5004"\n']:
   replies,calls=self.request(conf);self.assertEqual(calls,0);self.assertFalse(replies[-1]['changed'])
 def test_comment_is_not_configuration(self):
  _,calls=self.request('#RETURN_DEST_IP=100.88.12.3\n#RETURN_DEST_PORT=5004\n');self.assertEqual(calls,1)
if __name__=='__main__':unittest.main()
