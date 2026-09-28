"""Provisioning transport failures cannot turn an otherwise completed tick into failure."""
import importlib.util
import pathlib
import unittest
from unittest.mock import patch
from http.client import IncompleteRead
spec = importlib.util.spec_from_file_location('agent_provision', pathlib.Path(__file__).resolve().parents[1] / 'pi/scripts/bridge-agent.py')
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)

class Provision(unittest.TestCase):
    def test_expected_network_and_json_errors_are_nonfatal(self):
        for error in (TimeoutError(), ConnectionResetError(), IncompleteRead(b''), ValueError('JSON')):
            with self.subTest(error=type(error).__name__), patch.object(a, 'http', side_effect=error), patch.object(a.subprocess, 'run') as run:
                a.apply_provision('https://fleet.test', 'test-only')
                run.assert_not_called()

    def test_invalid_payloads_do_not_run_commands(self):
        for response in ([], 'bad', {'provision': []}, {'provision': 'bad'}, {'provision': {'tailscale_auth_key': 123}}):
            with self.subTest(response=response), patch.object(a, 'http', return_value=response), patch.object(a, 'syslog'), patch.object(a.subprocess, 'run') as run:
                a.apply_provision('https://fleet.test', 'test-only')
                run.assert_not_called()

    def test_tick_finishes_commands_before_timed_out_provision(self):
        order = []
        def request(method, url, **kwargs):
            order.append(url.rsplit('/', 1)[-1])
            if url.endswith('/provision'): raise TimeoutError()
            if url.endswith('/commands'): return []
            return {}
        with patch.object(a, 'http', side_effect=request), patch.object(a, 'load_conf', return_value={'CONTROL_URL': 'https://fleet.test'}), patch.object(a, 'telemetry', return_value={}), patch.object(a, 'enroll', return_value='test-only'), patch.object(a, 'mark_ok'), patch.object(a, 'collect_results'):
            a.main()
        self.assertLess(order.index('commands'), order.index('provision'))

if __name__ == '__main__': unittest.main()
