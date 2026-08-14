cat << 'EOF' > /data/server_v18.py
#!/usr/bin/env python3
import json, threading, time, os, subprocess, glob, traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
import cereal.messaging as messaging
from openpilot.common.params import Params

HOST, PORT = "0.0.0.0", 7074
PARAMS = {
    "personality": "LongitudinalPersonality",
    "follow_distance": "NAPFollowDistance",
    "adaptive_accel": "NAPAdaptiveAccel",
    "experimental": "ExperimentalMode"
}
PERSONALITIES = {0: "aggressive", 1: "standard", 2: "chill"}
STATE = {"ts":0, "car":{}, "drive":{}, "plan":{}, "lead1":{}, "settings":{}, "health":{}, "errors":[]}
LOCK = threading.Lock()
STOP = threading.Event()

params = Params()

def num(v, d=0.0):
    try:
        if hasattr(v, 'raw'): v = v.raw
        x = float(v)
        return x if x == x and abs(x) != float("inf") else d
    except Exception: 
        return d

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
    out = {}
    if lead is None: return out
    for k in ("status","dRel","yRel","vRel","vLead","aLeadK","aLeadTau","modelProb","radar","fcw","aLead","dPath","vLat"):
        try:
            v = getattr(lead, k)
            out[k] = v if isinstance(v, bool) else num(v)
        except Exception:
            pass
    return out

def read_params():
    try:
        p = Params()
        r = {}
        try: 
            v = p.get(PARAMS["personality"])
            r["personality_raw"] = int(v) if v is not None else 1
        except Exception: r["personality_raw"] = 1
        
        try: 
            v = p.get(PARAMS["follow_distance"])
            r["follow_distance"] = int(v) if v is not None else 4
        except Exception: r["follow_distance"] = 4
        
        try: r["adaptive_accel"] = p.get_bool(PARAMS["adaptive_accel"])
        except Exception: r["adaptive_accel"] = False
        
        try: r["experimental"] = p.get_bool(PARAMS["experimental"])
        except Exception: r["experimental"] = False
        
        r["personality"] = PERSONALITIES.get(r["personality_raw"], "unknown")
        return r
    except Exception:
        return {"personality_raw": 1, "follow_distance": 4, "adaptive_accel": False, "experimental": False, "personality": "standard"}

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
    target_services = ["carState", "selfdriveState", "controlsState", "longitudinalPlan", "radarState", "deviceState"]
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
                with LOCK: STATE["errors"] = [f"IPC Bind Error: {str(e)}"]
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
            
            if tick % 20 == 0: 
                current_settings = read_params()
            else: 
                current_settings = STATE.get("settings", {})

            enabled = safe_attr(sd, 'enabled', safe_attr(ctl, 'enabled', False))
            active = safe_attr(sd, 'active', safe_attr(ctl, 'active', False))
            exp_mode = safe_attr(sd, 'experimentalMode', safe_attr(ctl, 'experimentalMode', False))
            pers_raw = safe_int(safe_attr(sd, 'personality', 1))
            
            v_cruise = num(safe_attr(cs, 'vCruise', 0))
            v_cruise_cluster = num(safe_attr(cs, 'vCruiseCluster', v_cruise))
            
            left_blinker = bool(safe_attr(cs, 'leftBlinker', False))
            right_blinker = bool(safe_attr(cs, 'rightBlinker', False))
            
            temps = safe_attr(ds, 'cpuTempC', [])
            try: max_temp = max(temps) if len(temps) > 0 else 0
            except Exception: max_temp = 0
                
            therm_stat = safe_int(safe_attr(ds, 'thermalStatus', 0))
            free_space = num(safe_attr(ds, 'freeSpacePercent', 0))

            with LOCK:
                STATE.update({
                    "ts": time.time(),
                    "health": {"temp": max_temp, "thermal": therm_stat, "space": free_space},
                    "car": {
                        "vEgo": num(safe_attr(cs, 'vEgo', 0)),
                        "aEgo": num(safe_attr(cs, 'aEgo', 0)),
                        "vCruise": v_cruise,
                        "vCruiseCluster": v_cruise_cluster,
                        "standstill": bool(safe_attr(cs, 'standstill', False)),
                        "brakePressed": bool(safe_attr(cs, 'brakePressed', False)),
                        "gasPressed": bool(safe_attr(cs, 'gasPressed', False)),
                        "leftBlinker": left_blinker,
                        "rightBlinker": right_blinker
                    },
                    "drive": {
                        "enabled": enabled,
                        "active": active,
                        "experimentalMode": exp_mode,
                        "personalityRaw": pers_raw,
                        "personality": PERSONALITIES.get(pers_raw, "unknown"),
                        "alertHudVisual": safe_int(safe_attr(sd, 'alertHudVisual', safe_attr(ctl, 'alertHudVisual', 0)))
                    },
                    "plan": {
                        "aTarget": num(safe_attr(lp, 'aTarget', safe_attr(lp, 'aEgoTarget', 0))),
                        "shouldStop": bool(safe_attr(lp, 'shouldStop', False)),
                        "hasLead": bool(safe_attr(lp, 'hasLead', False)),
                        "allowBrake": bool(safe_attr(lp, 'allowBrake', False)),
                        "allowThrottle": bool(safe_attr(lp, 'allowThrottle', False)),
                        "source": str(safe_attr(lp, 'longitudinalPlanSource', ''))
                    },
                    "lead1": lead_dict(safe_attr(radar, 'leadOne', None)),
                    "settings": current_settings,
                    "errors": []
                })
            tick += 1
        except Exception as e:
            with LOCK: STATE["errors"] = [f"Parser Error: {str(e)}"]
        time.sleep(.05)

