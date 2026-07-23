# LunarTransit — Windows installer.
#
# Installs the LunarTransit server (web dashboard + Moon-transit prediction +
# optional SharpCap capture trigger) to C:\LunarTransit and registers a
# scheduled task that starts it at boot. Tested on Windows 11.
#
# Run from an elevated PowerShell inside the cloned repo:
#     Set-ExecutionPolicy -Scope Process Bypass
#     .\install\install_windows.ps1
#
# ADS-B source options (chosen during install):
#   [1] Remote: read aircraft.json from a Pi running this stack (zero extra setup)
#   [2] Local:  RTL-SDR dongle on THIS machine via WSL2 + dump1090 (see notes)
$ErrorActionPreference = "Stop"
$dst = "C:\LunarTransit"
$src = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

Write-Host ">> Python 3.12" -ForegroundColor Cyan
if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
  winget install -e --id Python.Python.3.12 --source winget `
    --accept-package-agreements --accept-source-agreements
}
py -3.12 -m pip install --quiet flask numpy skyfield certifi

Write-Host ">> files -> $dst" -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path $dst | Out-Null
Copy-Item "$src\*.py","$src\doc8643.json","$src\horizon.hrz" -Destination $dst
Copy-Item "$src\capture-pc\adsb_supervisor.py","$src\capture-pc\sharpcap_listener.py" -Destination $dst

# ---- observer location -----------------------------------------------------
Write-Host ">> observing site (press Enter to accept the SFO default)" -ForegroundColor Cyan
$lat = Read-Host "  Latitude  (deg, +N) [37.6213]"  ; if (-not $lat) { $lat = "37.6213" }
$lon = Read-Host "  Longitude (deg, +E) [-122.3790]"; if (-not $lon) { $lon = "-122.3790" }
$alt = Read-Host "  Altitude  (m)       [4]"        ; if (-not $alt) { $alt = "4" }

$mode = Read-Host "ADS-B source: [1] remote Pi feed / [2] local dongle via WSL"
if ($mode -eq "2") {
  $adsb = "C:/LunarTransit/adsb_local/aircraft.json"
} else {
  $piurl = Read-Host "  Pi feed URL [http://raspberrypi.local:8080/api/adsb/raw]"
  if (-not $piurl) { $piurl = "http://raspberrypi.local:8080/api/adsb/raw" }
  $adsb = $piurl
}

@{
  home_lat = [double]$lat; home_lon = [double]$lon; home_alt_m = [double]$alt
  web_port = 8080; adsb_source = $adsb
  lunar_enabled = $true; lunar_min_elev_deg = 10.0
  capture_enabled = $false; capture_host = "127.0.0.1"; capture_port = 5580
  telegram_enabled = $false; telegram_bot_token = ""; telegram_chat_id = ""
  horizon_file = "horizon.hrz"
} | ConvertTo-Json | Set-Content "$dst\config.json"

Write-Host ">> lunar ephemeris (de421.bsp, ~17 MB, one time)" -ForegroundColor Cyan
if (-not (Test-Path "$dst\de421.bsp")) {
  py -3.12 -c "from skyfield.api import Loader; Loader(r'$dst')('de421.bsp')"
}

Write-Host ">> firewall + scheduled task" -ForegroundColor Cyan
netsh advfirewall firewall add rule name="LunarTransit web" dir=in action=allow protocol=TCP localport=8080 | Out-Null
$pyw = (py -3.12 -c "import sys; print(sys.executable.replace('python.exe','pythonw.exe'))")
Set-Content "$dst\run_server.bat" "@echo off`ncd /d $dst`n`"$pyw`" lunar_server.py >> server.log 2>&1"
schtasks /Create /TN LunarTransit /TR "$dst\run_server.bat" /SC ONSTART /RU SYSTEM /RL HIGHEST /F | Out-Null
schtasks /Run /TN LunarTransit | Out-Null

if ($mode -eq "2") {
  Write-Host @"
>> LOCAL DONGLE via WSL2 (manual steps) -------------------------------------
 1. wsl --install --no-distribution        (reboot if asked, then re-run:)
 2. wsl --install -d Ubuntu --no-launch ; ubuntu install --root
 3. In WSL (Ubuntu, as root): build dump1090 and enable systemd --
    apt-get update && apt-get install -y build-essential git pkg-config \
      libusb-1.0-0-dev librtlsdr-dev libncurses-dev zlib1g-dev usbutils
    git clone --depth 1 https://github.com/flightaware/dump1090 /opt/dump1090
    make -C /opt/dump1090 -j4 RTLSDR=yes BLADERF=no HACKRF=no LIMESDR=no
    printf '[boot]\nsystemd=true\n' > /etc/wsl.conf
 4. winget install dorssel.usbipd-win ; plug in the dongle
 5. usbipd list  ->  note the RTL2838 busid  ->  usbipd bind --busid <ID>
 6. adsb_supervisor.py attaches the dongle to WSL, runs dump1090, and mirrors
    aircraft.json to C:\LunarTransit\adsb_local. Edit its BUSID to match step 5,
    then register it:
    schtasks /Create /TN LunarADSB /TR "<pythonw> $dst\adsb_supervisor.py" /SC ONLOGON /RL HIGHEST
-----------------------------------------------------------------------------
"@ -ForegroundColor Yellow
}
Write-Host "Done. Dashboard: http://localhost:8080/adsb3d  (also /lunar)" -ForegroundColor Green
Write-Host "SharpCap capture trigger (optional): run sharpcap_listener.py in SharpCap's script console,"
Write-Host "then set capture_enabled true in config.json and restart the task."
