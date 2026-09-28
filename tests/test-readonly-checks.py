"""Polling diagnostics must not create or replace the presenter's live mesh."""
import pathlib, sys, types, unittest, threading, json, urllib.request, urllib.error
from unittest.mock import patch, Mock
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]/'app/netbridge-source'))
import source_app as app
class Checks(unittest.TestCase):
    def test_existing_route_is_reused_without_reconfiguration(self):
        mesh=Mock(bridge_id='one');mesh.proc.poll.return_value=None
        mesh._mesh_route.return_value={'via':'mesh','control_host':'127.0.0.1','control_port':18080}
        with patch.object(app,'MESH',mesh),patch.object(app,'_bridge_rec',return_value={'id':'one'}):
            route=app.bridge_route('one',{},read_only=True)
        self.assertEqual(route['base'],'http://127.0.0.1:18080');mesh.route.assert_not_called()
    def test_other_bridge_or_dead_helper_never_replaces_live_route(self):
        for recid, exitcode in [('two',None),('one',1),(None,None)]:
            mesh=Mock(bridge_id='one');mesh.proc.poll.return_value=exitcode
            with patch.object(app,'MESH',mesh),patch.object(app,'_bridge_rec',return_value={'id':recid}):
                result=app.bridge_route('requested',{},read_only=True)
            self.assertEqual(result['via'],'none');mesh.route.assert_not_called();mesh.stop.assert_not_called()
    def test_http_checks_are_read_only_and_cross_site_requests_refused(self):
        with patch.object(app,'load_state',return_value={}),patch.object(app,'bridge_route',return_value={'via':'none','error':'no route'}) as route:
            srv=app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler)
            threading.Thread(target=srv.serve_forever,daemon=True).start()
            try:
                for headers in [{'Origin':'https://untrusted.invalid'}, {'Referer':'https://untrusted.invalid/'}, {'Sec-Fetch-Site':'cross-site'}]:
                    req=urllib.request.Request('http://127.0.0.1:%d/api/checks?host=two'%srv.server_port,headers=headers)
                    with self.assertRaises(urllib.error.HTTPError) as cm:urllib.request.urlopen(req)
                    self.assertEqual(cm.exception.code,403);route.assert_not_called()
                try:urllib.request.urlopen('http://127.0.0.1:%d/api/checks?host=one'%srv.server_port)
                except urllib.error.HTTPError as e:self.assertEqual(e.code,502)
                route.assert_called_once_with('one',{},read_only=True)
            finally:srv.shutdown();srv.server_close()
if __name__=='__main__':unittest.main()
