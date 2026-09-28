import importlib.util,pathlib,tempfile,sqlite3,unittest,os
P=pathlib.Path(__file__).resolve().parents[1]/'tools/fleet-backup.py'
spec=importlib.util.spec_from_file_location('backup',P);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
class Backup(unittest.TestCase):
 def test_backup_restores_rows_without_touching_source(self):
  with tempfile.TemporaryDirectory() as td:
   src=pathlib.Path(td)/'source.db';dst=pathlib.Path(td)/'copy.db'
   with sqlite3.connect(src) as c:c.execute('CREATE TABLE example (value TEXT)');c.execute("INSERT INTO example VALUES ('retained')")
   r=m.backup(src,dst);self.assertEqual(r['integrity'],'ok');self.assertFalse(r['uploaded'])
   with sqlite3.connect(dst) as c:self.assertEqual(c.execute('SELECT * FROM example').fetchall(),[('retained',)])
   if os.name!='nt':self.assertEqual(dst.stat().st_mode&0o777,0o600)
   with self.assertRaises(ValueError):m.backup(src,dst)
   with self.assertRaises(ValueError):m.backup(src,src)
if __name__=='__main__':unittest.main()
