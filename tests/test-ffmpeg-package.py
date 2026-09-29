"""Download archives must not leak temporary or AppleDouble files into signed runtimes."""
import importlib.util
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import zipfile

spec=importlib.util.spec_from_file_location('packaging',Path(__file__).resolve().parents[1]/'app/netbridge-source/build.py')
build=importlib.util.module_from_spec(spec);spec.loader.exec_module(build)

class Archive(unittest.TestCase):
    def run_archive(self, entries):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); archive=root/'input.zip'; destination=root/'runtime'
            with zipfile.ZipFile(archive,'w') as zipped:
                for name,body in entries.items():zipped.writestr(name,body)
            with patch.object(build.platform,'system',return_value='Darwin'), patch.object(build.platform,'machine',return_value='arm64'), patch.object(build,'IS_WIN',False), patch.object(build.urllib.request,'urlretrieve',side_effect=lambda url,path:shutil.copyfile(archive,path)):
                result=build.fetch_ffmpeg(destination)
            return result is not None,{p.name:p.read_bytes() for p in destination.iterdir()}

    def test_copies_only_executable_not_appledouble_archive_or_unrelated_paths(self):
        success,files=self.run_archive({'bin/ffmpeg':b'executable','__MACOSX/._ffmpeg':b'metadata','../../unrelated':b'never extracted'})
        self.assertTrue(success);self.assertEqual(files,{'ffmpeg':b'executable'})

    def test_missing_executable_does_not_leave_a_partial_runtime(self):
        success,files=self.run_archive({'__MACOSX/._ffmpeg':b'metadata'})
        self.assertFalse(success);self.assertEqual(files,{})

    def test_ambiguous_executable_is_refused(self):
        success,files=self.run_archive({'one/ffmpeg':b'one','two/ffmpeg':b'two'})
        self.assertFalse(success);self.assertEqual(files,{})

if __name__=='__main__':unittest.main()
