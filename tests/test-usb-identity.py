"""Execute only the descriptor identity function, never hardware configuration."""
import pathlib,re,subprocess,unittest
ROOT=pathlib.Path(__file__).resolve().parents[1]
SCRIPT=(ROOT/'pi/scripts/uvc-raw-setup.sh').read_text()
FN=re.search(r'bridge_usb_serial\(\) \{.*?\n\}',SCRIPT,re.S).group()
class USB(unittest.TestCase):
 def serial(self,mode,cpu):return subprocess.run(['sh','-c',FN+'\nbridge_usb_serial "$1" "$2"','test',mode,cpu],text=True,capture_output=True)
 def test_default_preserves_laptop_identity(self):
  self.assertEqual(self.serial('','1234567890abcdef').stdout,'0123456789')
 def test_pilot_unique_and_invalid_fails_closed(self):
  self.assertEqual(self.serial('unique','1234567890abcdef').stdout,'1234567890abcdef')
  for cpu in ('','1234','../../x','0000000000000000'):
   r=self.serial('unique',cpu);self.assertNotEqual(r.returncode,0);self.assertEqual(r.stdout,'')
if __name__=='__main__':unittest.main()
