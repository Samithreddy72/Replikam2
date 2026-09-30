"""Email-bound short codes: limited guesses, one use, resend safety and old-code compatibility."""
import os,pathlib,tempfile,sys,unittest,threading,datetime as dt,re
from unittest.mock import patch
ROOT=pathlib.Path(__file__).resolve().parents[1];TMP=tempfile.TemporaryDirectory(prefix='nb-codes-')
os.environ.update(DATABASE_URL='sqlite:///'+TMP.name+'/fleet.db',BOOTSTRAP_TOKENS='test:one',PAYLOAD_DIR=TMP.name+'/payloads',TS_API_KEY='',SMTP_HOST='',ALERT_WEBHOOK_URL='')
sys.path.insert(0,str(ROOT/'control-plane/backend'))
from app import main,notifier,auth
from app.db import SessionLocal
from app.models import User,utcnow
from fastapi.testclient import TestClient
class Codes(unittest.TestCase):
 def setUp(self):
  self.client=TestClient(main.app);self.email=self._testMethodName+'@example.test'
  with SessionLocal() as db:db.add(User(email=self.email,role='presenter',org_id='one'));db.commit()
 def send(self):
  event=threading.Event();messages=[]
  def mail(*args):messages.append(args);event.set();return True
  with patch.object(notifier,'send_mail',side_effect=mail),patch('secrets.randbelow',return_value=1234):
   result=self.client.post('/auth/magic-link',json={'email':self.email})
   event.wait(.2)
  return result,messages
 def redeem(self,code='001234',email=None):return self.client.post('/auth/magic-redeem',json={'code':code,'email':self.email if email is None else email})
 def test_leading_zero_email_bound_and_single_use(self):
  result,messages=self.send();self.assertEqual(result.status_code,200);self.assertIn('001234',messages[0][2]);self.assertNotIn('/?code=',messages[0][2])
  self.assertEqual(self.redeem(email='').status_code,401);self.assertEqual(self.redeem(email='wrong@example.test').status_code,401)
  self.assertEqual(self.redeem().status_code,200);self.assertEqual(self.redeem().status_code,401)
 def test_five_guesses_and_resend_cannot_reset_budget(self):
  self.send()
  for i in range(5):self.assertEqual(self.redeem('999999').status_code,401)
  with SessionLocal() as db:
   u=db.query(User).filter_by(email=self.email).one();u.login_sent_at=utcnow()-dt.timedelta(minutes=2);db.commit()
  _,messages=self.send();self.assertEqual(messages,[]);self.assertEqual(self.redeem().status_code,401)
 def test_resend_cooldown_and_unknown_same_response(self):
  result,_=self.send();again,messages=self.send();self.assertEqual(messages,[]);self.assertEqual(result.json(),again.json())
  unknown=self.client.post('/auth/magic-link',json={'email':'absent@example.test'});self.assertEqual(result.json(),unknown.json())
 def test_expiry_allows_new_window(self):
  self.send()
  with SessionLocal() as db:
   u=db.query(User).filter_by(email=self.email).one();u.login_expires=utcnow()-dt.timedelta(seconds=1);u.login_sent_at=utcnow()-dt.timedelta(minutes=20);u.login_attempts=5;db.commit()
  self.assertEqual(self.redeem().status_code,401);_,messages=self.send();self.assertTrue(messages);self.assertEqual(self.redeem().status_code,200)
 def test_scoped_code_cannot_use_legacy_path_to_bypass_attempt_limit(self):
  self.send()
  for i in range(5):self.redeem('999999')
  self.assertEqual(self.redeem(self.email+':001234').status_code,401)
 def test_old_long_code_remains_valid(self):
  with SessionLocal() as db:
   u=db.query(User).filter_by(email=self.email).one();u.login_hash=auth.hash_token('old-high-entropy-code');u.login_expires=utcnow()+dt.timedelta(minutes=1);db.commit()
  self.assertEqual(self.redeem('old-high-entropy-code',email='').status_code,200)
 def test_identical_codes_do_not_collide_across_accounts(self):
  self.send()
  with SessionLocal() as db:db.add(User(email='second@example.test',role='admin',org_id='two'));db.commit()
  self.email='second@example.test';self.send()
  r=self.redeem();self.assertEqual(r.status_code,200);self.assertEqual(r.json()['email'],self.email)
 def test_concurrent_redeem_consumed_once(self):
  self.send();results=[]
  def redeem():results.append(self.redeem().status_code)
  threads=[threading.Thread(target=redeem) for _ in range(2)]
  for t in threads:t.start()
  for t in threads:t.join()
  self.assertEqual(sorted(results),[200,401])
if __name__=='__main__':unittest.main()
