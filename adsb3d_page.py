# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 MaxBotev
"""HTML/JS for the 3D ADS-B terrain-plate view (served at /adsb3d).

A ~100-mile-diameter circular slab of real terrain (AWS Terrarium DEM via the
Pi proxy, ESRI World Imagery draped on top), free-rotatable in all three axes
(TrackballControls). Aircraft float above it at true (exaggerated) altitude
with heading cones, ground stalks, trails and labels.

Placeholders __LAT__ / __LON__ are substituted by the Flask route.
"""

ADSB3D_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ADS-B // 3D TERRAIN</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  :root{--bg:#05080f;--panel:#0b121fee;--line:#16263d;--cyan:#23e6ff;--green:#37ffb0;
        --amber:#ffcc4d;--red:#ff4d6d;--txt:#cfe6ff;--dim:#5d7a9c;}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--txt);overflow:hidden;
    font:13px/1.4 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;}
  #c{position:fixed;inset:0;display:block;}
  header{position:fixed;top:0;left:0;right:0;display:flex;align-items:center;gap:10px;
    padding:10px 16px;z-index:10;background:linear-gradient(180deg,rgba(5,8,15,.9),transparent);
    flex-wrap:wrap;pointer-events:none;}
  header>*{pointer-events:auto;}
  .logo{font-weight:700;letter-spacing:3px;font-size:16px;}
  .logo b{color:var(--cyan);}
  .pill{padding:3px 10px;border:1px solid var(--line);border-radius:20px;font-size:11px;
    color:var(--dim);background:var(--panel);}
  .pill.live{color:var(--green);border-color:rgba(55,255,176,.4);}
  .pill.dead{color:var(--red);border-color:rgba(255,77,109,.4);}
  .btn{padding:5px 11px;border:1px solid rgba(35,230,255,.4);border-radius:6px;
    color:var(--cyan);background:var(--panel);text-decoration:none;cursor:pointer;font:inherit;}
  .btn:hover{background:rgba(35,230,255,.15);}
  .sp{flex:1}
  label.ex{display:flex;align-items:center;gap:6px;font-size:10px;color:var(--dim);
    letter-spacing:1px;background:var(--panel);border:1px solid var(--line);
    border-radius:6px;padding:4px 10px;}
  input[type=range]{width:90px;accent-color:var(--cyan);}
  /* bottom-left stack: aircraft card sits above the controls hint, and the
     hint stays pinned to the corner whether or not a plane is selected */
  #bl{position:fixed;left:14px;bottom:14px;z-index:10;display:flex;
    flex-direction:column;align-items:flex-start;gap:8px;
    max-width:min(92vw,420px);pointer-events:none;}
  #bl>*{pointer-events:auto;}
  #info{background:var(--panel);border:1px solid var(--line);border-radius:10px;
    padding:12px 16px;min-width:240px;max-width:100%;display:none;}
  #info h3{margin:0 0 6px;color:var(--cyan);font-size:14px;letter-spacing:1px;}
  #info .row{display:flex;justify-content:space-between;gap:18px;padding:2px 0;
    border-bottom:1px dashed var(--line);font-size:12px;}
  #info .row span:first-child{color:var(--dim);}
  #status{position:fixed;right:14px;bottom:14px;z-index:10;color:var(--dim);font-size:11px;
    text-align:right;max-width:44vw;}
  #help{color:var(--dim);font-size:10px;letter-spacing:1px;line-height:1.6;}
  /* manual record: amber idle, pulsing red while rolling */
  .btn.rec{border-color:rgba(255,176,32,.6);color:var(--amber);font-weight:700;}
  .btn.rec:hover{background:rgba(255,176,32,.15);}
  .btn.rec.live{border-color:rgba(255,64,64,.9);color:#ff5252;
    background:rgba(255,64,64,.14);animation:recpulse 1.4s infinite;}
  .btn:disabled{opacity:.45;cursor:not-allowed;}
  @keyframes recpulse{0%,100%{box-shadow:0 0 0 0 rgba(255,64,64,.45);}
                      50%{box-shadow:0 0 0 6px rgba(255,64,64,0);}}
  /* ---- phones/tablets: one swipeable toolbar row instead of 5 wrapped ones ---- */
  @media (max-width:900px){
    header{flex-wrap:nowrap;overflow-x:auto;overflow-y:hidden;gap:6px;
      padding:8px 10px;scrollbar-width:none;-webkit-overflow-scrolling:touch;}
    header::-webkit-scrollbar{display:none;}
    header>*{flex:0 0 auto;}
    .sp{display:none;}
    .logo{font-size:13px;letter-spacing:2px;}
    .btn{padding:5px 9px;font-size:11px;}
    .pill{font-size:10px;padding:3px 8px;}
    label.ex{font-size:9px;padding:3px 8px;}
    input[type=range]{width:64px;}
    #bl{left:8px;bottom:8px;right:8px;max-width:none;}
    #info{min-width:0;width:100%;padding:10px 12px;}
    #help{font-size:9px;letter-spacing:.5px;}
    /* the bottom belongs to the info card + hint on a narrow screen, so the
       status line moves up under the toolbar instead of colliding with them */
    #status{top:44px;bottom:auto;right:8px;left:auto;font-size:9px;max-width:58vw;}
  }
</style>
</head>
<body>
<canvas id="c"></canvas>
<header>
  <div class="logo">ADSB<b>▸</b>3D</div>
  <span id="pillLive" class="pill">—</span>
  <span id="pillN" class="pill">0 AC</span>
  <button class="btn rec" id="btnRec">⏺ REC</button>
  <label class="ex">RELIEF ×<span id="exVal">2.5</span>
    <input id="exSlider" type="range" min="1" max="5" step="0.5" value="2.5"></label>
  <label class="ex">Ø<span id="diamVal">100</span>mi
    <input id="diamSlider" type="range" min="40" max="300" step="20" value="100"></label>
  <button class="btn" id="btnWx">☁ WX: OFF</button>
  <button class="btn" id="btnHrz">⛰ HORIZON: ON</button>
  <button class="btn" id="btnMap">MAP: SAT</button>
  <button class="btn" id="btnTour">🎬 TOUR</button>
  <button class="btn" id="btnPov">🌖 MOON POV</button>
  <button class="btn" id="btnTop">⬇ TOP</button>
  <button class="btn" id="btnReset">⟲ RESET</button>
  <button class="btn" id="btnLabels">LABELS: ON</button>
  <label class="ex">☾ MOON @
    <input type="datetime-local" id="tPick"
      style="background:var(--panel);color:var(--txt);border:1px solid var(--line);
             border-radius:4px;padding:2px 4px;font:inherit;color-scheme:dark;"></label>
  <button class="btn" id="btnNow" style="display:none;color:var(--amber);
    border-color:rgba(255,204,77,.5)">⏱ BACK TO LIVE</button>
  <button class="btn" id="btnSite">📍 SITE</button>
  <select id="atcSel" class="btn" style="appearance:none;-webkit-appearance:none">
    <option value="">📻 ATC</option>
    <option value="ksjc6_twr">SJC Tower 124.0</option>
    <option value="ksjc6_gnd">SJC Ground 121.7</option>
    <option value="ksjc_del_gnd">SJC Del/Gnd/Ops</option>
    <option value="ksjc_app">NorCal Approach 135.2</option>
    <option value="ksjc_app2">NorCal App LICKE 120.1</option>
    <option value="ksjc_dep">NorCal Departure 121.3</option>
    <option value="ksjc_atis">SJC D-ATIS 126.95</option>
  </select>
  <div class="sp"></div>
  <a class="btn" href="/adsb">🗺 2D MAP</a>
  <a class="btn" href="/lunar">🌕 LUNAR</a>
  <a class="btn" href="/">📡 RF</a>
</header>
<div id="bl">
  <div id="info"></div>
  <div id="help">DRAG rotate · WHEEL zoom · RIGHT-DRAG pan · CLICK aircraft ·
  trail dots: <span style="color:#37ffb0">●climb</span>
  <span style="color:#ff884d">●descend</span>
  <span style="color:#23e6ff">●level</span></div>
</div>
<div id="sitePanel" style="position:fixed;right:14px;top:56px;z-index:30;background:var(--panel);
     border:1px solid var(--line);border-radius:10px;padding:14px 16px;display:none;width:300px">
  <h3 style="margin:0 0 10px;color:var(--cyan);font-size:12px;letter-spacing:2px">OBSERVER SITE</h3>
  <div style="display:flex;gap:6px;margin:0 0 8px">
    <input id="siteSearch" placeholder="San Jose, CA"
      style="flex:1;background:var(--panel);color:var(--txt);border:1px solid var(--line);
             border-radius:4px;padding:4px 6px;font:inherit">
    <button class="btn" id="siteSearchBtn" style="font-size:11px">🔍</button>
  </div>
  <div id="siteResults" style="font-size:10px;margin:0 0 6px;max-height:90px;overflow:auto"></div>
  <div style="position:relative;margin:0 0 10px">
    <div id="siteMap" style="width:100%;height:190px;border:1px solid var(--line);
         border-radius:6px;background:#0a0f18"></div>
    <div style="position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);
         color:var(--red);font-size:22px;font-weight:700;pointer-events:none;
         z-index:500;text-shadow:0 0 4px #000">+</div>
  </div>
  <div class="row" style="margin:0 0 6px;display:flex;justify-content:space-between;align-items:center">
    <span style="color:var(--dim);font-size:11px">LATITUDE</span>
    <input id="siteLat" type="number" step="0.00001" min="-90" max="90"
      style="width:130px;background:var(--panel);color:var(--txt);border:1px solid var(--line);
             border-radius:4px;padding:3px 6px;font:inherit"></div>
  <div class="row" style="margin:0 0 6px;display:flex;justify-content:space-between;align-items:center">
    <span style="color:var(--dim);font-size:11px">LONGITUDE</span>
    <input id="siteLon" type="number" step="0.00001" min="-180" max="180"
      style="width:130px;background:var(--panel);color:var(--txt);border:1px solid var(--line);
             border-radius:4px;padding:3px 6px;font:inherit"></div>
  <div class="row" style="margin:0 0 10px;display:flex;justify-content:space-between;align-items:center">
    <span style="color:var(--dim);font-size:11px">ALTITUDE m</span>
    <input id="siteAlt" type="number" step="1" min="-430" max="9000"
      style="width:130px;background:var(--panel);color:var(--txt);border:1px solid var(--line);
             border-radius:4px;padding:3px 6px;font:inherit"></div>
  <div style="display:flex;gap:8px">
    <button class="btn" id="siteGeo" style="font-size:11px">📍 MY LOCATION</button>
    <button class="btn" id="siteSave" style="font-size:11px;color:var(--green);
      border-color:rgba(55,255,176,.4)">💾 SAVE</button>
  </div>
  <div id="siteMsg" class="muted" style="margin-top:8px;font-size:11px;color:var(--dim)"></div>
