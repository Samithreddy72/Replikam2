"""Version, collect, and publish Studio artifacts. Called only by release CI."""
import argparse
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]


def validate_version(version):
    number = r'(?:0|[1-9][0-9]*)'
    if not re.fullmatch(rf'{number}\.{number}\.{number}', version):
        raise ValueError('Use a numeric Studio version such as 0.1.1 (no v prefix)')
    return version


def stamp(version, root=ROOT):
    validate_version(version)
    for name in ('package.json', 'package-lock.json', 'src-tauri/tauri.conf.json'):
        path = root / name
        data = json.loads(path.read_text())
        data['version'] = version
        if name == 'package-lock.json':
            data['packages']['']['version'] = version
        path.write_text(json.dumps(data, indent=2) + '\n')
    for name in ('src-tauri/Cargo.toml', 'src-tauri/Cargo.lock'):
        path = root / name
        value, count = re.subn(r'(name = "netbridge-desktop"\nversion = ")[^"]+',
                               lambda match: match[1] + version, path.read_text(), count=1)
        if count != 1:
            raise ValueError('Studio package version not found in ' + name)
        path.write_text(value)


def collect(version, platform, output, target=None):
    validate_version(version)
    output = pathlib.Path(output)
    output.mkdir(parents=True, exist_ok=True)
    target = pathlib.Path(target or ROOT / 'src-tauri/target')
    bundle = target / 'release/bundle'
    prefix = f'NetBridge-Studio-{version}-{platform}'
    if platform == 'macos-arm64':
        app = bundle / 'macos/NetBridge Studio.app'
        if not app.is_dir():
            raise ValueError('Missing macOS application')
        subprocess.run(['ditto', '-c', '-k', '--sequesterRsrc', '--keepParent',
                        str(app), str(output / (prefix + '.zip'))], check=True)
        candidates = list((bundle / 'dmg').glob('*.dmg'))
        extension = '.dmg'
    else:
        candidates = list((bundle / 'nsis').glob('*-setup.exe'))
        extension = '-setup.exe'
    if len(candidates) != 1:
        raise ValueError(f'Expected one installer for {platform}, got {len(candidates)}')
    shutil.copy2(candidates[0], output / (prefix + extension))
    for artifact in bundle.rglob('*.sig'):
        payload = artifact.with_suffix('')
        if not payload.is_file():
            raise ValueError('Updater signature has no payload: ' + str(artifact))
        # Prefix names to avoid collisions when merging platform artifacts.
        for file in (payload, artifact):
            shutil.copy2(file, output / (platform + '-' + file.name))
    runtime = ROOT / 'src-tauri/resources/NetBridgeEngine/runtime/runtime-build.json'
    if runtime.exists():
        shutil.copy2(runtime, output / (platform + '-runtime-build.json'))
    (output / (platform + '-build-info.json')).write_text(json.dumps({
        'product': 'NetBridge Studio', 'version': version, 'platform': platform,
        'commit': os.environ.get('GITHUB_SHA'), 'run_id': os.environ.get('GITHUB_RUN_ID'),
        'hardware_qualified': False,
    }, indent=2) + '\n')
    lines = [hashlib.sha256(file.read_bytes()).hexdigest() + '  ' + file.name
             for file in sorted(output.iterdir()) if file.is_file() and not file.name.endswith('SHA256SUMS.txt')]
    (output / (platform + '-SHA256SUMS.txt')).write_text('\n'.join(lines) + '\n')


def publish(version, directory, draft):
    validate_version(version)
    repo = os.environ['GITHUB_REPOSITORY']
    sha = os.environ['GITHUB_SHA']
    tag = 'studio-v' + version
    command = ['gh', 'release']
    existing = subprocess.run(command + ['view', tag, '--repo', repo, '--json', 'isDraft,targetCommitish'],
                              capture_output=True, text=True)
    if existing.returncode == 0:
        info = json.loads(existing.stdout)
        if not info['isDraft'] or info['targetCommitish'] != sha:
            raise ValueError('Refusing to overwrite an existing release from a different commit or a published release')
    directory = pathlib.Path(directory)
    for platform, suffix in (('macos-arm64', '.dmg'), ('windows-x64', '-setup.exe')):
        if not (directory / f'NetBridge-Studio-{version}-{platform}{suffix}').is_file():
            raise ValueError('Both platforms must pass before creating a release')
    notes = directory / 'release-notes.md'
    notes.write_text(f'''NetBridge Studio {version}\n\nSource commit: {sha}\n\nIncludes macOS Apple Silicon ZIP/DMG and Windows x64 NSIS installer.\nBuilt with the bundled Python/media runtime and freshly compiled mesh helper.\n\nAutomated checks passed; a physical Pi/meeting test is still required.\nWindows artifacts are unsigned. macOS is signed/notarized only when Apple\ncredentials are configured; otherwise it is an ad-hoc developer build.\nUpdater signatures, if present, are separate from OS code signing.\nDo not treat this build as customer-qualified without reviewing signing and hardware results.\n\nEnd the session and quit Studio before installing. Keep the previous whole-app\ninstaller for rollback; do not delete the user's NetBridge profile.\n''')
    if existing.returncode:
        subprocess.run(command + ['create', tag, '--repo', repo, '--target', sha, '--draft',
                                  '--title', 'NetBridge Studio ' + version, '--notes-file', str(notes)], check=True)
    else:
        subprocess.run(command + ['edit', tag, '--repo', repo, '--notes-file', str(notes)], check=True)
    artifacts = [str(path) for path in sorted(directory.iterdir()) if path.is_file() and path != notes]
    subprocess.run(command + ['upload', tag, '--repo', repo, '--clobber', *artifacts], check=True)
    if not draft:
        subprocess.run(command + ['edit', tag, '--repo', repo, '--draft=false', '--prerelease', '--latest=false'], check=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=['version', 'collect', 'publish'])
    parser.add_argument('--version', required=True)
    parser.add_argument('--platform', choices=['macos-arm64', 'windows-x64'])
    parser.add_argument('--directory')
    parser.add_argument('--draft', choices=['true', 'false'], default='true')
    args = parser.parse_args()
    if args.operation == 'version':
        stamp(args.version)
    elif args.operation == 'collect':
        collect(args.version, args.platform, args.directory, os.environ.get('CARGO_TARGET_DIR'))
    else:
        publish(args.version, args.directory, args.draft == 'true')
