import datetime as dt
import importlib.util
import pathlib
import sqlite3
import tempfile
import unittest

ROOT=pathlib.Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('backup',ROOT/'control-plane/backend/app/backup.py')
b=importlib.util.module_from_spec(spec);spec.loader.exec_module(b)
class Backup(unittest.TestCase):
 def test_wal_database_backup_can_be_restored_and_retention_is_bounded(self):
  with tempfile.TemporaryDirectory() as d:
   root=pathlib.Path(d);db=root/'fleet.db';out=root/'backups'
   live=sqlite3.connect(db)
   try:
    live.execute('PRAGMA journal_mode=WAL');live.execute('CREATE TABLE devices(id TEXT)')
    live.execute("INSERT INTO devices VALUES ('NB-002')");live.commit()
    for day in range(1,6): target=b.backup(db,out,retain=2,now=dt.datetime(2026,9,day))
    self.assertEqual(len(list(out.glob('fleet-daily-*'))),2)
    with sqlite3.connect(target) as restored:
     self.assertEqual(restored.execute('PRAGMA integrity_check').fetchone(),('ok',))
     self.assertEqual(restored.execute('SELECT id FROM devices').fetchall(),[('NB-002',)])
    self.assertEqual(target.stat().st_mode&0o777,0o600)
    self.assertFalse(list(out.glob('.fleet-backup-*')))
   finally:live.close()
 def test_missing_database_is_not_created(self):
  with tempfile.TemporaryDirectory() as d:
   root=pathlib.Path(d)
   with self.assertRaises(ValueError):b.backup(root/'missing',root/'backups')
   self.assertFalse((root/'missing').exists())
 def test_corrupt_database_does_not_replace_last_good_copy(self):
  with tempfile.TemporaryDirectory() as d:
   root=pathlib.Path(d);db=root/'fleet.db';out=root/'backups';now=dt.datetime(2026,9,1)
   with sqlite3.connect(db) as c:c.execute('CREATE TABLE devices(id TEXT)')
   target=b.backup(db,out,now=now);good=target.read_bytes();db.write_bytes(b'broken')
   with self.assertRaises(sqlite3.DatabaseError):b.backup(db,out,now=now)
   self.assertEqual(target.read_bytes(),good)
   self.assertFalse(list(out.glob('.fleet-backup-*')))
if __name__=='__main__':unittest.main()
