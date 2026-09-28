"""Daily delivery retries and malformed mesh inventory cannot invent a healthy state."""
import os,pathlib,sys,tempfile,unittest,datetime as dt,json
from unittest.mock import patch,MagicMock
from types import SimpleNamespace
ROOT=pathlib.Path(__file__).resolve().parents[1]
T=tempfile.TemporaryDirectory();os.environ['DATABASE_URL']='sqlite:///'+T.name+'/fleet.db'
sys.path.insert(0,str(ROOT/'control-plane/backend'))
from app import workflow,workflow_mesh as mesh,workflow_digest as digest
from app.db import Base,engine,SessionLocal
from app.models import User,Device,WorkflowSettings
Base.metadata.create_all(engine)
class Notifications(unittest.TestCase):
 def test_malformed_expiry_unknown_and_disabled_never_warn(self):
  now=dt.datetime.now(dt.timezone.utc)
  for value in ({},[],12,False,'bad',None):self.assertIsNone(workflow.utc(value))
  dev=SimpleNamespace(operations={'mesh_expiry':{'known':True,'checked_at':now.isoformat(),'expires':{}}})
  self.assertIsNone(mesh.expiry_warning(dev,now))
  dev.operations['mesh_expiry']['expires']=(now+dt.timedelta(days=10)).isoformat()
  self.assertIn('10.0 days',mesh.expiry_warning(dev,now))
  dev.operations['mesh_expiry']['disabled']=True;self.assertIsNone(mesh.expiry_warning(dev,now))
  dev.operations['mesh_expiry'].update(disabled=False,checked_at=(now-dt.timedelta(hours=3)).isoformat())
  self.assertIsNone(mesh.expiry_warning(dev,now))
 def test_inventory_is_read_only_and_malformed_addresses_ignored(self):
  response=MagicMock();response.__enter__.return_value.read.return_value=json.dumps({'devices':[{'addresses':17}, {'addresses':['100.1.2.3'],'expires':{}}]}).encode()
  with SessionLocal() as db,patch.dict(os.environ,{'TS_DEVICES_READ_TOKEN':'test'}),patch.object(mesh,'_last_attempt',0),patch.object(mesh.time,'monotonic',return_value=4000),patch.object(mesh.urllib.request,'urlopen',return_value=response) as call:
   db.add(Device(id='mesh',pairing_code='M',tailscale_ip='100.1.2.3'));db.commit();mesh.refresh(db)
   self.assertEqual(call.call_args.args[0].get_method(),'GET');self.assertIsNone(mesh.expiry_warning(db.get(Device,'mesh')))
 def test_digest_opt_in_scoped_partial_retry_and_once_per_day(self):
  now=dt.datetime(2026,9,28,4,tzinfo=dt.timezone.utc)
  with SessionLocal() as db:
   db.add_all([User(email='a@test',role='admin',org_id='one'),User(email='b@test',role='admin',org_id='one'),User(email='p@test',role='presenter',org_id='one'),User(email='other@test',role='admin',org_id='two'),WorkflowSettings(org_id='one',data={'daily_digest':True}),WorkflowSettings(org_id='two',data={'daily_digest':False})]);db.commit()
   with patch.object(digest.notifier,'email_configured',return_value=True),patch.object(digest.notifier,'send_mail',side_effect=[True,False,True]) as send:
    digest.tick(db,now);self.assertEqual(send.call_count,2)
    digest.tick(db,now+dt.timedelta(minutes=1));self.assertEqual(send.call_count,2)
    digest.tick(db,now+dt.timedelta(minutes=11));self.assertEqual(send.call_count,3)
    digest.tick(db,now+dt.timedelta(hours=1));self.assertEqual(send.call_count,3)
    self.assertEqual([c.args[0] for c in send.call_args_list],['a@test','b@test','b@test'])
if __name__=='__main__':unittest.main()
