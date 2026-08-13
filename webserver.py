#!/usr/bin/env python3
import json, threading, time, os, subprocess, glob, traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
import cereal.messaging as messaging
from openpilot.common.params import Params

HOST, PORT = "0.0.0.0", 7070
PARAMS = {"personality":"LongitudinalPersonality","follow_distance":"NAPFollowDistance",
          "adaptive_accel":"NAPAdaptiveAccel","experimental":"ExperimentalMode"}
PERSONALITIES = {0:"aggressive", 1:"standard", 2:"chill"}
STATE = {"ts":0,"car":{},"drive":{},"plan":{},"lead1":{},"lead2":{},"path":{"curvature":0.0},
         "settings":{},"health":{},"errors":[]}
LOCK=threading.Lock(); STOP=threading.Event()

params = Params()

def num(v,d=0.0):
  try:
    if hasattr(v, 'raw'): v = v.raw
    x=float(v); return x if x==x and abs(x)!=float("inf") else d
  except Exception: return d

def safe_int(v, d=0):
  try:
    if hasattr(v, 'raw'): v = v.raw
    return int(v)
  except Exception:
    return d

def safe_attr(obj, attr, default=0):
  if obj is None: return default
  try:
    return getattr(obj, attr, default)
  except Exception:
    return default

def lead_dict(lead):
  out={}
  if lead is None:return out
  for k in ("status","dRel","yRel","vRel","vLead","aLeadK","aLeadTau","modelProb","radar","fcw","aLead","dPath","vLat"):
    try:
      v=getattr(lead,k)
      out[k]=v if isinstance(v,bool) else num(v)
    except Exception:pass
  return out

def path_curvature(mdl):
  if mdl is None: return 0.0
  try:
    pos = mdl.position
    xs = list(pos.x); ys = list(pos.y)
    if len(xs) < 5: return 0.0
    def y_at(target_x):
      best_i, best_d = 0, 1e9
      for i, x in enumerate(xs):
        d = abs(x - target_x)
        if d < best_d: best_d = d; best_i = i
      return ys[best_i]
    near = y_at(5.0)
    far = y_at(40.0)
    c = (far - near) / 8.0
    return max(-1.0, min(1.0, c))
  except Exception:
    return 0.0

def read_params():
  try:
    p = Params()
    r = {}
    try: v = p.get(PARAMS["personality"]); r["personality_raw"] = int(v) if v is not None else 1
    except Exception: r["personality_raw"] = 1

    try: v = p.get(PARAMS["follow_distance"]); r["follow_distance"] = int(v) if v is not None else 4
    except Exception: r["follow_distance"] = 4

    try: r["adaptive_accel"] = p.get_bool(PARAMS["adaptive_accel"])
    except Exception: r["adaptive_accel"] = False

    try: r["experimental"] = p.get_bool(PARAMS["experimental"])
    except Exception: r["experimental"] = False

    r["personality"] = PERSONALITIES.get(r["personality_raw"], "unknown")
    return r
  except Exception:
    return {"personality_raw":1, "follow_distance":4, "adaptive_accel":False, "experimental":False, "personality":"standard"}

def get_routes():
  routes_dict = {}
  base = "/data/media/0/realdata"
  if not os.path.exists(base): return []

  try:
    for f in os.listdir(base):
      path = os.path.join(base, f)
      if os.path.isdir(path) and "--" in f:
        route_name, _, seg = f.rpartition("--")
        if seg.isdigit():
          if route_name not in routes_dict:
            routes_dict[route_name] = []
          routes_dict[route_name].append(int(seg))
  except Exception: pass

  routes = []
  for route, segs in routes_dict.items():
    routes.append({"name": route, "segs": sorted(segs)})

  routes.sort(key=lambda x: x["name"], reverse=True)
  return routes

def telemetry():
  target_services = ["carState", "selfdriveState", "controlsState", "longitudinalPlan", "radarState", "deviceState", "modelV2"]
  valid_services = target_services.copy()

  sm = None
  while valid_services:
    try:
      sm = messaging.SubMaster(valid_services)
      break
    except Exception as e:
      bad_srv = str(e.args[0]) if e.args else str(e)
      removed = False
      for s in valid_services.copy():
        if s in bad_srv:
          valid_services.remove(s)
          removed = True
      if not removed:
        with LOCK: STATE["errors"]=[f"IPC Bind Error: {str(e)}"]
        return

  tick = 0
  while not STOP.is_set():
    try:
      sm.update(100)
      cs = sm["carState"] if "carState" in valid_services else None
      sd = sm["selfdriveState"] if "selfdriveState" in valid_services else None
      ctl = sm["controlsState"] if "controlsState" in valid_services else None
      lp = sm["longitudinalPlan"] if "longitudinalPlan" in valid_services else None
      radar = sm["radarState"] if "radarState" in valid_services else None
      ds = sm["deviceState"] if "deviceState" in valid_services else None
      mdl = sm["modelV2"] if "modelV2" in valid_services else None

      if tick % 20 == 0: current_settings = read_params()
      else: current_settings = STATE.get("settings", {})

      enabled = safe_attr(sd, 'enabled', safe_attr(ctl, 'enabled', False))
      active = safe_attr(sd, 'active', safe_attr(ctl, 'active', False))
      exp_mode = safe_attr(sd, 'experimentalMode', safe_attr(ctl, 'experimentalMode', False))
      pers_raw = safe_int(safe_attr(sd, 'personality', 1))

      v_cruise = num(safe_attr(cs, 'vCruise', 0))
      v_cruise_cluster = num(safe_attr(cs, 'vCruiseCluster', v_cruise))

      temps = safe_attr(ds, 'cpuTempC', [])
      try: max_temp = max(temps) if len(temps) > 0 else 0
      except Exception: max_temp = 0
      therm_stat = safe_int(safe_attr(ds, 'thermalStatus', 0))
      free_space = num(safe_attr(ds, 'freeSpacePercent', 0))

      curvature = path_curvature(mdl)

      with LOCK:
        STATE.update({"ts":time.time(),
          "health":{"temp":max_temp, "thermal":therm_stat, "space":free_space},
          "car":{
            "vEgo":num(safe_attr(cs, 'vEgo', 0)),
            "aEgo":num(safe_attr(cs, 'aEgo', 0)),
            "vCruise":v_cruise,
            "vCruiseCluster":v_cruise_cluster,
            "standstill":bool(safe_attr(cs, 'standstill', False)),
            "brakePressed":bool(safe_attr(cs, 'brakePressed', False)),
            "gasPressed":bool(safe_attr(cs, 'gasPressed', False))
          },
          "drive":{
            "enabled":enabled,
            "active":active,
            "experimentalMode":exp_mode,
            "personalityRaw":pers_raw,
            "personality":PERSONALITIES.get(pers_raw, "unknown"),
            "alertHudVisual":safe_int(safe_attr(sd, 'alertHudVisual', safe_attr(ctl, 'alertHudVisual', 0)))
          },
          "plan":{
            "aTarget":num(safe_attr(lp, 'aTarget', safe_attr(lp, 'aEgoTarget', 0))),
            "shouldStop":bool(safe_attr(lp, 'shouldStop', False)),
            "hasLead":bool(safe_attr(lp, 'hasLead', False)),
            "allowBrake":bool(safe_attr(lp, 'allowBrake', False)),
            "allowThrottle":bool(safe_attr(lp, 'allowThrottle', False)),
            "source":str(safe_attr(lp, 'longitudinalPlanSource', ''))
          },
          "lead1":lead_dict(safe_attr(radar, 'leadOne', None)),
          "lead2":lead_dict(safe_attr(radar, 'leadTwo', None)),
          "path":{"curvature":curvature},
          "settings":current_settings,
          "errors":[]})
      tick += 1
    except Exception as e:
      with LOCK:STATE["errors"]=[f"Parser Error: {str(e)}"]
    time.sleep(.05)