</div>
<div id="status">loading terrain…</div>

<script type="importmap">
{"imports":{"three":"https://unpkg.com/three@0.160.0/build/three.module.js",
"three/addons/":"https://unpkg.com/three@0.160.0/examples/jsm/"}}
</script>
<script type="module">
import * as THREE from 'three';
import {TrackballControls} from 'three/addons/controls/TrackballControls.js';

const HLAT = __LAT__, HLON = __LON__;
// slab diameter: user-adjustable (Ø slider), persisted; page reloads to rebuild
const DIAM_MI = Math.max(40, Math.min(300,
  parseFloat(localStorage.getItem('slabDiamMi') || '100')));
const R_KM = DIAM_MI * 1.609344 / 2;
const SCALE = R_KM / 80.5;         // scale factor vs the original 100-mile plate
// tile zoom adapts to radius so coverage stays ~6x6 tiles at any diameter
const Z = Math.max(7, Math.min(12, Math.round(
  Math.log2(120225 * Math.cos(__LAT__ * Math.PI / 180) / R_KM))));
const KM_LAT = 111.32, KM_LON = 111.32 * Math.cos(HLAT * Math.PI / 180);
const FT2KM = 0.0003048;
let EX = 2.5;                      // vertical exaggeration

// ---------- three.js scaffolding ----------
const canvas = document.getElementById('c');
const renderer = new THREE.WebGLRenderer({canvas, antialias: true});
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
const scene = new THREE.Scene();
scene.background = new THREE.Color(0x05080f);
scene.fog = new THREE.Fog(0x05080f, 300 * SCALE, 700 * SCALE);
const camera = new THREE.PerspectiveCamera(55, 1, 0.5, 2000 * Math.max(1, SCALE));
const HOME_VIEW = new THREE.Vector3(0, 95 * SCALE, 150 * SCALE);
camera.position.copy(HOME_VIEW);

const controls = new TrackballControls(camera, canvas);   // true 3-axis rotation
controls.rotateSpeed = 3.5;
controls.zoomSpeed = 1.4;
controls.panSpeed = 0.7;
controls.minDistance = 15;
controls.maxDistance = 800 * SCALE;

scene.add(new THREE.AmbientLight(0xffffff, 1.6));
const sun = new THREE.DirectionalLight(0xfff4e0, 1.1);
sun.position.set(-120, 180, 90);
scene.add(sun);

function resize(){
  renderer.setSize(innerWidth, innerHeight);
  camera.aspect = innerWidth / innerHeight;
  camera.updateProjectionMatrix();
}
addEventListener('resize', resize); resize();

// ---------- slippy-tile helpers ----------
function tileXY(lat, lon, z){
  const n = 2 ** z;
  const x = (lon + 180) / 360 * n;
  const la = lat * Math.PI / 180;
  const y = (1 - Math.log(Math.tan(la) + 1 / Math.cos(la)) / Math.PI) / 2 * n;
  return [x, y];
}
function loadImg(url, cors){
  return new Promise((res, rej) => {
    const im = new Image();
    if (cors) im.crossOrigin = 'anonymous';
    im.onload = () => res(im);
    im.onerror = () => rej(url);
    im.src = url;
  });
}

// tile range covering home ± R_KM
const dLat = R_KM / KM_LAT, dLon = R_KM / KM_LON;
const [tx0, ty0] = tileXY(HLAT + dLat, HLON - dLon, Z).map(Math.floor);
const [tx1, ty1] = tileXY(HLAT - dLat, HLON + dLon, Z).map(Math.floor);
const NX = tx1 - tx0 + 1, NY = ty1 - ty0 + 1;
const statusEl = document.getElementById('status');

// throttled (6 at a time) + retried (3 attempts) tile loader; failed tiles
// show the baseFill color instead of void-black. Returns the canvas at once —
// await .done for completion; onTile fires per landed tile.
function loadTileGrid(urlFn, filter, baseFill, onTile){
  const cv = document.createElement('canvas');
  cv.width = NX * 256; cv.height = NY * 256;
  const ctx = cv.getContext('2d', {willReadFrequently: true});
  if (baseFill){ ctx.fillStyle = baseFill; ctx.fillRect(0, 0, cv.width, cv.height); }
  if (filter) ctx.filter = filter;
  const jobs = [];
  let ok = 0, fail = 0;
  for (let x = tx0; x <= tx1; x++)
    for (let y = ty0; y <= ty1; y++)
      jobs.push(async () => {
        for (let att = 0; att < 3; att++){
          try {
            const im = await loadImg(urlFn(x, y), false);
            ctx.drawImage(im, (x - tx0) * 256, (y - ty0) * 256);
            ok++; if (onTile) onTile(); return;
          } catch(e){ await new Promise(r => setTimeout(r, 350 * (att + 1))); }
        }
        fail++;
      });
  const q = jobs.slice();
  const done = Promise.all(Array.from({length: 6}, async () => {
    while (q.length) await q.shift()();
  })).then(() => ({ok, fail}));
  return {cv, ctx, done};
}

// world position (km, y-up, north = -z) for a lat/lon
function toWorld(lat, lon){
  return [ (lon - HLON) * KM_LON, -(lat - HLAT) * KM_LAT ];
}
// canvas pixel for a lat/lon
function toPx(lat, lon){
  const [x, y] = tileXY(lat, lon, Z);
  return [ (x - tx0) * 256, (y - ty0) * 256 ];
}

// ---------- terrain plate ----------
const plate = new THREE.Group();
scene.add(plate);
let terrainMesh = null, baseHeights = null, terrainGeo = null;
let shadeArr = null, hypsoArr = null;
const LIGHT_DIR = new THREE.Vector3(-0.45, 0.8, 0.35).normalize();
const hueCol = h => new THREE.Color().setHSL(
  0.55 - Math.min(h, 1.3) * 0.35, 0.7, 0.32 + Math.min(h, 1.3) * 0.30);

// The terrain is UNLIT (MeshBasicMaterial) so basemap brightness is identical
// from every angle; relief comes from a baked per-vertex hillshade that
// multiplies the texture. Re-baked whenever exaggeration changes.
function bakeShade(){
  terrainGeo.computeVertexNormals();
  const n = terrainGeo.attributes.normal;
  for (let i = 0; i < baseHeights.length; i++){
    const s = 0.74 + 0.26 * Math.max(0,
      n.getX(i) * LIGHT_DIR.x + n.getY(i) * LIGHT_DIR.y + n.getZ(i) * LIGHT_DIR.z);
    shadeArr[i * 3] = shadeArr[i * 3 + 1] = shadeArr[i * 3 + 2] = s;
    const c = hueCol(baseHeights[i]);
    hypsoArr[i * 3] = c.r * s; hypsoArr[i * 3 + 1] = c.g * s; hypsoArr[i * 3 + 2] = c.b * s;
  }
  if (terrainGeo.attributes.color) terrainGeo.attributes.color.needsUpdate = true;
}

// basemap cycling — all tiles proxied by the Pi (same origin, disk-cached)
const MAPS = ['sat', 'osm', 'dark'];    // Google-Earth-style satellite first
let mapIdx = 0;
async function applyBasemap(src){
  if (!terrainMesh) return;
  statusEl.textContent = 'loading ' + src + ' basemap…';
  // per-style grading: sat gets a sunlit Google-Earth pop; dark-matter tiles
  // are near-black by design and need a heavy lift
  const filter = {dark: 'brightness(3.2) saturate(1.2)',
                  sat: 'brightness(1.35) saturate(1.25)'}[src] || null;
  const fill = {dark: '#1d2633', sat: '#2a3340', osm: '#f2efe9'}[src];
  let tex;
  const g = loadTileGrid((x, y) => `/api/mtile/${src}/${Z}/${x}/${y}`,
                         filter, fill, () => { if (tex) tex.needsUpdate = true; });
  tex = new THREE.CanvasTexture(g.cv);
  tex.colorSpace = THREE.SRGBColorSpace;
  tex.anisotropy = renderer.capabilities.getMaxAnisotropy();
  terrainGeo.setAttribute('color', new THREE.BufferAttribute(shadeArr, 3));
  terrainMesh.material.dispose();
  terrainMesh.material = new THREE.MeshBasicMaterial({map: tex, vertexColors: true});
  const {ok, fail} = await g.done;      // tiles pop in live while this waits
  tex.needsUpdate = true;
  statusEl.textContent = `${src} basemap · ${ok} tiles` + (fail ? ` · ${fail} FAILED` : '');
}