def write_setting(name, value):
    p = Params()
    if name == "personality":
        v = int(value)
        if v not in PERSONALITIES: raise ValueError("personality must be 0, 1, or 2")
        p.put(PARAMS[name], v)
    elif name == "follow_distance":
        v = int(value)
        if not 1 <= v <= 7: raise ValueError("follow distance must be 1..7")
        p.put(PARAMS[name], v)
    elif name in ("adaptive_accel", "experimental"):
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
.top{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:12px}.title{font-size:22px;font-weight:800}.status{font-size:12px;color:var(--m);font-weight:700;text-align:right}
.health-bar{font-size:11px;color:var(--m);display:flex;gap:12px;margin-top:4px;justify-content:flex-end}
.nav-tabs{display:flex;gap:10px;margin-bottom:15px;background:#111720;padding:6px;border-radius:14px;border:1px solid var(--line)}
.nav-tabs button{flex:1;background:transparent;border:none;color:var(--m);padding:10px;font-weight:700;border-radius:10px;font-size:14px}
.nav-tabs button.active{background:#1a222e;color:var(--t)}

.grid-layout{display:grid;grid-template-columns:1fr 1fr;gap:12px;align-items:stretch;}
.left-col{display:flex;flex-direction:column;gap:12px;}
@media(max-width:720px){.grid-layout{grid-template-columns:1fr;}}

.card{background:#111720f5;border:1px solid var(--line);border-radius:18px;padding:14px;box-shadow:0 8px 28px #0005}.label{font-size:12px;color:var(--m);text-transform:uppercase;letter-spacing:.08em;margin-bottom:9px}
.buttons{display:flex;gap:8px}.btn{flex:1;border:1px solid #344153;background:#1a222e;color:var(--t);padding:12px 8px;border-radius:12px;font-weight:700}.btn.active{background:#284d68;border-color:var(--a)}
.row{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:8px 0}.small{font-size:12px;color:var(--m)}
.switch{width:54px;height:30px;border-radius:20px;background:#303a47;border:0;position:relative}.switch i{position:absolute;width:24px;height:24px;top:3px;left:3px;border-radius:50%;background:#fff;transition:.15s}.switch.on{background:var(--g)}.switch.on i{left:27px}
.follow{display:grid;grid-template-columns:repeat(7,1fr);gap:5px}.follow button{padding:10px 2px;border-radius:9px;border:1px solid #344153;background:#1a222e;color:#fff}.follow button.active{background:#315b75;border-color:var(--a)}

/* Tesla FSD Style Road Canvas */
.road-card{display:flex;flex-direction:column;}
.road{flex:1;min-height:560px;position:relative;overflow:hidden;border-radius:16px;background:radial-gradient(circle at 50% 100%, #151d2a 0%, #080c14 100%); perspective:700px}
@keyframes scrollEdge { 0% { background-position: 0 0; } 100% { background-position: 0 64px; } }
@keyframes scrollCenter { 0% { background-position: 0 0; } 100% { background-position: 0 48px; } }
.road:before,.road:after{content:"";position:absolute;top:0;bottom:0;width:3px;opacity:.5;background:repeating-linear-gradient(to bottom,transparent 0,transparent 32px,#8093a7 32px,#8093a7 64px);animation:scrollEdge var(--road-speed) linear infinite}
.road:before{left:25%}.road:after{right:25%}
.centerline{position:absolute;left:50%;top:0;bottom:0;width:3px;opacity:.4;transform:translateX(-50%);background:repeating-linear-gradient(to bottom,transparent 0,transparent 24px,#506377 24px,#506377 48px);animation:scrollCenter var(--road-speed) linear infinite}

/* Tesla Vector Vehicle Styling */
.tesla-ego{position:absolute;left:50%;transform:translateX(-50%);bottom:15px;width:76px;height:128px;z-index:10;filter:drop-shadow(0 12px 20px rgba(0,0,0,0.6));}
.tesla-svg{width:100%;height:100%;}
.brake-glow{fill:#3a0b0d;transition:fill 0.15s;}
.brake-active .brake-glow{fill:#ff3b30;filter:drop-shadow(0 0 8px #ff3b30);}
.blinker-light{fill:#223028;transition:fill 0.15s;}
.blinker-active{fill:#34c759;animation:pulseBlinker 0.6s infinite;}
@keyframes pulseBlinker{0%,100%{fill:#34c759;filter:drop-shadow(0 0 6px #34c759);}50%{fill:#104018;filter:none;}}

/* Tesla FSD Lead Bounding Box */
.tesla-lead{position:absolute;left:50%;transform-origin:bottom center;width:72px;height:110px;z-index:5;transition:bottom 0.2s linear, transform 0.2s linear, opacity 0.2s ease-out;}
.lead-box-svg{width:100%;height:100%;}
.lead-badge{position:absolute;top:-26px;left:50%;transform:translateX(-50%);background:rgba(13,19,27,0.88);border:1px solid #344153;padding:2px 8px;border-radius:6px;font-size:11px;font-weight:800;color:var(--a);white-space:nowrap;backdrop-filter:blur(4px);box-shadow:0 4px 12px #0008;}

.telemetry{display:grid;grid-template-columns:repeat(4,1fr);gap:7px}.metric{background:var(--p2);border-radius:10px;padding:9px}.metric b{display:block;font-size:16px}.metric span{font-size:10px;color:var(--m)}
.leads{display:grid;grid-template-columns:1fr;gap:8px}.leadbox{background:var(--p2);border-radius:12px;padding:10px}.leadbox h3{margin:0 0 7px;font-size:13px}.kv{display:grid;grid-template-columns:1fr 1fr;gap:5px;font-size:11px}.kv span{color:var(--m)}.footer{margin-top:10px;font-size:11px;color:#718093;text-align:center}

/* Dashcam HUD Overlay */
.vid-container{width:100%;position:relative;aspect-ratio:16/9;background:#000;border-radius:16px;overflow:hidden;margin-bottom:12px;border:1px solid var(--line)}
video{width:100%;height:100%}
.hud-overlay{position:absolute;inset:0;pointer-events:none;padding:15px;display:none;flex-direction:column;justify-content:space-between;z-index:20;background:linear-gradient(180deg, rgba(0,0,0,0.5) 0%, transparent 20%, transparent 80%, rgba(0,0,0,0.6) 100%);}
.hud-top{display:flex;justify-content:space-between;align-items:center;}
.hud-bot{display:flex;justify-content:space-between;align-items:flex-end;}
.hud-box{background:#00000088;backdrop-filter:blur(4px);border:1px solid #ffffff33;padding:6px 12px;border-radius:10px;color:#fff;font-weight:700;font-family:monospace;text-shadow:0 2px 4px #000;font-size:14px;display:flex;flex-direction:column;align-items:center;}
.hud-pedals{display:flex;gap:14px;}
.hud-pedal-col{display:flex;flex-direction:column;align-items:center;gap:4px;}
.hud-pedal{width:34px;height:9px;border-radius:5px;background:#344153;transition:background 0.1s, box-shadow 0.1s;}
.hud-pedal.gas.active{background:var(--g);box-shadow:0 0 10px var(--g);}
.hud-pedal.brk.active{background:#ff5d67;box-shadow:0 0 10px #ff5d67;}
.hud-pedal-label{font-size:9px;font-weight:800;letter-spacing:.06em;color:#8f9baa;text-shadow:0 1px 3px #000;transition:color 0.1s;}
.hud-pedal.brk.active + .hud-pedal-label{color:#ff5d67;}
.hud-pedal.gas.active + .hud-pedal-label{color:var(--g);}

.turn-arrow{font-size:18px;color:#344153;transition:color 0.1s, text-shadow 0.1s;}
.turn-arrow.active{color:#34c759;text-shadow:0 0 8px #34c759;}

.steer-wrap{width:100%; margin-top:4px; display:flex; justify-content:center;}
.steer-gauge{width:100px; height:6px; background:#293241; border-radius:3px; position:relative;}
.steer-center{position:absolute; width:2px; height:10px; background:#8f9baa; left:50%; top:-2px; transform:translateX(-50%);}
.steer-ind{position:absolute; width:12px; height:10px; background:#fff; border-radius:2px; top:-2px; left:50%; transform:translateX(-50%); transition:transform 0.1s ease-out; box-shadow:0 1px 3px #000;}

select.cam-drop{background:#1a222e;color:var(--t);border:1px solid #344153;padding:8px 12px;border-radius:10px;font-weight:700;font-size:13px;outline:none;}
.route-item{background:var(--p2);border-radius:12px;padding:12px;margin-bottom:10px;cursor:pointer;border:1px solid var(--line)}
.route-item b{font-size:14px;display:block}
.route-item .small{margin-top:3px}
.segs-grid{display:flex;flex-wrap:wrap;gap:6px;margin-top:12px;display:none}
.route-item.open .segs-grid{display:flex}
.seg-btn{background:#1a222e;border:1px solid #344153;color:var(--t);padding:8px 12px;border-radius:8px;font-size:12px;font-weight:700}
.seg-btn:active{background:var(--a)}.seg-btn.playing{background:var(--a);border-color:#fff;color:#000}
.btn-ui{text-decoration:none;padding:8px 14px;background:#1a222e;color:var(--t);border-radius:10px;font-weight:700;font-size:13px;border:1px solid #344153;cursor:pointer;}
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

<div class="nav-tabs">
  <button id="tabbtn-drive" class="active" onclick="switchTab('drive')">Drive Settings</button>
  <button id="tabbtn-video" onclick="switchTab('video')">Dashcam Viewer</button>
</div>

<div id="tab-video" style="display:none;">
  <div class="vid-container">
    <video id="player" playsinline autoplay></video>
    <div id="hud-overlay" class="hud-overlay">
      <div class="hud-top">
        <div id="hud-left-arrow" class="turn-arrow">◀</div>
        <div class="hud-box" style="color:var(--a);">LEAD: <span id="hud-lead">--</span> m</div>
        <div id="hud-right-arrow" class="turn-arrow">▶</div>
      </div>
      <div class="hud-bot">
        <div class="hud-box" style="font-size:22px"><span id="hud-speed">0</span> <span style="font-size:12px;color:var(--m)">MPH</span></div>
        <div class="hud-pedals">
          <div class="hud-pedal-col">
            <div id="hud-brake" class="hud-pedal brk"></div>
            <span class="hud-pedal-label">BRAKE</span>
          </div>
          <div class="hud-pedal-col">
            <div id="hud-gas" class="hud-pedal gas"></div>
            <span class="hud-pedal-label">GAS</span>
          </div>
        </div>
        <div class="hud-box">
          STR: <span id="hud-steer">0</span>°
          <div class="steer-wrap">
            <div class="steer-gauge">
              <div class="steer-center"></div>
              <div id="steer-ind" class="steer-ind"></div>
            </div>
          </div>
        </div>
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
      <button class="btn-ui" onclick="toggleHud()">Toggle HUD</button>
    </div>
    
    <div style="display:flex; gap:10px; align-items:center;">
      <button class="btn-ui" onclick="togglePlay()" id="vid-play-btn" style="display:none; background:var(--a); color:#000; border:none;">Pause</button>
      <a id="download-btn" class="btn-ui" href="#" style="display:none; background:var(--g); color:#000; border:none;">Save Clip ↓</a>
      <button class="btn-ui" onclick="exportWithHud()" id="export-btn" style="display:none;">Export w/ HUD ↓</button>
    </div>
  </div>
  <div id="routes-list"><div class="label" style="text-align:center;margin-top:20px;">Loading routes...</div></div>
</div>

<div id="tab-drive">
<div class="grid-layout">
  <div class="left-col">
    <div class=card>
      <div class=label>Driving personality</div>
      <div class=buttons>
        <button class="btn p-btn" data-val="2" onclick="setv('personality',2)">Chill</button>
        <button class="btn p-btn" data-val="1" onclick="setv('personality',1)">Standard</button>
        <button class="btn p-btn" data-val="0" onclick="setv('personality',0)">Aggressive</button>
      </div>
      <div class=row><div><b>Follow distance</b><div class=small>1 closest · 7 farthest</div></div></div>
      <div class=follow id=follow></div>
      <div class=row><div><b>Experimental Mode</b><div class=small>Live planner mode</div></div><button id=exp class=switch onclick="toggle('experimental')"><i></i></button></div>
      <div class=row><div><b>Adaptive Accel</b><div class=small>NAP close-lead acceleration cap</div></div><button id=ada class=switch onclick="toggle('adaptive_accel')"><i></i></button></div>
    </div>
    
    <div class=card>
      <div class=label>Live vehicle</div>
      <div class=telemetry>
        <div class=metric><b id=speed>—</b><span>mph</span></div>
        <div class=metric><b id=accel>—</b><span>m/s²</span></div>
        <div class=metric><b id=target>—</b><span>target accel</span></div>
        <div class=metric><b id=state>—</b><span>control</span></div>
      </div>
      <div class=row><span>Planner</span><b id=planner>—</b></div>
      <div class=row><span>Lead</span><b id=haslead>—</b></div>
    </div>
    
    <div class=card>
      <div class=label>Lead telemetry</div>
      <div class=leads>
        <div class=leadbox><h3>Lead</h3><div id=l1 class=kv></div></div>
      </div>
    </div>
  </div>

  <div class="card road-card">
    <div class=label>Drive view</div>
    <div class=road>
      <div class=centerline></div>
      
      <!-- Tesla FSD Lead Bounding Model -->
      <div id=leadcar class="tesla-lead" style="display:none; opacity:0;">
        <div id="lead-badge" class="lead-badge">-- m</div>
        <svg class="lead-box-svg" viewBox="0 0 72 110">
          <rect x="6" y="10" width="60" height="90" rx="12" fill="rgba(86,182,255,0.18)" stroke="#56b6ff" stroke-width="2.5"/>
          <rect x="14" y="18" width="44" height="24" rx="6" fill="rgba(255,255,255,0.12)" stroke="#56b6ff" stroke-width="1.5"/>
          <rect x="12" y="90" width="14" height="6" rx="2" fill="#ff3b30"/>
          <rect x="46" y="90" width="14" height="6" rx="2" fill="#ff3b30"/>
        </svg>
      </div>
      
      <!-- Tesla Vector Ego Model -->
      <div id="ego-car" class="tesla-ego">
        <svg class="tesla-svg" viewBox="0 0 76 128">
          <!-- Body Base -->
          <rect x="10" y="12" width="56" height="104" rx="18" fill="#a0aab8"/>
          <!-- Cabin Roof -->
          <path d="M 18 38 C 22 22, 54 22, 58 38 L 54 86 C 50 94, 26 94, 22 86 Z" fill="#111720" stroke="#344153" stroke-width="1.5"/>
          <!-- Windshield Glare -->
          <path d="M 22 38 C 26 28, 50 28, 54 38 L 52 50 L 24 50 Z" fill="rgba(255,255,255,0.15)"/>
          <!-- Side Mirrors & Turn Signals -->
          <rect id="mirror-l" class="blinker-light" x="2" y="34" width="8" height="6" rx="2"/>
          <rect id="mirror-r" class="blinker-light" x="66" y="34" width="8" height="6" rx="2"/>
          <!-- Tail Lamps / Brake Lights -->
          <path id="brake-l" class="brake-glow" d="M 12 108 L 26 108 L 24 114 L 14 114 Z"/>
          <path id="brake-r" class="brake-glow" d="M 50 108 L 64 108 L 62 114 L 52 114 Z"/>
          <!-- Rear Blinker Overlays -->
          <rect id="blinker-l" class="blinker-light" x="8" y="108" width="6" height="6" rx="2"/>
          <rect id="blinker-r" class="blinker-light" x="62" y="108" width="6" height="6" rx="2"/>
        </svg>
      </div>

    </div>
  </div>
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

let logRequestId = 0;

async function fetchLogData(route, seg){
  const reqId = ++logRequestId;
  logData = [];
  $('hud-speed').textContent = "...";
  $('hud-steer').textContent = "...";
  $('hud-lead').textContent = "...";
  $('steer-ind').style.transform = `translateX(-50%)`;
  try {
    let r = await fetch(`/api/log/${route}--${seg}`);
    let j = await r.json();
    if(reqId !== logRequestId) return; 
    if(j.data && j.data.length) {
      logData = j.data;
    } else {
      $('hud-speed').textContent = "--";
      $('hud-steer').textContent = "--";
      $('hud-lead').textContent = "--";
    }
  } catch(e) {
    if(reqId !== logRequestId) return;
    $('hud-speed').textContent = "--";
    $('hud-steer').textContent = "--";
    $('hud-lead').textContent = "--";
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

  let expBtn = $('export-btn');
  expBtn.style.display = 'inline-block';
  expBtn.disabled = false;
  expBtn.textContent = 'Export w/ HUD ↓';

  window.scrollTo({top: 0, behavior: 'smooth'});
}

async function exportWithHud(){
  if(!currentRoute) return;
  let btn = $('export-btn');
  let cam = $('cam-select').value;
  btn.disabled = true;
  btn.textContent = 'Exporting… (can take a minute)';
  try {
    let r = await fetch(`/export/${currentRoute}--${currentSeg}?cam=${cam}`);
    if(!r.ok) {
      let j = await r.json().catch(() => ({}));
      throw new Error(j.error || `export failed (${r.status})`);
    }
    let blob = await r.blob();
    let url = URL.createObjectURL(blob);
    let a = document.createElement('a');
    a.href = url;
    a.download = `Comma_Clip_HUD_${currentRoute}_${currentSeg}_${cam}.mp4`.replace(/[|]/g, '_');
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
  } catch(e) {
    console.error('Export failed:', e);
    alert('Export failed: ' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Export w/ HUD ↓';
  }
}

$('player').addEventListener('timeupdate', () => {
  if(!hudActive || logData.length === 0) return;
  
  let t = $('player').currentTime;
  let idx = Math.floor(t * 10);
  
  if(idx >= 0 && idx < logData.length){
    let frame = logData[idx];
    $('hud-speed').textContent = frame[0].toFixed(0);
    
    let steerDeg = frame[1];
    $('hud-steer').textContent = steerDeg.toFixed(1);
    
    let steerPx = Math.max(-50, Math.min(50, (-steerDeg / 45.0) * 50));
    $('steer-ind').style.transform = `translateX(calc(-50% + ${steerPx}px))`;
    
    $('hud-gas').classList.toggle('active', frame[2] === 1);
    $('hud-brake').classList.toggle('active', frame[3] === 1);
    $('hud-lead').textContent = frame[4] > 0 ? frame[4].toFixed(1) : '--';

    $('hud-left-arrow').classList.toggle('active', frame[5] === 1);
    $('hud-right-arrow').classList.toggle('active', frame[6] === 1);
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
    let l = s.lead1 || {};
    
    let speedMph = (+c.vEgo) * 2.23694;
    $("speed").textContent=f(speedMph,0);$("accel").textContent=f(c.aEgo,2);
    $("target").textContent=f(pl.aTarget,2);$("state").textContent=d.active?"ACTIVE":d.enabled?"ENABLED":"OFF";
    $("planner").textContent=pl.source||"—";$("haslead").textContent=pl.hasLead?"YES":"NO";
    
    lb("l1",s.lead1);
    
    if(speedMph > 1) {
      let animSpeed = Math.max(0.15, 20 / speedMph).toFixed(2);
      document.documentElement.style.setProperty('--road-speed', animSpeed + 's');
    } else {
      document.documentElement.style.setProperty('--road-speed', '0s'); 
    }

    // Tesla Ego Car Active Lights Update
    let egoEl = $("ego-car");
    if(egoEl){
      egoEl.classList.toggle("brake-active", c.brakePressed === true);
      $("mirror-l").classList.toggle("blinker-active", c.leftBlinker === true);
      $("blinker-l").classList.toggle("blinker-active", c.leftBlinker === true);
      $("mirror-r").classList.toggle("blinker-active", c.rightBlinker === true);
      $("blinker-r").classList.toggle("blinker-active", c.rightBlinker === true);
    }

    // Tesla FSD Style Lead Bounding Model
    let lc=$("leadcar");
    let dist = +l.dRel || 0;
    let isRealLead = (l.status === true || l.status === 1) && dist > 2.0;

    if(isRealLead){
      let vRel = +l.vRel || 0;
      let danger = (vRel < -3.0) || l.fcw;
      
      let bottomPx = 155 + (dist * 4.5); 
      bottomPx = Math.max(165, Math.min(520, bottomPx)); 
      
      let scale = Math.max(0.2, Math.min(1.0, 1.0 - (dist / 140)));
      
      lc.style.bottom = bottomPx + "px";
      lc.style.top = "auto";
      lc.style.transformOrigin = "bottom center";
      lc.style.transform = `translateX(-50%) scale(${scale})`;
      
      let badge = $("lead-badge");
      if(badge) badge.textContent = `${dist.toFixed(1)} m`;

      lc.style.display = "block";
      lc.style.opacity = "1";
    } else {
      lc.style.display = "none";
      lc.style.opacity = "0";
    }
  } catch(e) { console.error("Render crash avoided:", e); }
}

async function get(){
  try{
    let r=await fetch("/api/state",{cache:"no-store"});
    if(!r.ok)throw Error();
    render(await r.json());
  }catch(e){
    $("status").textContent="Disconnected (Reconnecting...)";
    $("status").style.color="#ff5d67";
  }
}
async function setv(name,value){try{let r=await fetch("/api/set",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name,value})}),j=await r.json();if(!r.ok)throw Error(j.error||"write failed");render(j)}catch(e){alert("Change failed: "+e.message)}}
function toggle(n){setv(n,!S.settings[n])}follow(4);get();setInterval(get,250);
</script></body></html>"""

def find_log_path(seg_dir):
    for filename in ["rlog.zst", "rlog.bz2", "qlog.zst", "qlog.bz2"]:
        candidate = os.path.join(seg_dir, filename)
        if os.path.exists(candidate):
            return candidate
    return None

def parse_telemetry_timeline(log_path):
    import sys
    if "/data/openpilot" not in sys.path:
        sys.path.insert(0, "/data/openpilot")
    try:
        from tools.lib.logreader import LogReader
    except Exception:
        from openpilot.tools.lib.logreader import LogReader

    lr = LogReader(log_path)
    timeline = []
    engagement_timeline = []
    vEgo, steer, gas, brake, lead_d = 0.0, 0.0, False, False, 0.0
    left_blinker, right_blinker = False, False
    engaged = False
    t0 = None
    skipped = 0
    cs_count, rs_count = 0, 0
    sd_count, ctl_count = 0, 0
    engaged_samples = 0
    nonzero_vego_seen = False
    sample = None

    for msg in lr:
        try:
            w = msg.which()
            if w not in ("carState", "radarState", "selfdriveState", "controlsState"):
                continue

            t = msg.logMonoTime / 1e9
            if t0 is None:
                t0 = t
            rel_t = t - t0
            if rel_t < 0 or rel_t > 61.0:
                continue

            if w == "carState":
                cs_count += 1
                cs = msg.carState
                vEgo = getattr(cs, "vEgo", 0)
                steer = getattr(cs, "steeringAngleDeg", 0)
                gas = getattr(cs, "gasPressed", False)
                brake = getattr(cs, "brakePressed", False)
                left_blinker = getattr(cs, "leftBlinker", False)
                right_blinker = getattr(cs, "rightBlinker", False)
                if vEgo:
                    nonzero_vego_seen = True
                if sample is None:
                    sample = {
                        "vEgo": vEgo,
                        "steeringAngleDeg": steer,
                        "gasPressed": gas,
                        "brakePressed": brake,
                    }

            elif w == "radarState":
                rs_count += 1
                rs = msg.radarState
                lead = getattr(rs, "leadOne", None)
                if lead and getattr(lead, "status", False):
                    lead_d = getattr(lead, "dRel", 0)
                else:
                    lead_d = 0

            elif w == "selfdriveState":
                sd_count += 1
                sd = msg.selfdriveState
                active = getattr(sd, "active", None)
                enabled = getattr(sd, "enabled", None)
                if active is not None:
                    engaged = bool(active)
                elif enabled is not None:
                    engaged = bool(enabled)

            elif w == "controlsState":
                ctl_count += 1
                ctl = msg.controlsState
                active = getattr(ctl, "active", None)
                enabled = getattr(ctl, "enabled", None)
                if active is not None and bool(active):
                    engaged = True
                elif enabled is not None and bool(enabled):
                    engaged = True
                elif w == "controlsState":
                    if active is False and enabled is False:
                        engaged = False

            expected_idx = int(rel_t * 10)
            while len(timeline) <= expected_idx and len(timeline) < 600:
                timeline.append([
                    round(vEgo * 2.23694, 1),
                    round(steer, 1),
                    1 if gas else 0,
                    1 if brake else 0,
                    round(lead_d, 1),
                    1 if left_blinker else 0,
                    1 if right_blinker else 0
                ])
                engagement_timeline.append(1 if engaged else 0)

            if engaged:
                engaged_samples += 1

        except Exception:
            skipped += 1
            continue

    if engagement_timeline:
        last = engagement_timeline[0]
        for i in range(len(engagement_timeline)):
            if engagement_timeline[i]:
                last = 1
            elif last:
                engagement_timeline[i] = 1

    diagnostics = {
        "carState": cs_count,
        "radarState": rs_count,
        "selfdriveState": sd_count,
        "controlsState": ctl_count,
        "engaged_samples": engaged_samples,
        "skipped": skipped,
        "frames": len(timeline),
        "nonzero_vEgo_seen": nonzero_vego_seen,
        "sample_first_carState": sample
    }
    return timeline, engagement_timeline, diagnostics


def probe_video_size(path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0", path],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15
        ).stdout.decode().strip()
        w, h = out.split(",")[:2]
        return int(w), int(h)
    except Exception:
        return 1928, 1208  

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
        except Exception: 
            pass
        
        subprocess.run(["ffmpeg", "-y", "-i", cam_file, "-c", "copy", "-movflags", "faststart", tmp_path], 
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                     
    return tmp_path if os.path.exists(tmp_path) else None

def serve_file_with_range(handler, path, content_type="application/octet-stream", attachment_name=None):
    try:
        file_size = os.path.getsize(path)
    except OSError as e:
        handler.send_error(404, str(e))
        return

    range_header = handler.headers.get("Range")
    start, end = 0, file_size - 1

    if range_header:
        try:
            units, _, rng = range_header.partition("=")
            if units.strip() != "bytes":
                raise ValueError("unsupported range unit")
            start_str, _, end_str = rng.partition("-")
            if start_str.strip():
                start = int(start_str)
                end = int(end_str) if end_str.strip() else file_size - 1
            else:
                suffix_len = int(end_str)
                start = max(0, file_size - suffix_len)
                end = file_size - 1
            start = max(0, start)
            end = min(file_size - 1, end)
            if start > end:
                raise ValueError("invalid range")
        except (ValueError, IndexError):
            handler.send_response(416)
            handler.send_header("Content-Range", f"bytes */{file_size}")
            handler.end_headers()
            return

    length = end - start + 1

    try:
        f = open(path, "rb")
    except OSError as e:
        handler.send_error(404, str(e))
        return

    with f:
        handler.send_response(206 if range_header else 200)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Accept-Ranges", "bytes")
        handler.send_header("Content-Length", str(length))
        if range_header:
            handler.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
        if attachment_name:
            handler.send_header("Content-Disposition", f'attachment; filename="{attachment_name}"')
        handler.end_headers()

        f.seek(start)
        remaining = length
        chunk_size = 1024 * 1024
        try:
            while remaining > 0:
                chunk = f.read(min(chunk_size, remaining))
                if not chunk:
                    break
                handler.wfile.write(chunk)
                remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*a):
        pass

    def send_json(self, o, status=200):
        b = json.dumps(o, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)
    
    def do_GET(self):
        parsed = urlparse(self.path)
        p = parsed.path
        qs = parse_qs(parsed.query)
        
        if p in ("/", "/index.html"):
            b = HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        elif p == "/api/state":
            with LOCK:
                o = json.loads(json.dumps(STATE))
            self.send_json(o)
        elif p == "/api/routes":
            self.send_json(get_routes())
        elif p.startswith("/api/log/"):
            route_seg = p.split("/api/log/")[1]
            cache_path = f"/dev/shm/rlog_v3_{route_seg}.json"

            if os.path.exists(cache_path):
                try:
                    with open(cache_path, "r") as f:
                        cached = json.load(f)
                    if cached.get("data"):
                        self.send_json(cached)
                        return
                except Exception:
                    pass

            seg_dir = f"/data/media/0/realdata/{route_seg}"
            log_path = find_log_path(seg_dir)

            if not log_path:
                self.send_json({"error": f"No log found in {seg_dir}"})
                return

            try:
                timeline, engagement_timeline, diag = parse_telemetry_timeline(log_path)
                res = {"data": timeline}
                if timeline:
                    with open(cache_path, "w") as f: json.dump(res, f)
                self.send_json(res)
            except Exception as e:
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
        elif p.startswith("/export/"):
            route_seg = p.split("/export/")[1]
            cam_type = qs.get("cam", ["qcamera"])[0]
            self.handle_export(route_seg, cam_type)
        else:
            self.send_json({"error": "not found"}, 404)

    def handle_export(self, route_seg, cam_type):
        src_path = get_mp4_path(route_seg, cam_type)
        if not src_path:
            self.send_json({"error": "source video not found"}, 404)
            return

        seg_dir = f"/data/media/0/realdata/{route_seg}"
        log_path = find_log_path(seg_dir)
        if not log_path:
            self.send_json({"error": f"No telemetry log found in {seg_dir}"}, 404)
            return

        out_path = f"/dev/shm/export_{route_seg}_{cam_type}.mp4"

        try:
            if not os.path.exists(out_path):
                timeline, engagement_timeline, diag = parse_telemetry_timeline(log_path)
                if not timeline:
                    self.send_json({"error": "telemetry log parsed to 0 frames, nothing to overlay"}, 422)
                    return

                width, height = probe_video_size(src_path)
                width = (width // 2) * 2
                height = (height // 2) * 2

                hud_proc = None
                try:
                    cmd = [
                        "ffmpeg", "-y",
                        "-f", "rawvideo",
                        "-vcodec", "rawvideo",
                        "-pix_fmt", "yuv420p",
                        "-s", f"{width}x{height}",
                        "-r", "20",
                        "-i", "pipe:0",
                        "-i", src_path,
                        "-filter_complex", "[1:v][0:v]blend=all_mode=addition:all_opacity=1[outv]",
                        "-map", "[outv]",
                        "-map", "1:a?",
                        "-c:v", "libx264", 
                        "-pix_fmt", "yuv420p", 
                        "-preset", "veryfast", "-crf", "23",
                        "-c:a", "copy", "-movflags", "+faststart",
                        out_path
                    ]

                    hud_proc = subprocess.Popen(
                        cmd,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT
                    )

                    FONT = {
                        " ": ["000000"]*10,
                        "0": ["011110","110011","110011","110011","110011","110011","110011","110011","110011","011110"],
                        "1": ["001100","011100","001100","001100","001100","001100","001100","001100","001100","011110"],
                        "2": ["011110","110011","000011","000011","000110","011100","110000","110000","110000","111111"],
                        "3": ["011110","110011","000011","000011","001110","000011","000011","000011","110011","011110"],
                        "4": ["000011","000111","001111","011011","110011","111111","000011","000011","000011","000011"],
                        "5": ["111111","110000","110000","111110","000011","000011","000011","000011","110011","011110"],
                        "6": ["011110","110011","110000","110000","111110","110011","110011","110011","110011","011110"],
                        "7": ["111111","110011","000011","000110","001100","011000","110000","110000","110000","110000"],
                        "8": ["011110","110011","110011","110011","011110","110011","110011","110011","110011","011110"],
                        "9": ["011110","110011","110011","110011","110011","011111","000011","000011","110011","011110"],
                        "A": ["001100","011110","110011","110011","110011","111111","110011","110011","110011","110011"],
                        "B": ["111110","110011","110011","110011","111110","110011","110011","110011","110011","111110"],
                        "C": ["011110","110011","110000","110000","110000","110000","110000","110000","110011","011110"],
                        "D": ["111100","110110","110011","110011","110011","110011","110011","110011","110110","111100"],
                        "E": ["111111","110000","110000","110000","111110","110000","110000","110000","110000","111111"],
                        "F": ["111111","110000","110000","110000","111110","110000","110000","110000","110000","110000"],
                        "G": ["011110","110011","110000","110000","110000","110111","110011","110011","110011","011110"],
                        "H": ["110011","110011","110011","110011","111111","110011","110011","110011","110011","110011"],
                        "I": ["111111","001100","001100","001100","001100","001100","001100","001100","001100","111111"],
                        "K": ["110011","110110","111100","111000","111000","111100","110110","110011","110011","110011"],
                        "L": ["110000","110000","110000","110000","110000","110000","110000","110000","110000","111111"],
                        "M": ["110011","111111","111111","110011","110011","110011","110011","110011","110011","110011"],
                        "N": ["110011","111011","111011","111011","111011","110111","110111","110111","110011","110011"],
                        "O": ["011110","110011","110011","110011","110011","110011","110011","110011","110011","011110"],
                        "P": ["111110","110011","110011","110011","111110","110000","110000","110000","110000","110000"],
                        "R": ["111110","110011","110011","110011","111110","111100","110110","110011","110011","110011"],
                        "S": ["011110","110011","110000","110000","011110","000011","000011","110011","110011","011110"],
                        "T": ["111111","001100","001100","001100","001100","001100","001100","001100","001100","001100"],
                        "U": ["110011","110011","110011","110011","110011","110011","110011","110011","110011","011110"],
                        "V": ["110011","110011","110011","110011","110011","110011","011110","011110","001100","001100"],
                        "-": ["000000","000000","000000","000000","111111","111111","000000","000000","000000","000000"],
                        ".": ["000000","000000","000000","000000","000000","000000","000000","000000","001100","001100"],
                        "<": ["000011","000110","001100","011000","110000","011000","001100","000110","000011","000000"],
                        ">": ["110000","011000","001100","000110","000011","000110","001100","011000","110000","000000"],
                    }

                    def put_text(buf, x, y, text_value, scale=2, intensity=255):
                        cursor = x
                        for ch in str(text_value).upper():
                            glyph = FONT.get(ch, FONT[" "])
                            for gy, row in enumerate(glyph):
                                for gx, bit in enumerate(row):
                                    if bit == "1":
                                        px = cursor + gx * scale
                                        py = y + gy * scale
                                        for yy in range(py, min(py + scale + 1, height)):  
                                            if yy < 0:
                                                continue
                                            row_base = yy * width 
                                            for xx in range(px, min(px + scale + 1, width)):
                                                if xx < 0:
                                                    continue
                                                buf[row_base + xx] = intensity
                            cursor += 8 * scale  
                        return cursor

                    def draw_rect(buf, rx, ry, rw, rh, intensity):
                        rx, ry = int(rx), int(ry)
                        rw, rh = int(rw), int(rh)
                        for yy in range(max(0, ry), min(height, ry + rh)):
                            row_base = yy * width
                            for xx in range(max(0, rx), min(width, rx + rw)):
                                buf[row_base + xx] = intensity

                    frames = max(1, min(len(timeline), 600))
                    scale_big = 2
                    scale_small = 1
                    
                    frame_size = int(width * height * 1.5)
                    engagement_series = engagement_timeline

                    for frame_idx in range(frames):
                        frame = timeline[frame_idx]
                        speed, steer, gas, brake, lead = frame[0], frame[1], frame[2], frame[3], frame[4]
                        left_blink = frame[5] if len(frame) > 5 else 0
                        right_blink = frame[6] if len(frame) > 6 else 0

                        buf = bytearray(frame_size) 

                        engaged = bool(
                            engagement_series[frame_idx]
                            if engagement_series is not None and frame_idx < len(engagement_series)
                            else False
                        )

                        status = "ENGAGED" if engaged else "STANDBY"
                        put_text(buf, max(2, int(width * 0.04)), max(2, int(height * 0.04)), status, scale_small, 255 if engaged else 150)

                        put_text(buf, max(2, int(width * 0.04)), max(2, int(height * 0.78)), f"{speed:.0f} MPH", scale_big, 255)

                        if left_blink:
                            put_text(buf, max(2, int(width * 0.02)), max(2, int(height * 0.04)), "<", scale_small, 255)
                        if right_blink:
                            put_text(buf, max(2, int(width * 0.94)), max(2, int(height * 0.04)), ">", scale_small, 255)

                        if brake:
                            put_text(buf, max(2, int(width * 0.78)), max(2, int(height * 0.04)), "BRAKE", scale_small, 255)
                        if gas:
                            put_text(buf, max(2, int(width * 0.88)), max(2, int(height * 0.04)), "GAS", scale_small, 255)

                        lead_txt = f"LEAD {lead:.1f} M" if lead > 0 else "LEAD --"
                        lead_width = len(lead_txt) * 8 * scale_small
                        put_text(buf, max(2, (width - lead_width) // 2), max(2, int(height * 0.05)), lead_txt, scale_small, 255)

                        steer_txt = f"STR {steer:.1f}"
                        steer_width = len(steer_txt) * 8 * scale_small
                        put_text(buf, max(2, (width - steer_width) // 2), max(2, int(height * 0.88)), steer_txt, scale_small, 255)

                        cx = width // 2
                        cy = int(height * 0.94)
                        gw = int(width * 0.12)  
                        gh = int(height * 0.01) 
                        
                        draw_rect(buf, cx - gw, cy, gw * 2, gh, 80)
                        draw_rect(buf, cx - 2, cy - 4, 4, gh + 8, 255)
                        
                        steer_offset = int(max(-1.0, min(1.0, -steer / 45.0)) * gw)
                        draw_rect(buf, cx + steer_offset - 6, cy - 3, 12, gh + 6, 255)

                        try:
                            hud_proc.stdin.write(buf)
                        except BrokenPipeError:
                            break 

                    hud_proc.stdin.close()
                    hud_proc.stdin = None
                    output = hud_proc.stdout.read()
                    returncode = hud_proc.wait()

                    if returncode != 0 or not os.path.exists(out_path):
                        err = output.decode(errors="replace")[-4000:]
                        if os.path.exists(out_path):
                            os.remove(out_path)
                        self.send_json({"error": "ffmpeg HUD burn-in failed, see server console", "detail": err[-1000:]}, 500)
                        return
                finally:
                    if hud_proc is not None and hud_proc.poll() is None:
                        try:
                            if hud_proc.stdin:
                                hud_proc.stdin.close()
                        except Exception:
                            pass
                        hud_proc.kill()
                        hud_proc.wait()

            safe_name = f"Comma_Clip_HUD_{route_seg.replace('|', '_').replace('--', '_')}_{cam_type}.mp4"
            serve_file_with_range(self, out_path, content_type="video/mp4", attachment_name=safe_name)
        except subprocess.TimeoutExpired:
            self.send_json({"error": "export timed out (video too long/high-res for this device)"}, 504)
        except Exception as e:
            traceback.print_exc()
            self.send_json({"error": str(e)}, 500)
    
    def do_POST(self):
        if urlparse(self.path).path != "/api/set":
            return self.send_json({"error": "not found"}, 404)
        try:
            n = int(self.headers.get("Content-Length", "0"))
            d = json.loads(self.rfile.read(n).decode())
            settings = write_setting(str(d.get("name")), d.get("value"))
            with LOCK:
                STATE["settings"] = settings 
                o = json.loads(json.dumps(STATE))
            self.send_json(o)
        except Exception as e:
            self.send_json({"error": str(e)}, 400)

def main():
    print(f"NAP Drive Panel listening on port {PORT}")
    threading.Thread(target=telemetry, daemon=True).start()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        STOP.set()
        srv.server_close()

if __name__ == "__main__":
    main()
EOF
