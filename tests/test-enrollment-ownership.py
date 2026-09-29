"""Fleet-image credentials allow same-org replacement cards, never cross-org enrollment."""
import os,pathlib,tempfile,sys,unittest
ROOT=pathlib.Path(__file__).resolve().parents[1]
TMP=tempfile.TemporaryDirectory(prefix='nb-enroll-owner-')
os.environ.update(DATABASE_URL='sqlite:///'+TMP.name+'/fleet.db',BOOTSTRAP_TOKENS='factory-a:a,factory-b:b',PAYLOAD_DIR=TMP.name+'/payloads',TS_API_KEY='',SMTP_HOST='',ALERT_WEBHOOK_URL='')
sys.path.insert(0,str(ROOT/'control-plane/backend'))
from app import main as main
from app.db import SessionLocal
from app.models import Device,utcnow
from fastapi.testclient import TestClient
class Ownership(unittest.TestCase):
 def test_automatic_replacement_preserves_org_and_rotates_device_token(self):
  with TestClient(main.app) as c:
   body={'bootstrap_token':'factory-a','device_id':'audit-device-a','pairing_code':'TEST-A','version':'test','hostname':'original'}
   first=c.post('/v1/enroll',json=body);self.assertEqual(first.status_code,200)
   token=first.json()['device_token'];hdr={'Authorization':'Bearer '+token}
   with SessionLocal() as db:
    dev=db.get(Device,'audit-device-a');dev.claimed_at=utcnow();dev.provision={'pin':'TEST-PIN'};db.commit()
   attack=dict(body,hostname='attacker')
   self.assertEqual(c.post('/v1/enroll',json=dict(attack,bootstrap_token='invalid')).status_code,401)
   self.assertEqual(c.post('/v1/enroll',json=dict(attack,bootstrap_token='factory-b')).status_code,403)
   self.assertEqual(c.post('/v1/enroll',json=dict(attack,bootstrap_token='factory-b'),headers=hdr).status_code,403)
   with SessionLocal() as db:
    dev=db.get(Device,'audit-device-a');self.assertEqual(dev.hostname,'original');self.assertEqual(dev.provision,{'pin':'TEST-PIN'})
   self.assertEqual(c.post('/v1/telemetry',json={},headers=hdr).status_code,200)
   rotated=c.post('/v1/enroll',json=body);self.assertEqual(rotated.status_code,200)
   self.assertNotEqual(rotated.json()['device_token'],token)
   self.assertEqual(c.post('/v1/telemetry',json={},headers=hdr).status_code,401)
   newhdr={'Authorization':'Bearer '+rotated.json()['device_token']}
   self.assertEqual(c.post('/v1/telemetry',json={},headers=newhdr).status_code,200)
   # A lost enrollment response can be retried using the same Fleet-image credential.
   body['device_id']='audit-unclaimed'
   self.assertEqual(c.post('/v1/enroll',json=body).status_code,200)
   self.assertEqual(c.post('/v1/enroll',json=body).status_code,200)
if __name__=='__main__':unittest.main()
