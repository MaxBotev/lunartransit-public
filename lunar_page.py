# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 MaxBotev
"""HTML/JS for the LunarTransit predictor page (served at /lunar)."""

LUNAR_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LUNAR TRANSIT // PREDICTOR</title>
<style>
  :root{
    --bg:#05080f; --panel:#0b121f; --panel2:#0e1726; --line:#16263d;
    --cyan:#23e6ff; --green:#37ffb0; --amber:#ffcc4d; --red:#ff4d6d;
    --moon:#d8dee9; --txt:#cfe6ff; --dim:#5d7a9c;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--txt);
    font:13px/1.4 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;}
  header{display:flex;align-items:center;gap:12px;padding:12px 18px;border-bottom:1px solid var(--line);
    background:linear-gradient(180deg,rgba(35,230,255,.06),transparent);flex-wrap:wrap;}
  .logo{font-weight:700;letter-spacing:3px;font-size:17px;}
  .logo b{color:var(--cyan);}
  .pill{padding:3px 10px;border:1px solid var(--line);border-radius:20px;font-size:11px;color:var(--dim);}
  .pill.live{color:var(--green);border-color:rgba(55,255,176,.4);}
  .pill.warn{color:var(--amber);border-color:rgba(255,204,77,.4);}
  .pill.dead{color:var(--red);border-color:rgba(255,77,109,.4);}
  .pill.hot{color:var(--red);border-color:var(--red);animation:blink 1s infinite;}
  @keyframes blink{50%{opacity:.35}}
  .dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:6px;vertical-align:middle;}
  .dot.g{background:var(--green);box-shadow:0 0 8px var(--green);}
  .dot.a{background:var(--amber);box-shadow:0 0 8px var(--amber);}
  .dot.r{background:var(--red);box-shadow:0 0 8px var(--red);}
  .btn{padding:6px 12px;border:1px solid rgba(35,230,255,.4);border-radius:6px;
    color:var(--cyan);background:var(--panel2);text-decoration:none;cursor:pointer;font:inherit;}
  .btn:hover{background:rgba(35,230,255,.12);}
  .sp{flex:1}
  .wrap{display:grid;grid-template-columns:1fr 340px;gap:14px;padding:14px;}
  .panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;overflow:hidden;margin-bottom:14px;}
  .panel h2{margin:0;padding:9px 14px;font-size:11px;letter-spacing:2px;color:var(--dim);
    border-bottom:1px solid var(--line);text-transform:uppercase;background:var(--panel2);
    display:flex;justify-content:space-between;}
  .panel h2 span{color:var(--cyan);text-transform:none;letter-spacing:0;}
  .panel .body{padding:12px 14px;position:relative;}
  canvas{display:block;width:100%;border-radius:6px;background:#03060c;}
  table{width:100%;border-collapse:collapse;font-size:12px;}
  th{color:var(--dim);text-align:left;font-weight:400;font-size:10px;letter-spacing:1px;
    padding:4px 8px;border-bottom:1px solid var(--line);}
  td{padding:5px 8px;border-bottom:1px dashed var(--line);white-space:nowrap;}
  tr.transit td{color:var(--red);}
  tr.transit td:first-child{font-weight:700;}
  tr.watch td{color:var(--amber);}
  tr.sim td:first-child::after{content:" ⦿SIM";color:var(--cyan);font-size:10px;}
  .stat{display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px dashed var(--line);}
  .stat span:first-child{color:var(--dim);}
  .stat b{color:var(--txt);font-weight:400;}
  .stat b.hi{color:var(--cyan);}
  .events{max-height:260px;overflow:auto;font-size:11px;}
  .ev{display:grid;grid-template-columns:52px 1fr;gap:8px;padding:5px 0;border-bottom:1px dashed var(--line);}
  .ev .t{color:var(--dim);}
  .ev.transit .x{color:var(--red);} .ev.watch .x{color:var(--amber);}
  .ev.capture .x{color:var(--green);} .ev.sim .x{color:var(--cyan);}
  .muted{color:var(--dim);}
  .row{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap;}
  .big{font-size:22px;color:var(--moon);letter-spacing:1px;}
  @media(max-width:980px){.wrap{grid-template-columns:1fr}}
</style>
</head>
<body>
<header>
  <div class="logo">LUNAR<b>▸</b>TRANSIT</div>
  <span id="pillMoon" class="pill">MOON —</span>
  <span id="pillAdsb" class="pill">ADS-B —</span>
  <span id="pillCap" class="pill">CAPTURE —</span>
  <span id="pillHot" class="pill hot" style="display:none">⦿ TRANSIT WINDOW</span>
  <div class="sp"></div>
  <a class="btn" href="/">📡 RF</a>
  <a class="btn" href="/adsb">🗺 ADSB</a>
  <a class="btn" href="/adsb3d">🌐 3D</a>
  <a class="btn" href="/stats">📈 STATS</a>
</header>

<div class="wrap">
  <div>
    <div class="panel">
      <h2>TARGET SCOPE — MOON ±1.6° <span id="scopeInfo"></span></h2>
      <div class="body"><canvas id="scope" width="900" height="560"></canvas></div>
    </div>
    <div class="panel">
      <h2>TRANSIT CANDIDATES <span id="nTracked"></span></h2>
      <div class="body" style="padding:0 0 4px">
        <table>
          <thead><tr>
            <th>FLIGHT</th><th>ALT FT</th><th>GS KT</th><th>AZ / EL</th>
            <th>SEP NOW</th><th>MIN SEP</th><th>TCA</th><th>STATUS</th>
          </tr></thead>
          <tbody id="candRows"><tr><td colspan="8" class="muted">awaiting data…</td></tr></tbody>
        </table>
      </div>
    </div>
  </div>

  <div>
    <div class="panel">
      <h2>MOON</h2>
      <div class="body">
        <div class="big" id="moonBig">—</div>
        <div class="stat"><span>AZIMUTH</span><b id="mAz">—</b></div>
        <div class="stat"><span>ELEVATION</span><b id="mEl">—</b></div>
        <div class="stat"><span>DISTANCE</span><b id="mDist">—</b></div>
        <div class="stat"><span>ANGULAR Ø</span><b id="mDia">—</b></div>
        <div class="stat"><span>ILLUMINATION</span><b id="mIll">—</b></div>
        <div class="stat"><span>TRANSIT THRESHOLD</span><b class="hi" id="mThresh">—</b></div>
        <div style="margin-top:12px;font-size:10px;letter-spacing:2px;color:var(--dim)"
             id="bestHdr">BEST DATES</div>
        <div id="bestDates" class="muted" style="margin-top:4px">—</div>
      </div>
    </div>
    <div class="panel">
      <h2>CAPTURE LINK</h2>
      <div class="body">
        <div class="stat"><span>TARGET</span><b id="cHost">—</b></div>
        <div class="stat"><span>STATE</span><b id="cState">—</b></div>
        <div class="stat"><span>LAST</span><b id="cLast" style="font-size:11px">—</b></div>
        <div class="row">
          <button class="btn" onclick="testCapture()">⚡ TEST LINK</button>
          <button class="btn" onclick="simulate()">🛰 SIMULATE TRANSIT</button>
        </div>
        <div class="muted" id="capMsg" style="margin-top:8px"></div>
      </div>
    </div>
    <div class="panel">
      <h2>EVENT LOG</h2>
      <div class="body events" id="evLog"><span class="muted">no events yet</span></div>
    </div>
  </div>
</div>

<script>
const FOV_W = 1.28, FOV_H = 0.72;      // IMX585 @ 500 mm
const VIEW = 1.6;                       // half-width of scope, degrees
let D = null;

function fmtTca(u, now){
  const dt = Math.round(u - now);
  if (dt < 0) return 'past';
  return 'T-' + (dt >= 60 ? Math.floor(dt/60) + 'm' + String(dt % 60).padStart(2,'0') : dt + 's');
}

function drawScope(){
  const cv = document.getElementById('scope'), ctx = cv.getContext('2d');
  const W = cv.width, H = cv.height;
  ctx.clearRect(0, 0, W, H);
  const sc = W / (2 * VIEW);                 // px per degree
  const cx = W / 2, cy = H / 2;
  const X = d => cx + d * sc, Y = d => cy - d * sc;

  // grid
  ctx.strokeStyle = 'rgba(22,38,61,.9)'; ctx.lineWidth = 1;
  for (let g = -1.5; g <= 1.5; g += 0.5){
    ctx.beginPath(); ctx.moveTo(X(g), 0); ctx.lineTo(X(g), H); ctx.stroke();
    if (Math.abs(g) <= 1){ ctx.beginPath(); ctx.moveTo(0, Y(g)); ctx.lineTo(W, Y(g)); ctx.stroke(); }
  }
  if (!D || !D.moon) return;
  const r = D.moon.radius_deg * sc;

  // FOV rectangle
  ctx.strokeStyle = 'rgba(35,230,255,.5)'; ctx.setLineDash([6,4]);
  ctx.strokeRect(X(-FOV_W/2), Y(FOV_H/2), FOV_W*sc, FOV_H*sc);
  ctx.setLineDash([]);
  ctx.fillStyle = 'rgba(35,230,255,.6)'; ctx.font = '10px ui-monospace';
  ctx.fillText('IMX585 FOV 1.28°×0.72°', X(-FOV_W/2) + 4, Y(FOV_H/2) - 5);

  // watch + transit rings
  ctx.strokeStyle = 'rgba(255,204,77,.35)'; ctx.setLineDash([3,5]);
  ctx.beginPath(); ctx.arc(cx, cy, D.thresholds.watch_deg * sc, 0, 7); ctx.stroke();
  ctx.setLineDash([]);
  ctx.strokeStyle = 'rgba(255,77,109,.6)';
  ctx.beginPath(); ctx.arc(cx, cy, D.thresholds.transit_deg * sc, 0, 7); ctx.stroke();

  // moon disc with terminator hint
  const grad = ctx.createRadialGradient(cx - r*.3, cy - r*.3, r*.2, cx, cy, r);
  grad.addColorStop(0, '#e8edf5'); grad.addColorStop(1, '#9aa7bd');
  ctx.fillStyle = grad;
  ctx.beginPath(); ctx.arc(cx, cy, r, 0, 7); ctx.fill();
  ctx.fillStyle = 'rgba(3,6,12,' + (1 - D.moon.illum) * 0.85 + ')';
  ctx.beginPath(); ctx.arc(cx - r*0.35*(1 - D.moon.illum)*2, cy, r, 0, 7); ctx.fill();
  ctx.strokeStyle = 'rgba(216,222,233,.8)';
  ctx.beginPath(); ctx.arc(cx, cy, r, 0, 7); ctx.stroke();

  // candidate paths + markers
  for (const c of (D.candidates || [])){
    if (!c.path) continue;
    const col = c.transit ? 'rgba(255,77,109,.9)' : c.watch ? 'rgba(255,204,77,.8)' : 'rgba(93,122,156,.6)';
    ctx.strokeStyle = col; ctx.lineWidth = c.transit ? 2 : 1;
    ctx.beginPath();
    let started = false;
    for (const [px, py] of c.path){
      if (Math.abs(px) > VIEW*1.2 || Math.abs(py) > VIEW*1.2){ started = false; continue; }
      if (!started){ ctx.moveTo(X(px), Y(py)); started = true; }
      else ctx.lineTo(X(px), Y(py));
    }
    ctx.stroke();
    const [hx, hy] = c.path[0];
    if (Math.abs(hx) <= VIEW && Math.abs(hy) <= VIEW){
      ctx.fillStyle = col;
      ctx.save(); ctx.translate(X(hx), Y(hy)); ctx.rotate(Math.PI/4);
      ctx.fillRect(-4, -4, 8, 8); ctx.restore();
      ctx.font = '11px ui-monospace';
      ctx.fillText(c.flight, X(hx) + 8, Y(hy) - 6);
    }
  }
  ctx.lineWidth = 1;
}

function render(){
  if (!D) return;
  const m = D.moon;
  const pm = document.getElementById('pillMoon');
  if (m){
    pm.textContent = 'MOON ' + (m.up ? 'UP ' : 'LOW ') + m.el.toFixed(1) + '°';
    pm.className = 'pill ' + (m.up ? 'live' : 'warn');
    document.getElementById('moonBig').textContent =
      '☾ ' + (m.illum*100).toFixed(0) + '% · ' + (m.up ? 'TRACKING' : 'BELOW ' + m.min_elev_deg + '°');
    document.getElementById('mAz').textContent = m.az.toFixed(2) + '°';
    document.getElementById('mEl').textContent = m.el.toFixed(2) + '°';
    document.getElementById('mDist').textContent = m.dist_km.toLocaleString() + ' km';
    document.getElementById('mDia').textContent = (m.radius_deg*2).toFixed(3) + '°';
    document.getElementById('mIll').textContent = (m.illum*100).toFixed(1) + '%';
    document.getElementById('mThresh').textContent = D.thresholds.transit_deg.toFixed(3) + '°';
    const bd = D.best_dates;
    if (bd){
      document.getElementById('bestHdr').textContent = 'BEST DATES — ' + bd.month;
      document.getElementById('bestDates').innerHTML = bd.dates.length
        ? bd.dates.map(d =>
            `<div class="stat"><span>${d.label} ${d.dow}</span>
             <b>☾${(d.illum*100).toFixed(0)}% · max ${d.max_el}° · ${d.from}–${d.to}</b></div>`
          ).join('')
        : '<span class="muted">no good evenings left this month</span>';
    }
  } else { pm.textContent = 'MOON — ' + (D.message || ''); pm.className = 'pill dead'; }

  const pa = document.getElementById('pillAdsb');
  if (D.adsb_age_s != null && D.adsb_age_s < 10){
    pa.innerHTML = '<span class="dot g"></span>ADS-B LIVE · ' + D.n_tracked + ' AC';
    pa.className = 'pill live';
  } else {
    pa.innerHTML = '<span class="dot r"></span>ADS-B STALE';
    pa.className = 'pill dead';
  }

  const cap = D.capture || {};
  const pc = document.getElementById('pillCap');
  if (cap.recording){ pc.textContent = '● REC'; pc.className = 'pill hot'; }
  else if (cap.armed_for){ pc.textContent = 'CAPTURE ARMED: ' + cap.armed_for; pc.className = 'pill warn'; }
  else { pc.textContent = 'CAPTURE ' + (cap.enabled ? 'READY' : 'OFF');
         pc.className = 'pill ' + (cap.enabled ? 'live' : ''); }
  document.getElementById('cHost').textContent = cap.host ? cap.host + ':' + cap.port : 'not configured';
  document.getElementById('cState').textContent =
    cap.recording ? 'RECORDING (stop in ' + cap.stop_in_s + 's)'
    : cap.armed_for ? 'armed — REC in ' + cap.rec_in_s + 's' : 'idle';
  document.getElementById('cLast').textContent = cap.last_result || '—';

  const anyTransit = (D.candidates || []).some(c => c.transit);
  document.getElementById('pillHot').style.display = anyTransit ? '' : 'none';

  document.getElementById('nTracked').textContent = D.n_tracked + ' tracked';
  const rows = (D.candidates || []).slice(0, 14).map(c =>
    `<tr class="${c.transit ? 'transit' : c.watch ? 'watch' : ''}${c.sim ? ' sim' : ''}">
      <td>${c.flight}</td><td>${c.alt_ft.toLocaleString()}</td><td>${c.gs_kt}</td>
      <td>${c.az.toFixed(0)}° / ${c.el.toFixed(1)}°</td>
      <td>${c.sep_now.toFixed(2)}°</td><td>${c.min_sep.toFixed(3)}°</td>
      <td>${fmtTca(c.tca_unix, D.now)}</td>
      <td>${c.transit ? '⦿ TRANSIT' : c.watch ? '◑ NEAR MISS' : '—'}</td>
    </tr>`).join('');
  document.getElementById('candRows').innerHTML =
    rows || '<tr><td colspan="8" class="muted">no aircraft in range</td></tr>';

  const evs = (D.events || []).map(e => {
    const t = new Date(e.t * 1000).toTimeString().slice(0, 8);
    return `<div class="ev ${e.kind}"><span class="t">${t}</span><span class="x">${e.text}</span></div>`;
  }).join('');
  document.getElementById('evLog').innerHTML = evs || '<span class="muted">no events yet</span>';

  drawScope();
}

async function poll(){
  try {
    D = await (await fetch('/api/lunar')).json();
    render();
  } catch(e){}
}
async function testCapture(){
  document.getElementById('capMsg').textContent = 'testing…';
  const r = await (await fetch('/api/lunar/capture-test', {method:'POST'})).json();
  document.getElementById('capMsg').textContent = (r.ok ? '✅ ' : '❌ ') + r.info;
}
async function simulate(){
  const r = await (await fetch('/api/lunar/simulate', {method:'POST'})).json();
  document.getElementById('capMsg').textContent = r.ok ? '🛰 simulation running' : '❌ ' + r.info;
}
poll(); setInterval(poll, 1000);
</script>
</body>
</html>
"""
