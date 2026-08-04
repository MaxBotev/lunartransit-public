# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 MaxBotev
"""The /sites page: score a candidate observing site, or hunt for a better one.

Everything here is on demand. The scoring walks the Moon's path across a month
against every recorded traffic bin, which is far too heavy to run during a
session, so it happens when this page asks for it and not before.
"""

SITES_HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Site finder — LunarTransit</title>
<style>
 *{box-sizing:border-box}
 body{margin:0;background:#050b16;color:#dbeafe;
      font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}
 a{color:#7fd4ff;text-decoration:none}
 header{display:flex;align-items:center;gap:16px;padding:14px 18px;
        border-bottom:1px solid #14283f;background:#081120;flex-wrap:wrap}
 header h1{margin:0;font-size:14px;letter-spacing:.18em;color:#4fc3f7;
           text-transform:uppercase;font-weight:600}
 header nav{display:flex;gap:14px;font-size:12px;letter-spacing:.08em;
            text-transform:uppercase}
 .wrap{max-width:1100px;margin:0 auto;padding:18px}
 .panel{background:#0b1220;border:1px solid #1e3a5f;border-radius:10px;
        padding:16px;margin-bottom:16px}
 h2{margin:0 0 12px;font-size:13px;letter-spacing:.14em;color:#4fc3f7;
    text-transform:uppercase}
 label{display:block;font-size:11px;color:#7fa8c9;margin:8px 0 3px;
       letter-spacing:.08em;text-transform:uppercase}
 input{background:#050b16;border:1px solid #1e3a5f;color:#dbeafe;padding:7px 9px;
       border-radius:6px;font:inherit;width:150px}
 .row{display:flex;gap:14px;flex-wrap:wrap;align-items:flex-end}
 .btn{background:#0d2137;border:1px solid #2b6fa8;color:#7fd4ff;padding:9px 16px;
      border-radius:6px;cursor:pointer;font:inherit;letter-spacing:.06em}
 .btn:hover{background:#123152} .btn:disabled{opacity:.45;cursor:default}
 .big{font-size:34px;color:#7fd4ff;font-weight:600;line-height:1}
 .muted{color:#6b8ba8;font-size:12px}
 table{width:100%;border-collapse:collapse;font-size:12px;margin-top:8px}
 th,td{text-align:left;padding:5px 8px;border-bottom:1px solid #14283f}
 th{color:#4fc3f7;font-weight:500;letter-spacing:.08em;font-size:10px;
    text-transform:uppercase}
 #grid{display:grid;gap:2px;margin-top:10px}
 .cell{aspect-ratio:1;border-radius:3px;position:relative;cursor:pointer}
 .cell.best{outline:2px solid #ffd166;outline-offset:1px}
 .cell.here{outline:2px dashed #7fd4ff;outline-offset:1px}
 .legend{display:flex;align-items:center;gap:8px;margin-top:10px;font-size:11px;
         color:#6b8ba8}
 .bar{height:10px;width:180px;border-radius:5px;
      background:linear-gradient(90deg,#0d2137,#1b5e8f,#31a8c9,#7fe0a8,#ffd166)}
 select{background:#050b16;border:1px solid #1e3a5f;color:#dbeafe;padding:7px 9px;
        border-radius:6px;font:inherit}
 #sky{width:100%;height:520px;border-radius:8px;border:1px solid #14283f;
      background:#03070e;display:block;margin-top:10px;touch-action:none}
 .hint{font-size:11px;color:#5b7a96;margin-top:6px}
</style></head><body>
<header>
 <h1>◎ Site finder</h1>
 <nav><a href="/lunar">Lunar</a><a href="/adsb3d">3D map</a></nav>
</header>
<div class="wrap">
 <div class="panel">
  <h2>Traffic database</h2>
  <div id="dbstat" class="muted">loading…</div>
 </div>

 <div class="panel">
  <h2>Score a site</h2>
  <div class="row">
   <div><label>Latitude</label><input id="lat" value="__LAT__"></div>
   <div><label>Longitude</label><input id="lon" value="__LON__"></div>
   <div><label>Altitude m</label><input id="alt" value="30" style="width:90px"></div>
   <div><label>Days ahead</label><input id="days" value="30" style="width:90px"></div>
   <div><label>Min Moon el°</label><input id="minel" value="15" style="width:90px"></div>
   <button class="btn" id="scoreBtn" onclick="scoreSite()">SCORE THIS SITE</button>
   <button class="btn" id="hereBtn" onclick="useHome()">USE MY SITE</button>
  </div>
  <div id="result" style="margin-top:16px"></div>
 </div>

 <div class="panel">
  <h2>The sky from here</h2>
  <div class="row">
   <div><label>Time of day</label><select id="tod"></select></div>
   <div><label>Days</label><select id="dow">
     <option value="">All days</option><option value="0">Weekdays</option>
     <option value="1">Weekends</option></select></div>
   <div><label>Height ×<span id="exv">6</span></label>
     <input id="ex" type="range" min="1" max="20" value="6" style="width:130px"></div>
   <button class="btn" id="skyBtn" onclick="drawSky()">SHOW TRAFFIC</button>
  </div>
  <canvas id="sky"></canvas>
  <div class="hint">Drag to rotate · wheel to zoom · each dot is recorded traffic,
   brighter means busier · the gold arc is the Moon's path from these coordinates
   over the next <span id="skyDays">30</span> days. Corridors that pierce the arc
   are the ones that make transits.</div>
  <div id="skyinfo" class="muted" style="margin-top:6px"></div>
 </div>

 <div class="panel">
  <h2>Where is better?</h2>
  <div class="row">
   <div><label>Search radius °</label><input id="half" value="0.12" style="width:110px"></div>
   <div><label>Grid</label><input id="nside" value="9" style="width:70px"></div>
   <button class="btn" id="mapBtn" onclick="findHotspots()">MAP THE AREA</button>
  </div>
  <div class="muted" style="margin-top:6px">
   Scores every point on a grid around the coordinates above. 0.12° is about
   13 km north–south. Coarse on purpose — it answers “which way”, not “which field”.
  </div>
  <div id="grid"></div>
  <div id="gridinfo" class="muted" style="margin-top:8px"></div>
 </div>
</div>
<script>
let HOME = null;
async function dbstat(){
  try{
    const r = await (await fetch('/api/sites/stats')).json();
    HOME = r.home || null;
    if(HOME && !document.getElementById('lat').value){
      document.getElementById('lat').value = HOME.lat.toFixed(4);
      document.getElementById('lon').value = HOME.lon.toFixed(4);
    }
    document.getElementById('dbstat').innerHTML = r.enabled
      ? `<b style="color:#7fd4ff">${(r.rows||0).toLocaleString()}</b> bins ·
         <b style="color:#7fd4ff">${(r.observations||0).toLocaleString()}</b> observations ·
         ${((r.bytes||0)/1e6).toFixed(1)} MB · keeping ${r.retain_days} days
         ${r.rows<5000?'<br><span style="color:#ffb86b">Still filling up — scores need a night or two of data to mean anything.</span>':''}`
      : 'Traffic recording is <b>off</b> — set traffic_log to true.';
  }catch(e){ document.getElementById('dbstat').textContent = 'stats failed: '+e; }
}
function useHome(){
  if(!HOME) return;
  document.getElementById('lat').value = HOME.lat.toFixed(4);
  document.getElementById('lon').value = HOME.lon.toFixed(4);
}
function body(){
  return {lat:+document.getElementById('lat').value,
          lon:+document.getElementById('lon').value,
          alt_m:+document.getElementById('alt').value,
          days:+document.getElementById('days').value,
          min_elev:+document.getElementById('minel').value};
}
async function scoreSite(){
  const b=document.getElementById('scoreBtn'), out=document.getElementById('result');
  b.disabled=true; b.textContent='SCORING…'; out.innerHTML='<span class="muted">walking the Moon’s path against every recorded bin…</span>';
  try{
    const r = await (await fetch('/api/sites/score',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(body())})).json();
    if(!r.ok){ out.innerHTML='<span style="color:#ff8080">'+r.info+'</span>'; return; }
    let t='';
    if(r.top_directions && r.top_directions.length){
      t='<table><tr><th>Azimuth</th><th>Elev</th><th>Range</th><th>Weight</th></tr>'
        + r.top_directions.map(d=>`<tr><td>${d.az}°</td><td>${d.el}°</td>
            <td>${d.range_km} km</td><td>${d.weight}</td></tr>`).join('')+'</table>';
    }
    out.innerHTML = `<div class="big">${r.score_per_hour}</div>
      <div class="muted">expected transits per hour of Moon above ${r.min_elev_deg}°,
      averaged over the next ${r.days} days (${r.moon_hours} h of usable Moon)</div>
      <div class="muted" style="margin-top:8px">${r.bins_on_path} of ${r.bins_total}
      recorded traffic bins fall on the Moon’s path from here.</div>
      ${t?'<div style="margin-top:14px"><b class="muted">Busiest directions on the path</b>'+t+'</div>':''}`;
  }catch(e){ out.innerHTML='<span style="color:#ff8080">failed: '+e+'</span>'; }
  finally{ b.disabled=false; b.textContent='SCORE THIS SITE'; }
}
function colour(v,lo,hi){
  const f = hi>lo ? (v-lo)/(hi-lo) : 0.5;
  const stops=[[13,33,55],[27,94,143],[49,168,201],[127,224,168],[255,209,102]];
  const x=f*(stops.length-1), i=Math.min(stops.length-2,Math.floor(x)), k=x-i;
  const c=stops[i].map((s,j)=>Math.round(s+(stops[i+1][j]-s)*k));
  return `rgb(${c[0]},${c[1]},${c[2]})`;
}
async function findHotspots(){
  const b=document.getElementById('mapBtn'), g=document.getElementById('grid'),
        info=document.getElementById('gridinfo');
  b.disabled=true; b.textContent='MAPPING…'; g.innerHTML=''; info.textContent='scoring the grid — this takes a moment…';
  try{
    const p = Object.assign(body(), {half_deg:+document.getElementById('half').value,
                                     n_side:+document.getElementById('nside').value});
    const r = await (await fetch('/api/sites/hotspots',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(p)})).json();
    if(!r.ok){ info.innerHTML='<span style="color:#ff8080">'+r.info+'</span>'; return; }
    const n=r.n_side;
    g.style.gridTemplateColumns=`repeat(${n},1fr)`;
    // north at the top: rows run from the highest latitude down
    const byRow={}; r.cells.forEach(c=>{ (byRow[c.lat]=byRow[c.lat]||[]).push(c); });
    const lats=Object.keys(byRow).map(Number).sort((a,b)=>b-a);
    lats.forEach(la=>{
      byRow[la].sort((a,b)=>a.lon-b.lon).forEach(c=>{
        const d=document.createElement('div');
        d.className='cell';
        d.style.background=colour(c.score,r.min,r.max);
        d.title=`${c.lat.toFixed(4)}, ${c.lon.toFixed(4)}\n${c.score} per hour`;
        if(c.lat===r.best.lat && c.lon===r.best.lon) d.className+=' best';
        if(Math.abs(c.lat-r.centre.lat)<1e-6 && Math.abs(c.lon-r.centre.lon)<1e-6)
          d.className+=' here';
        d.onclick=()=>{ document.getElementById('lat').value=c.lat.toFixed(5);
                        document.getElementById('lon').value=c.lon.toFixed(5);
                        scoreSite(); };
        g.appendChild(d);
      });
    });
    const gain = r.centre_score ? (r.best.score/r.centre_score) : null;
    info.innerHTML = `<div class="legend"><span>${r.min}</span><div class="bar"></div>
      <span>${r.max}</span><span style="margin-left:14px">◻ dashed = your coordinates ·
      ◻ gold = best</span></div>
      <div style="margin-top:8px">Best: <b style="color:#ffd166">${r.best.lat.toFixed(5)},
      ${r.best.lon.toFixed(5)}</b> at <b>${r.best.score}</b> per hour${
      gain?` — <b>${gain.toFixed(1)}×</b> your current spot`:''}.
      Click any cell to score it properly.</div>`;
  }catch(e){ info.innerHTML='<span style="color:#ff8080">failed: '+e+'</span>'; }
  finally{ b.disabled=false; b.textContent='MAP THE AREA'; }
}
// ---------- 3D sky view ----------
// Hand-rolled rather than three.js: this is a static point cloud plus one
// polyline, so a projection and a painter's-algorithm sort is the whole job,
// and it keeps the page dependency-free.
let SKY = null, rotX = -0.5, rotZ = 0.6, zoom = 1.0, drag = null;
const cv = document.getElementById('sky');
function fitCanvas(){
  const r = cv.getBoundingClientRect(), d = window.devicePixelRatio || 1;
  cv.width = r.width * d; cv.height = r.height * d;
  return d;
}
function project(p, cx, cy, sc, ex){
  // rotate about Z (compass) then X (tilt); altitude exaggerated so a 12 km
  // ceiling is visible against an 80 km footprint
  const cz = Math.cos(rotZ), sz = Math.sin(rotZ);
  let x = p[0]*cz - p[1]*sz, y = p[0]*sz + p[1]*cz, z = p[2]*ex;
  const cx2 = Math.cos(rotX), sx2 = Math.sin(rotX);
  const y2 = y*cx2 - z*sx2, z2 = y*sx2 + z*cx2;
  return [cx + x*sc, cy - z2*sc, y2];
}
function renderSky(){
  if(!SKY) return;
  const d = fitCanvas(), g = cv.getContext('2d');
  const W = cv.width, H = cv.height;
  g.clearRect(0,0,W,H);
  const ex = +document.getElementById('ex').value;
  const sc = zoom * Math.min(W,H) / (2.2 * SKY.extent);
  const cx = W/2, cy = H*0.60;

  // ground rings every 20 km, for scale
  g.strokeStyle = '#132a44'; g.lineWidth = 1*d;
  for(let r=20000; r<=SKY.extent; r+=20000){
    g.beginPath();
    for(let a=0; a<=64; a++){
      const t = a/64*Math.PI*2;
      const q = project([r*Math.sin(t), r*Math.cos(t), 0], cx, cy, sc, ex);
      a ? g.lineTo(q[0],q[1]) : g.moveTo(q[0],q[1]);
    }
    g.stroke();
  }
  // north marker
  const npt = project([0, SKY.extent, 0], cx, cy, sc, ex);
  g.fillStyle='#4fc3f7'; g.font=(11*d)+'px monospace'; g.fillText('N', npt[0]-4*d, npt[1]);
  // observer
  const o = project([0,0,0], cx, cy, sc, ex);
  g.fillStyle='#7fd4ff'; g.beginPath(); g.arc(o[0],o[1],4*d,0,7); g.fill();

  // traffic, far to near so nearer dots land on top
  const pts = SKY.points.map(p => {
    const q = project(p, cx, cy, sc, ex); return [q[0],q[1],q[2],p[3]];
  }).sort((a,b)=>b[2]-a[2]);
  const mx = SKY.maxw || 1;
  for(const p of pts){
    const f = Math.min(1, Math.log1p(p[3])/Math.log1p(mx));
    g.fillStyle = `rgba(${Math.round(60+195*f)},${Math.round(140+70*f)},${Math.round(230-30*f)},${0.25+0.65*f})`;
    g.beginPath(); g.arc(p[0], p[1], (0.9+2.2*f)*d, 0, 7); g.fill();
  }
  // the Moon's path
  if(SKY.arc && SKY.arc.length){
    g.strokeStyle='#ffd166'; g.lineWidth=2*d; g.beginPath();
    let started=false;
    for(const a of SKY.arc){
      const q = project(a, cx, cy, sc, ex);
      started ? g.lineTo(q[0],q[1]) : (g.moveTo(q[0],q[1]), started=true);
    }
    g.stroke();
  }
}
cv.addEventListener('pointerdown', e=>{ drag={x:e.clientX,y:e.clientY}; cv.setPointerCapture(e.pointerId); });
cv.addEventListener('pointerup', ()=> drag=null);
cv.addEventListener('pointermove', e=>{
  if(!drag) return;
  rotZ += (e.clientX-drag.x)*0.008;
  rotX = Math.max(-1.5, Math.min(0.2, rotX + (e.clientY-drag.y)*0.006));
  drag={x:e.clientX,y:e.clientY}; renderSky();
});
cv.addEventListener('wheel', e=>{ e.preventDefault();
  zoom = Math.max(0.3, Math.min(6, zoom * (e.deltaY>0?0.9:1.1))); renderSky(); },
  {passive:false});
document.getElementById('ex').addEventListener('input', e=>{
  document.getElementById('exv').textContent = e.target.value; renderSky(); });
window.addEventListener('resize', renderSky);

async function drawSky(){
  const b=document.getElementById('skyBtn'), info=document.getElementById('skyinfo');
  b.disabled=true; b.textContent='LOADING…';
  document.getElementById('skyDays').textContent = document.getElementById('days').value;
  try{
    const p = Object.assign(body(), {tod:document.getElementById('tod').value,
                                     dow:document.getElementById('dow').value});
    const r = await (await fetch('/api/sites/cloud',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(p)})).json();
    if(!r.ok){ info.innerHTML='<span style="color:#ff8080">'+r.info+'</span>'; return; }
    SKY = {points:r.points, arc:r.arc, extent:r.extent_m||80000,
           maxw:r.max_weight||1};
    renderSky();
    info.textContent = `${r.points.length.toLocaleString()} traffic points`
      + (r.arc.length ? ` · ${r.arc.length} Moon samples` : ' · Moon path unavailable')
      + ` · showing ${r.shown_of.toLocaleString()} of ${r.total_bins.toLocaleString()} stored bins`;
  }catch(e){ info.innerHTML='<span style="color:#ff8080">failed: '+e+'</span>'; }
  finally{ b.disabled=false; b.textContent='SHOW TRAFFIC'; }
}
(function todOptions(){
  const sel=document.getElementById('tod');
  sel.innerHTML='<option value="">Any time</option>';
  for(let i=0;i<8;i++){
    const a=String(i*3).padStart(2,'0'), z=String(i*3+3).padStart(2,'0');
    sel.innerHTML += `<option value="${i}">${a}:00 – ${z}:00</option>`;
  }
})();
dbstat();
</script></body></html>
"""