async function buildTerrain(){
  let elev = null;
  try {
    const g = loadTileGrid((x, y) => `/api/mtile/terrain/${Z}/${x}/${y}`, null, null, null);
    const {ok, fail} = await g.done;
    if (ok > 0) elev = g.ctx.getImageData(0, 0, g.cv.width, g.cv.height);
    statusEl.textContent = `DEM tiles ${ok} ok / ${fail} failed`;
  } catch(err){ statusEl.textContent = 'DEM load failed — flat plate'; }

  function heightAt(lat, lon){
    if (!elev) return 0;
    const [px, py] = toPx(lat, lon);
    const xi = Math.max(0, Math.min(elev.width - 1, Math.round(px)));
    const yi = Math.max(0, Math.min(elev.height - 1, Math.round(py)));
    const i = (yi * elev.width + xi) * 4;
    const m = elev.data[i] * 256 + elev.data[i+1] + elev.data[i+2] / 256 - 32768;
    return Math.max(0, m) / 1000;   // km, clamp sea
  }

  // polar grid: RINGS x SECTORS, round by construction
  const RINGS = 80, SECT = 200;
  const pos = [], uv = [], idx = [];
  baseHeights = [];
  for (let r = 0; r <= RINGS; r++){
    const rad = R_KM * r / RINGS;
    for (let s = 0; s <= SECT; s++){
      const a = 2 * Math.PI * s / SECT;
      const x = rad * Math.sin(a), zz = -rad * Math.cos(a);
      const lat = HLAT - zz / KM_LAT, lon = HLON + x / KM_LON;
      const h = heightAt(lat, lon);
      baseHeights.push(h);
      pos.push(x, h * EX, zz);
      const [px, py] = toPx(lat, lon);
      uv.push(px / (NX * 256), 1 - py / (NY * 256));
    }
  }
  for (let r = 0; r < RINGS; r++)
    for (let s = 0; s < SECT; s++){
      const a = r * (SECT + 1) + s, b = a + SECT + 1;
      idx.push(a, a + 1, b, b, a + 1, b + 1);   // CCW from above -> normals point up
    }
  terrainGeo = new THREE.BufferGeometry();
  terrainGeo.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3));
  terrainGeo.setAttribute('uv', new THREE.Float32BufferAttribute(uv, 2));
  terrainGeo.setIndex(idx);
  shadeArr = new Float32Array(baseHeights.length * 3);
  hypsoArr = new Float32Array(baseHeights.length * 3);
  bakeShade();
  terrainGeo.setAttribute('color', new THREE.BufferAttribute(hypsoArr, 3));
  terrainMesh = new THREE.Mesh(terrainGeo,
    new THREE.MeshBasicMaterial({vertexColors: true}));   // unlit — angle-independent
  plate.add(terrainMesh);
  applyBasemap(MAPS[mapIdx]);          // async texture swap when tiles arrive

  // plate side wall + bottom + glowing rim
  const wall = new THREE.Mesh(
    new THREE.CylinderGeometry(R_KM, R_KM, 5, 128, 1, true),
    new THREE.MeshStandardMaterial({color: 0x0b121f, side: THREE.DoubleSide, roughness: .8}));
  wall.position.y = -2.5; plate.add(wall);
  const bottom = new THREE.Mesh(new THREE.CircleGeometry(R_KM, 128),
    new THREE.MeshBasicMaterial({color: 0x070c16, side: THREE.DoubleSide}));
  bottom.rotation.x = Math.PI / 2; bottom.position.y = -5; plate.add(bottom);
  const rim = new THREE.Mesh(new THREE.TorusGeometry(R_KM, .35, 8, 200),
    new THREE.MeshBasicMaterial({color: 0x23e6ff}));
  rim.rotation.x = Math.PI / 2; plate.add(rim);

  // range rings + compass letters
  const ringMat = new THREE.LineBasicMaterial({color: 0x23e6ff, transparent: true, opacity: .18});
  for (const rr of [20, 40, 60]){
    const pts = [];
    for (let s = 0; s <= 128; s++){
      const a = 2 * Math.PI * s / 128;
      pts.push(new THREE.Vector3(rr * Math.sin(a), 0.15, -rr * Math.cos(a)));
    }
    plate.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts), ringMat));
  }
  const dirs = [['N', 0, -1], ['E', 1, 0], ['S', 0, 1], ['W', -1, 0]];
  for (const [t, dx, dz] of dirs){
    const sp = compassSprite(t, t === 'N' ? '#ff4d6d' : '#9db4cc');
    sp.position.set(dx * (R_KM + 9), 3, dz * (R_KM + 9));
    plate.add(sp);
  }
  // home beacon
  const home = new THREE.Mesh(new THREE.OctahedronGeometry(1.1),
    new THREE.MeshBasicMaterial({color: 0x37ffb0}));
  home.position.y = heightAt(HLAT, HLON) * EX + 1.5;
  home.name = 'home'; plate.add(home);
  statusEl.textContent += ' · terrain ready';
}

function setExaggeration(ex){
  EX = ex;
  if (!terrainGeo) return;
  const p = terrainGeo.attributes.position;
  for (let i = 0; i < baseHeights.length; i++) p.setY(i, baseHeights[i] * EX);
  p.needsUpdate = true;
  bakeShade();
  for (const pl of planes.values()) placePlane(pl);
  buildWx();                       // cloud altitudes track the relief scale
}

function compassSprite(letter, color){
  const cv = document.createElement('canvas');
  cv.width = cv.height = 128;                 // square canvas = no squeeze
  const cx = cv.getContext('2d');
  cx.font = '900 100px ui-monospace, Menlo, Consolas, monospace';
  cx.textAlign = 'center'; cx.textBaseline = 'middle';
  cx.shadowColor = '#000'; cx.shadowBlur = 10;
  cx.fillStyle = color;
  cx.fillText(letter, 64, 72);
  const sp = new THREE.Sprite(new THREE.SpriteMaterial(
    {map: new THREE.CanvasTexture(cv), depthTest: false}));
  sp.scale.set(10, 10, 1);
  return sp;
}

// ---------- text sprites ----------
function textSprite(text, color, px = 30){
  const cv = document.createElement('canvas');
  const H = 64;
  cv.height = H;
  const font = `bold ${px}px ui-monospace, Menlo, monospace`;
  let cx = cv.getContext('2d');
  cx.font = font;
  cv.width = Math.max(96, Math.ceil(cx.measureText(text).width) + 18);
  cx = cv.getContext('2d');            // canvas resize resets state
  cx.font = font;
  cx.fillStyle = color; cx.textBaseline = 'middle';
  cx.shadowColor = '#000'; cx.shadowBlur = 6;
  cx.fillText(text, 9, 34);
  const tex = new THREE.CanvasTexture(cv);
  const sp = new THREE.Sprite(new THREE.SpriteMaterial({map: tex, depthTest: false}));
  sp.scale.set(3.2 * cv.width / H, 3.2, 1);
  sp.userData.baseScale = [3.2 * cv.width / H, 3.2];
  return sp;
}

// ---------- aircraft 3D models (low-poly; forward = -Z, up = +Y) ----------
function shapeMesh(pts, mat, thick, vertical){
  const s = new THREE.Shape();
  s.moveTo(pts[0][0], pts[0][1]);
  for (let i = 1; i < pts.length; i++) s.lineTo(pts[i][0], pts[i][1]);
  s.closePath();
  const geo = new THREE.ExtrudeGeometry(s, {depth: thick, bevelEnabled: false});
  if (vertical) geo.rotateY(-Math.PI / 2);   // shape x=aft,y=up -> ZY plane
  else geo.rotateX(Math.PI / 2);             // shape x=span,y=aft -> flat XZ
  return new THREE.Mesh(geo, mat);
}
function acMats(kind){
  const mil = kind === 'military' || kind === 'mil_helicopter';
  return {
    fus: new THREE.MeshLambertMaterial({color: mil ? 0x9aa0ab : 0xe3e9f0}),
    wing: new THREE.MeshLambertMaterial({color: mil ? 0x7d838d : 0xb9c4d2}),
    acc: new THREE.MeshLambertMaterial({color: kindColor(kind)}),
    disc: new THREE.MeshBasicMaterial({color: kindColor(kind), transparent: true,
                                       opacity: .22, side: THREE.DoubleSide}),
  };
}

function buildJet(kind){
  const m = acMats(kind), g = new THREE.Group();
  const fus = new THREE.Mesh(new THREE.CylinderGeometry(.19, .23, 2.5, 12), m.fus);
  fus.rotation.x = Math.PI / 2; g.add(fus);
  const nose = new THREE.Mesh(new THREE.SphereGeometry(.22, 12, 8), m.fus);
  nose.scale.z = 1.7; nose.position.z = -1.25; g.add(nose);
  const tailc = new THREE.Mesh(new THREE.ConeGeometry(.19, .8, 12), m.fus);
  tailc.rotation.x = Math.PI / 2; tailc.position.z = 1.6; g.add(tailc);
  // swept wing, one piece through the fuselage
  g.add(shapeMesh([[-2.3, 1.0], [-2.3, .72], [0, -.3], [2.3, .72], [2.3, 1.0], [0, .5]],
                  m.wing, .07));
  // engines
  for (const sx of [-1, 1]){
    const eng = new THREE.Mesh(new THREE.CylinderGeometry(.12, .12, .5, 10), m.wing);
    eng.rotation.x = Math.PI / 2;
    eng.position.set(sx * .85, -.16, .28); g.add(eng);
  }
  // horizontal stabilizer + fin (accent = airline tail)
  const hs = shapeMesh([[-.85, .32], [-.85, .16], [0, -.1], [.85, .16], [.85, .32], [0, .18]],
                       m.wing, .05);
  hs.position.z = 1.35; g.add(hs);
  const fin = shapeMesh([[-.05, 0], [.85, 0], [.85, .22], [.5, .85], [.2, .85]],
                        m.acc, .06, true);
  fin.position.set(.03, .08, .95); g.add(fin);
  return g;
}