def write_setting(name,value):
  p = Params()
  if name=="personality":
    v=int(value)
    if v not in PERSONALITIES:raise ValueError("personality must be 0, 1, or 2")
    p.put(PARAMS[name], v)
  elif name=="follow_distance":
    v=int(value)
    if not 1<=v<=7:raise ValueError("follow distance must be 1..7")
    p.put(PARAMS[name], v)
  elif name in ("adaptive_accel","experimental"):
    v = bool(value)
    try: p.put_bool(PARAMS[name], v)
    except Exception: p.put(PARAMS[name], 1 if v else 0)
  else:
    raise ValueError("setting is not exposed")
  time.sleep(0.1)
  return read_params()

HTML = r"""<!doctype html><html><head><meta name=viewport content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>NAP Drive</title><style>
:root{--p:#111720;--p2:#0d131b;--line:#293241;--t:#f5f7fa;--m:#9aa7b7;--a:#56b6ff;--g:#45d483;--road-speed:0s;--curve:0;}
*{box-sizing:border-box}body{margin:0;background:linear-gradient(#070a0e,#0c1118);color:var(--t);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}.wrap{max-width:980px;margin:auto;padding:14px}
.top{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:12px}.title{font-size:22px;font-weight:800}.status{font-size:12px;color:var(--m);font-weight:700;text-align:right}
.health-bar{font-size:11px;color:var(--m);display:flex;gap:12px;margin-top:4px;justify-content:flex-end}
.nav-tabs{display:flex;gap:10px;margin-bottom:15px;background:#111720;padding:6px;border-radius:14px;border:1px solid var(--line)}
.nav-tabs button{flex:1;background:transparent;border:none;color:var(--m);padding:10px;font-weight:700;border-radius:10px;font-size:14px}
.nav-tabs button.active{background:#1a222e;color:var(--t)}
#demo-toggle{flex:0 0 auto;padding:10px 14px}
#demo-toggle.active{background:#3a2a1a;color:#ffb84d}
#demo-banner{display:none;text-align:center;font-size:12px;font-weight:800;letter-spacing:.04em;color:#ffb84d;background:#241a0f;border:1px solid #4a3319;border-radius:12px;padding:8px;margin-bottom:12px;animation:demoPulse 2s ease-in-out infinite}
@keyframes demoPulse{0%,100%{opacity:1}50%{opacity:.55}}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}@media(max-width:720px){.grid{grid-template-columns:1fr}}
.card{background:#111720f5;border:1px solid var(--line);border-radius:18px;padding:14px;box-shadow:0 8px 28px #0005}.label{font-size:12px;color:var(--m);text-transform:uppercase;letter-spacing:.08em;margin-bottom:9px}
.buttons{display:flex;gap:8px}.btn{flex:1;border:1px solid #344153;background:#1a222e;color:var(--t);padding:12px 8px;border-radius:12px;font-weight:700}.btn.active{background:#284d68;border-color:var(--a)}
.row{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:8px 0}.small{font-size:12px;color:var(--m)}
.switch{width:54px;height:30px;border-radius:20px;background:#303a47;border:0;position:relative}.switch i{position:absolute;width:24px;height:24px;top:3px;left:3px;border-radius:50%;background:#fff;transition:.15s}.switch.on{background:var(--g)}.switch.on i{left:27px}
.follow{display:grid;grid-template-columns:repeat(7,1fr);gap:5px}.follow button{padding:10px 2px;border-radius:9px;border:1px solid #344153;background:#1a222e;color:#fff}.follow button.active{background:#315b75;border-color:var(--a)}

.road{height:330px;position:relative;overflow:hidden;border-radius:16px;background:linear-gradient(90deg,#121922 0 25%,#1c2229 25% 75%,#121922 75%)}
@keyframes dashScroll{0%{stroke-dashoffset:0}100%{stroke-dashoffset:-64px}}
.pathline-svg{position:absolute;inset:0;width:100%;height:100%;pointer-events:none;z-index:2}
.lane-edge{stroke:#d9dde2;stroke-width:4;stroke-dasharray:32 32;opacity:.6;fill:none;animation:dashScroll var(--road-speed) linear infinite;transition:d .3s ease-out}
.lane-center{stroke:#87929d;stroke-width:4;stroke-dasharray:24 24;opacity:.5;fill:none;animation:dashScroll var(--road-speed) linear infinite;transition:d .3s ease-out}
#pathline{stroke:#ff9d3d;stroke-width:5;fill:none;opacity:.9;stroke-linecap:round;filter:drop-shadow(0 0 6px #ff9d3d88);transition:d .25s ease-out}
.car{position:absolute;left:50%;transform:translateX(-50%);bottom:15px;width:82px;height:126px;border-radius:24px 24px 16px 16px;background:linear-gradient(90deg,#6e7d8b,#e7edf2 45%,#657482);box-shadow:0 10px 28px #9dd8ff44, inset 0 -5px 10px #0004; z-index:10;transition:transform .35s ease-out}.car:before{content:"";position:absolute;left:12px;right:12px;top:15px;height:43px;border-radius:14px;background:linear-gradient(135deg,#263442,#0c131b);border:1px solid #9ab0c255}.wheel{position:absolute;width:9px;height:35px;background:#080b0f;border-radius:5px;top:45px}.w1{left:-5px}.w2{right:-5px}
.lead{position:absolute;left:50%;transform-origin:bottom center;width:64px;height:94px;border-radius:17px 17px 11px 11px;background:linear-gradient(90deg,#c95159,#ff9b72 48%,#b8444f);box-shadow:0 5px 25px #ff5d6744;transition:top 0.25s linear, left 0.25s linear, transform 0.25s linear, box-shadow 0.2s ease-out; z-index:5}.lead:before{content:"";position:absolute;left:10px;right:10px;top:10px;height:27px;border-radius:9px;background:#19222c;border:1px solid #fff3}.lead.off{opacity:0; transition:opacity 0.2s;}
.lead2{opacity:.7;filter:brightness(.82) saturate(.85);z-index:4}
.lead.vision-only{border:2px dashed #ffffffaa}
.lead.danger{animation:dangerPulse .55s ease-in-out infinite}
@keyframes dangerPulse{0%,100%{box-shadow:0 10px 40px #ff0000cc;transform:translateX(-50%) scale(var(--s,1))}50%{box-shadow:0 10px 60px #ff3344ee;transform:translateX(-50%) scale(calc(var(--s,1) * 1.04))}}

.telemetry{display:grid;grid-template-columns:repeat(4,1fr);gap:7px}.metric{background:var(--p2);border-radius:10px;padding:9px}.metric b{display:block;font-size:16px}.metric span{font-size:10px;color:var(--m)}
.leads{display:grid;grid-template-columns:1fr 1fr;gap:8px}.leadbox{background:var(--p2);border-radius:12px;padding:10px}.leadbox h3{margin:0 0 7px;font-size:13px;display:flex;align-items:center;gap:6px}.leadbox h3 .dot{width:8px;height:8px;border-radius:50%;background:#344153}.leadbox h3 .dot.on{background:var(--g)}.leadbox h3 .dot.vis{background:#56b6ff}.kv{display:grid;grid-template-columns:1fr 1fr;gap:5px;font-size:11px}.kv span{color:var(--m)}.footer{margin-top:10px;font-size:11px;color:#718093;text-align:center}

/* Video HUD Overlay Styles */
.vid-container{width:100%;position:relative;aspect-ratio:16/9;background:#000;border-radius:16px;overflow:hidden;margin-bottom:12px;border:1px solid var(--line)}
video{width:100%;height:100%}
.hud-overlay{position:absolute;inset:0;pointer-events:none;padding:15px;display:none;flex-direction:column;justify-content:space-between;z-index:20;background:linear-gradient(180deg, rgba(0,0,0,0.5) 0%, transparent 20%, transparent 80%, rgba(0,0,0,0.6) 100%);}
.hud-top{display:flex;justify-content:center;}
.hud-bot{display:flex;justify-content:space-between;align-items:flex-end;}
.hud-box{background:#00000088;backdrop-filter:blur(4px);border:1px solid #ffffff33;padding:6px 12px;border-radius:10px;color:#fff;font-weight:700;font-family:monospace;text-shadow:0 2px 4px #000;font-size:14px;}
.hud-pedals{display:flex;gap:6px;}
.hud-pedal{width:30px;height:8px;border-radius:4px;background:#344153;transition:background 0.1s;}
.hud-pedal.gas.active{background:var(--g);box-shadow:0 0 8px var(--g);}
.hud-pedal.brk.active{background:#ff5d67;box-shadow:0 0 8px #ff5d67;}

select.cam-drop{background:#1a222e;color:var(--t);border:1px solid #344153;padding:8px 12px;border-radius:10px;font-weight:700;font-size:13px;outline:none;}
.route-item{background:var(--p2);border-radius:12px;padding:12px;margin-bottom:10px;cursor:pointer;border:1px solid var(--line)}
.route-item b{font-size:14px;display:block}
.route-item .small{margin-top:3px}
.segs-grid{display:flex;flex-wrap:wrap;gap:6px;margin-top:12px;display:none}
.route-item.open .segs-grid{display:flex}
.seg-btn{background:#1a222e;border:1px solid #344153;color:var(--t);padding:8px 12px;border-radius:8px;font-size:12px;font-weight:700}
.seg-btn:active{background:var(--a)}.seg-btn.playing{background:var(--a);border-color:#fff;color:#000}
.btn-ui{text-decoration:none;padding:8px 14px;background:var(--a);color:#000;border-radius:10px;font-weight:700;font-size:13px;border:none;cursor:pointer;}
</style></head><body><div class=wrap>
<div class=top>
  <div class=title>NAP Drive</div>
  <div>
    <div id=status class=status>Connecting…</div>
    <div class=health-bar>
      <span>CPU: <b id="cpu-temp">—</b>°C</span>
      <span>Storage: <b id="free-space">—</b>%</span>
    </div>
  </div>
</div>

<div id="demo-banner"></div>

<div class="nav-tabs">
  <button id="tabbtn-drive" class="active" onclick="switchTab('drive')">Drive Settings</button>
  <button id="tabbtn-video" onclick="switchTab('video')">Dashcam Viewer</button>
  <button id="demo-toggle" onclick="toggleDemo()">▶ Demo Mode</button>
</div>

<div id="tab-video" style="display:none;">
  <div class="vid-container">
    <video id="player" playsinline autoplay></video>
    
    <!-- DASHCAM TELEMETRY OVERLAY -->
    <div id="hud-overlay" class="hud-overlay">
      <div class="hud-top">
        <div class="hud-box" style="color:var(--a);">LEAD: <span id="hud-lead">--</span> m</div>
      </div>
      <div class="hud-bot">
        <div class="hud-box" style="font-size:22px"><span id="hud-speed">0</span> <span style="font-size:12px;color:var(--m)">MPH</span></div>
        <div class="hud-pedals">
          <div id="hud-brake" class="hud-pedal brk"></div>
          <div id="hud-gas" class="hud-pedal gas"></div>
        </div>
        <div class="hud-box">STR: <span id="hud-steer">0</span>°</div>
      </div>
    </div>
    
  </div>
  <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:15px; padding:0 4px; flex-wrap:wrap; gap:10px;">
    <div style="display:flex; gap:10px;">
      <select id="cam-select" class="cam-drop" onchange="if(currentRoute) playVid(currentRoute, currentSeg)">
        <option value="qcamera">Road (Fast)</option>
        <option value="fcamera">Road (High-Res)</option>
        <option value="ecamera">Road (Wide)</option>
        <option value="dcamera">Driver Cam</option>
      </select>
      <button class="btn-ui" onclick="toggleHud()" style="background:#1a222e; color:var(--t); border:1px solid #344153;">Toggle HUD</button>
    </div>
    
    <div style="display:flex; gap:10px; align-items:center;">
      <button class="btn-ui" onclick="togglePlay()" id="vid-play-btn" style="display:none;">Pause</button>
      <a id="download-btn" class="btn-ui" href="#" style="display:none; background:var(--g);">Save Clip ↓</a>
    </div>
  </div>
  <div id="routes-list"><div class="label" style="text-align:center;margin-top:20px;">Loading routes...</div></div>
</div>

<div id="tab-drive">
<div class=grid>
<div class=card><div class=label>Driving personality</div><div class=buttons>
<button class="btn p-btn" data-val="2" onclick="setv('personality',2)">Chill</button>
<button class="btn p-btn" data-val="1" onclick="setv('personality',1)">Standard</button>
<button class="btn p-btn" data-val="0" onclick="setv('personality',0)">Aggressive</button>
</div>
<div class=row><div><b>Follow distance</b><div class=small>1 closest · 7 farthest</div></div></div><div class=follow id=follow></div>
<div class=row><div><b>Experimental Mode</b><div class=small>Live planner mode</div></div><button id=exp class=switch onclick="toggle('experimental')"><i></i></button></div>
<div class=row><div><b>Adaptive Accel</b><div class=small>NAP close-lead acceleration cap</div></div><button id=ada class=switch onclick="toggle('adaptive_accel')"><i></i></button></div></div>
<div class=card><div class=label>Drive view</div><div class=road>
  <svg class="pathline-svg" viewBox="0 0 300 330" preserveAspectRatio="none">
    <path id="laneLeft" class="lane-edge" d="M75,330 L75,10"/>
    <path id="laneRight" class="lane-edge" d="M225,330 L225,10"/>
    <path id="laneCenter" class="lane-center" d="M150,330 L150,10"/>
    <path id="pathline" d="M150,330 L150,10"/>
  </svg>
  <div id=leadcar2 class="lead lead2 off"></div>
  <div id=leadcar class="lead off"></div>
  <div class=car><i class="wheel w1"></i><i class="wheel w2"></i></div>
</div></div>
<div class=card><div class=label>Live vehicle</div><div class=telemetry><div class=metric><b id=speed>—</b><span>mph</span></div><div class=metric><b id=accel>—</b><span>m/s²</span></div><div class=metric><b id=target>—</b><span>target accel</span></div><div class=metric><b id=state>—</b><span>control</span></div></div>
<div class=row><span>Planner</span><b id=planner>—</b></div><div class=row><span>Lead</span><b id=haslead>—</b></div></div>
<div class=card><div class=label>Lead telemetry</div><div class=leads><div class=leadbox><h3><span id=l1dot class=dot></span>Lead 1</h3><div id=l1 class=kv></div></div><div class=leadbox><h3><span id=l2dot class=dot></span>Lead 2</h3><div id=l2 class=kv></div></div></div></div>
</div>
<div class=footer>Local NAP LAN panel · Comma 3X Dashcam integration</div>
</div>

</div>
<script>
let S={settings:{}};const $=x=>document.getElementById(x);const f=(x,d=1)=>Number.isFinite(Number(x))?Number(x).toFixed(d):"—";

function switchTab(t){
  $('tab-drive').style.display = t==='drive'?'block':'none';
  $('tab-video').style.display = t==='video'?'block':'none';
  $('tabbtn-drive').classList.toggle('active', t==='drive');
  $('tabbtn-video').classList.toggle('active', t==='video');
  if(t==='video') loadRoutes();
}

let routesLoaded = false;
let routeData = [];
let currentRoute = null;
let currentSeg = null;

let hudActive = false;
let logData = []; 

function toggleHud(){
  hudActive = !hudActive;
  $('hud-overlay').style.display = hudActive ? 'flex' : 'none';
}

function togglePlay(){
  let v = $('player');
  if(v.paused) { v.play(); $('vid-play-btn').textContent = "Pause"; }
  else { v.pause(); $('vid-play-btn').textContent = "Play"; }
}

async function loadRoutes(){
  if(routesLoaded) return;
  try {
    let r = await fetch("/api/routes").then(x=>x.json());
    routeData = r;
    let h = r.length === 0 ? "<div class='label' style='text-align:center;'>No drives found on disk.</div>" : "";
    r.forEach(route => {
      let d = route.name.includes("|") ? route.name.split("|")[1].split("--") : route.name.split("--");
      let readable = (d[0] || "Unknown") + " at " + (d[1] ? d[1].replace(/-/g, ":") : "Time");
      let segBtns = route.segs.map(s => `<button id="btn-${route.name}-${s}" class="seg-btn" onclick="playVid('${route.name}', ${s}, event)">Seg ${s}</button>`).join("");
      h += `<div class="route-item" onclick="this.classList.toggle('open')">
              <b>Drive: ${readable}</b>
              <div class="small">${route.segs.length} recorded minute(s)</div>
              <div class="segs-grid">${segBtns}</div>
            </div>`;
    });
    $('routes-list').innerHTML = h;
    routesLoaded = true;
  } catch(e) { $('routes-list').innerHTML = "<div class='label' style='text-align:center;'>Failed to load. Is drive mounted?</div>"; }
}

async function fetchLogData(route, seg){
  logData = [];
  $('hud-speed').textContent = "...";
  $('hud-steer').textContent = "...";
  $('hud-lead').textContent = "...";
  try {
    let r = await fetch(`/api/log/${route}--${seg}`);
    let j = await r.json();
    if(j.error) {
      console.error("Server Log Error:", j.error);
      $('hud-speed').textContent = "ERR";
      $('hud-lead').textContent = "ERR";
      $('hud-steer').textContent = "ERR";
    } else if(j.data) {
      logData = j.data;
      console.log("HUD Data Loaded:", logData.length, "frames");
    }
  } catch(e) {
    $('hud-speed').textContent = "ERR";
  }
}

function playVid(route, seg, e){
  if(e) e.stopPropagation();
  document.querySelectorAll('.seg-btn').forEach(b => b.classList.remove('playing'));

  currentRoute = route;
  currentSeg = seg;

  let btn = $(`btn-${route}-${seg}`);
  if(btn) {
    btn.classList.add('playing');
    btn.closest('.route-item').classList.add('open');
  }

  let cam = $('cam-select').value;
  let vid = $('player');
  vid.src = `/stream/${route}--${seg}?cam=${cam}`;
  vid.play();
  
  $('vid-play-btn').style.display = 'inline-block';
  $('vid-play-btn').textContent = "Pause";
  
  fetchLogData(route, seg);

  let dlBtn = $('download-btn');
  dlBtn.href = `/download/${route}--${seg}?cam=${cam}`;
  dlBtn.style.display = 'inline-block';

  window.scrollTo({top: 0, behavior: 'smooth'});
}

$('player').addEventListener('timeupdate', () => {
  if(!hudActive || logData.length === 0) return;
  
  let t = $('player').currentTime;
  let idx = Math.floor(t * 10);
  
  if(idx >= 0 && idx < logData.length){
    let frame = logData[idx];
    $('hud-speed').textContent = frame[0].toFixed(0);
    $('hud-steer').textContent = frame[1].toFixed(1);
    
    $('hud-gas').classList.toggle('active', frame[2] === 1);
    $('hud-brake').classList.toggle('active', frame[3] === 1);
    
    $('hud-lead').textContent = frame[4] > 0 ? frame[4].toFixed(1) : '--';
  }
});

$('player').addEventListener('ended', () => {
  if(!currentRoute) return;
  let rt = routeData.find(x => x.name === currentRoute);
  if(rt) {
    let idx = rt.segs.indexOf(currentSeg);
    if(idx !== -1 && idx < rt.segs.length - 1) {
      playVid(currentRoute, rt.segs[idx + 1]);
    }
  }
});

function follow(v){let h="";for(let i=1;i<8;i++)h+=`<button class="${+v===i?'active':''}" onclick="setv('follow_distance',${i})">${i}</button>`;$("follow").innerHTML=h}
function sw(id,v){$(id).classList.toggle("on",!!v)}
function lb(id,dotId,l){
  if(!l||!l.status){
    $(id).innerHTML="<span>status</span><b>none</b>";
    if(dotId){$(dotId).classList.remove('on','vis')}
    return;
  }
  let a=[["dRel","m"],["yRel","m"],["vRel","m/s"],["vLead","m/s"],["aLeadK","m/s²"],["aLeadTau","tau"],["modelProb","prob"],["radar","radar"]];
  $(id).innerHTML=a.map(x=>`<span>${x[0]}</span><b>${typeof l[x[0]]==="number"?f(l[x[0]],2):l[x[0]]??"—"} ${x[1]}</b>`).join("");
  if(dotId){
    $(dotId).classList.add('on');
    $(dotId).classList.toggle('vis', l.radar===false);
  }
}

const ROAD_TOP = 10, ROAD_BOTTOM = 330, CURVE_AMPLITUDE = 150;
function depthFrac(y){ return Math.max(0, Math.min(1, (ROAD_BOTTOM - y) / (ROAD_BOTTOM - ROAD_TOP))); }
function curveOffsetPx(curve, y){ let df = depthFrac(y); return curve * CURVE_AMPLITUDE * df * df; }

function buildCurvePath(curve, baseX){
  let pts = [];
  for(let y = ROAD_BOTTOM; y >= ROAD_TOP; y -= 20){
    pts.push(`${(baseX + curveOffsetPx(curve, y)).toFixed(1)},${y}`);
  }
  return 'M' + pts.join(' L ');
}

function updateRoadCurve(curve){
  $('laneLeft').setAttribute('d', buildCurvePath(curve, 75));
  $('laneRight').setAttribute('d', buildCurvePath(curve, 225));
  $('laneCenter').setAttribute('d', buildCurvePath(curve, 150));
  $('pathline').setAttribute('d', buildCurvePath(curve, 150));
}

const ROAD_H = 330, EGO_TOP = ROAD_H - 15 - 126, LEAD_H = 94, MIN_GAP = 10;

function positionLead(el, l, farScale, curve){
  if(!l || !l.status){
    el.classList.add('off');
    el.style.opacity = "0"; 
    return;
  }
  let dist = +l.dRel || 0;
  let yRel = +l.yRel || 0;
  let vRel = +l.vRel || 0;

  let maxBottom = Math.min(farScale.maxBottom, EGO_TOP - MIN_GAP);
  let bottomPx = Math.max(farScale.minBottom, Math.min(maxBottom, maxBottom - (dist - farScale.closeAt) * farScale.distScale));
  let scale = Math.max(farScale.minScale, Math.min(farScale.maxScale, farScale.maxScale - dist / farScale.scaleDiv));
  let topPx = bottomPx - LEAD_H;

  let laneOffset = curveOffsetPx(curve, bottomPx);
  let lateralPx = Math.max(-140, Math.min(140, laneOffset - yRel * 13));
  let danger = (vRel < -3.0) || l.fcw;

  el.style.top = topPx + "px";
  el.style.left = `calc(50% + ${lateralPx}px)`;
  el.style.setProperty('--s', scale.toFixed(3));
  el.style.transform = `translateX(-50%) scale(${scale})`;
  el.classList.remove('off');
  el.style.opacity = "1";
  el.classList.toggle('danger', !!danger);
  el.classList.toggle('vision-only', l.radar === false);
  if(!danger){
    el.style.boxShadow = '0 5px 25px #ff5d6744';
  }
}

function render(s){
  try {
    S=s;
    if(s.errors && s.errors.length > 0) {
      $("status").textContent = s.errors[0];
      $("status").style.color = "#ff5d67";
    } else {
      $("status").textContent=S.drive&&S.drive.active?"ONROAD":"Connected";
      $("status").style.color = "var(--m)";
    }

    let h_data = s.health || {};
    $("cpu-temp").textContent = f(h_data.temp, 0);
    $("free-space").textContent = f(h_data.space, 1);

    let t_color = "var(--g)";
    if(h_data.thermal === 1) t_color = "#ffb84d";
    else if(h_data.thermal >= 2) t_color = "#ff5d67";
    $("cpu-temp").style.color = t_color;

    let q=s.settings||{},p=+q.personality_raw;follow(q.follow_distance);
    document.querySelectorAll(".p-btn").forEach(b=>b.classList.toggle("active",+b.dataset.val===p));
    sw("exp",q.experimental);sw("ada",q.adaptive_accel);

    let c = s.car || {};
    let d = s.drive || {};
    let pl = s.plan || {};

    let speedMph = (+c.vEgo) * 2.23694;
    $("speed").textContent=f(speedMph,0);$("accel").textContent=f(c.aEgo,2);
    $("target").textContent=f(pl.aTarget,2);$("state").textContent=d.active?"ACTIVE":d.enabled?"ENABLED":"OFF";
    $("planner").textContent=pl.source||"—";$("haslead").textContent=pl.hasLead?"YES":"NO";

    lb("l1","l1dot",s.lead1);lb("l2","l2dot",s.lead2);

    if(speedMph > 1) {
      let animSpeed = Math.max(0.15, 20 / speedMph).toFixed(2);
      document.documentElement.style.setProperty('--road-speed', animSpeed + 's');
    } else {
      document.documentElement.style.setProperty('--road-speed', '0s');
    }

    let curve = (s.path && typeof s.path.curvature === 'number') ? s.path.curvature : 0;
    document.documentElement.style.setProperty('--curve', curve.toFixed(3));
    updateRoadCurve(curve);
    let carEl = document.querySelector('.car');
    if(carEl) carEl.style.transform = `translateX(-50%) rotate(${(curve*9).toFixed(2)}deg)`;

    positionLead($("leadcar"), s.lead1, {maxBottom:190, minBottom:40, closeAt:4, distScale:1.6, minScale:0.4, maxScale:1.1, scaleDiv:140}, curve);
    positionLead($("leadcar2"), s.lead2, {maxBottom:150, minBottom:30, closeAt:4, distScale:1.3, minScale:0.25, maxScale:0.9, scaleDiv:160}, curve);
  } catch(e) { console.error("Render crash avoided:", e); }
}

const DEMO_DURATION = 36;
let demoMode = false, autoDemo = false, demoT0 = null, failCount = 0;

function lerp(a,b,t){return a+(b-a)*t}
function clamp01(t){return Math.max(0,Math.min(1,t))}

function demoState(elapsed){
  let t = elapsed % DEMO_DURATION;
  let speedMs = 26;
  let curvature = Math.sin(elapsed/13) * 0.35;

  let carA = {status:false};
  if(t >= 6 && t < 32){
    if(t < 14){
      let pr = clamp01((t-6)/8);
      carA = {dRel: lerp(100,55,pr), yRel:0, vRel: lerp(2,-0.5,pr), vLead: speedMs-1,
              aLeadK:-0.15, aLeadTau:1.5, modelProb:0.96, radar:true, fcw:false};
    } else if(t < 27){
      let wob = Math.sin((t-14)/3) * 4;
      carA = {dRel: 62+wob, yRel:0, vRel: 0.3, vLead: speedMs+0.3,
              aLeadK:0.05, aLeadTau:1.4, modelProb:0.95, radar:true, fcw:false};
    } else {
      let pr = clamp01((t-27)/5);
      carA = {dRel: lerp(62,110,pr), yRel:0, vRel: lerp(0.3,3,pr), vLead: speedMs+2,
              aLeadK:0.2, aLeadTau:1.3, modelProb: lerp(0.95,0.6,pr), radar: pr<0.7, fcw:false};
    }
    carA.status = true;
  }

  let carB = {status:false};
  if(t >= 14 && t < 32){
    if(t < 19){
      let pr = clamp01((t-14)/5);
      carB = {dRel: lerp(40,15,pr), yRel: lerp(3.6,0,pr), vRel:-3.2, vLead: speedMs-4,
              aLeadK:-1.1, aLeadTau:1.2, modelProb: lerp(0.5,0.97,pr), radar: pr>0.5, fcw: pr>0.55 && pr<0.85};
    } else if(t < 27){
      let wob = Math.sin((t-19)/2.4) * 2;
      carB = {dRel: 17+wob, yRel: Math.sin((t-19)/6)*0.4, vRel:-0.2, vLead: speedMs-1,
              aLeadK:0.1, aLeadTau:1.3, modelProb:0.97, radar:true, fcw:false};
    } else {
      let pr = clamp01((t-27)/5);
      carB = {dRel: lerp(17,55,pr), yRel:0, vRel: lerp(-0.2,4,pr), vLead: speedMs+3,
              aLeadK:0.3, aLeadTau:1.2, modelProb:0.9, radar:true, fcw:false};
    }
    carB.status = true;
  }

  let candidates = [carA, carB].filter(c => c.status);
  candidates.sort((a,b) => a.dRel - b.dRel);
  let lead1 = candidates[0] || {status:false};
  let lead2 = candidates[1] || {status:false};

  let hasLead = lead1.status;
  let aTarget = 0.2, source = hasLead ? "lead" : "cruise";
  if(hasLead){
    if(lead1.vRel < -2.5) aTarget = -2.4;
    else if(lead1.dRel < 20) aTarget = -0.6;
    else aTarget = 0.2;
  }
  if(t >= 14 && t < 20) speedMs = lerp(26,19,clamp01((t-14)/6));
  else if(t >= 20 && t < 27) speedMs = lerp(19,24,clamp01((t-20)/7));

  return {
    ts: Date.now()/1000, errors: [],
    health: {temp: 52+Math.sin(elapsed/20)*4, thermal:0, space:71},
    car: {vEgo: speedMs, aEgo: aTarget*0.6, vCruise:65, vCruiseCluster:65, standstill:false, brakePressed:false, gasPressed:false},
    drive: {enabled:true, active:true, experimentalMode:true, personalityRaw: S.settings&&S.settings.personality_raw!==undefined?S.settings.personality_raw:1, personality:"standard", alertHudVisual:0},
    plan: {aTarget, shouldStop:false, hasLead, allowBrake:true, allowThrottle:true, source},
    lead1, lead2,
    settings: (S.settings && Object.keys(S.settings).length) ? S.settings : {personality_raw:1, follow_distance:4, adaptive_accel:false, experimental:true, personality:"standard"},
    path: {curvature}
  };
}

function setDemo(on, auto){
  demoMode = on; autoDemo = !!auto;
  let banner = $('demo-banner');
  banner.style.display = on ? 'block' : 'none';
  banner.textContent = autoDemo ? '⚠ CAR NOT DETECTED — RUNNING SIMULATED DEMO' : '▶ DEMO MODE — SIMULATED DATA, NOT LIVE';
  $('demo-toggle').classList.toggle('active', on && !autoDemo);
  $('demo-toggle').textContent = on && !autoDemo ? '■ Stop Demo' : '▶ Demo Mode';
  if(on) demoT0 = performance.now();
}
function toggleDemo(){ setDemo(!demoMode, false); }

async function get(){
  if(demoMode){
    let elapsed = (performance.now()-demoT0)/1000;
    render(demoState(elapsed));
    return;
  }
  try{
    let r=await fetch("/api/state",{cache:"no-store"});
    if(!r.ok)throw Error();
    let data = await r.json();
    failCount = 0;
    render(data);
  }catch(e){
    $("status").textContent="Disconnected";
    $("status").style.color="var(--m)";
    failCount++;
    if(failCount >= 3 && !demoMode){
      setDemo(true, true);
    }
  }
}
async function setv(name,value){try{let r=await fetch("/api/set",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name,value})}),j=await r.json();if(!r.ok)throw Error(j.error||"write failed");render(j)}catch(e){alert("Change failed: "+e.message)}}
function toggle(n){setv(n,!S.settings[n])}
follow(4);get();setInterval(get,250);
</script></body></html>"""

