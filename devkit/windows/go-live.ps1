# ═══════════════════════════════════════════════════════════════════════════
#  RepliKam Developer GO-LIVE (Windows)
#  Right-click → Run with PowerShell   (or: powershell -ExecutionPolicy Bypass -File go-live.ps1)
#  Streams your camera + microphone to YOUR bridge and plays the meeting audio back.
#  Your bridge is set in developer.conf (next to this file) — done by your admin.
# ═══════════════════════════════════════════════════════════════════════════
$ErrorActionPreference = "SilentlyContinue"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path

# ── developer.conf ──────────────────────────────────────────────────────────
$conf = @{}
Get-Content "$Here\developer.conf" | ForEach-Object {
  if ($_ -match '^\s*([A-Z_]+)\s*=\s*(.+?)\s*$') { $conf[$Matches[1]] = $Matches[2] }
}
$PI       = $conf["BRIDGE"];        if (-not $PI) { Write-Host "❌ developer.conf missing BRIDGE="; pause; exit 1 }
$MICGAIN  = $conf["MIC_GAIN_DB"];   if (-not $MICGAIN) { $MICGAIN = "8" }
$FPS      = $conf["FPS"];           if (-not $FPS) { $FPS = "20" }
Write-Host ">> RepliKam go-live -> bridge $PI  (mic +${MICGAIN}dB, ${FPS}fps)" -ForegroundColor Green

# ── ffmpeg (auto-locate; offer winget install) ──────────────────────────────
$ff = (Get-Command ffmpeg -EA SilentlyContinue).Source
if (-not $ff) { $ff = (Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Recurse -Filter ffmpeg.exe -EA SilentlyContinue | Select-Object -First 1).FullName }
if (-not $ff) {
  Write-Host ">> ffmpeg not found — installing via winget (one time)..." -ForegroundColor Yellow
  winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements | Out-Null
  $ff = (Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Recurse -Filter ffmpeg.exe -EA SilentlyContinue | Select-Object -First 1).FullName
}
if (-not $ff) { Write-Host "❌ ffmpeg unavailable — install from ffmpeg.org and re-run"; pause; exit 1 }
$ffplay = Join-Path (Split-Path $ff) "ffplay.exe"

# ── auto-detect camera + mic (first of each; override in developer.conf) ────
$devs = & $ff -hide_banner -list_devices true -f dshow -i dummy 2>&1
$cam = $conf["CAMERA"]; $mic = $conf["MIC"]
if (-not $cam) { $cam = ($devs | Select-String '"(.+)" \(video\)' | Select-Object -First 1).Matches.Groups[1].Value }
if (-not $mic) { $mic = ($devs | Select-String '"(.+)" \(audio\)' | Select-Object -First 1).Matches.Groups[1].Value }
if (-not $cam -or -not $mic) { Write-Host "❌ no camera/mic detected"; pause; exit 1 }
Write-Host ">> camera: $cam" ; Write-Host ">> mic:    $mic"

# ── return-audio listener (meeting audio -> this PC) ────────────────────────
$sdp = "$env:TEMP\replikam-return.sdp"
@"
v=0
o=- 0 0 IN IP4 0.0.0.0
s=RepliKam return
c=IN IP4 0.0.0.0
m=audio 5004 RTP/AVP 97
a=rtpmap:97 opus/48000/2
"@ | Set-Content $sdp
$listener = Start-Process $ffplay -ArgumentList "-nodisp -loglevel error -protocol_whitelist file,rtp,udp -fflags nobuffer -i `"$sdp`"" -PassThru -WindowStyle Hidden
Write-Host ">> return-audio listener started (meeting audio plays here)"

# ── mic sender (background, auto-restart) ───────────────────────────────────
$micJob = Start-Job -ArgumentList $ff,$mic,$PI,$MICGAIN -ScriptBlock {
  param($ff,$mic,$PI,$g)
  while ($true) {
    & $ff -hide_banner -loglevel error -f dshow -i "audio=$mic" `
      -af "volume=${g}dB,alimiter=limit=0.9" `
      -c:a libopus -b:a 64k -ar 48000 -ac 2 -application lowdelay `
      -payload_type 97 -f rtp "rtp://${PI}:5002" 2>$null
    Start-Sleep 1
  }
}
Write-Host ">> mic streaming (auto-restarts on glitches)"

# ── camera sender (foreground loop; Ctrl-C or close window to stop) ─────────
Write-Host ">> camera streaming — CLOSE THIS WINDOW (or Ctrl-C) to stop everything" -ForegroundColor Green
try {
  while ($true) {
    & $ff -hide_banner -loglevel error -f dshow -framerate 30 -i "video=$cam" `
      -vf "scale=320:180,format=yuv420p" -r $FPS `
      -c:v libx264 -preset ultrafast -tune zerolatency -b:v 400k -g $FPS `
      -bsf:v dump_extra=freq=keyframe -an `
      -f rtp "rtp://${PI}:5000?pkt_size=1100" 2>$null
    Start-Sleep 2
  }
} finally {
  Stop-Job $micJob -EA SilentlyContinue; Remove-Job $micJob -Force -EA SilentlyContinue
  Stop-Process -Id $listener.Id -EA SilentlyContinue
  Write-Host ">> all streams stopped."
}
