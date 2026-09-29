import hashlib
import pathlib
import sys
import unittest
from unittest.mock import patch, Mock
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'app/netbridge-source'))
import source_app as app

class Binding(unittest.TestCase):
    def test_binding_replaces_only_ssrc(self):
        argv=['ffmpeg','-g','30','-f','rtp','rtp://127.0.0.1:5000']
        first=app.bind_video_ticket(argv,'first')
        second=app.bind_video_ticket(first,'second')
        self.assertEqual(second.count('-ssrc'),1)
        value=int(second[second.index('-ssrc')+1])
        self.assertEqual(value,(int(hashlib.sha256(b'second').hexdigest()[:8],16)&0x7fffffff)or 1)
        self.assertTrue(0<value<2**31)
        self.assertEqual(second[-1],argv[-1])
        self.assertNotEqual(first,second)
        self.assertNotIn('-ssrc',argv)

    def test_pin_renewal_rebinds_video_only(self):
        with patch.object(app,'bridge_route',return_value={'via':'mesh','base':'http://127.0.0.1:18080'}), \
             patch.object(app,'_bridge_pin_protocol',return_value=(2,{})), \
             patch.object(app,'api',return_value={'ok':True,'ticket':'new-ticket'}), \
             patch.object(app,'SESSION') as session, patch.object(app,'PINS') as pins:
            session.wanted=True
            result=app._unlock_bridge('bridge',{},'1234')
            self.assertTrue(result['ok'])
            pins.set.assert_called_once_with('bridge','new-ticket')
            session.respawn_leg.assert_called_once_with('video')
            session.stop.assert_not_called()
            session.set_return.assert_not_called()

    def test_guard_recovers_even_if_all_processes_have_died(self):
        with patch.object(app,'SESSION') as session:
            session.wanted=True
            session.live=False
            session.leg_status.return_value={'video':False}
            session.voice_backend='ffmpeg'
            guard=app.StreamGuard();guard.GRACE_S=0
            guard._tick()
            session.respawn_leg.assert_called_once_with('video')

    def test_missing_selected_device_never_activates_an_alternative(self):
        session=app.Session();session.wanted=True
        session.capture_names={'video':'Chosen camera','voice':'Chosen microphone'}
        session.leg_argv={'video':['ffmpeg','-i','0:none','rtp://example:5000'],
                          'voice':['ffmpeg','-i','audio=Chosen microphone','rtp://example:5002']}
        with patch.object(app,'IS_WIN',True), patch.object(app,'av_devices',return_value={
                'video':[{'name':'Other camera','index':'Other camera'}],
                'audio':[{'name':'Other microphone','index':'Other microphone'}]}), \
                patch.object(app.subprocess,'Popen') as launch:
            self.assertFalse(session.respawn_leg('video'))
            self.assertFalse(session.respawn_leg('voice'))
            launch.assert_not_called()
            self.assertEqual(session.waiting_for_devices,session.capture_names)

    def test_absent_device_does_not_exhaust_repair_budget(self):
        with patch.object(app,'SESSION') as session:
            session.wanted=True;session.live=False;session.voice_backend='ffmpeg'
            session.leg_status.return_value={'video':False}
            session.respawn_leg.return_value=False
            session.waiting_for_devices={'video':'Chosen camera'}
            guard=app.StreamGuard();guard.GRACE_S=0
            for _ in range(10):guard._tick()
            self.assertEqual(guard.repairs,{})
            self.assertEqual(guard.giving_up,[])

    def test_superseded_presenter_stops_capture_without_touching_new_session(self):
        watch=app.BridgeWatch()
        with patch.object(app,'SESSION') as session,patch.object(app,'PINS') as pins, \
                patch.object(watch,'_fetch',return_value={'pin':{'protocol':2,'locked':False,
                    'session':{'video_epoch':'different-session'}}}),patch.object(watch,'_repair') as repair:
            session.wanted=True;session.live=True;pins.ticket.return_value='old-ticket'
            watch._tick()
            session.stop.assert_called_once();pins.clear.assert_called_once_with(lost='superseded')
            repair.assert_not_called()
            self.assertTrue(session.interruption)

    def test_admin_lock_dict_reason_stops_capture(self):
        watch=app.BridgeWatch()
        with patch.object(app,'SESSION') as session,patch.object(app,'PINS') as pins, \
                patch.object(watch,'_fetch',return_value={'pin':{'protocol':2,'locked':True,
                    'last_end':{'reason':'admin'}}}):
            session.wanted=True;session.live=True;pins.ticket.return_value='old-ticket'
            watch._tick();session.stop.assert_called_once()
            pins.clear.assert_called_once_with(lost='admin')

if __name__=='__main__':unittest.main()
