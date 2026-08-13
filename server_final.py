cat << 'EOF' > /data/server_final.py
#!/usr/bin/env python3
import json, threading, time, os, subprocess, glob
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
import cereal.messaging as messaging
from openpilot.common.params import Params
import cereal.log as log

HOST, PORT = "0.0.0.0", 7070
PARAMS = {"personality":"LongitudinalPersonality","follow_distance":"NAPFollowDistance",
          "adaptive_accel":"NAPAdaptiveAccel","experimental":"ExperimentalMode"}
PERSONALITIES = {0:"standard", 1:"chill", 2:"aggressive"}
STATE = {"ts":0,"car":{},"drive":{},"plan":{},"lead1":{},"lead2":{},"settings":{},"errors":[]}
LOCK=threading.Lock(); STOP=threading.Event()

params = Params()

def num(v,d=0.0):
  try:
    x=float(v); return x if x==x and abs(x)!=float("inf") else d
  except Exception: return d

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
    return {"personality_raw":1, "follow_distance":4, "adaptive_accel":False, "experimental":False, "personality":"chill"}

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
  # Universal Fork Introspection - dynamically check what services the fork's schema actually supports
  try: available_fields = list(log.Event.schema.fields.keys())
  except: available_fields = ["carState", "controlsState", "radarState", "longitudinalPlan", "plan"]
    
  target_services = ["carState", "selfdriveState", "controlsState", "longitudinalPlan", "plan", "radarState"]
  valid_services = [s for s in target_services if s in available_fields]
  
  try: sm = messaging.SubMaster(valid_services)
  except Exception as e:
    with LOCK: STATE["errors"]=[f"IPC Schema Error: {str(e)}"]
    return

  tick = 0
  while not STOP.is_set():
    try:
      sm.update(100)
      cs = sm["carState"] if "carState" in valid_services else None
      sd = sm["selfdriveState"] if "selfdriveState" in valid_services else None
      ctl = sm["controlsState"] if "controlsState" in valid_services else None
      lp = sm["longitudinalPlan"] if "longitudinalPlan" in valid_services else (sm["plan"] if "plan" in valid_services else None)
      radar = sm["radarState"] if "radarState" in valid_services else None
      
      if tick % 20 == 0: current_settings = read_params()
      else: current_settings = STATE.get("settings", {})

      # Fallback logic for legacy vs modern OP forks
      enabled = safe_attr(sd, 'enabled', safe_attr(ctl, 'enabled', False))
      active = safe_attr(sd, 'active', safe_attr(ctl, 'active', False))
      exp_mode = safe_attr(sd, 'experimentalMode', safe_attr(ctl, 'experimentalMode', False))
      pers_raw = int(safe_attr(sd, 'personality', 0))
      
      # Some forks lack vCruiseCluster, fallback to vCruise
      v_cruise = num(safe_attr(cs, 'vCruise', 0))
      v_cruise_cluster = num(safe_attr(cs, 'vCruiseCluster', v_cruise))

      with LOCK:
        STATE.update({"ts":time.time(),
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
            "alertHudVisual":int(safe_attr(sd, 'alertHudVisual', safe_attr(ctl, 'alertHudVisual', 0)))
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
:root{--p:#111720;--p2:#0d131b;--line:#293241;--t:#f5f7fa;--m:#9aa7b7;--a:#56b6ff;--g:#45d483;--road-speed:0s;}
*{box-sizing:border-box}body{margin:0;background:linear-gradient(#070a0e,#0c1118);color:var(--t);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}.wrap{max-width:980px;margin:auto;padding:14px}
.top{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}.title{font-size:22px;font-weight:800}.status{font-size:12px;color:var(--m);font-weight:700}
.nav-tabs{display:flex;gap:10px;margin-bottom:15px;background:#111720;padding:6px;border-radius:14px;border:1px solid var(--line)}
.nav-tabs button{flex:1;background:transparent;border:none;color:var(--m);padding:10px;font-weight:700;border-radius:10px;font-size:14px}
.nav-tabs button.active{background:#1a222e;color:var(--t)}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}@media(max-width:720px){.grid{grid-template-columns:1fr}}
.card{background:#111720f5;border:1px solid var(--line);border-radius:18px;padding:14px;box-shadow:0 8px 28px #0005}.label{font-size:12px;color:var(--m);text-transform:uppercase;letter-spacing:.08em;margin-bottom:9px}
.buttons{display:flex;gap:8px}.btn{flex:1;border:1px solid #344153;background:#1a222e;color:var(--t);padding:12px 8px;border-radius:12px;font-weight:700}.btn.active{background:#284d68;border-color:var(--a)}
.row{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:8px 0}.small{font-size:12px;color:var(--m)}
.switch{width:54px;height:30px;border-radius:20px;background:#303a47;border:0;position:relative}.switch i{position:absolute;width:24px;height:24px;top:3px;left:3px;border-radius:50%;background:#fff;transition:.15s}.switch.on{background:var(--g)}.switch.on i{left:27px}
.follow{display:grid;grid-template-columns:repeat(7,1fr);gap:5px}.follow button{padding:10px 2px;border-radius:9px;border:1px solid #344153;background:#1a222e;color:#fff}.follow button.active{background:#315b75;border-color:var(--a)}

@keyframes scrollEdge { 0% { background-position: 0 0; } 100% { background-position: 0 64px; } }
@keyframes scrollCenter { 0% { background-position: 0 0; } 100% { background-position: 0 48px; } }
.road{height:330px;position:relative;overflow:hidden;border-radius:16px;background:linear-gradient(90deg,#121922 0 25%,#1c2229 25% 75%,#121922 75%); perspective:600px}
.road:before,.road:after{content:"";position:absolute;top:0;bottom:0;width:4px;opacity:.6;
  background:repeating-linear-gradient(to bottom,transparent 0,transparent 32px,#d9dde2 32px,#d9dde2 64px);
  animation:scrollEdge var(--road-speed) linear infinite}
.road:before{left:25%}.road:after{right:25%}
.centerline{position:absolute;left:50%;top:0;bottom:0;width:4px;opacity:.5;transform:translateX(-50%);
  background:repeating-linear-gradient(to bottom,transparent 0,transparent 24px,#87929d 24px,#87929d 48px);
  animation:scrollCenter var(--road-speed) linear infinite}
.car{position:absolute;left:50%;transform:translateX(-50%);bottom:15px;width:82px;height:126px;border-radius:24px 24px 16px 16px;background:linear-gradient(90deg,#6e7d8b,#e7edf2 45%,#657482);box-shadow:0 10px 28px #9dd8ff44, inset 0 -5px 10px #0004; z-index:10}.car:before{content:"";position:absolute;left:12px;right:12px;top:15px;height:43px;border-radius:14px;background:linear-gradient(135deg,#263442,#0c131b);border:1px solid #9ab0c255}.wheel{position:absolute;width:9px;height:35px;background:#080b0f;border-radius:5px;top:45px}.w1{left:-5px}.w2{right:-5px}

.lead{position:absolute;left:50%;transform-origin:bottom center;width:64px;height:94px;border-radius:17px 17px 11px 11px;background:linear-gradient(90deg,#c95159,#ff9b72 48%,#b8444f);box-shadow:0 5px 25px #ff5d6744;transition:top 0.25s linear, transform 0.25s linear, box-shadow 0.2s ease-out; z-index:5}.lead:before{content:"";position:absolute;left:10px;right:10px;top:10px;height:27px;border-radius:9px;background:#19222c;border:1px solid #fff3}.lead.off{opacity:0; transition:opacity 0.2s;}

.telemetry{display:grid;grid-template-columns:repeat(4,1fr);gap:7px}.metric{background:var(--p2);border-radius:10px;padding:9px}.metric b{display:block;font-size:16px}.metric span{font-size:10px;color:var(--m)}
.leads{display:grid;grid-template-columns:1fr 1fr;gap:8px}.leadbox{background:var(--p2);border-radius:12px;padding:10px}.leadbox h3{margin:0 0 7px;font-size:13px}.kv{display:grid;grid-template-columns:1fr 1fr;gap:5px;font-size:11px}.kv span{color:var(--m)}.footer{margin-top:10px;font-size:11px;color:#718093;text-align:center}

.vid-container{width:100%;aspect-ratio:16/9;background:#000;border-radius:16px;overflow:hidden;margin-bottom:12px;border:1px solid var(--line)}
video{width:100%;height:100%}
.route-item{background:var(--p2);border-radius:12px;padding:12px;margin-bottom:10px;cursor:pointer;border:1px solid var(--line)}
.route-item b{font-size:14px;display:block}
.route-item .small{margin-top:3px}
.segs-grid{display:flex;flex-wrap:wrap;gap:6px;margin-top:12px;display:none}
.route-item.open .segs-grid{display:flex}
.seg-btn{background:#1a222e;border:1px solid #344153;color:var(--t);padding:8px 12px;border-radius:8px;font-size:12px;font-weight:700}
.seg-btn:active{background:var(--a)}.seg-btn.playing{background:var(--a);border-color:#fff;color:#000}
.save-btn{display:none; text-decoration:none; padding:8px 16px; background:var(--g); color:#000; border-radius:10px; font-weight:700; font-size:13px;}
</style></head><body><div class=wrap><div class=top><div class=title>NAP Drive</div><div id=status class=status>Connecting…</div></div>

<div class="nav-tabs">
  <button id="tabbtn-drive" class="active" onclick="switchTab('drive')">Drive Settings</button>
  <button id="tabbtn-video" onclick="switchTab('video')">Dashcam Viewer</button>
</div>

<div id="tab-video" style="display:none;">
  <div class="vid-container">
    <video id="player" controls playsinline autoplay></video>
  </div>
  <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:15px; padding:0 4px;">
    <div id="vid-title" style="font-weight:700; font-size:14px; color:var(--m);">Select a drive below</div>
    <a id="download-btn" class="save-btn" href="#">Download Clip ↓</a>
  </div>
  <div id="routes-list"><div class="label" style="text-align:center;margin-top:20px;">Loading routes...</div></div>
</div>

<div id="tab-drive">
<div class=grid>
<div class=card><div class=label>Driving personality</div><div class=buttons>
<button class="btn p-btn" data-val="1" onclick="setv('personality',1)">Chill</button>
<button class="btn p-btn" data-val="0" onclick="setv('personality',0)">Standard</button>
<button class="btn p-btn" data-val="2" onclick="setv('personality',2)">Aggressive</button>
</div>
<div class=row><div><b>Follow distance</b><div class=small>1 closest · 7 farthest</div></div></div><div class=follow id=follow></div>
<div class=row><div><b>Experimental Mode</b><div class=small>Live planner mode</div></div><button id=exp class=switch onclick="toggle('experimental')"><i></i></button></div>
<div class=row><div><b>Adaptive Accel</b><div class=small>NAP close-lead acceleration cap</div></div><button id=ada class=switch onclick="toggle('adaptive_accel')"><i></i></button></div></div>
<div class=card><div class=label>Drive view</div><div class=road><div class=centerline></div><div id=leadcar class="lead off"></div><div class=car><i class="wheel w1"></i><i class="wheel w2"></i></div></div></div>
<div class=card><div class=label>Live vehicle</div><div class=telemetry><div class=metric><b id=speed>—</b><span>mph</span></div><div class=metric><b id=accel>—</b><span>m/s²</span></div><div class=metric><b id=target>—</b><span>target accel</span></div><div class=metric><b id=state>—</b><span>control</span></div></div>
<div class=row><span>Planner</span><b id=planner>—</b></div><div class=row><span>Lead</span><b id=haslead>—</b></div></div>
<div class=card><div class=label>Lead telemetry</div><div class=leads><div class=leadbox><h3>Lead 1</h3><div id=l1 class=kv></div></div><div class=leadbox><h3>Lead 2</h3><div id=l2 class=kv></div></div></div></div>
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

  let vid = $('player');
  vid.src = `/stream/${route}--${seg}`;
  vid.play();
  
  $('vid-title').textContent = `Playing: Segment ${seg}`;
  $('vid-title').style.color = 'var(--t)';
  let dlBtn = $('download-btn');
  dlBtn.href = `/download/${route}--${seg}`;
  dlBtn.style.display = 'inline-block';

  window.scrollTo({top: 0, behavior: 'smooth'});
}

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
function lb(id,l){if(!l||!l.status){$(id).innerHTML="<span>status</span><b>none</b>";return}let a=[["dRel","m"],["yRel","m"],["vRel","m/s"],["vLead","m/s"],["aLeadK","m/s²"],["aLeadTau","tau"],["modelProb","prob"],["radar","radar"]];$(id).innerHTML=a.map(x=>`<span>${x[0]}</span><b>${typeof l[x[0]]==="number"?f(l[x[0]],2):l[x[0]]??"—"} ${x[1]}</b>`).join("")}

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

    let q=s.settings||{},p=+q.personality_raw;follow(q.follow_distance);
    document.querySelectorAll(".p-btn").forEach(b=>b.classList.toggle("active",+b.dataset.val===p));
    sw("exp",q.experimental);sw("ada",q.adaptive_accel);
    
    let c = s.car || {};
    let d = s.drive || {};
    let pl = s.plan || {};
    let l = s.lead1 || {};
    
    let speedMph = (+c.vEgo) * 2.23694;
    $("speed").textContent=f(speedMph,0);$("accel").textContent=f(c.aEgo,2);
    $("target").textContent=f(pl.aTarget,2);$("state").textContent=d.active?"ACTIVE":d.enabled?"ENABLED":"OFF";
    $("planner").textContent=pl.source||"—";$("haslead").textContent=pl.hasLead?"YES":"NO";
    
    lb("l1",s.lead1);lb("l2",s.lead2);
    
    if(speedMph > 1) {
      let animSpeed = Math.max(0.15, 20 / speedMph).toFixed(2);
      document.documentElement.style.setProperty('--road-speed', animSpeed + 's');
    } else {
      document.documentElement.style.setProperty('--road-speed', '0s'); 
    }

    let lc=$("leadcar");
    if(l.status){
      let dist = +l.dRel || 0;
      let vRel = +l.vRel || 0;
      let topPx = Math.max(10, Math.min(200, 200 - (dist * 1.5)));
      let scale = Math.max(0.4, Math.min(1.1, 1.1 - (dist / 140)));
      let danger = (vRel < -3.0) || l.fcw;
      
      lc.style.top = topPx + "px";
      lc.style.transform = `translateX(-50%) scale(${scale})`;
      lc.style.boxShadow = danger ? '0 10px 40px #ff0000, inset 0 0 10px #ff0000' : '0 10px 25px #ff5d6744';
      lc.classList.remove("off");
      lc.style.opacity = "1";
    } else {
      lc.classList.add("off");
    }
  } catch(e) { console.error("Render crash avoided:", e); }
}

async function get(){
  try{
    let r=await fetch("/api/state",{cache:"no-store"});
    if(!r.ok)throw Error();
    render(await r.json());
  }catch(e){
    $("status").textContent="Disconnected";
    $("status").style.color="var(--m)";
  }
}
async function setv(name,value){try{let r=await fetch("/api/set",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name,value})}),j=await r.json();if(!r.ok)throw Error(j.error||"write failed");render(j)}catch(e){alert("Change failed: "+e.message)}}
function toggle(n){setv(n,!S.settings[n])}follow(4);get();setInterval(get,250);
</script></body></html>"""

def get_mp4_path(route_seg):
  ts_file = f"/data/media/0/realdata/{route_seg}/qcamera.ts"
  if not os.path.exists(ts_file): return None
  tmp_path = f"/dev/shm/vid_{route_seg}.mp4"
  if not os.path.exists(tmp_path):
    try:
      cached_vids = glob.glob("/dev/shm/vid_*.mp4")
      if len(cached_vids) >= 3:
        cached_vids.sort(key=os.path.getctime)
        for old_vid in cached_vids[:-2]:
          os.remove(old_vid)
    except Exception: pass
    subprocess.run(["ffmpeg", "-y", "-i", ts_file, "-c", "copy", "-movflags", "faststart", tmp_path], 
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
  return tmp_path if os.path.exists(tmp_path) else None

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

class Handler(BaseHTTPRequestHandler):
  def log_message(self,*a):pass
  def send_json(self,o,status=200):
    b=json.dumps(o,separators=(",",":")).encode();self.send_response(status);self.send_header("Content-Type","application/json");self.send_header("Cache-Control","no-store");self.send_header("Content-Length",str(len(b)));self.end_headers();self.wfile.write(b)
  
  def do_GET(self):
    p=urlparse(self.path).path
    if p in ("/","/index.html"):
      b=HTML.encode();self.send_response(200);self.send_header("Content-Type","text/html; charset=utf-8");self.send_header("Content-Length",str(len(b)));self.end_headers();self.wfile.write(b)
    elif p == "/api/state":
      with LOCK:o=json.loads(json.dumps(STATE))
      self.send_json(o)
    elif p == "/api/routes":
      self.send_json(get_routes())
    
    elif p.startswith("/stream/"):
      route_seg = p.split("/stream/")[1]
      mp4_path = get_mp4_path(route_seg)
      if mp4_path:
        serve_file_with_range(self, mp4_path, content_type="video/mp4")
      else:
        self.send_error(404)
    
    elif p.startswith("/download/"):
      route_seg = p.split("/download/")[1]
      mp4_path = get_mp4_path(route_seg)
      if mp4_path:
        safe_name = "Comma_Clip_" + route_seg.replace("|", "_").replace("--", "_") + ".mp4"
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

def main():
  print(f"NAP Drive Panel listening on port {PORT}")
  threading.Thread(target=telemetry,daemon=True).start();srv=ThreadingHTTPServer((HOST,PORT),Handler)
  try:srv.serve_forever()
  except KeyboardInterrupt:pass
  finally:STOP.set();srv.server_close()

if __name__=="__main__":main()
EOF
