#!/usr/bin/env bash
# LunarTransit — Raspberry Pi installer.
#
# Installs the LunarTransit server (web dashboard + Moon-transit prediction) and
# a dump1090 ADS-B receiver feeding it from a local RTL-SDR dongle. Tested on a
# Pi 4B (Bookworm/Debian 12). Works with any RTL2832U dongle (RTL-SDR v3/v4,
# FlightStick, ...).
#
#   git clone https://github.com/MaxBotev/lunartransit-public
#   cd lunartransit-public/install && sudo -v && ./install_pi.sh
set -euo pipefail

APP_DIR="$HOME/lunartransit"
VENV="$HOME/lunartransit-venv"
JSON_DIR="/run/dump1090"
SRC="$(cd "$(dirname "$0")/.." && pwd)"

echo ">> apt dependencies"
sudo apt-get update -qq
sudo apt-get install -y -qq build-essential git pkg-config libusb-1.0-0-dev \
  librtlsdr-dev libncurses-dev zlib1g-dev python3-venv rtl-sdr

echo ">> blacklisting the DVB kernel driver (it grabs the dongle otherwise)"
echo "blacklist dvb_usb_rtl28xxu" | sudo tee /etc/modprobe.d/blacklist-rtlsdr.conf >/dev/null
sudo modprobe -r dvb_usb_rtl28xxu 2>/dev/null || true

if [ ! -x /usr/local/bin/dump1090 ]; then
  echo ">> building dump1090 (FlightAware)"
  tmp=$(mktemp -d)
  git clone -q --depth 1 https://github.com/flightaware/dump1090 "$tmp"
  make -C "$tmp" -j4 RTLSDR=yes BLADERF=no HACKRF=no LIMESDR=no >/dev/null
  sudo install "$tmp/dump1090" /usr/local/bin/dump1090
  rm -rf "$tmp"
fi

echo ">> application files -> $APP_DIR"
mkdir -p "$APP_DIR"
cp "$SRC"/*.py "$SRC"/doc8643.json "$SRC"/requirements.txt "$SRC"/horizon.hrz "$APP_DIR/"

echo ">> python venv + deps (numpy/skyfield take a few minutes on a Pi)"
[ -d "$VENV" ] || python3 -m venv --system-site-packages "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -r "$APP_DIR/requirements.txt"

echo ">> lunar ephemeris (de421.bsp, ~17 MB, one time)"
[ -f "$APP_DIR/de421.bsp" ] || \
  "$VENV/bin/python" -c "from skyfield.api import Loader; Loader('$APP_DIR')('de421.bsp')"

# ---- observer location -----------------------------------------------------
echo
echo "Enter your observing site (press Enter to accept the SFO default)."
read -rp "  Latitude  (deg, +N) [37.6213]: "  LAT;  LAT=${LAT:-37.6213}
read -rp "  Longitude (deg, +E) [-122.3790]: " LON; LON=${LON:--122.3790}
read -rp "  Altitude  (m)       [4]: "        ALT; ALT=${ALT:-4}

cat > "$APP_DIR/config.json" <<EOF
{
  "home_lat": $LAT,
  "home_lon": $LON,
  "home_alt_m": $ALT,
  "web_port": 8080,
  "adsb_source": "$JSON_DIR/aircraft.json",
  "lunar_enabled": true,
  "lunar_min_elev_deg": 10.0,
  "capture_enabled": false,
  "capture_host": "",
  "telegram_enabled": false,
  "telegram_bot_token": "",
  "telegram_chat_id": "",
  "horizon_file": "horizon.hrz"
}
EOF
chmod 600 "$APP_DIR/config.json"

# ---- dump1090 receiver service ---------------------------------------------
echo ">> dump1090 systemd service (writes aircraft.json to $JSON_DIR)"
sudo tee /etc/systemd/system/dump1090.service >/dev/null <<EOF
[Unit]
Description=dump1090 ADS-B receiver
After=network.target
[Service]
ExecStartPre=/bin/mkdir -p $JSON_DIR
ExecStart=/usr/local/bin/dump1090 --device-index 0 --lat $LAT --lon $LON \\
  --write-json $JSON_DIR --write-json-every 1 --quiet
Restart=on-failure
RestartSec=10
[Install]
WantedBy=multi-user.target
EOF

# ---- lunartransit web service ----------------------------------------------
echo ">> lunartransit systemd service"
sudo tee /etc/systemd/system/lunartransit.service >/dev/null <<EOF
[Unit]
Description=LunarTransit predictor + dashboard
After=network.target dump1090.service
[Service]
User=$USER
WorkingDirectory=$APP_DIR
ExecStart=$VENV/bin/python3 $APP_DIR/lunar_server.py
Restart=on-failure
RestartSec=10
[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now dump1090 lunartransit

echo
echo "Done."
echo "  Dashboard:  http://$(hostname).local:8080/adsb3d   (also /lunar)"
echo "  Logs:       journalctl -u lunartransit -f"
echo "  Telegram (optional): add bot token + chat id to $APP_DIR/config.json,"
echo "                       set telegram_enabled true, then: systemctl restart lunartransit"
