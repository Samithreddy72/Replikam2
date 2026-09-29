"""Exercise scheduling, freshness, tenant isolation and expiry through real HTTP routes."""
import os, pathlib, tempfile, sys, unittest, datetime as dt
ROOT=pathlib.Path(__file__).resolve().parents[1]
TMP=tempfile.TemporaryDirectory(prefix='nb-workflow-')
os.environ.update(DATABASE_URL='sqlite:///'+TMP.name+'/fleet.db',BOOTSTRAP_TOKENS='factory:default',PAYLOAD_DIR=TMP.name+'/payloads',TS_API_KEY='',SMTP_HOST='',ALERT_WEBHOOK_URL='')
sys.path.insert(0,str(ROOT/'control-plane/backend'))
from app import main,auth,workflow
from app.models import Device,Command,AlertEvent,ObservedSession,SupportIncident,RecoveryGrant,utcnow
from app.db import SessionLocal
from fastapi.testclient import TestClient
from types import SimpleNamespace
main._start_background = lambda: []  # deterministic fixture; alert-loop behavior is tested separately
class Workflow(unittest.TestCase):
 def setUp(self):
  main.app.dependency_overrides[auth.require_viewer]=lambda:SimpleNamespace(org="default",email="presenter@test",role="presenter")
  main.app.dependency_overrides[auth.require_admin]=lambda:SimpleNamespace(org='default',email='admin@test',role='admin')
  self.client=TestClient(main.app);self.client.__enter__()
  with SessionLocal() as db:
   for cls in (Command,AlertEvent,ObservedSession,SupportIncident,RecoveryGrant,Device): db.query(cls).delete()
   for name,org in [('a','default'),('b','other')]:
    db.add(Device(id=name,org_id=org,pairing_code=name,claimed_at=utcnow(),last_seen=utcnow(),latest={'udc':'configured','streams':{'video':True}},token_hash=auth.hash_token('device-'+name)))
   db.commit()
 def tearDown(self):self.client.__exit__(None,None,None);main.app.dependency_overrides.clear()
 def test_incomplete_media_telemetry_does_not_report_session_ended(self):
  with SessionLocal() as db:
   d=db.get(Device,'a');now=utcnow()
   workflow.observe_session(db,d,now);db.commit()
   row=db.query(ObservedSession).one()
   for streams in ({'video':False},{'video':False,'voice':None},{'video':'false','voice':False}):
    d.latest={'streams':streams};workflow.observe_session(db,d,now+dt.timedelta(seconds=1));db.commit()
    self.assertIsNone(row.ended_at)
   d.latest={'streams':{'video':False,'voice':False}}
   workflow.observe_session(db,d,now+dt.timedelta(seconds=2));db.commit()
   self.assertIsNotNone(row.ended_at)
 def test_busy_action_waits_then_delivers_once(self):
  c=self.client
  self.assertEqual(c.post('/admin/devices/a/commands',json={'type':'reboot','confirm':True}).status_code,409)
  r=c.post('/admin/devices/a/commands',json={'type':'reboot','confirm':True,'when':'idle'});self.assertEqual(r.status_code,200,r.text)
  hdr={'Authorization':'Bearer device-a'}
  self.assertEqual(c.get('/v1/commands',headers=hdr).json(),[])
  with SessionLocal() as db:
   d=db.get(Device,'a');d.latest={'udc':'not attached','streams':{'video':False,'voice':False}};db.commit()
  rows=c.get('/v1/commands',headers=hdr).json();self.assertEqual(len(rows),1)
  self.assertEqual(c.get('/v1/commands',headers=hdr).json(),[])
 def test_incomplete_stream_state_never_releases_disruptive_work(self):
  c=self.client
  for streams in ({},{'video':False},{'video':False,'voice':None},{'video':'false','voice':False}):
   with SessionLocal() as db:
    d=db.get(Device,'a');d.latest={'udc':'not attached','streams':streams};db.commit()
   self.assertEqual(c.post('/admin/devices/a/commands',json={'type':'reboot','confirm':True}).status_code,409)
 def test_rollout_guard_and_old_power_advice_are_normalized_without_rewriting_evidence(self):
  raw={'live':True,'ok':False,'summary':'raise the return buffer; enough to be audible','rate':{'pct':100.0,'samples':500}}
  with SessionLocal() as db:
   d=db.get(Device,'a');d.latest={'udc':'suspended','streams':{'video':False,'voice':False},'power':raw};db.commit()
   self.assertIsNotNone(main._busy_reason(d))
   view=main._device_view(d,{})
   self.assertTrue(view['laptop']);self.assertIn('500 samples',view['latest']['power']['summary'])
   self.assertNotIn('return buffer',view['latest']['power']['summary'])
   self.assertEqual(d.latest['power']['summary'],raw['summary'])
 def test_stale_usb_configuration_is_uncertain_and_still_blocks_disruption(self):
  for state in ('configured','suspended'):
   with SessionLocal() as db:
    d=db.get(Device,'a');d.latest={'udc':state,'streams':{'video':False,'voice':False}};db.commit()
   report=self.client.get('/admin/devices/a/health').json()
   self.assertTrue(report['activity']['busy']);self.assertTrue(report['activity']['unknown'])
   self.assertFalse(report['activity']['attachment_verified'])
   self.assertIn('unverified',report['activity']['reason'])
   usb=next(row for row in report['checks'] if row['key']=='usb')
   self.assertEqual(usb['status'],'unknown');self.assertIn('unplugging',usb['detail'])
   self.assertEqual(self.client.post('/admin/devices/a/commands',json={'type':'reboot','confirm':True}).status_code,409)
 def test_cancel_and_expire_waiting(self):
  c=self.client;r=c.post('/admin/devices/a/commands',json={'type':'reboot','confirm':True,'when':'idle'}).json()
  self.assertEqual(c.delete('/admin/devices/a/commands/'+str(r['id'])).status_code,200)
  r=c.post('/admin/devices/a/commands',json={'type':'reboot','confirm':True,'when':'idle'}).json()
  with SessionLocal() as db:
   row=db.get(Command,r['id']);row.policy={'expires_at':(utcnow()-dt.timedelta(seconds=1)).isoformat()};db.commit()
  rows=c.get('/admin/devices/a/commands').json();self.assertEqual(rows[0]['status'],'expired')
 def test_maintenance_notes_scope_and_unknown_health(self):
  c=self.client
  self.assertEqual(c.post('/admin/devices/b/operations',json={'site':'room'}).status_code,404)
  self.assertEqual(c.get('/admin/devices/b/health').status_code,404)
  r=c.post('/admin/devices/a/operations',json={'site':'Room A','notes':'USB port left','maintenance_minutes':30,'reason':'Scheduled work'})
  self.assertEqual(r.status_code,200,r.text);self.assertEqual(r.json()['site'],'Room A')
  report=c.get('/admin/devices/a/health').json()
  self.assertFalse(report['receiver_picture_verified']);self.assertNotEqual(report['status'],'ready')
  self.assertTrue(any(row['key']=='power' and row['status']=='unknown' for row in report['checks']))
  self.assertEqual(len(c.get('/admin/readiness').json()['devices']),1)
  self.assertEqual(c.post('/admin/devices/a/operations',json={'maintenance_minutes':0}).status_code,200)
 def test_sessions_timeline_and_alert_ownership(self):
  c=self.client;hdr={'Authorization':'Bearer device-a'}
  c.post('/v1/telemetry',headers=hdr,json={'udc':'configured','streams':{'video':True}})
  c.post('/v1/telemetry',headers=hdr,json={'udc':'configured','streams':{'video':False,'voice':False}})
  rows=c.get('/admin/devices/a/sessions').json();self.assertEqual(len(rows),1);self.assertEqual(rows[0]['status'],'ended')
  self.assertEqual(c.get('/admin/devices/b/sessions').status_code,404)
  with SessionLocal() as db:
   event=AlertEvent(device_id='a',kind='power',detail='power warning');db.add(event);db.commit();eid=event.id
   for i in range(35):db.add(Command(device_id='a',type='logs',status='done'))
   db.commit()
  self.assertEqual(c.post(f'/admin/devices/a/open-alerts/{eid}',json={'action':'ack'}).status_code,200)
  self.assertEqual(c.post(f'/admin/devices/a/open-alerts/{eid}',json={'action':'snooze','hours':1}).status_code,200)
  self.assertEqual(c.post(f'/admin/devices/b/open-alerts/{eid}',json={'action':'ack'}).status_code,404)
  first=c.get('/admin/devices/a/timeline').json();self.assertEqual(len(first['entries']),30)
  second=c.get('/admin/devices/a/timeline',params={'cursor':first['next_cursor']}).json()
  self.assertTrue(second['entries']);self.assertFalse({(r['kind'],r['id']) for r in first['entries']} & {(r['kind'],r['id']) for r in second['entries']})
 def test_owner_recovery_is_one_time_and_preserves_device(self):
  c=self.client
  self.assertEqual(c.post('/admin/devices/a/recovery-grant',json={'confirm':True}).status_code,409)
  with SessionLocal() as db:
   d=db.get(Device,'a');d.latest={'udc':'not attached','streams':{'video':False,'voice':False}};d.name='Room A';db.commit()
  r=c.post('/admin/devices/a/recovery-grant',json={'confirm':True});self.assertEqual(r.status_code,200,r.text)
  token=r.json()['recovery_token']
  body={'device_id':'a','pairing_code':'A','bootstrap_token':'factory','recovery_token':token}
  self.assertEqual(c.post('/v1/enroll',json=dict(body,device_id='b')).status_code,403)
  restored=c.post('/v1/enroll',json=body);self.assertEqual(restored.status_code,200,restored.text)
  self.assertEqual(c.post('/v1/enroll',json=body).status_code,401)
  self.assertEqual(c.post('/v1/telemetry',headers={'Authorization':'Bearer device-a'},json={}).status_code,401)
  with SessionLocal() as db:self.assertEqual(db.get(Device,'a').name,'Room A')
 def test_support_report_is_scoped_and_rejects_secrets(self):
  c=self.client
  self.assertEqual(c.post('/auth/support-reports',json={'device_id':'b'}).status_code,404)
  r=c.post('/auth/support-reports',json={'device_id':'a','report':{'app_version':'1.5.1','platform':'macOS','token':'SECRET','logs':'SECRET','live':True}})
  self.assertEqual(r.status_code,200,r.text)
  rows=c.get('/admin/devices/a/support-reports').json()
  self.assertEqual(rows[0]['id'],r.json()['id']);self.assertNotIn('SECRET',str(rows))
  self.assertEqual(c.get('/admin/devices/b/support-reports').status_code,404)
 def test_health_understands_real_service_pairs_and_unreadable_power(self):
  with SessionLocal() as db:
   d=db.get(Device,'a');d.latest={'services':[['bridge-web','active'],['bridge-feeder-net','failed']],'power':{'ok':None,'summary':'unreadable'}};db.commit()
  checks={r['key']:r for r in self.client.get('/admin/devices/a/health').json()['checks']}
  self.assertEqual(checks['services']['status'],'warn');self.assertIn('bridge-feeder-net',checks['services']['detail'])
  self.assertEqual(checks['power']['status'],'unknown')
 def test_stale_status_never_authorizes_disruption(self):
  with SessionLocal() as db:
   d=db.get(Device,'a');d.last_seen=utcnow()-dt.timedelta(minutes=5);d.latest={'udc':'not attached','streams':{}};db.commit()
  self.assertEqual(self.client.post('/admin/devices/a/commands',json={'type':'reboot','confirm':True}).status_code,409)
if __name__=='__main__':unittest.main()