function buildProp(kind){
  const m = acMats(kind), g = new THREE.Group();
  const fus = new THREE.Mesh(new THREE.CylinderGeometry(.15, .17, 1.5, 10), m.fus);
  fus.rotation.x = Math.PI / 2; g.add(fus);
  const nose = new THREE.Mesh(new THREE.SphereGeometry(.16, 10, 8), m.fus);
  nose.position.z = -.75; g.add(nose);
  const tailc = new THREE.Mesh(new THREE.ConeGeometry(.15, .6, 10), m.fus);
  tailc.rotation.x = Math.PI / 2; tailc.position.z = 1.0; g.add(tailc);
  // straight high wing
  const wing = shapeMesh([[-1.65, -.2], [1.65, -.2], [1.65, .2], [-1.65, .2]], m.wing, .06);
  wing.position.set(0, .16, -.15); g.add(wing);
  const hs = shapeMesh([[-.6, -.12], [.6, -.12], [.6, .12], [-.6, .12]], m.wing, .05);
  hs.position.z = 1.05; g.add(hs);
  const fin = shapeMesh([[0, 0], [.45, 0], [.45, .5], [.15, .5]], m.acc, .05, true);
  fin.position.set(.025, .05, .7); g.add(fin);
  // prop disc + spinner
  const disc = new THREE.Mesh(new THREE.CylinderGeometry(.5, .5, .02, 20), m.disc);
  disc.rotation.x = Math.PI / 2; disc.position.z = -.95; g.add(disc);
  const spin = new THREE.Mesh(new THREE.ConeGeometry(.07, .2, 8), m.acc);
  spin.rotation.x = -Math.PI / 2; spin.position.z = -1.0; g.add(spin);
  return g;
}

function buildHelo(kind){
  const m = acMats(kind), g = new THREE.Group();
  const cab = new THREE.Mesh(new THREE.SphereGeometry(.36, 12, 10), m.fus);
  cab.scale.set(.9, .85, 1.5); cab.position.z = -.25; g.add(cab);
  const boom = new THREE.Mesh(new THREE.CylinderGeometry(.06, .09, 1.3, 8), m.fus);
  boom.rotation.x = Math.PI / 2; boom.position.z = .75; g.add(boom);
  const fin = shapeMesh([[0, -.15], [.3, -.15], [.35, .32], [.15, .32]], m.acc, .05, true);
  fin.position.set(.025, .05, 1.3); g.add(fin);
  for (const sx of [-1, 1]){                            // skids
    const sk = new THREE.Mesh(new THREE.CylinderGeometry(.03, .03, 1.0, 6), m.wing);
    sk.rotation.x = Math.PI / 2; sk.position.set(sx * .28, -.42, -.2); g.add(sk);
  }
  // main rotor: translucent disc + two blades (spun in the render loop)
  const rotor = new THREE.Group();
  rotor.position.set(0, .42, -.2);
  const rdisc = new THREE.Mesh(new THREE.CylinderGeometry(1.45, 1.45, .015, 24), m.disc);
  rotor.add(rdisc);
  for (const ang of [0, Math.PI / 2]){
    const bl = new THREE.Mesh(new THREE.BoxGeometry(2.8, .02, .09), m.wing);
    bl.rotation.y = ang; rotor.add(bl);
  }
  g.add(rotor);
  g.userData.rotor = rotor;
  const tr = new THREE.Mesh(new THREE.CylinderGeometry(.28, .28, .015, 12), m.disc);
  tr.rotation.z = Math.PI / 2; tr.position.set(.12, .1, 1.32); g.add(tr);
  return g;
}

function buildGround(kind){
  // airport ground vehicle: low box body + cab + amber beacon, no wings
  const m = acMats(kind), g = new THREE.Group();
  const body = new THREE.Mesh(new THREE.BoxGeometry(.8, .4, 1.5), m.acc);
  body.position.y = .25; g.add(body);
  const cab = new THREE.Mesh(new THREE.BoxGeometry(.7, .32, .55), m.wing);
  cab.position.set(0, .6, -.35); g.add(cab);
  const bcn = new THREE.Mesh(new THREE.SphereGeometry(.11, 8, 6),
    new THREE.MeshBasicMaterial({color: 0xffcc4d}));
  bcn.position.y = .9; g.add(bcn);
  return g;
}

function buildBalloon(kind){
  const m = acMats(kind), g = new THREE.Group();
  const env = new THREE.Mesh(new THREE.SphereGeometry(.9, 14, 12), m.acc);
  env.scale.y = 1.15; env.position.y = .9; g.add(env);
  const basket = new THREE.Mesh(new THREE.BoxGeometry(.3, .25, .3), m.wing);
  basket.position.y = -.45; g.add(basket);
  return g;
}

function buildDrone(kind){
  const m = acMats(kind), g = new THREE.Group();
  const hub = new THREE.Mesh(new THREE.BoxGeometry(.35, .12, .35), m.fus);
  g.add(hub);
  for (const [sx, sz] of [[-1,-1],[1,-1],[-1,1],[1,1]]){
    const arm = new THREE.Mesh(new THREE.CylinderGeometry(.04,.04,.5,6), m.wing);
    arm.rotation.z = Math.PI/2; arm.rotation.y = Math.atan2(sz,sx);
    arm.position.set(sx*.3, 0, sz*.3); g.add(arm);
    const disc = new THREE.Mesh(new THREE.CylinderGeometry(.22,.22,.015,12), m.disc);
    disc.position.set(sx*.5, .06, sz*.5); g.add(disc);
  }
  return g;
}

function buildModelFor(kind){
  if (kind === 'helicopter' || kind === 'mil_helicopter') return buildHelo(kind);
  if (kind === 'prop' || kind === 'glider') return buildProp(kind);
  if (kind === 'ground') return buildGround(kind);
  if (kind === 'balloon') return buildBalloon(kind);
  if (kind === 'drone') return buildDrone(kind);
  return buildJet(kind);
}
function labelFor(a){ return a.type_short || a.flight || a.hex; }

// ---------- aircraft ----------
const planes = new Map();     // hex -> {group, cone, stalk, label, trail, pts, data, seen}
const planeLayer = new THREE.Group();
plate.add(planeLayer);
let showLabels = true;

const KIND_COLOR = {military: 0xff4d6d, mil_helicopter: 0xff4d6d,
                    helicopter: 0xffcc4d, ground: 0x9aa7b5,
                    balloon: 0xff9d5c, drone: 0xc77dff,
                    prop: 0x7fd4ff, unknown: 0x5d7a9c};
function kindColor(k){ return KIND_COLOR[k] ?? 0x23e6ff; }

// trails: round dots, colored by vertical rate at each sample
const TRAIL_UP = new THREE.Color(0x37ffb0);              // climbing
const TRAIL_DOWN = new THREE.Color(0xff884d);            // descending
const TRAIL_LVL = new THREE.Color(0x23e6ff).multiplyScalar(0.55);
const TRAIL_LEN = 120;
const dotTex = (() => {
  const cv = document.createElement('canvas');
  cv.width = cv.height = 32;
  const cx = cv.getContext('2d');
  const grd = cx.createRadialGradient(16, 16, 2, 16, 16, 15);
  grd.addColorStop(0, 'rgba(255,255,255,1)');
  grd.addColorStop(0.7, 'rgba(255,255,255,.9)');
  grd.addColorStop(1, 'rgba(255,255,255,0)');
  cx.fillStyle = grd; cx.fillRect(0, 0, 32, 32);
  return new THREE.CanvasTexture(cv);
})();
const TRAIL_MAT = new THREE.PointsMaterial({
  size: 1.1, map: dotTex, vertexColors: true, transparent: true,
  opacity: .95, alphaTest: .3, sizeAttenuation: true, depthWrite: false});

function makePlane(a){
  const g = new THREE.Group();
  const col = kindColor(a.kind);
  const model = buildModelFor(a.kind);
  model.scale.setScalar(1.25);
  g.add(model);
  const stalk = new THREE.Line(
    new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(), new THREE.Vector3()]),
    new THREE.LineBasicMaterial({color: col, transparent: true, opacity: .3}));
  planeLayer.add(stalk);
  const label = textSprite(labelFor(a), '#cfe6ff');
  label.position.y = 3; g.add(label);
  const trailGeo = new THREE.BufferGeometry();
  trailGeo.setAttribute('position', new THREE.Float32BufferAttribute(new Float32Array(TRAIL_LEN * 3), 3));
  trailGeo.setAttribute('color', new THREE.Float32BufferAttribute(new Float32Array(TRAIL_LEN * 3), 3));
  trailGeo.setDrawRange(0, 0);
  const trail = new THREE.Points(trailGeo, TRAIL_MAT);
  planeLayer.add(trail);
  planeLayer.add(g);
  return {group: g, model, rotor: model.userData.rotor, stalk, label,
          labelText: labelFor(a), kind: a.kind, trail, pts: [],
          data: a, seen: Date.now()};
}

