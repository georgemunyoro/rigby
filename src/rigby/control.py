"""Live control surface: a small HTTP server and a single-page UI.

Tuning by editing a flag and restarting loses the thing you were listening to,
so every knob here is applied to the running show between frames. The page also
shows the analysis -- bands, dynamics, swell, pulse -- because most of the
tuning questions are really "what does the analyser think is happening", and
answering that from a number is far quicker than inferring it from the lights.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field, fields
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOOKS_LIST = ["auto", "duotone", "rain", "spectrum", "chase"]
DUOS_LIST = ["ember", "toxic", "vapor", "cobalt", "mono"]
PALETTES_LIST = ["sunset", "cyanmag", "acid", "ice"]
HIT_STYLES = ["swing", "accent", "white"]

# name -> (min, max, step). Anything here renders as a slider.
SLIDERS = {
    "master":          (0.0, 1.0, 0.01),
    "gain":            (0.2, 4.0, 0.05),
    "curve":           (0.2, 1.0, 0.01),
    "gamma":           (1.0, 3.0, 0.05),
    "saturation":      (0.0, 1.0, 0.01),
    "hot":             (0.0, 1.0, 0.01),
    "hue_drift":       (0.0, 6.0, 0.1),
    "dynamics_db":     (6.0, 40.0, 0.5),
    "onset_k":         (0.5, 4.0, 0.05),
    "swing_min_beats": (1, 24, 1),
    "offset_ms":       (0, 400, 5),
}

# Changing these means building a new Look; the rest are live attributes.
REBUILD = {"look", "duo"}


@dataclass
class Params:
    look: str = "auto"
    duo: str = "ember"
    palette: str = "sunset"
    hit_style: str = "swing"
    master: float = 1.0
    gain: float = 1.6
    curve: float = 0.45
    gamma: float = 2.2
    saturation: float = 0.88
    hot: float = 0.5
    hue_drift: float = 1.0
    dynamics_db: float = 15.0
    onset_k: float = 1.7
    swing_min_beats: int = 6
    offset_ms: int = 0
    blackout: bool = False
    playground: bool = False

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _dirty: set = field(default_factory=set, repr=False)

    def snapshot(self) -> dict:
        # Not dataclasses.asdict: it deep-copies every field, and this one
        # holds a lock, which cannot be copied.
        with self._lock:
            return {f.name: getattr(self, f.name) for f in fields(self)
                    if not f.name.startswith("_")}

    def update(self, changes: dict) -> set:
        """Apply changes, returning the set of names that actually changed."""
        changed = set()
        with self._lock:
            for k, v in changes.items():
                if k.startswith("_") or not hasattr(self, k):
                    continue
                cur = getattr(self, k)
                try:
                    v = type(cur)(v) if not isinstance(cur, bool) else bool(v)
                except (TypeError, ValueError):
                    continue
                if v != cur:
                    setattr(self, k, v)
                    changed.add(k)
            self._dirty |= changed
        return changed

    def take_dirty(self) -> set:
        with self._lock:
            d, self._dirty = self._dirty, set()
            return d


class Canvas:
    """Hand-set LED colours, used instead of the look while in playground mode.

    Stored per fixture as plain 0..1 floats so it round-trips through JSON
    without conversion, and so it can be handed to the sink unchanged.
    """

    def __init__(self, geometry: dict):
        self._lock = threading.Lock()
        self.cells: dict[str, list[list[float]]] = {
            name: [[0.0, 0.0, 0.0] for _ in range(g["n"])]
            for name, g in geometry.items()}

    def snapshot(self) -> dict:
        with self._lock:
            return {k: [list(c) for c in v] for k, v in self.cells.items()}

    def apply(self, msg: dict) -> None:
        op = msg.get("op")
        with self._lock:
            if op == "clear":
                for v in self.cells.values():
                    for c in v:
                        c[0] = c[1] = c[2] = 0.0
                return
            if op == "fill":
                rgb = [float(x) for x in msg.get("rgb", [0, 0, 0])][:3]
                names = msg.get("fixtures") or list(self.cells)
                for n in names:
                    for c in self.cells.get(n, []):
                        c[:] = rgb
                return
            if op == "set":
                # {"op":"set","cells":{"fan_a":{"0":[r,g,b], ...}, ...}}
                for name, idxs in (msg.get("cells") or {}).items():
                    cells = self.cells.get(name)
                    if cells is None:
                        continue
                    for i, rgb in idxs.items():
                        try:
                            j = int(i)
                        except (TypeError, ValueError):
                            continue
                        if 0 <= j < len(cells):
                            cells[j][:] = [max(0.0, min(1.0, float(x)))
                                           for x in rgb[:3]]
                return
            if op == "load":
                # Adopt a whole frame, e.g. captured from the running show.
                for name, arr in (msg.get("cells") or {}).items():
                    cells = self.cells.get(name)
                    if cells is None:
                        continue
                    for j, rgb in enumerate(arr[:len(cells)]):
                        cells[j][:] = [max(0.0, min(1.0, float(x)))
                                       for x in rgb[:3]]


class Telemetry:
    """Latest analysis values, written by the render loop, read by the UI."""

    def __init__(self):
        self._lock = threading.Lock()
        self.data: dict = {}

    def set(self, **kw) -> None:
        with self._lock:
            self.data.update(kw)

    def get(self) -> dict:
        with self._lock:
            return dict(self.data)


def _handler(params: Params, telem: Telemetry, patch_text: str,
             geometry: dict, canvas: Canvas, devices):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):        # keep the terminal for the show
            pass

        def _send(self, code, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.startswith("/state"):
                body = json.dumps({"params": params.snapshot(),
                                   "telemetry": telem.get(),
                                   "patch": patch_text}).encode()
                return self._send(200, body, "application/json")
            if self.path.startswith("/devices"):
                return self._send(200, json.dumps(devices.state()).encode(),
                                  "application/json")
            if self.path.startswith("/patch"):
                return self._send(200, json.dumps(geometry).encode(),
                                  "application/json")
            if self.path.startswith("/canvas"):
                return self._send(200, json.dumps(canvas.snapshot()).encode(),
                                  "application/json")
            if self.path in ("/", "/index.html"):
                return self._send(200, page().encode(), "text/html; charset=utf-8")
            self._send(404, b"not found", "text/plain")

        def do_POST(self):
            if self.path.startswith("/devices"):
                n = int(self.headers.get("Content-Length", 0) or 0)
                try:
                    msg = json.loads(self.rfile.read(n) or b"{}")
                except json.JSONDecodeError:
                    return self._send(400, b"bad json", "text/plain")
                out = devices.command(msg if isinstance(msg, dict) else {})
                return self._send(200, json.dumps(out).encode(),
                                  "application/json")
            if self.path.startswith("/canvas"):
                n = int(self.headers.get("Content-Length", 0) or 0)
                try:
                    msg = json.loads(self.rfile.read(n) or b"{}")
                except json.JSONDecodeError:
                    return self._send(400, b"bad json", "text/plain")
                if isinstance(msg, dict):
                    canvas.apply(msg)
                return self._send(200, b"{}", "application/json")
            if not self.path.startswith("/set"):
                return self._send(404, b"not found", "text/plain")
            n = int(self.headers.get("Content-Length", 0) or 0)
            try:
                changes = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                return self._send(400, b"bad json", "text/plain")
            params.update(changes if isinstance(changes, dict) else {})
            self._send(200, json.dumps(params.snapshot()).encode(),
                       "application/json")

    return H


class ControlServer:
    """Unauthenticated by design -- bind it to localhost unless you mean it.

    Anyone who can reach the port can drive the lights, so the default is
    127.0.0.1 and reaching it from a phone is an explicit opt-in.
    """

    def __init__(self, params: Params, telem: Telemetry, patch_text: str,
                 geometry: dict, canvas: Canvas, devices,
                 host: str = "127.0.0.1", port: int = 8721):
        self.params, self.telem, self.canvas = params, telem, canvas
        self._patch_text, self._devices = patch_text, devices
        self.httpd = ThreadingHTTPServer(
            (host, port),
            _handler(params, telem, patch_text, geometry, canvas, devices))
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self._t = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def set_geometry(self, geometry: dict, canvas: Canvas) -> None:
        """Swap in a new patch after a rebuild, without dropping the server."""
        self.canvas = canvas
        self.httpd.RequestHandlerClass = _handler(
            self.params, self.telem, self._patch_text, geometry, canvas,
            self._devices)

    def start(self) -> None:
        self._t.start()

    def stop(self) -> None:
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except Exception:
            pass


PAGE = r"""<!doctype html>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>rigby</title>
<style>
:root{--bg:#0e0f13;--fg:#e7e9ee;--dim:#8b90a0;--line:#22252e;--acc:#7dd3fc}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace}
header{padding:14px 18px;border-bottom:1px solid var(--line);display:flex;gap:16px;align-items:baseline;flex-wrap:wrap}
h1{font-size:15px;margin:0;letter-spacing:.14em;text-transform:uppercase}
.fps{color:var(--dim)}
main{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:0}
@media(max-width:760px){main{grid-template-columns:1fr}}
section{padding:16px 18px;border-right:1px solid var(--line)}
h2{font-size:11px;letter-spacing:.16em;text-transform:uppercase;color:var(--dim);margin:0 0 12px}
.row{display:grid;grid-template-columns:118px 1fr 54px;gap:10px;align-items:center;margin-bottom:9px}
label{color:var(--dim);font-size:12px}
input[type=range]{width:100%;accent-color:var(--acc)}
.val{text-align:right;font-variant-numeric:tabular-nums}
select,button{background:#171a21;color:var(--fg);border:1px solid var(--line);border-radius:5px;padding:6px 9px;font:inherit;font-size:12px}
button{cursor:pointer}
button.on{background:var(--acc);color:#08131a;border-color:var(--acc)}
.bars{display:grid;grid-template-columns:78px 1fr 46px;gap:8px;align-items:center;margin-bottom:6px}
.bar{height:9px;background:#171a21;border-radius:5px;overflow:hidden}
.bar>i{display:block;height:100%;background:var(--acc);width:0}
.bands{display:flex;gap:3px;height:52px;align-items:flex-end;margin:6px 0 12px}
.bands>i{flex:1;background:var(--acc);border-radius:2px 2px 0 0;height:2px}
pre{color:var(--dim);font-size:11px;white-space:pre-wrap;margin:0}
.hit{color:#08131a;background:#fca5a5;border-radius:4px;padding:0 6px;opacity:0}
.tools{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:16px}
.tools input[type=color]{width:42px;height:30px;padding:0;background:none;border:1px solid var(--line);border-radius:5px}
.sw{color:var(--dim);font-size:12px}
.fx{margin:0 0 18px}
.fxname{color:var(--dim);font-size:11px;letter-spacing:.14em;text-transform:uppercase;margin-bottom:7px;display:flex;gap:8px;align-items:center}
.fxname button{padding:2px 7px;font-size:10px}
.led{stroke:#2b2f3a;stroke-width:1;cursor:pointer}
.led:hover{stroke:var(--acc);stroke-width:2}
.strip{display:flex;flex-wrap:wrap;gap:4px}
.cell{width:19px;height:19px;border-radius:4px;border:1px solid #2b2f3a;cursor:pointer;background:#000}
.cell.gap{border:0;background:none;cursor:default}
.cell:hover{border-color:var(--acc)}
.grid{display:grid;gap:3px}
table{border-collapse:collapse;font-size:12px}
td,th{padding:4px 10px 4px 0;text-align:left;color:var(--fg)}
th{color:var(--dim);font-weight:400;font-size:11px;letter-spacing:.1em;text-transform:uppercase}
td input[type=number],td input[type=text]{background:#171a21;color:var(--fg);border:1px solid var(--line);border-radius:4px;padding:3px 6px;font:inherit;font-size:12px}
.dim{color:var(--dim)}
button.sp{padding:1px 6px;font-size:10px;margin-right:3px}
.frow{display:grid;grid-template-columns:130px 54px 62px 1fr 46px 78px;gap:9px;align-items:center;margin-bottom:7px;font-size:12px}
</style>
<div id="err" style="display:none;background:#7f1d1d;color:#fee;padding:8px 18px;font-size:12px"></div>
<header>
  <h1>rigby</h1>
  <span class="fps" id="fps">--</span>
  <span class="fps" id="inp">--</span>
  <span class="hit" id="hit">BEAT</span>
  <button id="bo">blackout</button>
  <span style="flex:1"></span>
  <button id="tab_show" class="on">show</button>
  <button id="tab_pg">playground</button>
  <button id="tab_dev">devices</button>
</header>
<main id="view_show">
<section>
  <h2>show</h2>
  <div class="row"><label>look</label><select id="look"></select><span></span></div>
  <div class="row"><label>duo</label><select id="duo"></select><span></span></div>
  <div class="row"><label>palette</label><select id="palette"></select><span></span></div>
  <div class="row"><label>hit style</label><select id="hit_style"></select><span></span></div>
  <div id="sliders"></div>
</section>
<section>
  <h2>analysis</h2>
  <div class="bands" id="bands"></div>
  <div class="bars"><label>level</label><div class="bar"><i id="b_level"></i></div><span class="val" id="v_level">-</span></div>
  <div class="bars"><label>dynamics</label><div class="bar"><i id="b_dynamics"></i></div><span class="val" id="v_dynamics">-</span></div>
  <div class="bars"><label>swell</label><div class="bar"><i id="b_swell"></i></div><span class="val" id="v_swell">-</span></div>
  <div class="bars"><label>pulse</label><div class="bar"><i id="b_pulse"></i></div><span class="val" id="v_pulse">-</span></div>
  <div class="bars"><label>out</label><div class="bar"><i id="b_out"></i></div><span class="val" id="v_out">-</span></div>
  <h2 style="margin-top:18px">patch</h2>
  <pre id="patch"></pre>
</section>
</main>

<div id="view_dev" style="display:none">
  <section style="border-right:0">
    <div class="tools"><button id="d_save">save config</button>
      <span class="fps">changes apply live; save to keep them</span></div>

    <h2>zones &mdash; length, and how to carve them into fixtures</h2>
    <table id="d_zones"></table>

    <h2 style="margin-top:20px">fixtures &mdash; calibrate each ring</h2>
    <div id="d_fixtures"></div>

    <h2 style="margin-top:20px">log</h2>
    <pre id="d_notes"></pre>
  </section>
</div>

<div id="view_pg" style="display:none">
  <section style="border-right:0">
    <h2>playground &mdash; click or drag to paint</h2>
    <div class="tools">
      <input type="color" id="pick" value="#ff3b30">
      <label class="sw">brightness</label>
      <input type="range" id="pbright" min="0" max="1" step="0.01" value="1" style="width:120px">
      <span class="val" id="pbrightv">1.00</span>
      <button id="t_paint" class="on">paint</button>
      <button id="t_erase">erase</button>
      <button id="fill">fill all</button>
      <button id="clear">clear</button>
      <button id="grab">capture from show</button>
      <span class="fps" id="pgnote"></span>
    </div>
    <div id="rig"></div>
  </section>
</div>
<script>
const SL=__SLIDERS__, OPTS=__OPTS__;
let params=null, busy=false, geo=null, mode='show', painting=false, tool='paint';
let pending={}, flushTimer=null;
const $=id=>document.getElementById(id);

function hex2rgb(h){return [parseInt(h.slice(1,3),16)/255,parseInt(h.slice(3,5),16)/255,parseInt(h.slice(5,7),16)/255];}
function brushRGB(){
  if(tool==='erase') return [0,0,0];
  const b=+$('pbright').value; return hex2rgb($('pick').value).map(c=>c*b);
}
function queue(fixture,idx){
  (pending[fixture]=pending[fixture]||{})[idx]=brushRGB();
  if(!flushTimer) flushTimer=setTimeout(flush,40);
}
async function flush(){
  flushTimer=null; const cells=pending; pending={};
  if(!Object.keys(cells).length) return;
  try{ await fetch('/canvas',{method:'POST',body:JSON.stringify({op:'set',cells})}); }catch(e){}
}
async function canvasOp(o){ try{ await fetch('/canvas',{method:'POST',body:JSON.stringify(o)}); }catch(e){} }

function buildOnce(p){
  for(const [k,list] of Object.entries(OPTS)){
    const el=$(k); el.innerHTML='';
    list.forEach(v=>{const o=document.createElement('option');o.value=v;o.textContent=v;el.appendChild(o)});
    el.onchange=()=>send({[k]:el.value});
  }
  const box=$('sliders');
  for(const [k,[lo,hi,st]] of Object.entries(SL)){
    const row=document.createElement('div'); row.className='row';
    row.innerHTML=`<label>${k.replace(/_/g,' ')}</label>
      <input type="range" id="s_${k}" min="${lo}" max="${hi}" step="${st}">
      <span class="val" id="n_${k}"></span>`;
    box.appendChild(row);
    const s=row.querySelector('input');
    s.oninput=()=>{$('n_'+k).textContent=(+s.value).toFixed(st<1?2:0); send({[k]:+s.value})};
  }
  $('bands').innerHTML='<i></i>'.repeat(8);
  $('bo').onclick=()=>send({blackout:!params.blackout});

  $('tab_show').onclick=()=>setMode('show');
  $('tab_pg').onclick=()=>setMode('pg');
  $('tab_dev').onclick=()=>{setMode('dev');loadDevices();};
  $('d_save').onclick=()=>devCmd({op:'save'});
  $('pbright').oninput=()=>$('pbrightv').textContent=(+$('pbright').value).toFixed(2);
  $('t_paint').onclick=()=>{tool='paint';$('t_paint').className='on';$('t_erase').className='';};
  $('t_erase').onclick=()=>{tool='erase';$('t_erase').className='on';$('t_paint').className='';};
  $('fill').onclick=()=>canvasOp({op:'fill',rgb:brushRGB()});
  $('clear').onclick=()=>canvasOp({op:'clear'});
  $('grab').onclick=async()=>{
    const r=await fetch('/state'); const d=await r.json();
    const fr=d.telemetry.frame||{}, cells={};
    for(const k in fr) cells[k]=fr[k].map(hex2rgb);
    canvasOp({op:'load',cells});
  };
  document.addEventListener('pointerup',()=>painting=false);
  window.addEventListener('hashchange',()=>applyHash());
  applyHash();
}

function applyHash(){
  const m=(location.hash||'').replace('#','');
  if(m==='pg'||m==='playground') setMode('pg',true);
  else if(m==='dev'||m==='devices'){ setMode('dev',true); loadDevices(); }
  else setMode('show',true);
  send({playground: mode==='pg'});
}

function setMode(m, skipHash){
  mode=m;
  if(!skipHash) location.hash = m==='show' ? '' : m;
  $('view_show').style.display = m==='show'?'':'none';
  $('view_pg').style.display   = m==='pg'?'':'none';
  $('view_dev').style.display  = m==='dev'?'':'none';
  $('tab_show').className = m==='show'?'on':'';
  $('tab_pg').className   = m==='pg'?'on':'';
  $('tab_dev').className  = m==='dev'?'on':'';
  send({playground: m==='pg'});
  $('pgnote').textContent = m==='pg'
    ? 'show paused \u00b7 colours are written exactly, no gamma' : '';
}

async function devCmd(o){
  try{
    const r=await fetch('/devices',{method:'POST',body:JSON.stringify(o)});
    renderDevices(await r.json());
    geo=null;                      // patch may have changed; redraw playground
  }catch(e){}
}
async function loadDevices(){
  try{ renderDevices(await (await fetch('/devices')).json()); }catch(e){}
}
function renderDevices(d){
  let h='<tr><th>device</th><th>zone</th><th>leds</th><th></th>'
       +'<th>carve into</th><th>name</th><th></th></tr>';
  d.zones.forEach((z,i)=>{
    const g=z.group||{};
    const splits=(z.splits||[]).map(([k,per])=>
      `<button class="sp" data-k="${z.key}" data-r="${k}" data-p="${per}">${k}x${per}</button>`).join(' ');
    h+=`<tr>
      <td class="dim">${z.device}${z.virtual?' <span class="dim">(virtual, skipped)</span>':''}</td>
      <td>${z.zone}</td>
      <td><input type="number" min="0" max="512" value="${z.leds}" id="z_${i}" ${z.resizable?'':'disabled'}></td>
      <td>${z.resizable?`<button data-z="${i}" data-k="${z.key}">set</button>`:'<span class="dim">fixed</span>'}</td>
      <td>
        <input type="number" min="0" max="64" placeholder="rings" value="${g.rings||''}" id="gr_${i}" style="width:56px">
        <span class="dim">x</span>
        <input type="number" min="0" max="256" placeholder="each" value="${g.leds_per_ring||''}" id="gp_${i}" style="width:56px">
        <button data-g="${i}" data-k="${z.key}">apply</button>
        <div class="dim" style="margin-top:4px">${splits||'&nbsp;'}</div>
      </td>
      <td><input type="text" value="${g.name||''}" placeholder="auto" id="gn_${i}" style="width:88px"></td>
      <td class="dim">${z.patched?'in patch':''}</td></tr>`;
  });
  $('d_zones').innerHTML=h;
  $('d_zones').querySelectorAll('[data-z]').forEach(b=>b.onclick=()=>
    devCmd({op:'resize',key:b.dataset.k,leds:+$('z_'+b.dataset.z).value}));
  $('d_zones').querySelectorAll('[data-g]').forEach(b=>b.onclick=()=>{
    const i=b.dataset.g;
    devCmd({op:'group',key:b.dataset.k,rings:+$('gr_'+i).value||0,
            leds_per_ring:+$('gp_'+i).value||0,name:$('gn_'+i).value});
  });
  $('d_zones').querySelectorAll('.sp').forEach(b=>b.onclick=()=>
    devCmd({op:'group',key:b.dataset.k,rings:+b.dataset.r,leds_per_ring:+b.dataset.p}));

  let f='';
  for(const [name,x] of Object.entries(d.fixtures)){
    f+=`<div class="frow">
      <span>${name}</span>
      <span class="dim">${x.n}${x.mirror>1?'x'+x.mirror:''}</span>
      <button data-sp="${name}" data-v="${x.spin>=0?-1:1}">${x.spin>=0?'cw':'ccw'}</button>
      <input type="range" min="0" max="0.99" step="0.01" value="${x.rotate}" data-rot="${name}">
      <span class="val">${(+x.rotate).toFixed(2)}</span>
      <button data-id="${name}">identify</button></div>`;
  }
  $('d_fixtures').innerHTML=f;
  $('d_fixtures').querySelectorAll('[data-sp]').forEach(b=>b.onclick=()=>
    devCmd({op:'calibrate',fixture:b.dataset.sp,spin:+b.dataset.v}));
  $('d_fixtures').querySelectorAll('[data-id]').forEach(b=>b.onclick=()=>
    devCmd({op:'identify',fixture:b.dataset.id,seconds:3}));
  $('d_fixtures').querySelectorAll('[data-rot]').forEach(r=>r.onchange=()=>
    devCmd({op:'calibrate',fixture:r.dataset.rot,rotate:+r.value}));

  $('d_notes').textContent=(d.notes||[]).join('\n')+'\n\n'+(d.patch||'');
}

function buildRig(){
  const box=$('rig'); box.innerHTML='';
  for(const [name,g] of Object.entries(geo)){
    const wrap=document.createElement('div'); wrap.className='fx';
    wrap.innerHTML=`<div class="fxname">${name} &middot; ${g.n} ${g.kind}${g.slow?' &middot; i2c':''}
      <button data-fill="${name}">fill</button></div>`;
    wrap.querySelector('button').onclick=()=>canvasOp({op:'fill',rgb:brushRGB(),fixtures:[name]});

    if(g.kind==='ring'){
      const R=g.n>10?62:40, pad=16, S=(R+pad)*2, r=g.n>10?7:9;
      const ns='http://www.w3.org/2000/svg';
      const svg=document.createElementNS(ns,'svg');
      svg.setAttribute('width',S); svg.setAttribute('height',S);
      g.angle.forEach((a,i)=>{
        const th=a*2*Math.PI-Math.PI/2;
        const c=document.createElementNS(ns,'circle');
        c.setAttribute('cx',S/2+R*Math.cos(th)); c.setAttribute('cy',S/2+R*Math.sin(th));
        c.setAttribute('r',r); c.setAttribute('class','led');
        c.dataset.fx=name; c.dataset.i=i; c.setAttribute('fill','#000');
        svg.appendChild(c);
      });
      wrap.appendChild(svg);
    } else if(g.matrix){
      const grid=document.createElement('div'); grid.className='grid';
      grid.style.gridTemplateColumns=`repeat(${g.matrix[0].length},19px)`;
      g.matrix.forEach(row=>row.forEach(v=>{
        const d=document.createElement('div');
        d.className='cell'+(v===null?' gap':'');
        if(v!==null){ d.dataset.fx=name; d.dataset.i=v; }
        grid.appendChild(d);
      }));
      wrap.appendChild(grid);
    } else {
      const strip=document.createElement('div'); strip.className='strip';
      for(let i=0;i<g.n;i++){
        const d=document.createElement('div'); d.className='cell';
        d.dataset.fx=name; d.dataset.i=i; strip.appendChild(d);
      }
      wrap.appendChild(strip);
    }
    box.appendChild(wrap);
  }
  box.onpointerdown=e=>{const t=e.target; if(!t.dataset||!t.dataset.fx)return;
    painting=true; e.preventDefault(); queue(t.dataset.fx,t.dataset.i);};
  box.onpointerover=e=>{const t=e.target; if(!painting||!t.dataset||!t.dataset.fx)return;
    queue(t.dataset.fx,t.dataset.i);};
}

function paintRig(frame){
  if(!geo) return;
  for(const [name,cols] of Object.entries(frame||{})){
    for(let i=0;i<cols.length;i++){
      const el=$('rig').querySelector(`[data-fx="${name}"][data-i="${i}"]`);
      if(!el) continue;
      if(el.tagName==='circle') el.setAttribute('fill',cols[i]);
      else el.style.background=cols[i];
    }
  }
}
async function send(o){ if(busy)return; busy=true;
  try{ const r=await fetch('/set',{method:'POST',body:JSON.stringify(o)}); params=await r.json(); paint(); }
  catch(e){} busy=false; }
function paint(){
  for(const k of Object.keys(OPTS)) $(k).value=params[k];
  for(const [k,[lo,hi,st]] of Object.entries(SL)){
    const s=$('s_'+k); if(document.activeElement!==s) s.value=params[k];
    $('n_'+k).textContent=(+params[k]).toFixed(st<1?2:0);
  }
  $('bo').className=params.blackout?'on':'';
}
function bar(id,v){ $('b_'+id).style.width=Math.max(0,Math.min(1,v))*100+'%'; $('v_'+id).textContent=v.toFixed(2); }
function fail(e){
  const b=$('err');
  b.textContent='ui error: '+(e && e.message ? e.message : e);
  b.style.display='';
  console.error(e);
}
async function tick(){
  try{
    const r=await fetch('/state'); const d=await r.json();
    if(!params){ params=d.params; buildOnce(params); }
    else if(!busy && document.activeElement.tagName!=='INPUT'){ params=d.params; paint(); }
    const t=d.telemetry||{};
    $('fps').textContent=(t.fps||0).toFixed(1)+' fps';
    $('inp').textContent=(t.dbfs==null?'--':t.dbfs.toFixed(1)+' dBFS');
    $('patch').textContent=d.patch||'';
    ['level','dynamics','swell','pulse','out'].forEach(k=>bar(k,t[k]||0));
    const bs=$('bands').children, b=t.bands||[];
    for(let i=0;i<bs.length;i++) bs[i].style.height=(2+(b[i]||0)*50)+'px';
    if(t.onset) { $('hit').style.opacity=1; setTimeout(()=>$('hit').style.opacity=0,110); }
    if(!geo){ const g=await (await fetch('/patch')).json(); geo=g; buildRig(); }
    paintRig(t.frame);
    $('err').style.display='none';
  }catch(e){ fail(e); }
}
setInterval(tick,100); tick();
</script>
"""


def geometry_of(fixtures) -> dict:
    """Shape of each fixture, so the UI can draw the rig as it physically is."""
    out = {}
    for name, f in fixtures.items():
        out[name] = {
            "n": f.n,
            "kind": f.kind,
            "origin": list(f.origin),
            "slow": bool(f.slow),
            "angle": ([float(a) for a in f.angle] if f.angle is not None
                      else None),
            "matrix": getattr(f, "matrix", None),
        }
    return out


def page() -> str:
    return (PAGE.replace("__SLIDERS__", json.dumps(SLIDERS))
                .replace("__OPTS__", json.dumps({
                    "look": LOOKS_LIST, "duo": DUOS_LIST,
                    "palette": PALETTES_LIST, "hit_style": HIT_STYLES})))
