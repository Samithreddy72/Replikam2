"""Release versions, complete artifact sets, and immutable publication boundaries."""
import importlib.util
import json
import os
import pathlib
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('studio_release', ROOT / 'app/netbridge-desktop/scripts/ci-release.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class StudioRelease(unittest.TestCase):
    def test_rejects_invalid_versions_and_shell_content(self):
        for value in ('', 'v1.2.3', '1.2', '01.2.3', '1.2.3;echo bad', '1.2.3\n', '../1.2.3'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                release.validate_version(value)
        self.assertEqual(release.validate_version('1.2.3'), '1.2.3')

    def test_stamps_all_package_versions_without_changing_dependencies(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            (root / 'src-tauri').mkdir()
            for name in ('package.json', 'src-tauri/tauri.conf.json'):
                (root / name).write_text('{"version":"0.1.0"}')
            (root / 'package-lock.json').write_text(json.dumps({'version': '0.1.0', 'packages': {
                '': {'version': '0.1.0'}, 'node_modules/example': {'version': '7.0.0'}}}))
            for name in ('Cargo.toml', 'Cargo.lock'):
                (root / 'src-tauri' / name).write_text('name = "netbridge-desktop"\nversion = "0.1.0"\n')
            release.stamp('2.3.4', root)
            for name in ('package.json', 'src-tauri/tauri.conf.json', 'package-lock.json'):
                self.assertEqual(json.loads((root / name).read_text())['version'], '2.3.4')
            lock = json.loads((root / 'package-lock.json').read_text())
            self.assertEqual(lock['packages']['']['version'], '2.3.4')
            self.assertEqual(lock['packages']['node_modules/example']['version'], '7.0.0')
            self.assertIn('version = "2.3.4"', (root / 'src-tauri/Cargo.lock').read_text())

    def test_published_release_is_never_overwritten(self):
        with patch.dict(os.environ, GITHUB_REPOSITORY='test/repo', GITHUB_SHA='abc'), patch.object(
            release.subprocess, 'run', return_value=SimpleNamespace(returncode=0,
                stdout=json.dumps({'isDraft': False, 'targetCommitish': 'abc'}))) as run:
            with self.assertRaises(ValueError):
                release.publish('1.2.3', '/unused', False)
            self.assertEqual(run.call_count, 1)

    def test_draft_from_different_commit_is_never_overwritten(self):
        with patch.dict(os.environ, GITHUB_REPOSITORY='test/repo', GITHUB_SHA='abc'), patch.object(
            release.subprocess, 'run', return_value=SimpleNamespace(returncode=0,
                stdout=json.dumps({'isDraft': True, 'targetCommitish': 'other'}))) as run:
            with self.assertRaises(ValueError):
                release.publish('1.2.3', '/unused', True)
            self.assertEqual(run.call_count, 1)

    def test_partial_platform_build_cannot_publish(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ,
            GITHUB_REPOSITORY='test/repo', GITHUB_SHA='abc'), patch.object(
            release.subprocess, 'run', return_value=SimpleNamespace(returncode=1, stdout='')) as run:
            pathlib.Path(temp, 'NetBridge-Studio-1.2.3-macos-arm64.dmg').touch()
            with self.assertRaises(ValueError):
                release.publish('1.2.3', temp, True)
            self.assertEqual(run.call_count, 1)

    def test_stage_refuses_published_or_other_source(self):
        for draft, sha in ((False, 'abc'), (True, 'other')):
            with patch.dict(os.environ, GITHUB_REPOSITORY='test/repo', GITHUB_SHA='abc'), patch.object(
                release.subprocess, 'run', return_value=SimpleNamespace(returncode=0,
                    stdout=json.dumps({'isDraft': draft, 'targetCommitish': sha}))) as run:
                with self.assertRaises(ValueError):
                    release.stage('1.2.3', '/unused')
                self.assertEqual(run.call_count, 1)

    def test_stage_preserves_verified_files_in_draft(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ,
            GITHUB_REPOSITORY='test/repo', GITHUB_SHA='abc'), patch.object(
            release.subprocess, 'run', return_value=SimpleNamespace(returncode=0,
                stdout=json.dumps({'isDraft': True, 'targetCommitish': 'abc'}))) as run:
            artifact = pathlib.Path(temp, 'macos-arm64-SHA256SUMS.txt')
            artifact.write_text('verified checksum')
            release.stage('1.2.3', temp)
            self.assertEqual(run.call_count, 2)
            self.assertIn(str(artifact), run.call_args.args[0])
            self.assertIn('upload', run.call_args.args[0])
            self.assertNotIn('--draft=false', run.call_args.args[0])

    def test_default_draft_does_not_publish(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ,
            GITHUB_REPOSITORY='test/repo', GITHUB_SHA='abc'), patch.object(
            release.subprocess, 'run', return_value=SimpleNamespace(returncode=1, stdout='')) as run:
            for suffix in ('macos-arm64.dmg', 'windows-x64-setup.exe'):
                pathlib.Path(temp, 'NetBridge-Studio-1.2.3-' + suffix).touch()
            release.publish('1.2.3', temp, True)
            calls = [call.args[0] for call in run.call_args_list]
            self.assertTrue(any('create' in call and '--draft' in call for call in calls))
            self.assertFalse(any('--draft=false' in call for call in calls))


if __name__ == '__main__':
    unittest.main()