function placePlane(p){
  const a = p.data;
  const [x, z] = toWorld(a.lat, a.lon);
  const y = (a.kind === 'ground' || a.alt === 'ground') ? 0.25
    : Math.max(0.5, (typeof a.alt === 'number' ? a.alt : 0) * FT2KM * EX);
  p.group.position.set(x, y, z);
  p.group.rotation.y = -(a.track || 0) * Math.PI / 180;
  p.label.visible = showLabels;
  const sp = p.stalk.geometry.attributes.position;
  sp.setXYZ(0, x, 0, z); sp.setXYZ(1, x, y, z); sp.needsUpdate = true;
}

function updateTrail(p){
  const pos = p.group.position;
  const last = p.pts[p.pts.length - 1];
  if (!last || last.v.distanceToSquared(pos) > 0.25){
    const vr = p.data.vrate || 0;
    const c = vr > 250 ? TRAIL_UP : vr < -250 ? TRAIL_DOWN : TRAIL_LVL;
    p.pts.push({v: pos.clone(), c});
    if (p.pts.length > TRAIL_LEN) p.pts.shift();
    const ap = p.trail.geometry.attributes.position;
    const ac = p.trail.geometry.attributes.color;
    p.pts.forEach((pt, i) => {
      ap.setXYZ(i, pt.v.x, pt.v.y, pt.v.z);
      ac.setXYZ(i, pt.c.r, pt.c.g, pt.c.b);
    });
    ap.needsUpdate = ac.needsUpdate = true;
    p.trail.geometry.setDrawRange(0, p.pts.length);
  }
}

async function poll(){
  try {
    const d = await (await fetch('/api/adsb')).json();
    const list = (d.aircraft || []).filter(a => a.lat != null && a.lon != null);
    document.getElementById('pillN').textContent = list.length + ' AC';
    const pill = document.getElementById('pillLive');
    pill.textContent = '● ADS-B LIVE'; pill.className = 'pill live';
    const now = Date.now(), seen = new Set();
    for (const a of list){
      const [x, z] = toWorld(a.lat, a.lon);
      if (Math.hypot(x, z) > R_KM * 1.05) continue;      // beyond the plate edge
      seen.add(a.hex);
      let p = planes.get(a.hex);
      if (!p){ p = makePlane(a); planes.set(a.hex, p); }
      p.data = a; p.seen = now;
      if (a.kind !== p.kind){              // classification arrives async —
        p.group.remove(p.model);           // rebuild the 3D model to match
        p.model = buildModelFor(a.kind);
        p.model.scale.setScalar(1.25);
        p.group.add(p.model);
        p.rotor = p.model.userData.rotor;
        p.kind = a.kind;
        p.stalk.material.color.set(kindColor(a.kind));
        p.alarmMats = null;                // re-capture colors on next alarm
      }
      const lt = labelFor(a);              // type info arrives async — refresh
      if (lt !== p.labelText){
        p.group.remove(p.label);
        p.label = textSprite(lt, '#cfe6ff');
        p.label.position.y = 3;
        p.label.visible = showLabels;
        p.group.add(p.label);
        p.labelText = lt;
      }
      placePlane(p); updateTrail(p);
    }
    for (const [hex, p] of planes)
      if (!seen.has(hex) && now - p.seen > 10000){
        planeLayer.remove(p.group, p.stalk, p.trail);
        planes.delete(hex);
      }
    // keep an open info panel live (values change as the plane flies)
    if (shownPlane){
      if (planes.has(shownPlane.data.hex)) renderInfo(shownPlane.data);
      else hideInfo();
    }
  } catch(e){
    const pill = document.getElementById('pillLive');
    pill.textContent = '● ADS-B STALE'; pill.className = 'pill dead';
  }
}

// ---------- info panel + picking ----------
const ray = new THREE.Raycaster(); ray.params.Points = {threshold: 2};
const infoEl = document.getElementById('info');
let shownPlane = null;                 // plane whose panel is open (kept live)

function renderInfo(a){
  const apt = p => !p ? null
    : (p.city && p.iata) ? `${p.city} (${p.iata})`
    : p.city || p.iata || p.icao || p.name || null;
  const o = apt(a.origin), d = apt(a.destination);
  const rows = [
    ['TYPE', a.type_short || a.icao_type || '—'],
    ['ALTITUDE', typeof a.alt === 'number' ? a.alt.toLocaleString() + ' ft' : '—'],
    ['SPEED', a.gs ? Math.round(a.gs) + ' kt' : '—'],
    ['TRACK', a.track != null ? Math.round(a.track) + '°' : '—'],
    ['ROUTE', (o && d) ? o + ' → ' + d : (o || d || '—')],
    ['OPERATOR', a.airline || a.owner || '—'],
    ['CLASS', a.kind || '—'],
  ].map(([k, v]) => `<div class="row"><span>${k}</span><span>${v}</span></div>`).join('');
  infoEl.innerHTML = `<h3>${a.flight || a.hex}</h3>${rows}`;
  infoEl.style.display = 'block';
}
function showPlaneInfo(p){ shownPlane = p; renderInfo(p.data); }
function hideInfo(){ shownPlane = null; infoEl.style.display = 'none'; }

canvas.addEventListener('click', ev => {
  const m = new THREE.Vector2(ev.clientX / innerWidth * 2 - 1, -(ev.clientY / innerHeight) * 2 + 1);
  ray.setFromCamera(m, camera);
  let best = null, bestD = 1e9;
  for (const p of planes.values()){
    const d = ray.ray.distanceToPoint(p.group.getWorldPosition(new THREE.Vector3()));
    if (d < 4 && d < bestD){ bestD = d; best = p; }
  }
  if (!best){ hideInfo(); return; }
  showPlaneInfo(best);
});

// ---------- UI ----------
{
  const ds = document.getElementById('diamSlider');
  ds.value = DIAM_MI;
  document.getElementById('diamVal').textContent = DIAM_MI;
  ds.oninput = e => document.getElementById('diamVal').textContent = e.target.value;
  ds.onchange = e => {                       // rebuild the world at new size
    localStorage.setItem('slabDiamMi', e.target.value);
    statusEl.textContent = 'rebuilding ' + e.target.value + ' mi slab…';
    setTimeout(() => location.reload(), 300);
  };
}
document.getElementById('exSlider').oninput = e => {
  document.getElementById('exVal').textContent = e.target.value;
  setExaggeration(parseFloat(e.target.value));
};
document.getElementById('btnReset').onclick = () => {
  controls.reset(); camera.position.copy(HOME_VIEW); camera.up.set(0, 1, 0);
  controls.target.set(0, 0, 0);
};
document.getElementById('btnTop').onclick = () => {
  camera.position.set(0, 260, 0.01); camera.up.set(0, 1, 0); controls.target.set(0, 0, 0);
};
document.getElementById('btnMap').onclick = e => {
  mapIdx = (mapIdx + 1) % MAPS.length;
  e.target.textContent = 'MAP: ' + MAPS[mapIdx].toUpperCase();
  applyBasemap(MAPS[mapIdx]);
};
// ---------- observer site panel ----------
const sitePanel = document.getElementById('sitePanel');
document.getElementById('btnSite').onclick = async () => {
  const show = sitePanel.style.display === 'none' || !sitePanel.style.display;
  sitePanel.style.display = show ? 'block' : 'none';
  if (!show) return;
  document.getElementById('siteMsg').textContent = '';
  try {   // prefill from server config (PC server has GET; Pi falls back)
    const c = await (await fetch('/api/config')).json();
    document.getElementById('siteLat').value = c.home_lat ?? HLAT;
    document.getElementById('siteLon').value = c.home_lon ?? HLON;
    document.getElementById('siteAlt').value = c.home_alt_m ?? '';
  } catch(e){
    document.getElementById('siteLat').value = HLAT;
    document.getElementById('siteLon').value = HLON;
  }
  initSiteMap(parseFloat(document.getElementById('siteLat').value) || HLAT,
              parseFloat(document.getElementById('siteLon').value) || HLON);
};

