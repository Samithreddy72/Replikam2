#!/usr/bin/env python3
"""Fast health measurements must not accelerate automatic media restarts."""
import importlib.util
import pathlib
import unittest
from unittest.mock import Mock, patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('source_app', ROOT / 'app/netbridge-source/source_app.py')
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)

class HealthRefresh(unittest.TestCase):
    def setUp(self):
        self.watch = app.BridgeWatch()
        self.session = Mock(wanted=True, live=True, voice_muted=False)
        self.patch = patch.object(app, 'SESSION', self.session)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.watch._fetch = Mock(return_value={'video_arriving': {'ok': False}})
        self.watch._power = Mock(return_value={})
        self.watch._apply_fleet_tuning = Mock()
        self.watch._repair = Mock(return_value='repaired')

    def test_startup_publishes_checks_without_repairs(self):
        with patch.object(app.time, 'time', return_value=100):
            self.watch._tick()
            snapshot = self.watch.snapshot()
        self.assertEqual(snapshot['checks'], {'video_arriving': {'ok': False}})
        self.assertTrue(snapshot['reachable'])
        self.assertEqual(snapshot['age_s'], 0)
        self.watch._repair.assert_not_called()
        self.watch._power.assert_not_called()
        self.assertEqual(self.watch.strikes, {})

    def test_more_frequent_measurements_do_not_accelerate_repairs(self):
        self.watch.live_since = 1
        with patch.object(app.time, 'time', return_value=100):
            for timestamp in (100, 105):
                with patch.object(app.time, 'monotonic', return_value=timestamp):
                    self.watch._tick()
            self.assertEqual(self.watch._fetch.call_count, 2)
            self.watch._repair.assert_not_called()
            with patch.object(app.time, 'monotonic', return_value=110):
                self.watch._tick()
        self.watch._repair.assert_called_once()
        self.assertEqual(self.watch.repairs['video_arriving'], 1)

    def test_unreachable_bridge_does_not_trigger_repairs(self):
        self.watch._fetch.return_value = None
        self.watch._tick()
        self.assertFalse(self.watch.snapshot()['reachable'])
        self.watch._repair.assert_not_called()

    def test_stopped_session_clears_checks(self):
        self.watch.last_checks = {'video_arriving': {'ok': True}}
        self.session.wanted = False
        self.watch._tick()
        self.assertIsNone(self.watch.snapshot()['checks'])
        self.watch._fetch.assert_not_called()

    def test_sampling_time_is_included_in_refresh_interval(self):
        self.watch._tick = Mock()
        self.watch.wake = Mock()
        self.watch.wake.wait.side_effect = StopIteration
        with patch.object(app.time, 'monotonic', side_effect=[10, 12]):
            with self.assertRaises(StopIteration):
                self.watch.run()
        self.watch.wake.wait.assert_called_once_with(3.0)

    def test_go_live_can_wake_waiting_sampler(self):
        self.watch.request_refresh()
        self.assertTrue(self.watch.wake.is_set())

if __name__ == '__main__':
    unittest.main()
