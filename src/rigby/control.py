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


def _handler(params: Params, telem: Telemetry, patch_text: str):
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
            if self.path in ("/", "/index.html"):
                return self._send(200, page().encode(), "text/html; charset=utf-8")
            self._send(404, b"not found", "text/plain")

        def do_POST(self):
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
                 host: str = "127.0.0.1", port: int = 8721):
        self.params, self.telem = params, telem
        self.httpd = ThreadingHTTPServer((host, port),
                                         _handler(params, telem, patch_text))
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self._t = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self) -> None:
        self._t.start()

    def stop(self) -> None:
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except Exception:
            pass


PAGE = """<!doctype html>
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
</style>
<header>
  <h1>rigby</h1>
  <span class="fps" id="fps">--</span>
  <span class="fps" id="inp">--</span>
  <span class="hit" id="hit">BEAT</span>
  <button id="bo">blackout</button>
</header>
<main>
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
<script>
const SL=__SLIDERS__, OPTS=__OPTS__;
let params=null, busy=false;
const $=id=>document.getElementById(id);

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
  }catch(e){}
}
setInterval(tick,100); tick();
</script>
"""


def page() -> str:
    return (PAGE.replace("__SLIDERS__", json.dumps(SLIDERS))
                .replace("__OPTS__", json.dumps({
                    "look": LOOKS_LIST, "duo": DUOS_LIST,
                    "palette": PALETTES_LIST, "hit_style": HIT_STYLES})))
