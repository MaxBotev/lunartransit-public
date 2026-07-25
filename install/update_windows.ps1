# LunarTransit — update an existing Windows install in place.
#
# Pulls the current lunar_*.py from the public repo, backs up what it replaces,
# and (optionally) points this machine at a Pi's ADS-B feed by hostname so a
# DHCP reassignment can't break the link.
#
# Run from an elevated PowerShell:
#     Set-ExecutionPolicy -Scope Process Bypass
#     .\update_windows.ps1                     # code only
#     .\update_windows.ps1 -PiHost sky         # code + repoint feed at "sky"
param(
  [string]$Dst    = "C:\LunarTransit",
  [string]$PiHost = "",          # e.g. "sky" -> http://sky.local:8080/api/adsb/raw
  [switch]$OwnCapture            # set capture_enabled true on THIS machine
)
$ErrorActionPreference = "Stop"
$repo = "https://raw.githubusercontent.com/MaxBotev/lunartransit-public/master"
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"

if (-not (Test-Path $Dst)) { throw "$Dst not found — run install_windows.ps1 first." }

Write-Host ">> backing up + updating code in $Dst" -ForegroundColor Cyan
foreach ($f in @("lunar_transit.py","lunar_server.py","lunar_page.py","adsb3d_page.py")) {
  $target = Join-Path $Dst $f
  if (Test-Path $target) { Copy-Item $target "$target.bak-$stamp" }
  Invoke-WebRequest -Uri "$repo/$f" -OutFile $target -UseBasicParsing
  Write-Host "   updated $f"
}

if ($PiHost -or $OwnCapture) {
  $cfgPath = Join-Path $Dst "config.json"
  if (-not (Test-Path $cfgPath)) { throw "config.json not found in $Dst" }
  Copy-Item $cfgPath "$cfgPath.bak-$stamp"
  $cfg = Get-Content $cfgPath -Raw | ConvertFrom-Json

  if ($PiHost) {
    # Prefer mDNS, fall back to the bare hostname, then to whatever the Pi
    # resolves to right now — recorded only as a last resort.
    $cfg.adsb_source = "http://$PiHost.local:8080/api/adsb/raw"
    $fallbacks = @("http://${PiHost}:8080/api/adsb/raw")
    try {
      $ip = ([System.Net.Dns]::GetHostAddresses("$PiHost.local") |
             Where-Object { $_.AddressFamily -eq 'InterNetwork' } |
             Select-Object -First 1).IPAddressToString
      if ($ip) { $fallbacks += "http://${ip}:8080/api/adsb/raw" }
    } catch { Write-Host "   (could not resolve $PiHost.local for a fallback)" -ForegroundColor Yellow }
    $cfg | Add-Member -NotePropertyName adsb_source_fallbacks -NotePropertyValue $fallbacks -Force
    Write-Host ">> feed -> $($cfg.adsb_source)" -ForegroundColor Cyan
    $fallbacks | ForEach-Object { Write-Host "   fallback: $_" }
  }

  if ($OwnCapture) {
    $cfg | Add-Member -NotePropertyName capture_enabled -NotePropertyValue $true -Force
    $cfg | Add-Member -NotePropertyName capture_host    -NotePropertyValue "127.0.0.1" -Force
    Write-Host ">> capture trigger enabled on this machine (127.0.0.1:5580)" -ForegroundColor Cyan
  }

  $cfg | ConvertTo-Json -Depth 6 | Set-Content $cfgPath
}

Write-Host ">> restarting scheduled task" -ForegroundColor Cyan
schtasks /End /TN LunarTransit 2>$null | Out-Null
Start-Sleep -Seconds 2
schtasks /Run /TN LunarTransit | Out-Null
Start-Sleep -Seconds 6

try {
  $r = Invoke-WebRequest -Uri "http://localhost:8080/api/lunar" -UseBasicParsing -TimeoutSec 8
  $j = $r.Content | ConvertFrom-Json
  Write-Host "OK — server up. ok=$($j.ok) tracked=$($j.n_tracked) msg='$($j.message)'" -ForegroundColor Green
} catch {
  Write-Host "Server did not answer on :8080 — check $Dst\server.log" -ForegroundColor Yellow
}