def get_mp4_path(route_seg, cam_type="qcamera"):
  base_path = f"/data/media/0/realdata/{route_seg}/{cam_type}"

  cam_file = base_path + ".hevc"
  if not os.path.exists(cam_file):
    cam_file = base_path + ".ts"

  if not os.path.exists(cam_file):
    return None

  tmp_path = f"/dev/shm/vid_{route_seg}_{cam_type}.mp4"
  if not os.path.exists(tmp_path):
    try:
      cached_vids = glob.glob("/dev/shm/vid_*.mp4")
      if len(cached_vids) >= 3:
        cached_vids.sort(key=os.path.getctime)
        for old_vid in cached_vids[:-2]:
          os.remove(old_vid)
    except Exception: pass

    subprocess.run(["ffmpeg", "-y", "-i", cam_file, "-c", "copy", "-movflags", "faststart", tmp_path],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

  return tmp_path if os.path.exists(tmp_path) else None

class Handler(BaseHTTPRequestHandler):
  def log_message(self,*a):pass
  def send_json(self,o,status=200):
    b=json.dumps(o,separators=(",",":")).encode();self.send_response(status);self.send_header("Content-Type","application/json");self.send_header("Cache-Control","no-store");self.send_header("Content-Length",str(len(b)));self.end_headers();self.wfile.write(b)

  def do_GET(self):
    parsed = urlparse(self.path)
    p = parsed.path
    qs = parse_qs(parsed.query)

    if p in ("/","/index.html"):
      b=HTML.encode();self.send_response(200);self.send_header("Content-Type","text/html; charset=utf-8");self.send_header("Content-Length",str(len(b)));self.end_headers();self.wfile.write(b)
    elif p == "/api/state":
      with LOCK:o=json.loads(json.dumps(STATE))
      self.send_json(o)
    elif p == "/api/routes":
      self.send_json(get_routes())
      
    elif p.startswith("/api/log/"):
      route_seg = p.split("/api/log/")[1]
      cache_path = f"/dev/shm/rlog_{route_seg}.json"
      
      if os.path.exists(cache_path):
        try:
          with open(cache_path, "r") as f:
             self.send_json(json.load(f))
             return
        except Exception: pass
        
      # Prioritize raw logs (rlog) over quick logs (qlog) for vehicle telemetry
      seg_dir = f"/data/media/0/realdata/{route_seg}"
      log_path = None
      for filename in ["rlog.zst", "rlog.bz2", "qlog.zst", "qlog.bz2"]:
        candidate = os.path.join(seg_dir, filename)
        if os.path.exists(candidate):
          log_path = candidate
          break
        
      if not log_path:
        print(f"No log files found in {seg_dir}")
        self.send_json({"error": f"No log found in {seg_dir}"})
        return
        
      try:
        import sys
        if "/data/openpilot" not in sys.path: sys.path.insert(0, "/data/openpilot")
        
        try: from tools.lib.logreader import LogReader
        except: from openpilot.tools.lib.logreader import LogReader
        
        print(f"Parsing raw log: {log_path} ...")
        lr = LogReader(log_path)
        timeline = []
        vEgo, steer, gas, brake, lead_d = 0.0, 0.0, False, False, 0.0
        t0 = None

        for msg in lr:
          w = msg.which()
          t = msg.logMonoTime / 1e9
          if t0 is None: t0 = t
          rel_t = t - t0

          if w == 'carState':
            cs = msg.carState
            vEgo = getattr(cs, 'vEgo', 0)
            steer = getattr(cs, 'steeringAngleDeg', 0)
            gas = getattr(cs, 'gasPressed', False)
            brake = getattr(cs, 'brakePressed', False)
          elif w == 'radarState':
            rs = msg.radarState
            lead = getattr(rs, 'leadOne', None)
            if lead and getattr(lead, 'status', False): lead_d = getattr(lead, 'dRel', 0)
            else: lead_d = 0
                  
          expected_idx = int(rel_t * 10)
          if expected_idx > 1000: break 
          
          while len(timeline) <= expected_idx:
            timeline.append([round(vEgo * 2.23694, 1), round(steer, 1), 1 if gas else 0, 1 if brake else 0, round(lead_d, 1)])

        res = {"data": timeline}
        with open(cache_path, "w") as f: json.dump(res, f)
        print(f"Log parsing complete! Extracted {len(timeline)} frames from {log_path}")
        self.send_json(res)
      except Exception as e:
        print("Log Parse Error:")
        traceback.print_exc()
        self.send_json({"error": str(e)})

    elif p.startswith("/stream/"):
      route_seg = p.split("/stream/")[1]
      cam_type = qs.get("cam", ["qcamera"])[0]
      mp4_path = get_mp4_path(route_seg, cam_type)

      if mp4_path:
        serve_file_with_range(self, mp4_path, content_type="video/mp4")
      else:
        self.send_error(404)

    elif p.startswith("/download/"):
      route_seg = p.split("/download/")[1]
      cam_type = qs.get("cam", ["qcamera"])[0]
      mp4_path = get_mp4_path(route_seg, cam_type)

      if mp4_path:
        safe_name = f"Comma_Clip_{route_seg.replace('|', '_').replace('--', '_')}_{cam_type}.mp4"
        serve_file_with_range(self, mp4_path, content_type="video/mp4", attachment_name=safe_name)
      else:
        self.send_error(404)

    else:self.send_json({"error":"not found"},404)

  def do_POST(self):
    if urlparse(self.path).path!="/api/set":return self.send_json({"error":"not found"},404)
    try:
      n=int(self.headers.get("Content-Length","0"));d=json.loads(self.rfile.read(n).decode());settings=write_setting(str(d.get("name")),d.get("value"))
      with LOCK:
        STATE["settings"]=settings
        o=json.loads(json.dumps(STATE))
      self.send_json(o)
    except Exception as e:self.send_json({"error":str(e)},400)

def serve_file_with_range(handler, path, content_type="video/mp4", attachment_name=None):
  if not os.path.exists(path):
    handler.send_error(404)
    return
  try:
    size = os.path.getsize(path)
    start, end = 0, size - 1
    if "Range" in handler.headers:
      range_match = handler.headers["Range"].replace("bytes=", "").split("-")
      start = int(range_match[0]) if range_match[0] else 0
      end = int(range_match[1]) if len(range_match) > 1 and range_match[1] else size - 1
      handler.send_response(206)
      handler.send_header("Content-Range", f"bytes {start}-{end}/{size}")
    else:
      handler.send_response(200)

    chunk_size = end - start + 1
    handler.send_header("Content-Type", content_type)
    if attachment_name:
      handler.send_header("Content-Disposition", f'attachment; filename="{attachment_name}"')
    handler.send_header("Accept-Ranges", "bytes")
    handler.send_header("Content-Length", str(chunk_size))
    handler.end_headers()

    with open(path, "rb") as f:
      f.seek(start)
      left = chunk_size
      while left > 0:
        buffer = f.read(min(65536, left))
        if not buffer: break
        try: handler.wfile.write(buffer)
        except: break
        left -= len(buffer)
  except Exception as e:
    pass

def main():
  print(f"NAP Drive Panel listening on port {PORT}")
  threading.Thread(target=telemetry,daemon=True).start();srv=ThreadingHTTPServer((HOST,PORT),Handler)
  try:srv.serve_forever()
  except KeyboardInterrupt:pass
  finally:STOP.set();srv.server_close()

if __name__=="__main__":main()