// ---------- site picker: mini map + geocoder + DEM altitude ----------
let siteMap = null, siteSync = false;
async function elevAt(lat, lon){          // decode a terrarium DEM pixel
  const z = 10, n = 2 ** z;
  const xf = (lon + 180) / 360 * n;
  const lr = lat * Math.PI / 180;
  const yf = (1 - Math.log(Math.tan(lr) + 1 / Math.cos(lr)) / Math.PI) / 2 * n;
  const tx = Math.floor(xf), ty = Math.floor(yf);
  const im = await loadImg(`/api/mtile/terrain/${z}/${tx}/${ty}`, false);
  const cv = document.createElement('canvas'); cv.width = cv.height = 256;
  const ctx = cv.getContext('2d'); ctx.drawImage(im, 0, 0);
  const px = Math.min(255, Math.floor((xf - tx) * 256));
  const py = Math.min(255, Math.floor((yf - ty) * 256));
  const d = ctx.getImageData(px, py, 1, 1).data;
  return Math.max(0, Math.round(d[0] * 256 + d[1] + d[2] / 256 - 32768));
}
function initSiteMap(lat, lon){
  if (!siteMap){
    siteMap = L.map('siteMap', {attributionControl: false}).setView([lat, lon], 11);
    L.tileLayer('/api/mtile/osm/{z}/{x}/{y}', {maxZoom: 16, minZoom: 5}).addTo(siteMap);
    L.control.attribution({prefix: '© OpenStreetMap'}).addTo(siteMap);
    siteMap.on('move', () => {              // crosshair center -> live fields
      if (siteSync) return;
      const c = siteMap.getCenter();
      siteSync = true;
      document.getElementById('siteLat').value = c.lat.toFixed(5);
      document.getElementById('siteLon').value = c.lng.toFixed(5);
      siteSync = false;
    });
    siteMap.on('moveend', async () => {     // altitude from DEM at rest
      const c = siteMap.getCenter();
      try {
        const e = await elevAt(c.lat, c.lng);
        document.getElementById('siteAlt').value = e;
        document.getElementById('siteMsg').textContent = 'altitude ' + e + ' m (from DEM)';
      } catch(_){}
    });
    for (const id of ['siteLat', 'siteLon'])   // typed fields -> pan map
      document.getElementById(id).addEventListener('change', () => {
        if (siteSync || !siteMap) return;
        const la = parseFloat(document.getElementById('siteLat').value);
        const lo = parseFloat(document.getElementById('siteLon').value);
        if (isFinite(la) && isFinite(lo)){
          siteSync = true; siteMap.setView([la, lo]); siteSync = false;
        }
      });
  } else siteMap.setView([lat, lon], siteMap.getZoom());
  setTimeout(() => siteMap.invalidateSize(), 80);
}
async function siteSearch(){
  const q = document.getElementById('siteSearch').value.trim();
  const res = document.getElementById('siteResults');
  if (!q) return;
  res.textContent = 'searching…';
  try {
    const d = await (await fetch('/api/geocode?q=' + encodeURIComponent(q))).json();
    if (!Array.isArray(d) || !d.length){ res.textContent = 'no results'; return; }
    res.innerHTML = d.map(r =>
      `<div style="cursor:pointer;padding:3px 2px;border-bottom:1px dashed var(--line);
       color:var(--cyan)">${r.name.slice(0, 48)}</div>`).join('');
    [...res.children].forEach((el, i) => el.onclick = () => {
      res.innerHTML = '';
      initSiteMap(d[i].lat, d[i].lon);
      siteMap.setView([d[i].lat, d[i].lon], 12);
    });
  } catch(e){ res.textContent = 'search failed'; }
}
document.getElementById('siteSearchBtn').onclick = siteSearch;
document.getElementById('siteSearch').addEventListener('keydown',
  e => { if (e.key === 'Enter') siteSearch(); });

document.getElementById('siteGeo').onclick = () => {
  const msg = document.getElementById('siteMsg');
  if (!navigator.geolocation){ msg.textContent = 'geolocation unavailable'; return; }
  msg.textContent = 'locating…';
  navigator.geolocation.getCurrentPosition(p => {
    document.getElementById('siteLat').value = p.coords.latitude.toFixed(5);
    document.getElementById('siteLon').value = p.coords.longitude.toFixed(5);
    if (p.coords.altitude != null)
      document.getElementById('siteAlt').value = Math.round(p.coords.altitude);
    msg.textContent = 'got fix ±' + Math.round(p.coords.accuracy) + ' m' +
      (p.coords.altitude == null ? ' (no altitude — enter manually)' : '');
  }, err => {
    msg.textContent = 'blocked: ' + err.message +
      ' (browsers require HTTPS for geolocation — enter manually)';
  }, {enableHighAccuracy: true, timeout: 10000});
};
document.getElementById('siteSave').onclick = async () => {
  const msg = document.getElementById('siteMsg');
  const body = {home_lat: parseFloat(document.getElementById('siteLat').value),
                home_lon: parseFloat(document.getElementById('siteLon').value),
                home_alt_m: parseFloat(document.getElementById('siteAlt').value || '60')};
  if (!isFinite(body.home_lat) || !isFinite(body.home_lon)){
    msg.textContent = 'invalid lat/lon'; return;
  }
  try {
    const r = await (await fetch('/api/config', {method: 'POST',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)})).json();
    if (r.ok === false){ msg.textContent = 'rejected: ' + (r.info || ''); return; }
    msg.textContent = 'saved — reloading terrain for new site…';
    setTimeout(() => location.reload(), 900);
  } catch(e){ msg.textContent = 'save failed: ' + e; }
};

// ATC audio: opens LiveATC's own compact HTML5 player (their ToS prohibits
// embedding the raw streams in third-party pages, and the mounts reject
// hotlinking anyway — this is the compliant, reliable path)
document.getElementById('atcSel').onchange = e => {
  if (!e.target.value) return;
  window.open('https://www.liveatc.net/hlisten.php?mount=' + e.target.value
              + '&icao=ksjc', 'atcPlayer',
              'width=440,height=260,menubar=no,toolbar=no,location=no');
};
document.getElementById('btnLabels').onclick = e => {
  showLabels = !showLabels;
  e.target.textContent = 'LABELS: ' + (showLabels ? 'ON' : 'OFF');
  for (const p of planes.values()) p.label.visible = showLabels;
};

// ---------- the Moon ----------
// True az/el direction from home (from /api/lunar), cosmetic distance just
// beyond the rim. Phase is rendered physically: an isolated directional light
// shines from the real Sun's az/el, so crescent shape + tilt are correct.
// When the Moon is up, a translucent sight-tube links home to it — a plane
// crossing that tube is about to transit the disc.
const MOON_DIST = 118 * SCALE, MOON_R = 7 * SCALE;
let moonMesh = null, moonTube, moonLabel, moonLabelText = '';
let transitDeg = 0.373;              // updated from /api/lunar thresholds
function azelToWorld(az, el, dist){
  const a = az * Math.PI / 180, e = el * Math.PI / 180;
  return new THREE.Vector3(Math.cos(e) * Math.sin(a) * dist,
                           Math.sin(e) * dist,
                          -Math.cos(e) * Math.cos(a) * dist);
}
let moonMat = null;
function initMoon(){
  // Phase is computed in a tiny custom shader (texture lit by a sun-direction
  // uniform + faint earthshine). Scene lights can't touch it — the previous
  // light-based approach was washed out by the terrain's ambient light.
  const flat = new THREE.DataTexture(new Uint8Array([185, 190, 198, 255]), 1, 1);
  flat.needsUpdate = true;
  moonMat = new THREE.ShaderMaterial({
    uniforms: {map: {value: flat},
               sunDir: {value: new THREE.Vector3(1, 0, 0)},
               earthshine: {value: 0.10}},
    vertexShader: `
      varying vec2 vUv; varying vec3 vN;
      void main(){
        vUv = uv;
        vN = normalize(mat3(modelMatrix) * normal);
        gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
      }`,
    fragmentShader: `
      uniform sampler2D map; uniform vec3 sunDir; uniform float earthshine;
      varying vec2 vUv; varying vec3 vN;
      void main(){
        vec3 tex = texture2D(map, vUv).rgb;
        float l = max(dot(normalize(vN), normalize(sunDir)), 0.0);
        l = pow(l, 0.8);                       // soften the terminator a touch
        gl_FragColor = vec4(tex * (l * 1.15 + earthshine), 1.0);
      }`});
  moonMesh = new THREE.Mesh(new THREE.SphereGeometry(MOON_R, 48, 32), moonMat);
  new THREE.TextureLoader().load('/api/moontex', t => {
    t.colorSpace = THREE.SRGBColorSpace;
    moonMat.uniforms.map.value = t;
  });
  scene.add(moonMesh);
  // sight CONE, not cylinder: thin at home, widening at exactly the transit
  // threshold angle — its width IS the alert zone at every distance
  const rTop = Math.tan(transitDeg * Math.PI / 180) * MOON_DIST;
  moonTube = new THREE.Mesh(
    new THREE.CylinderGeometry(rTop, 0.03, 1, 16, 1, true),
    new THREE.MeshBasicMaterial({color: 0x23e6ff, transparent: true,
      opacity: 0.16, depthWrite: false, side: THREE.DoubleSide}));
  scene.add(moonTube);
}
let lastMoon = null;
function updateMoon(m){
  lastMoon = m;
  if (!moonMesh) initMoon();
  const pos = azelToWorld(m.az, m.el, MOON_DIST);
  moonMesh.position.copy(pos);
  moonMesh.lookAt(0, 0, 0);          // near side faces home
  if (m.sun_az != null)              // phase: light from the real Sun direction
    moonMat.uniforms.sunDir.value.copy(azelToWorld(m.sun_az, m.sun_el, 1));
  updateSun(m);
  const txt = `☾ ${(m.illum * 100).toFixed(0)}% · el ${m.el.toFixed(0)}°`;
  if (txt !== moonLabelText){
    moonLabelText = txt;
    if (moonLabel) scene.remove(moonLabel);
    moonLabel = textSprite(txt, m.el > 0 ? '#d8dee9' : '#5d7a9c');
    scene.add(moonLabel);
  }
  moonLabel.position.copy(pos).add(new THREE.Vector3(0, MOON_R + 4, 0));
  moonTube.visible = m.el > 0;
  if (moonTube.visible){
    moonTube.scale.set(1, pos.length() - MOON_R, 1);
    moonTube.position.copy(pos).multiplyScalar(0.5);
    moonTube.quaternion.setFromUnitVectors(
      new THREE.Vector3(0, 1, 0), pos.clone().normalize());
    // cyan when the alert gate is open; dim gray when moon below min elevation
    const armed = m.el >= (m.min_elev_deg ?? 10);
    moonTube.material.color.set(armed ? 0x23e6ff : 0x5d7a9c);
    moonTube.material.opacity = armed ? 0.16 : 0.07;
  }
}
// ---------- the Sun (not to scale; true az/el; hidden below horizon) ----------
let sunMesh = null, sunGlow = null;
function updateSun(m){
  if (m.sun_az == null) return;
  if (!sunMesh){
    sunMesh = new THREE.Mesh(new THREE.SphereGeometry(11, 20, 14),
      new THREE.MeshBasicMaterial({color: 0xfff3d0, fog: false}));
    const cv = document.createElement('canvas');
    cv.width = cv.height = 128;
    const cx = cv.getContext('2d');
    const g = cx.createRadialGradient(64, 64, 4, 64, 64, 62);
    g.addColorStop(0, 'rgba(255,244,210,0.95)');
    g.addColorStop(0.35, 'rgba(255,220,150,0.30)');
    g.addColorStop(1, 'rgba(255,210,140,0)');
    cx.fillStyle = g; cx.fillRect(0, 0, 128, 128);
    sunGlow = new THREE.Sprite(new THREE.SpriteMaterial(
      {map: new THREE.CanvasTexture(cv), transparent: true, depthWrite: false, fog: false}));
    sunGlow.scale.set(55, 55, 1);
    scene.add(sunMesh); scene.add(sunGlow);
  }
  const p = azelToWorld(m.sun_az, m.sun_el, 560);
  sunMesh.position.copy(p);
  sunGlow.position.copy(p);
  sunMesh.visible = sunGlow.visible = m.sun_el > -6;
}

