# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 MaxBotev
"""The /sites page: score a candidate observing site, or hunt for a better one.

Everything here is on demand. The scoring walks the Moon's path across a month
against every recorded traffic bin, which is far too heavy to run during a
session, so it happens when this page asks for it and not before.
"""

PAGE = r"""
<!-- LunarTransit site finder -->
<style>
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
</style>
<div class="wrap">
 <div class="panel">
  <h2>Traffic database</h2>
  <div id="dbstat" class="muted">loading…</div>
 </div>

 <div class="panel">
  <h2>Score a site</h2>
  <div class="row">
   <div><label>Latitude</label><input id="lat" placeholder="37.2550"></div>
   <div><label>Longitude</label><input id="lon" placeholder="-122.0000"></div>
   <div><label>Altitude m</label><input id="alt" value="30" style="width:90px"></div>
   <div><label>Days ahead</label><input id="days" value="30" style="width:90px"></div>
   <div><label>Min Moon el°</label><input id="minel" value="15" style="width:90px"></div>
   <button class="btn" id="scoreBtn" onclick="scoreSite()">SCORE THIS SITE</button>
   <button class="btn" id="hereBtn" onclick="useHome()">USE MY SITE</button>
  </div>
  <div id="result" style="margin-top:16px"></div>
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
dbstat();
</script>
"""
