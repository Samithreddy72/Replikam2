"""An offline fleet cannot serialize startup or overwrite a returned presence result."""
import copy, pathlib, sys, threading, time, unittest
from unittest.mock import patch
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]/'app/netbridge-source'))
import source_app as app

class BridgeListLatency(unittest.TestCase):
    def test_many_unresponsive_bridges_share_one_deadline(self):
        rows = [{'id':str(i),'ip':'192.0.2.%d'%(i+1),'online':False} for i in range(100)]
        release = threading.Event()
        calls = []
        def probe(*args, **kwargs):
            calls.append((args[1],kwargs['timeout']))
            release.wait(2)
            return {'ok':True}
        with patch.object(app,'api',side_effect=probe):
            try:
                start = time.monotonic()
                app._bridge_list_presence(rows, preferred='99', budget=.05)
                self.assertLess(time.monotonic()-start,.5)
                self.assertLessEqual(len(calls),8)
                self.assertEqual(calls[0][0],'http://192.0.2.100:8080/api/status')
                self.assertTrue(all(timeout==.5 for _,timeout in calls))
                self.assertTrue(all(not row['online'] for row in rows))
            finally:
                release.set()
        time.sleep(.03)
        self.assertTrue(all(not row['online'] for row in rows), 'late workers changed returned rows')

    def test_reachable_stale_bridge_is_still_recognized(self):
        rows=[{'id':'one','ip':'192.0.2.1','tailscale_ip':'100.64.0.1','online':False},
              {'id':'two','ip':'192.0.2.2','online':True}]
        with patch.object(app,'api',return_value={'ok':True}) as probe:
            app._bridge_list_presence(rows)
        self.assertTrue(rows[0]['online']); self.assertEqual(rows[0]['online_via'],'probe')
        self.assertEqual(probe.call_count,1)
        self.assertNotIn('online_via',rows[1])

    def test_invalid_addresses_never_become_dns_lookups(self):
        rows=[{'id':'10000000e61fada0','ip':'room.invalid','tailscale_ip':None,'online':False}]
        with patch.object(app,'api',side_effect=AssertionError('must not resolve names')) as probe:
            app._bridge_list_presence(rows)
        probe.assert_not_called(); self.assertFalse(rows[0]['online'])

    def test_failed_probe_preserves_fleet_status_and_order(self):
        rows=[{'id':'a','ip':'192.0.2.1','tailscale_ip':'192.0.2.1','online':False},
              {'id':'b','ip':'192.0.2.2','online':False}]
        before=copy.deepcopy(rows)
        with patch.object(app,'api',return_value={'_error':'unreachable'}) as probe:
            app._bridge_list_presence(rows,preferred='b')
        self.assertEqual(rows,before); self.assertEqual(probe.call_count,2)

if __name__=='__main__': unittest.main()
