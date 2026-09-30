"""Assemble Studio's native media runtime on its target OS; no legacy app build."""
import importlib.util
import json
import os
import pathlib
import platform
import subprocess
import sys
import tempfile
import hashlib

SOURCE = pathlib.Path(__file__).resolve().parents[2] / 'netbridge-source'


def prepare(destination):
    destination = pathlib.Path(destination).resolve()
    if destination.exists() and any(destination.iterdir()):
        raise RuntimeError('Use an empty runtime directory to avoid stale libraries')
    destination.mkdir(parents=True, exist_ok=True)
    spec = importlib.util.spec_from_file_location('media_bundle', SOURCE / 'build.py')
    bundle = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bundle)
    # Studio's outgoing CoreAudio/Opus pipeline also needs these elements.
    required = list(dict.fromkeys(bundle.GST_ELEMENTS + ['fakesink', 'opusenc', 'rtpopuspay'] +
                                 (['osxaudiosrc'] if sys.platform == 'darwin' else [])))
    bundle.GST_ELEMENTS = required
    ffmpeg = bundle.fetch_ffmpeg(destination)
    gst = bundle.bundle_gstreamer(destination)
    if not ffmpeg or not gst:
        raise RuntimeError('Complete FFmpeg and GStreamer runtimes are required')
    gstdir = destination / 'gst'
    inspect = gstdir / ('gst-inspect-1.0.exe' if os.name == 'nt' else 'gst-inspect-1.0')
    env = dict(os.environ, GST_PLUGIN_PATH=str(gstdir / 'plugins'),
               GST_PLUGIN_SYSTEM_PATH_1_0=str(gstdir / 'plugins'), GST_REGISTRY_FORK='no',
               PATH=str(gstdir) + os.pathsep + os.environ.get('PATH', ''))
    with tempfile.TemporaryDirectory() as temp:
        env['GST_REGISTRY'] = str(pathlib.Path(temp) / 'registry.bin')
        for element in required:
            subprocess.run([str(inspect), element], env=env, check=True,
                           stdout=subprocess.DEVNULL, timeout=30)
    # Validate codecs and capture support without opening a camera or microphone.
    encoders = subprocess.check_output([str(ffmpeg), '-hide_banner', '-encoders'], text=True)
    devices = subprocess.check_output([str(ffmpeg), '-hide_banner', '-devices'], text=True)
    for codec in ('libopus', 'h264_videotoolbox' if sys.platform == 'darwin' else 'libx264'):
        if codec not in encoders:
            raise RuntimeError('FFmpeg is missing ' + codec)
    filters = subprocess.check_output([str(ffmpeg), "-hide_banner", "-filters"], text=True)
    if "lavfi" not in devices or not any(" color " in line for line in filters.splitlines()):
        raise RuntimeError("FFmpeg is missing the camera-off black video source (lavfi/color)")
    capture = 'avfoundation' if sys.platform == 'darwin' else 'dshow'
    if capture not in devices:
        raise RuntimeError('FFmpeg is missing capture device ' + capture)
    files = {}
    for path in sorted(destination.rglob('*')):
        if path.is_file():
            files[path.relative_to(destination).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    (destination / 'runtime-build.json').write_text(json.dumps({
        'platform': sys.platform, 'architecture': platform.machine(),
        'ffmpeg_source': bundle.FFMPEG_URLS.get(platform.system() + '-' + platform.machine()),
        'gstreamer': subprocess.check_output([str(inspect), '--version'], env=env, text=True).strip(),
        'files_sha256': files,
    }, indent=2) + '\n')
    print('Verified runtime:', destination)


if __name__ == '__main__':
    if sys.platform not in ('darwin', 'win32'):
        raise SystemExit('Build this runtime on macOS or Windows')
    prepare(sys.argv[1])
