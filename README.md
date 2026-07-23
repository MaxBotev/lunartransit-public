# LunarTransit

Predict and capture the moment an **aircraft crosses the disc of the Moon**.

LunarTransit ingests live [ADS-B](https://en.wikipedia.org/wiki/Automatic_Dependent_Surveillance%E2%80%93Broadcast)
aircraft positions, projects each trajectory ~90 s into the future, computes the
topocentric geometry of every plane and the Moon (via the JPL DE421 ephemeris),
and tells you — seconds in advance — when one will transit the lunar disc from
your exact location. It ships a live web dashboard, an optional Telegram alert,
and an optional TCP trigger that starts a high-frame-rate recording in
[SharpCap](https://www.sharpcap.co.uk/) so you never miss the frame.

<!-- Add a screenshot of the /lunar or /adsb3d dashboard here. -->

## What you get

| Route | What it shows |
|-------|---------------|
| `/lunar`  | The predictor: Moon position/phase, tracked aircraft, per-plane closest-approach separation, transit/near-miss alerts, and the best evenings this month. |
| `/adsb3d` | A 3D map of your sky — terrain, aircraft with trails, and the Moon in its true az/el direction. |
| `/api/lunar` | JSON snapshot of the full prediction state (poll this to build your own UI). |

Alerts and capture are **opt-in and off by default**. With no configuration
beyond your location, you get the dashboard and predictions.

## How it works

```
 RTL-SDR dongle ──► dump1090 ──► aircraft.json ─┐
                                                ├──► lunar_server.py ──► /lunar, /adsb3d
        (or a remote Pi feed over HTTP) ────────┘        │
                                                          ├─ predicts transits (Skyfield + DE421)
                                                          ├─ (optional) Telegram alert
                                                          └─ (optional) TCP "REC"/"STOP" to SharpCap
```

You need **an ADS-B source**. Either:

- **A Raspberry Pi with an RTL-SDR dongle** running the whole stack (recommended
  — cheap, always-on, one install script), or
- **A Windows PC** that either reads a Pi's feed over your LAN, or drives its own
  dongle through WSL2.

Both are covered below.

---

## Requirements

- An **RTL2832U / RTL-SDR** USB dongle and a 1090 MHz antenna (any RTL-SDR v3/v4,
  FlightAware/AirNav FlightStick, etc.).
- Python 3.9+ (the installers set up 3.12 on Windows).
- ~20 MB of disk for the DE421 lunar ephemeris (downloaded automatically).
- For automated capture: a camera + [SharpCap](https://www.sharpcap.co.uk/) 4.x
  with scripting enabled.

---

## Install on a Raspberry Pi (recommended)

Tested on a Pi 4B running Raspberry Pi OS / Debian 12 (Bookworm).

```bash
git clone https://github.com/<your-username>/lunartransit-public.git
cd lunartransit-public/install
sudo -v
./install_pi.sh
```

The script:

1. installs build tools and `rtl-sdr`, and blacklists the DVB kernel driver that
   otherwise grabs the dongle;
2. builds FlightAware's `dump1090` and installs it to `/usr/local/bin`;
3. creates a Python venv and installs `Flask`, `numpy`, `skyfield`;
4. downloads the DE421 ephemeris (~17 MB, once);
5. **prompts for your latitude / longitude / altitude** (press Enter to accept
   the SFO default — change it to your real site);
6. writes `~/lunartransit/config.json` (mode `0600`, git-ignored);
7. installs two systemd services — `dump1090` (the receiver) and
   `lunartransit` (the web server) — and starts them at boot.

When it finishes:

```
Dashboard:  http://<pi-hostname>.local:8080/adsb3d   (also /lunar)
Logs:       journalctl -u lunartransit -f
```

Open the dashboard from any device on your network.

---

## Install on Windows

Tested on Windows 11. Run from an **elevated** PowerShell inside the cloned repo:

```powershell
git clone https://github.com/<your-username>/lunartransit-public.git
cd lunartransit-public
Set-ExecutionPolicy -Scope Process Bypass
.\install\install_windows.ps1
```

The script installs Python 3.12 (via winget) and the Python deps, copies the app
to `C:\LunarTransit`, prompts for your **location** and an **ADS-B source**,
downloads the ephemeris, opens the firewall on port 8080, and registers a
scheduled task that runs the server at boot.

Choose your ADS-B source when prompted:

- **[1] Remote Pi feed** — point it at a Pi running this stack, e.g.
  `http://raspberrypi.local:8080/api/adsb/raw`. Nothing else to set up; the
  Windows box just predicts and (optionally) drives capture.
- **[2] Local dongle** — plug the RTL-SDR into the PC. dump1090 doesn't run
  natively on Windows, so the installer prints the WSL2 steps to build it inside
  Ubuntu and forward the USB dongle with `usbipd-win`. `adsb_supervisor.py` then
  keeps the dongle attached and mirrors `aircraft.json` to
  `C:\LunarTransit\adsb_local`.

When it finishes:

```
Dashboard: http://localhost:8080/adsb3d   (also /lunar)
```

---

## Configuration (`config.json`)

`config.json` is created by the installer, lives next to `lunar_server.py`, and
is **git-ignored** — your coordinates and any credentials never leave the
machine. You can also change the site live from the dashboard (📍 SITE control).

| Key | Default | Meaning |
|-----|---------|---------|
| `home_lat` | `37.6213` | Observer latitude (°, +N). Default is SFO — set your own. |
| `home_lon` | `-122.3790` | Observer longitude (°, +E). |
| `home_alt_m` | `4` | Observer altitude (metres). |
| `web_port` | `8080` | Dashboard port. |
| `adsb_source` | Pi URL | A local `aircraft.json` path, or an `http(s)://…/api/adsb/raw` feed. |
| `lunar_min_elev_deg` | `10` | Ignore the Moon below this elevation. |
| `lunar_margin_deg` | `0.10` | Extra margin beyond the Moon's radius counted as a transit. |
| `lunar_watch_deg` | `2.0` | "Near miss" heads-up zone (degrees). |
| `horizon_file` | `horizon.hrz` | Optional local skyline profile (see below). |
| `horizon_margin_deg` | `10` | Lower the effective horizon by this much for alerts. |
| `capture_enabled` | `false` | Enable the SharpCap TCP trigger. |
| `capture_host` / `capture_port` | `127.0.0.1` / `5580` | Where the SharpCap listener runs. |
| `capture_pre_s` / `capture_post_s` | `20` / `20` | Record from T−pre to T+post around closest approach. |
| `telegram_enabled` | `false` | Master switch for Telegram alerts. |
| `telegram_bot_token` | `""` | From [@BotFather](https://t.me/BotFather). **Never commit — it stays in the git-ignored `config.json`.** |
| `telegram_chat_id` | `""` | Your chat id (e.g. via @userinfobot). |

### Local horizon (optional)

`horizon.hrz` describes obstructions on your real skyline as
`azimuth altitude` pairs (NINA custom-horizon format). The shipped file is
**flat (altitude 0 all around)** — an unobstructed horizon. Edit it to your
buildings/trees/hills so the predictor skips transits you physically can't see.

### Telegram alerts (optional)

1. Create a bot with [@BotFather](https://t.me/BotFather) → you get a token.
2. Message your bot once, then read your chat id (e.g. via @userinfobot).
3. Put both in `config.json`, set `telegram_enabled` to `true`, and restart.

If you run **two hosts on one feed** (a Pi and a PC), enable capture and Telegram
on **one host only** to avoid double recordings and duplicate alerts.

### Automated capture with SharpCap (optional)

Run `sharpcap_listener.py` in SharpCap's script console. It listens on TCP
`5580` for `REC` / `STOP` and starts/stops a capture. Set `capture_enabled` to
`true` and point `capture_host`/`capture_port` at it. LunarTransit sends `REC`
at T−`capture_pre_s` and `STOP` at T+`capture_post_s` around each predicted
transit.

---

## Running manually

```bash
python lunar_server.py      # reads ./config.json, serves on web_port
```

---

## Repo layout

```
lunar_server.py     Flask app: routes, ADS-B ingest/enrichment, config API
lunar_transit.py    prediction engine (ECEF/ENU geometry, Skyfield Moon, alerts)
lunar_page.py       /lunar dashboard (HTML/JS)
adsb3d_page.py      /adsb3d 3D map (HTML/JS, three.js)
route_cache.py      aircraft route/registration enrichment (cached)
classify.py         ADS-B contact -> display category
aircraft_types.py   ICAO Doc 8643 type-designator lookup
doc8643.json        ICAO type database
notify.py           Telegram Bot API sender (stdlib only)
horizon.hrz         local horizon profile (flat by default)
requirements.txt    Python dependencies
install/            install_pi.sh, install_windows.ps1
capture-pc/         sharpcap_listener.py, adsb_supervisor.py (Windows helpers)
```

Machine-local files (`config.json`, `de421.bsp`, caches, logs) are git-ignored.

## License

Released under the MIT License — see [LICENSE](LICENSE).

## Acknowledgements

Moon ephemeris via [Skyfield](https://rhodesmill.org/skyfield/) + JPL DE421.
Aircraft data decoded by [dump1090](https://github.com/flightaware/dump1090).
Map tiles from OpenStreetMap / Carto / Esri; Moon texture from NASA SVS.
