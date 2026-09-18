#!/usr/bin/env python3
"""Default capture resolves CoreAudio identity in the capture process."""
import pathlib
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'app/netbridge-source'))
from audio_engine import mac_microphone_argv

class MicrophoneSelection(unittest.TestCase):
    def test_default_does_not_pass_parent_process_device_id(self):
        with patch('audio_engine.mac_microphone_device_id', return_value=114) as resolve:
            argv = mac_microphone_argv('gst', 'System default microphone', '127.0.0.1', 5002)
        self.assertIn('device=0', argv)
        resolve.assert_not_called()

    def test_named_input_preserves_explicit_selection(self):
        with patch('audio_engine.mac_microphone_device_id', return_value=42):
            argv = mac_microphone_argv('gst', 'USB microphone', '127.0.0.1', 5002)
        self.assertIn('device=42', argv)

if __name__ == '__main__':
    unittest.main()
