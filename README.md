# LunarTransit

Predict and capture the moment an **aircraft crosses the disc of the Moon**.

LunarTransit ingests live [ADS-B](https://en.wikipedia.org/wiki/Automatic_Dependent_Surveillance%E2%80%93Broadcast)
aircraft positions, projects each trajectory ~90 s into the future, computes the
topocentric geometry of every plane and the Moon (via the JPL DE421 ephemeris),
and tells you — seconds in advance — when one will transit the lunar disc from
your exact location. It ships a live web dashboard, an optional Telegram alert,
and an optional TCP trigger that starts a high-frame-rate recording in
[SharpCap](https://www.sharpcap.co.uk/) so you never miss the frame.

![A Boeing airliner silhouetted against the full Moon, rotated so the aircraft
flies horizontally. Landing gear, engine nacelles, wing flaps and the red and
green navigation lights are all visible against the lunar
surface.](docs/img/transit-f0941.jpg)

<p align="center"><em>A real capture: aircraft over the Moon, predicted by this
software and pulled from the raw video automatically.</em></p>

![Animated slow-motion clip of an airliner crossing the full Moon from left to
right.](docs/img/swa3886.gif)

<p align="center"><em>The same thing in motion — a 0.5 s crossing, slowed down.
(<a href="docs/img/swa3886.mov">4K original</a>)</em></p>

---

![The 3D sky view: live aircraft with climb/descent trails over shaded terrain,
and the Moon's true az/el direction drawn as a sight-tube from the observing
site — anything crossing that line is a transit candidate.](docs/img/adsb3d-sightline.png)

<p align="center"><em>Live aircraft over the terrain slab, with the sight-tube
running from the observer to the Moon.</em></p>

![Wide view of the full 100-mile slab tilted toward the horizon, with the Moon
rendered at its true elevation and phase (11% illuminated, 12° elevation).](docs/img/adsb3d-moon-wide.png)

<p align="center"><em>Wide view — the Moon rendered at its true phase and
elevation, with tracked traffic on the slab below.</em></p>

## What you get

| Route | What it shows |
|-------|---------------|
| `/lunar`  | The predictor: Moon position/phase, tracked aircraft, per-plane closest-approach separation, transit/near-miss alerts, and the best evenings this month. |
| `/adsb3d` | A 3D map of your sky — terrain, aircraft with trails, and the Moon in its true az/el direction. |
| `/api/lunar` | JSON snapshot of the full prediction state (poll this to build your own UI). |

Alerts and capture are **opt-in and off by default**. With no configuration
beyond your location, you get the dashboard and predictions.

Click any aircraft for its live detail panel — type, altitude, speed, track, and
the route/operator resolved from its callsign:

![Top-down 3D view with labelled traffic; the selected aircraft's panel shows a
Boeing 737NG at 27,325 ft, 431 kt, tracking 134°, on the Oakland (OAK) to Palm
Springs (PSP) route.](docs/img/adsb3d-detail-panel.png)

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

## SER Convert — the macOS companion app

Planetary cameras record **SER**, an uncompressed astronomy video format. This
repo includes a small native macOS app for working with those captures:
[`ser-convert/`](ser-convert/).

![The Transit tab: a SER file loaded, detected crossings listed with frame
ranges and scores, and a preview of the aircraft silhouette rotated 83 degrees
so it flies horizontally.](docs/img/serconvert-transit-tab.png)

**It finds the transit for you.** A silhouetted aircraft is far darker than even
the darkest maria, so the app scans for near-black pixels *inside* the lunar
disc. On a real capture the count went from a baseline of ~40 to **28155** at
mid-transit — a 700× signal. Scanning a 15 GB, 1905-frame file takes about
**8 seconds**, and the matching frames are exported as lossless PNGs straight
from the SER. A rotation slider straightens the crossing first, since transits
often run vertically down the frame.

![The Convert tab: a SER file converting to ProRes with a progress bar showing
frame 147 of 1905.](docs/img/serconvert-convert-tab.png)

It also batch-converts SER to ProRes or lossless FFV1 — reading the **true frame
rate** from the SER timestamp trailer. SER has no frame-rate field, so tools
default to 25 fps and captures play at the wrong speed; the file above was
actually 45.685 fps. Bayer captures are demosaiced automatically, and ffmpeg is
installed for you if it's missing.

Build it with `cd ser-convert && ./build.sh` — needs only the Xcode Command Line
Tools, no Xcode project. See [`ser-convert/README.md`](ser-convert/README.md).

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
git clone https://github.com/MaxBotev/lunartransit-public.git
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
git clone https://github.com/MaxBotev/lunartransit-public.git
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
| `adsb_source_fallbacks` | `[]` | Extra sources tried in order if the primary fails — see [Surviving DHCP](#surviving-dhcp-use-hostnames-not-ips). |
| `lunar_min_elev_deg` | `10` | Ignore the Moon below this elevation. |
| `lunar_margin_deg` | `0.10` | Extra margin beyond the Moon's radius counted as a transit. |
| `lunar_watch_deg` | `2.0` | "Near miss" heads-up zone (degrees). |
| `horizon_file` | `horizon.hrz` | Optional local skyline profile (see below). |
| `horizon_margin_deg` | `10` | Lower the effective horizon by this much for alerts. |
| `horizon_blocks_alerts` | `true` | Set `false` to keep drawing/reporting the skyline but stop it suppressing alerts and capture. |
| `capture_enabled` | `false` | Enable the SharpCap TCP trigger. |
| `capture_host` / `capture_port` | `127.0.0.1` / `5580` | Where the SharpCap listener runs. |
| `capture_pre_s` / `capture_post_s` | `20` / `20` | Record from T−pre to T+post around closest approach. |
| `uncertainty_alerts` | `true` | Announce transits that fall inside the per-target error bar as POSSIBLE. |
| `uncertainty_sigmas` | `2.0` | Width of that error bar in sigma (≈95%). |
| `capture_on_possible` | `true` | Arm the recorder for POSSIBLE transits too, not just confident ones. |
| `adsb_pos_sigma_m` / `lag_sigma_s` / `site_alt_sigma_m` | `20` / `0.25` / `10` | Error-budget inputs. |
| `manual_capture_max_s` | `300` | Safety auto-stop for a manual recording. |
| `meridian_warn_min` | `20` | Warn this many minutes before the Moon reaches the meridian (0 disables). |
| `auto_flip` | `false` | Perform the meridian flip automatically. **Commands real mount motion.** |
| `flip_after_min` / `flip_before_limit_min` | `2` / `12` | Flip window, in minutes **past** the meridian. |
| `flip_slew_s` | `90` | Expected slew time — the Moon's lead is computed from it. |
| `centre_tol_arcsec` / `centre_max_iter` | `90` / `6` | Closed-loop centring tolerance and iteration cap. |
| `min_illum_for_centring` | `0.35` | Below this the lit centroid isn't a reliable disc centre; the flip is skipped. |
| `telegram_enabled` | `false` | Master switch for Telegram alerts. |
| `telegram_bot_token` | `""` | From [@BotFather](https://t.me/BotFather). **Never commit — it stays in the git-ignored `config.json`.** |
| `telegram_chat_id` | `""` | Your chat id (e.g. via @userinfobot). |

### Two-host setups

With the dongle on an always-on Pi and the camera on a capture PC, there are two
sensible arrangements. **Whichever you pick, exactly one host must own capture
and Telegram** — otherwise you get double recordings and duplicate alerts.

**A. Pi-only server (simplest).** The Pi does everything; the PC runs nothing but
SharpCap and the listener script. Best if you want the capture PC quiet, or it's
busy driving the camera:

```
   Pi (dongle + engine + alerts)           Windows PC (SharpCap only)
   ├ adsb_source: local aircraft.json      └ sharpcap_listener.py in SharpCap's
   ├ telegram_enabled: true                   script console (listens on :5580)
   ├ capture_enabled: true
   └ capture_host: <pc>.local  ─────────►  REC / STOP over TCP
```

The PC needs an inbound firewall rule for TCP 5580:

```powershell
netsh advfirewall firewall add rule name="LunarTransit trigger" dir=in action=allow protocol=TCP localport=5580
```

*Note:* with the listener not running, a connection test to 5580 **times out**
rather than being refused — Windows Firewall silently drops packets to closed
ports. That's normal and doesn't mean the path is broken.

**B. Split roles.** The PC runs its own server against the Pi's feed and triggers
capture locally, so the REC command never crosses the network:

```
   Pi (dongle + dump1090)                  Windows PC (server + camera)
   ├ serves /api/adsb/raw  ────────────►   ├ adsb_source: http://<pi>:8080/api/adsb/raw
   ├ capture_enabled: false                ├ capture_enabled: true
   └ telegram_enabled: true                └ capture_host: 127.0.0.1
```

### Surviving DHCP: use hostnames, not IPs

If your router hands out addresses by DHCP, a reboot can renumber either
machine and a hard-coded IP in `adsb_source` will silently stop working. Use
the hostname instead, and list alternates as fallbacks:

```json
{
  "adsb_source": "http://sky.local:8080/api/adsb/raw",
  "adsb_source_fallbacks": [
    "http://sky:8080/api/adsb/raw",
    "http://192.168.1.42:8080/api/adsb/raw"
  ]
}
```

They're tried in order and the one that answers is remembered, so the normal
case costs a single request. `.local` names are mDNS: they work out of the box
on the Pi (Avahi) and on Windows 10/11, which resolves `.local` natively. The
bare hostname (`http://sky:8080/…`) is a useful second string via NetBIOS/DNS
suffix, and a literal IP makes a reasonable last resort.

Check resolution from either machine with `ping sky.local`.

### Fixing a mount that misses the Moon

A GoTo that lands degrees away usually is not a targeting bug. Measured on a
ZWO AM5N: the requested coordinates were **0.111°** from the true Moon — correct
— while the mount *reported* being **2.80°** away with the Moon plainly visible
in both cameras. The clock was fine (29 s, verified against true sidereal time).
The mount's coordinate frame was simply rotated ~2.7° from the sky, constant
over 40 minutes of tracking, so it failed identically every night.

The fix is a **pointing sync**, not a wider search field — a 2.8° error exceeds
the half-field of a typical guide scope too, so more FOV does not help.

Centre the Moon and press **🎯 SYNC MOUNT TO MOON** on `/lunar` (or
`POST /api/lunar/sync-moon`). It measures where the lunar disc actually sits in
the frame, **refuses if it is more than 8 arcmin off centre** — syncing
off-target would replace one pointing error with a larger one — then syncs to
the Moon's apparent position from the ephemeris and reports how far the model
moved. After that a GoTo lands within the residual polar-misalignment error
(~6 arcmin measured here), well inside a typical planetary-camera frame.

### Equatorial mounts: the meridian will bite you

A German-equatorial mount stops tracking at its meridian limit and then
**refuses to restart until it is flipped**. For lunar work that limit sits in
the worst possible place: the Moon is highest — best seeing, best transit
geometry — exactly where the mount wants to stop.

Observed on a ZWO AM5N: tracking cut out ~4° (16 min) past the meridian with

```
[WRN] Tracking: It might hit the tripod legs after meridian flip so tracking has been stopped
```

and every attempt to re-enable tracking logged `Tracking Set: Fail` until the
pier side flipped.

LunarTransit therefore warns (Telegram + event log + a MERIDIAN countdown on
`/lunar`) `meridian_warn_min` minutes before the crossing, so you can flip while
it is cheap. `/api/lunar` exposes `moon.ha_h` and `moon.to_meridian_min` if you
want to drive your own automation from it.

LunarTransit can also do the flip itself (`auto_flip`), which is the autonomous
path: it stops the capture, slews across the meridian to where the Moon **will
be** when the slew finishes, verifies the pier side actually changed, restores
lunar-rate tracking, and then **closes the loop on the Moon's own disc** until
it is centred.

That last step matters. With imperfect polar alignment the pointing error
depends on pier side, so it reverses across a flip and roughly doubles — a
blind GOTO back to the same coordinates does *not* land on the same spot. Plate
solving cannot help either, since a lunar exposure shows no stars. Using the
Moon as its own reference sidesteps all of it, and the same routine gives you a
reliable "centre the Moon" at any time.

The flip runs **after** the crossing, not before: while the target is still
east of the meridian a GOTO's natural destination is the side the mount is
already on, so there is nothing to flip to. The usable window is from the
crossing to the mount's own limit — measured at ~16–18 min on an AM5N, hence a
2–12 min default.

Pier side is verified two ways. If the driver implements
`DestinationSideOfPier`, that is used. Many do not (the ASI AM5N throws
`not implemented`, and reports `CanSetPierSide=False`), so it falls back to
geometry: a German equatorial sits **west** of the pier while the target is
east of the meridian, and **east** of it afterwards. The post-flip check that
`SideOfPier` actually changed always applies — that property *is* implemented.

Pier-side strings are normalised, because drivers spell the same enum at least
three ways: ASCOM's `pierWest`/`pierEast`, plain `West`/`East`, and the
*pointing state* names `ThroughThePole`/`Normal` (what the AM5N reports, and
what the spec's own wording describes). An exact compare against `pierWest`
silently never matches on such a driver, which would make every flip
unverifiable.

Measured on a ZWO AM5N with a 472 mm scope: centring converged from 5.6 arcmin
to **2 arcsec in two passes, 16 s each**. The mount under-delivers commanded
slews by about 10%, so a single correction leaves ~20% residual — hence a
40 arcsec tolerance, which forces the second pass that closes it.

Two calibrations happen automatically: **plate scale** comes from the Moon's
known angular diameter versus its measured pixel diameter (no focal length or
pixel size needed), and **camera rotation** from two small probe slews.

It refuses rather than guesses: if the mount says a GOTO would not change pier
side, if the pier side does not actually change, if the disc cannot be found,
or if the Moon is too thin a crescent for its lit centroid to be a trustworthy
centre.

SharpCap can perform the flip itself — the sequencer has a *Meridian Flip the
mount* step — but it is marked **experimental** and does not handle
mount-specific quirks, including the "stuck past the meridian" state this very
mount gets into. Flipping *before* the limit is far more reliable than trying to
recover after it.

### Local horizon (optional)

`horizon.hrz` describes obstructions on your real skyline as
`azimuth altitude` pairs (NINA custom-horizon format). The shipped file is
**flat (altitude 0 all around)** — an unobstructed horizon. Edit it to your
buildings/trees/hills so the predictor skips transits you physically can't see.

⚠️ **A horizon profile silences alerts.** When the Moon sits behind it, the
engine reports `moon behind local obstruction` and fires nothing — no Telegram,
no capture, no logged event. A profile that is too aggressive (or measured from
a different spot than you actually shoot from) will therefore look exactly like
"no transits are happening". If alerts go quiet after you add one, check
`/api/lunar` → `message` first.

Set `horizon_blocks_alerts` to `false` to keep the profile drawn on the
dashboard and keep `behind_horizon` reported, while letting alerts and capture
fire anyway — useful when you can step around the obstruction, or would rather
be told and judge for yourself. `lunar_min_elev_deg` still applies either way.

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

## Prediction accuracy

The lunar disc is only **0.52° across**, so small pointing errors decide whether
a transit is called at all. Two effects dominate, and both are handled:

- **Report latency.** An ADS-B position is already 1–2 s old when it arrives.
  At 250 m/s that is 250–500 m of along-track error — about 1–2° at 15 km slant
  range, i.e. *several lunar diameters*. Every contact is therefore advanced by
  its own measured lag (file age + `seen_pos`) before projection.
- **Sampling resolution.** Trajectories are sampled at 1 s, but a plane at 8 km
  crosses the entire disc in 0.29 s. Taking the smallest sample as the closest
  approach overestimates it by up to `ω·Δt/2`, which turns real hits into
  apparent misses. Because `sep²` is exactly parabolic in time for a straight
  pass, the true minimum and its timing are recovered by fitting a parabola to
  the three samples around the minimum — exact to <0.001° with no extra compute.

Remaining error sources are an order of magnitude smaller: barometric altitude
for contacts without `alt_geom` (flagged as low-confidence in alerts), and
dead-reckoning curvature (~0.01° at the moment of decision). Both sit inside the
default 0.10° margin.

### Range decides what is knowable

Every remaining error scales as 1/range, so the same setup that resolves a
transit cleanly at 30 km cannot resolve one at 2 km:

| slant range | typical target | angular rate | 1σ pointing error |
|---|---|---|---|
| 40 km | cruise, 35 kft | 0.1 °/s | ~0.08° |
| 12 km | mid-level, 10 kft | 0.4 °/s | ~0.18° |
| 2 km | on approach, 2.7 kft | 2.8 °/s | **~0.82°** |

The lunar disc is 0.52° across, so at 2 km the error bar is bigger than the
Moon: ADS-B simply cannot say whether a low, close aircraft will cross it.
Rather than report a confident miss, the engine computes a per-target error bar
from range, angular rate and report lag, and announces anything that could be a
transit as **POSSIBLE — min sep 1.5° ± 0.8°**. Tune with `uncertainty_sigmas`
(default 2, ≈95%), or set `uncertainty_alerts` false for strict behaviour.

⚠️ **`home_alt_m` must be ellipsoidal height (HAE), not MSL.** ADS-B `alt_geom`
is referenced to the WGS-84 ellipsoid, so the observer must be too. Map and
phone elevations are orthometric/MSL; in the SF Bay Area the geoid sits ~32 m
*below* the ellipsoid, so an MSL figure entered directly puts the observer 32 m
too high — harmless at cruise, but **0.87° of systematic error at 2 km**, in the
same direction every time. Subtract your local geoid undulation before entering
it.

Predictions are suspended entirely if the ADS-B feed goes stale (>15 s), so a
dead receiver can't arm a capture on frozen positions.

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

Copyright (C) 2026 MaxBotev.

Licensed under the **GNU Affero General Public License v3.0** (AGPL-3.0) — see
[LICENSE](LICENSE). This is a strong copyleft license: you may use, modify, and
redistribute this software, but any derivative you distribute **or run as a
network service** must be released in full source under the same license. It
cannot be incorporated into closed-source or proprietary products.

    LunarTransit — aircraft/Moon transit predictor
    Copyright (C) 2026 MaxBotev

    This program is free software: you can redistribute it and/or modify it
    under the terms of the GNU Affero General Public License as published by
    the Free Software Foundation, either version 3 of the License, or (at your
    option) any later version.

    This program is distributed in the hope that it will be useful, but
    WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY
    or FITNESS FOR A PARTICULAR PURPOSE. See the GNU Affero General Public
    License for more details.

    You should have received a copy of the GNU Affero General Public License
    along with this program. If not, see <https://www.gnu.org/licenses/>.

## Acknowledgements

Moon ephemeris via [Skyfield](https://rhodesmill.org/skyfield/) + JPL DE421.
Aircraft data decoded by [dump1090](https://github.com/flightaware/dump1090).
Map tiles from OpenStreetMap / Carto / Esri; Moon texture from NASA SVS.