// ---------- starfield ----------
(function stars(){
  const N = 1500;
  const pos = new Float32Array(N * 3), col = new Float32Array(N * 3);
  for (let i = 0; i < N; i++){
    const u = Math.random() * 2 - 1, th = Math.random() * Math.PI * 2;
    const s = Math.sqrt(1 - u * u), r = 620;
    pos[i*3] = r * s * Math.cos(th);
    pos[i*3+1] = r * u;
    pos[i*3+2] = r * s * Math.sin(th);
    const b = 0.30 + Math.random() * 0.70;      // brightness spread
    const w = Math.random();                     // slight color temperature
    col[i*3]   = b * (w > 0.85 ? 1.0 : 0.92);
    col[i*3+1] = b * 0.95;
    col[i*3+2] = b * (w < 0.15 ? 1.0 : 0.92);
  }
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.BufferAttribute(pos, 3));
  g.setAttribute('color', new THREE.BufferAttribute(col, 3));
  scene.add(new THREE.Points(g, new THREE.PointsMaterial(
    {size: 1.7, sizeAttenuation: false, vertexColors: true,
     transparent: true, opacity: .9, fog: false, depthWrite: false})));
})();



// ---------- local-horizon skyline (from the NINA horizon file) ----------
let horizonMesh = null;
let hrzOn = localStorage.getItem('hrzOn') !== '0';   // default visible
function horizonInterp(pts, az){
  az = ((az % 360) + 360) % 360;
  let i = pts.findIndex(p => p[0] >= az);
  let a0, h0, a1, h1;
  if (i === -1){ a0 = pts[pts.length-1][0]; h0 = pts[pts.length-1][1];
                 a1 = pts[0][0] + 360;      h1 = pts[0][1]; }
  else if (i === 0){ a0 = pts[pts.length-1][0] - 360; h0 = pts[pts.length-1][1];
                     a1 = pts[0][0];                  h1 = pts[0][1]; }
  else { a0 = pts[i-1][0]; h0 = pts[i-1][1]; a1 = pts[i][0]; h1 = pts[i][1]; }
  return h0 + (az - a0) / Math.max(1e-6, a1 - a0) * (h1 - h0);
}
function buildHorizon(pts){
  if (horizonMesh || !pts || pts.length < 3) return;
  const D = MOON_DIST * 0.985, N = 144;
  const verts = [], idx = [], edge = [];
  for (let i = 0; i <= N; i++){
    const az = i * 360 / N;
    const alt = Math.max(0.2, horizonInterp(pts, az));
    const top = azelToWorld(az, alt, D);
    const bot = azelToWorld(az, 0, D);
    verts.push(bot.x, bot.y, bot.z, top.x, top.y, top.z);
    edge.push(top);
  }
  for (let i = 0; i < N; i++){
    const a = i * 2, b = a + 1, c = a + 2, d = a + 3;
    idx.push(a, c, b, b, c, d);
  }
  const geo = new THREE.BufferGeometry();
  geo.setAttribute('position', new THREE.Float32BufferAttribute(verts, 3));
  geo.setIndex(idx);
  const wall = new THREE.Mesh(geo, new THREE.MeshBasicMaterial(
    {color: 0x141b26, transparent: true, opacity: .42, side: THREE.DoubleSide,
     depthWrite: false, fog: false}));
  const line = new THREE.Line(new THREE.BufferGeometry().setFromPoints(edge),
    new THREE.LineBasicMaterial({color: 0x3a4a60, transparent: true,
                                 opacity: .8, fog: false}));
  horizonMesh = new THREE.Group();
  horizonMesh.add(wall); horizonMesh.add(line);
  horizonMesh.visible = hrzOn;
  scene.add(horizonMesh);
  const b = document.getElementById('btnHrz');
  b.textContent = '⛰ HORIZON: ' + (hrzOn ? 'ON' : 'OFF');
  b.onclick = () => {
    hrzOn = !hrzOn;
    localStorage.setItem('hrzOn', hrzOn ? '1' : '0');
    horizonMesh.visible = hrzOn;
    b.textContent = '⛰ HORIZON: ' + (hrzOn ? 'ON' : 'OFF');
  };
}

let moonPreview = false;
async function pollMoon(){
  try {
    const d = await (await fetch('/api/lunar')).json();
    if (d.thresholds?.transit_deg) transitDeg = d.thresholds.transit_deg;
    if (d.horizon) buildHorizon(d.horizon);
    if (d.moon && !moonPreview) updateMoon(d.moon);   // preview freezes the moon
    scanTransitAlarms(d);                             // …but alarms stay live
    syncRec(d.capture);
  } catch(e){}
}
pollMoon(); setInterval(pollMoon, 5000);

// ---------- manual capture: roll the camera by hand ----------
let recording = false, recBusy = false;
const recBtn = document.getElementById('btnRec');

function syncRec(cap){
  // server is the source of truth, so the button stays right even if the
  // recording was started from /lunar or ended on the safety auto-stop
  if (recBusy || !cap) return;
  recording = !!cap.manual_rec;
  recBtn.textContent = recording ? '⏹ STOP' : '⏺ REC';
  recBtn.classList.toggle('live', recording);
  recBtn.disabled = !cap.enabled || !cap.host;
  recBtn.title = !cap.enabled ? 'capture_enabled is false in config.json'
               : !cap.host ? 'capture_host not configured'
               : (recording
                    ? 'Recording' + (cap.manual_stop_in_s != null
                        ? ' — auto-stop in ' + cap.manual_stop_in_s + 's' : '')
                    : 'Send REC to ' + cap.host + ':' + cap.port);
}

recBtn.onclick = async () => {
  if (recBusy) return;
  const action = recording ? 'STOP' : 'REC';
  recBusy = true; recBtn.disabled = true;
  recBtn.textContent = action === 'REC' ? '⏺ …' : '⏹ …';
  try {
    const r = await (await fetch('/api/lunar/capture-manual', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action: action})})).json();
    recording = !!r.recording;
    statusEl.textContent = (r.ok ? '✅ ' : '❌ ') + action + ' — ' + r.info;
  } catch(e){
    statusEl.textContent = '❌ ' + action + ' failed: ' + e;
  } finally {
    recBusy = false; recBtn.disabled = false;
    recBtn.textContent = recording ? '⏹ STOP' : '⏺ REC';
    recBtn.classList.toggle('live', recording);
  }
};

// ---------- transit alarm: flash the offending plane red for 60 s ----------
const ALARM_COL = new THREE.Color(0xff2233);
const ALARM_MS = 60000;
function scanTransitAlarms(d){
  const now = Date.now();
  for (const e of (d.events || [])){
    if (e.kind !== 'transit' || !e.hex) continue;
    const until = e.t * 1000 + ALARM_MS;
    if (until <= now) continue;
    const p = planes.get(e.hex);
    if (p) p.alarmUntil = Math.max(p.alarmUntil || 0, until);
  }
}
function alarmTick(p, now){
  if (p.alarmUntil && now < p.alarmUntil){
    if (!p.alarmMats){                 // capture original colors once
      p.alarmMats = [];
      p.model.traverse(o => {
        if (o.isMesh && o.material && o.material.color)
          p.alarmMats.push([o.material, o.material.color.clone()]);
      });
      p.alarmMats.push([p.stalk.material, p.stalk.material.color.clone()]);
    }
    const on = (now % 500) < 250;      // 2 Hz strobe
    for (const [m, orig] of p.alarmMats) m.color.copy(on ? ALARM_COL : orig);
  } else if (p.alarmMats){             // alarm expired — restore
    for (const [m, orig] of p.alarmMats) m.color.copy(orig);
    p.alarmMats = null;
    p.alarmUntil = 0;
  }
}

document.getElementById('tPick').onchange = async e => {
  if (!e.target.value) return;
  const t = new Date(e.target.value).getTime() / 1000;
  try {
    const d = await (await fetch('/api/lunar/at?t=' + t)).json();
    if (d.ok === false){ statusEl.textContent = 'preview failed: ' + d.info; return; }
    moonPreview = true;
    document.getElementById('btnNow').style.display = '';
    updateMoon(d);
    statusEl.textContent = `☾ preview ${e.target.value.replace('T', ' ')} — ` +
      `az ${d.az}° el ${d.el}° · ${(d.illum * 100).toFixed(0)}% lit` +
      (d.el > 0 ? '' : ' (below horizon)');
  } catch(err){ statusEl.textContent = 'preview failed'; }
};
document.getElementById('btnNow').onclick = () => {
  moonPreview = false;
  document.getElementById('btnNow').style.display = 'none';
  document.getElementById('tPick').value = '';
  statusEl.textContent = 'moon back to live';
  pollMoon();
};

