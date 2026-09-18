"""Import CI signing credentials without printing their values."""
import base64
import os
import pathlib
import secrets
import subprocess

required = ('APPLE_CERTIFICATE', 'APPLE_CERTIFICATE_PASSWORD', 'APPLE_SIGNING_IDENTITY',
            'APPLE_API_KEY_CONTENT', 'APPLE_API_KEY', 'APPLE_API_ISSUER')
missing = [name for name in required if not os.environ.get(name)]
if missing:
    raise SystemExit('Missing signing secrets: ' + ', '.join(missing))
root = pathlib.Path(os.environ['RUNNER_TEMP'])
certificate = root / 'studio-certificate.p12'
key = root / 'studio-notary.p8'
keychain = root / 'studio-signing.keychain-db'
certificate.write_bytes(base64.b64decode(os.environ['APPLE_CERTIFICATE'], validate=True))
key.write_text(os.environ['APPLE_API_KEY_CONTENT'])
certificate.chmod(0o600)
key.chmod(0o600)
password = secrets.token_hex(32)

def security(*args):
    subprocess.run(['security', *args], check=True, stdout=subprocess.DEVNULL)

security('create-keychain', '-p', password, str(keychain))
security('set-keychain-settings', '-lut', '21600', str(keychain))
security('unlock-keychain', '-p', password, str(keychain))
security('import', str(certificate), '-P', os.environ['APPLE_CERTIFICATE_PASSWORD'],
         '-A', '-t', 'cert', '-f', 'pkcs12', '-k', str(keychain))
security('set-key-partition-list', '-S', 'apple-tool:,apple:,codesign:', '-k', password, str(keychain))
security('list-keychains', '-d', 'user', '-s', str(keychain), str(pathlib.Path.home() / 'Library/Keychains/login.keychain-db'))
with open(os.environ['GITHUB_ENV'], 'a') as output:
    for name in ('APPLE_SIGNING_IDENTITY', 'APPLE_API_KEY', 'APPLE_API_ISSUER'):
        value = os.environ[name]
        if '\n' in value or '\r' in value:
            raise SystemExit('Invalid multiline credential metadata')
        output.write(name + '=' + value + '\n')
    output.write('APPLE_API_KEY_PATH=' + str(key) + '\n')
certificate.unlink()
