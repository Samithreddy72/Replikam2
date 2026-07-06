# RepliKam STOP (Windows) — ends all streams cleanly
Get-Job | Stop-Job -EA SilentlyContinue; Get-Job | Remove-Job -Force -EA SilentlyContinue
Get-Process ffmpeg,ffplay -EA SilentlyContinue | Stop-Process -Force
Write-Host "✅ all RepliKam streams stopped"