// ---------- cinematic tour mode ----------
// Slow clockwise orbit of the plate. While the Moon is up the orbit sweeps the
// arc opposite it (so the Moon stays in frame) and the look-target is biased
// toward it. Every ~20 s: ease into a chase-cam behind a random plane, follow
// ~9 s, glide back. Any mouse-down exits the tour.
let tour = null;
const TOUR = {radius: 175 * SCALE, height: 92 * SCALE, rate: 0.055, sweep: 72 * Math.PI / 180};
let tvAngle = 0, tvLast = 0, tvNextZoom = 0;
const tvTarget = new THREE.Vector3(0, 6, 0);

function startTour(){
  tour = {phase: 'orbit', osc: 0};
  controls.enabled = false;
  tvAngle = Math.atan2(camera.position.x, camera.position.z);
  tvLast = performance.now() / 1000;
  tvNextZoom = tvLast + 10;
  document.getElementById('btnTour').textContent = '⏹ STOP TOUR';
}
function stopTour(){
  tour = null;
  controls.enabled = true;
  hideInfo();
  document.getElementById('btnTour').textContent = '🎬 TOUR';
}
document.getElementById('btnTour').onclick = () => tour ? stopTour() : startTour();

// ---------- MOON POV: the camera stands at your house, looking along the
// line of sight. In THIS view (only), a plane overlapping the beam on screen
// is genuinely about to cross the Moon's disc — no parallax lies.
let homePOV = false;
function setPOV(on){
  homePOV = on;
  if (on && tour) stopTour();
  controls.enabled = !on;
  camera.fov = on ? 30 : 55;
  camera.updateProjectionMatrix();
  document.getElementById('btnPov').textContent = on ? '⏹ EXIT POV' : '🌖 MOON POV';
  if (!on){ camera.position.copy(HOME_VIEW); controls.target.set(0, 0, 0); }
}
document.getElementById('btnPov').onclick = () => setPOV(!homePOV);
canvas.addEventListener('pointerdown', () => {
  if (tour) stopTour();
  if (homePOV) setPOV(false);
});

function tourTick(){
  const now = performance.now() / 1000;
  const dt = Math.min(0.1, now - tvLast);
  tvLast = now;
  const moonUp = lastMoon && lastMoon.el > 0 && moonMesh;
  if (tour.phase === 'orbit'){
    if (moonUp){
      const center = Math.atan2(moonMesh.position.x, moonMesh.position.z);
      tour.osc += dt * TOUR.rate * 1.6;
      tvAngle = center + Math.sin(tour.osc) * TOUR.sweep;
    } else {
      tvAngle -= dt * TOUR.rate;               // full clockwise orbit
    }
    const want = new THREE.Vector3(Math.sin(tvAngle) * TOUR.radius, TOUR.height,
                                   Math.cos(tvAngle) * TOUR.radius);
    camera.position.lerp(want, 1 - Math.exp(-dt * 1.8));
    const tgt = new THREE.Vector3(0, 6, 0);
    if (moonUp) tgt.lerp(moonMesh.position, 0.30);
    tvTarget.lerp(tgt, 1 - Math.exp(-dt * 1.8));
    camera.up.set(0, 1, 0);
    camera.lookAt(tvTarget);
    if (now > tvNextZoom){
      const cands = [...planes.values()].filter(p =>
        (p.data.alt || 0) > 4000 && p.pts.length > 3);
      if (cands.length){
        tour.plane = cands[Math.floor(Math.random() * cands.length)];
        tour.phase = 'zoom';
        tour.until = now + 9;
        showPlaneInfo(tour.plane);              // open the info panel for it
      } else {
        tvNextZoom = now + 10;
      }
    }
  } else if (tour.phase === 'zoom'){
    const p = tour.plane;
    if (!planes.has(p.data.hex) || now > tour.until){
      tour.phase = 'orbit';
      hideInfo();                               // close panel when chase ends
      tvAngle = Math.atan2(camera.position.x, camera.position.z);
      tvNextZoom = now + 20;
      return;
    }
    const pos = p.group.position;
    const trk = (p.data.track || 0) * Math.PI / 180;
    const chase = pos.clone()
      .add(new THREE.Vector3(-Math.sin(trk), 0, Math.cos(trk)).multiplyScalar(11))
      .add(new THREE.Vector3(0, 4.5, 0));
    camera.position.lerp(chase, 1 - Math.exp(-dt * 1.4));
    tvTarget.lerp(pos, 1 - Math.exp(-dt * 3));
    camera.up.set(0, 1, 0);
    camera.lookAt(tvTarget);
  }
}


// ---------- weather: METAR cloud layers + ground fog (☁ WX toggle) ----------
const wxGroup = new THREE.Group();
plate.add(wxGroup);
let wxOn = false, wxData = null, wxTimer = null;
const wxTex = (() => {
  const cv = document.createElement('canvas'); cv.width = cv.height = 256;
  const cx = cv.getContext('2d');
  const g = cx.createRadialGradient(128, 128, 20, 128, 128, 126);
  g.addColorStop(0, 'rgba(255,255,255,0.9)');
  g.addColorStop(0.6, 'rgba(255,255,255,0.55)');
  g.addColorStop(1, 'rgba(255,255,255,0)');
  cx.fillStyle = g; cx.fillRect(0, 0, 256, 256);
  return new THREE.CanvasTexture(cv);
})();
const COVER_OP = {FEW: .12, SCT: .26, BKN: .5, OVC: .72};

function clearWx(){
  while (wxGroup.children.length){
    const c = wxGroup.children.pop();
    c.material.dispose(); c.geometry.dispose();
  }
}
function buildWx(){
  clearWx();
  if (!wxOn || !wxData) return;
  for (const s of wxData.stations){
    if (s.lat == null) continue;
    const [x, z] = toWorld(s.lat, s.lon);
    if (Math.hypot(x, z) > R_KM) continue;
    for (const c of (s.clouds || [])){
      const op = COVER_OP[c.cover];
      if (!op || c.base > 14000) continue;
      const mslFt = c.base + (s.elev_m || 0) * 3.281;
      const m = new THREE.Mesh(new THREE.CircleGeometry(9, 24),
        new THREE.MeshBasicMaterial({map: wxTex, transparent: true, opacity: op,
          depthWrite: false, fog: false, side: THREE.DoubleSide}));
      m.rotation.x = -Math.PI / 2;
      m.rotation.z = Math.random() * Math.PI;
      m.position.set(x, Math.max(0.4, mslFt * FT2KM * EX), z);
      wxGroup.add(m);
    }
    if (s.visib_mi != null && s.visib_mi < 3){       // fog / marine layer on deck
      const m = new THREE.Mesh(new THREE.CircleGeometry(11, 24),
        new THREE.MeshBasicMaterial({map: wxTex, color: 0xdfe9f2, transparent: true,
          opacity: Math.min(.6, .25 + .35 * (3 - s.visib_mi) / 3),
          depthWrite: false, fog: false, side: THREE.DoubleSide}));
      m.rotation.x = -Math.PI / 2;
      m.position.set(x, ((s.elev_m || 0) / 1000) * EX + 0.25, z);
      wxGroup.add(m);
    }
  }
}
async function fetchWx(){
  try {
    const d = await (await fetch('/api/wx?r_km=' + Math.round(R_KM * 1.1))).json();
    if (d.ok){
      wxData = d; buildWx();
      const newest = Math.max(0, ...d.stations.map(s => s.obs || 0));
      const age = newest ? Math.round((Date.now() / 1000 - newest) / 60) : null;
      statusEl.textContent = 'weather: ' + d.stations.length + ' stations, '
        + wxGroup.children.length + ' layers'
        + (age != null ? ' · newest METAR ' + age + ' min ago' : '');
    } else statusEl.textContent = 'weather fetch failed: ' + (d.info || '');
  } catch(e){ statusEl.textContent = 'weather fetch failed'; }
}
function setWx(on){
  wxOn = on;
  localStorage.setItem('wxOn', on ? '1' : '0');
  document.getElementById('btnWx').textContent = '☁ WX: ' + (on ? 'ON' : 'OFF');
  if (on){ fetchWx(); if (!wxTimer) wxTimer = setInterval(fetchWx, 300000); }
  else { clearWx(); if (wxTimer){ clearInterval(wxTimer); wxTimer = null; } }
}
document.getElementById('btnWx').onclick = () => setWx(!wxOn);
if (localStorage.getItem('wxOn') === '1') setWx(true);

// debug handle (harmless in production; used for live inspection)
window.DBG = {three: THREE, renderer, scene, camera, planes,
              term: () => ({mesh: terrainMesh, geo: terrainGeo})};

// ---------- go ----------
buildTerrain();
poll(); setInterval(poll, 1000);
(function loop(){
  requestAnimationFrame(loop);
  if (homePOV && moonMesh){
    // sit ON the sight-line axis just above the house: the cone becomes a
    // halo around the Moon instead of a beam seen from the side
    camera.position.copy(moonMesh.position).normalize().multiplyScalar(1.5);
    camera.up.set(0, 1, 0);
    camera.lookAt(moonMesh.position);
  } else if (tour) tourTick();
  else controls.update();
  const nowMs = Date.now();
  for (const p of planes.values()){
    if (p.rotor) p.rotor.rotation.y += 0.35;
    alarmTick(p, nowMs);
    // keep labels a sane screen size: full world scale when far, shrink
    // proportionally inside ~70 units so close-ups aren't billboard-sized
    const d = camera.position.distanceTo(p.group.position);
    const s = Math.min(1, Math.max(0.28, d / 70));
    const b = p.label.userData.baseScale;
    if (b) p.label.scale.set(b[0] * s, b[1] * s, 1);
  }
  renderer.render(scene, camera);
})();
</script>
</body>
</html>
"""
