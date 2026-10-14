// Build the complete desktop bundle. Nothing is fetched or installed on customer machines.
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";
import fs from "node:fs";
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const source = path.resolve(root, "../netbridge-source");
const windows = process.platform === "win32";
// Seal the entire bundle even without an Apple certificate. The linker's
// executable-only signature leaves downloaded apps looking damaged to macOS.
// Tauri signs before creating the DMG/updater archives. Preserve a configured
// Developer ID identity for signed and notarized releases.
if (process.platform === "darwin" && !process.env.APPLE_SIGNING_IDENTITY) {
  process.env.APPLE_SIGNING_IDENTITY = "-";
}
const python =
  process.env.NB_PYTHON ||
  path.join(source, windows ? ".venv/Scripts/python.exe" : ".venv/bin/python");
const media = process.env.NB_MEDIA_DIR || path.join(source, "_bundle/runtime");
for (const relative of [
  windows ? "ffmpeg.exe" : "ffmpeg",
  windows ? "gst/gst-launch-1.0.exe" : "gst/gst-launch-1.0",
]) {
  if (!fs.existsSync(path.join(media, relative)))
    throw new Error(
      `Missing bundled media dependency: ${path.join(media, relative)}. Set NB_MEDIA_DIR to a verified media runtime.`,
    );
}
function run(bin, args) {
  const r = spawnSync(bin, args, {
    cwd: root,
    stdio: "inherit",
    env: process.env,
  });
  if (r.error) throw r.error;
  if (r.status !== 0) process.exit(r.status || 1);
}
const resources = path.join(root, "src-tauri/resources");
const work = path.join(root, "src-tauri/target/engine-build");
fs.mkdirSync(resources, { recursive: true });
const build = ["--noconfirm", "--clean", "--distpath", resources, "--workpath", work];
const spec = [
  "--onedir",
  "--name",
  "NetBridgeEngine",
  "--specpath",
  work,
  "--paths",
  source,
  "--hidden-import",
  "certifi",
  "--collect-data",
  "certifi",
  "--hidden-import",
  "audio_engine",
  "--hidden-import",
  "audio_diagnostics",
];
if (process.env.NB_PERSISTENT_AUDIO === "1") {
  // PyInstaller's Gst hook otherwise copies EVERY plugin installed on the build machine
  // (hundreds on Windows, including gstpython, which links its own Python DLL). The plugin
  // filter exists only as a .spec option, so write the spec, then add the filter to it.
  spec.push("--hidden-import", "gi.repository.Gst",
    "--runtime-hook", path.join(root, "scripts/pyi-rth-gi-dlls.py"),
    path.join(source, "desktop_engine.py"));
  run(python, ["-c", "from PyInstaller.utils.cliutils.makespec import run; run()", ...spec]);
  const specFile = path.join(work, "NetBridgeEngine.spec");
  const plugins = ["coreelements", "udp", "rtpmanager", "rtp", "opus", "audioconvert",
    "audioresample", "audiofx", "volume", "autodetect", "webrtcdsp", "app",
    ...(windows ? ["wasapi2", "wasapi", "directsound"] : ["osxaudio"])];
  const hooksconfig = { gstreamer: { include_plugins: plugins }, gi: { languages: ["en_US"] } };
  const text = fs.readFileSync(specFile, "utf8");
  if (!text.includes("hooksconfig={},"))
    throw new Error(`Cannot restrict GStreamer plugins: no hooksconfig in ${specFile}`);
  fs.writeFileSync(specFile, text.replace("hooksconfig={},", `hooksconfig=${JSON.stringify(hooksconfig)},`));
  run(python, ["-m", "PyInstaller", ...build, specFile]);
} else {
  run(python, ["-m", "PyInstaller", ...build, ...spec, path.join(source, "desktop_engine.py")]);
}
const engine = path.join(resources, "NetBridgeEngine");
// Copy verbatim AFTER freezing: PyInstaller must not rewrite the Go mesh helper.
fs.cpSync(media, path.join(engine, "runtime"), { recursive: true });
// Build the helper from the checked-in source, never ship a stale cached binary.
const meshBuild = spawnSync(process.env.NB_GO || "go", ["build", "-o",
  path.join(engine, "runtime", windows ? "netbridge-mesh.exe" : "netbridge-mesh"), "."],
  { cwd: path.join(source, "mesh"), stdio: "inherit", env: process.env });
if (meshBuild.error) throw new Error(`Go is required to build the mesh helper; set NB_GO. ${meshBuild.error.message}`);
if (meshBuild.status !== 0) process.exit(meshBuild.status || 1);

const config = {
  bundle: { resources: { "resources/NetBridgeEngine/": "engine/" } },
};
if (process.env.NB_UPDATE_ENDPOINT || process.env.NB_UPDATE_PUBLIC_KEY) {
  if (
    !process.env.NB_UPDATE_ENDPOINT?.startsWith("https://") ||
    !process.env.NB_UPDATE_PUBLIC_KEY ||
    !process.env.TAURI_SIGNING_PRIVATE_KEY
  ) {
    throw new Error(
      "Updates require NB_UPDATE_ENDPOINT (HTTPS), NB_UPDATE_PUBLIC_KEY, and TAURI_SIGNING_PRIVATE_KEY.",
    );
  }
  config.plugins = {
    updater: {
      pubkey: process.env.NB_UPDATE_PUBLIC_KEY,
      endpoints: [process.env.NB_UPDATE_ENDPOINT],
    },
  };
  config.bundle.createUpdaterArtifacts = true;
}
const configPath = path.join(root, "src-tauri/target/package-config.json");
fs.writeFileSync(configPath, JSON.stringify(config));
// Invoke the JS entry point directly: Windows .cmd shims cannot be spawned
// as executables without a shell. Keep paths and arguments separate on all OSes.
run(process.execPath, [
  path.join(root, "node_modules/@tauri-apps/cli/tauri.js"),
  "build",
  "--config",
  configPath,
  ...process.argv.slice(2),
]);
