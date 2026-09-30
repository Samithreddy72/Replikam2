"""Presenter emails direct one-time codes to the app, without consuming admin-panel links."""
import os,pathlib,tempfile,sys,unittest,threading
from unittest.mock import patch
ROOT=pathlib.Path(__file__).resolve().parents[1]
TMP=tempfile.TemporaryDirectory(prefix='nb-email-')
os.environ.update(DATABASE_URL='sqlite:///'+TMP.name+'/fleet.db',BOOTSTRAP_TOKENS='factory:one',PAYLOAD_DIR=TMP.name+'/payloads',TS_API_KEY='',SMTP_HOST='',ALERT_WEBHOOK_URL='',PUBLIC_BASE_URL='https://fleet.example.com')
sys.path.insert(0,str(ROOT/'control-plane/backend'))
from app import main,notifier
from app.db import SessionLocal
from app.models import User
from fastapi.testclient import TestClient
class Email(unittest.TestCase):
 def test_role_specific_destination_and_private_unknown_account(self):
  with TestClient(main.app) as client:
   with SessionLocal() as db:
    db.add_all([User(email='presenter@example.com',role='presenter',org_id='one'),User(email='admin@example.com',role='admin',org_id='one')]);db.commit()
   for role in ('presenter','admin'):
    delivered=threading.Event();messages=[]
    def send(*args):messages.append(args);delivered.set();return True
    with patch.object(notifier,'send_mail',side_effect=send):
     r=client.post('/auth/magic-link',json={'email':role+'@example.com'})
     self.assertEqual(r.status_code,200);self.assertTrue(delivered.wait(3))
    body=messages[0][2]
    self.assertNotIn('/?code=',body);self.assertIn('six-digit',body);self.assertIn('Mac or Windows',body)
   unknown=client.post('/auth/magic-link',json={'email':'unknown@example.com'})
   self.assertEqual(unknown.json(),r.json())
if __name__=='__main__':unittest.main()
