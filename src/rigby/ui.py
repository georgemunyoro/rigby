"""The control surface: markup, styling and behaviour for the web UI.

Kept apart from the server because a page is a design artefact, not
request-handling code, and because the two change for different reasons.

The page is a raw string. Every escape in it belongs to JavaScript -- a plain
string turns `\n` into a real newline at import, which leaves an unterminated
JS literal and kills the entire script while the page still renders. See
check_ui.py, which exists because that failure is invisible.
"""

from __future__ import annotations

import json

LOOKS_LIST = ["auto", "duotone", "rain", "spectrum", "chase"]
DUOS_LIST = ["ember", "toxic", "vapor", "cobalt", "mono"]
PALETTES_LIST = ["sunset", "cyanmag", "acid", "ice"]
HIT_STYLES = ["swing", "accent", "white"]

# Grouped and annotated, because eleven undifferentiated sliders is a control
# panel you have to remember rather than read.
SLIDERS: dict[str, dict] = {
    "master":     dict(min=0.0, max=1.0, step=0.01, group="Output",
                       label="Master", unit="",
                       help="Grand master. Scales everything at the output."),
    "gamma":      dict(min=1.0, max=3.0, step=0.05, group="Output",
                       label="Gamma", unit="",
                       help="Display curve. 2.2 is perceptually linear."),
    "gain":       dict(min=0.2, max=4.0, step=0.05, group="Brightness",
                       label="Drive", unit="x",
                       help="Pre-curve multiplier on control signals."),
    "curve":      dict(min=0.2, max=1.0, step=0.01, group="Brightness",
                       label="Curve", unit="",
                       help="Below 1 lifts the low end. 1.0 is linear and "
                            "reads flat."),
    "saturation": dict(min=0.0, max=1.0, step=0.01, group="Colour",
                       label="Saturation", unit="",
                       help="Overall colour intensity."),
    "hot":        dict(min=0.0, max=1.0, step=0.01, group="Colour",
                       label="Hot core", unit="",
                       help="How far bright cores run toward white. Flat "
                            "saturation hides movement on a small ring."),
    "hue_drift":  dict(min=0.0, max=6.0, step=0.1, group="Colour",
                       label="Hue drift", unit="x",
                       help="Speed the tone pair walks the wheel. 0 pins it."),
    "dynamics_db": dict(min=6.0, max=40.0, step=0.5, group="Response",
                        label="Dynamic range", unit=" dB",
                        help="dB below the running average that reads as dark. "
                             "Lower is more dramatic."),
    "onset_k":    dict(min=0.5, max=4.0, step=0.05, group="Response",
                       label="Beat threshold", unit="σ",
                       help="Standard deviations above recent flux. Raise if "
                            "beats trigger too eagerly."),
    "swing_min_beats": dict(min=1, max=24, step=1, group="Response",
                            label="Swing every", unit=" beats",
                            help="Minimum gap between colour swings. Ordinary "
                                 "beats get the plain flash."),
    "offset_ms":  dict(min=0, max=400, step=5, group="Response",
                       label="Latency offset", unit=" ms",
                       help="Delays the lights to match output latency. "
                           "Bluetooth sinks need ~200ms."),
}

GROUPS = ["Output", "Brightness", "Colour", "Response"]

OPTS = {"look": LOOKS_LIST, "duo": DUOS_LIST,
        "palette": PALETTES_LIST, "hit_style": HIT_STYLES}

OPT_HELP = {
    "look": "auto follows the music's character; duotone is beat-driven, "
            "rain is swell-driven.",
    "duo": "Two-tone relationship: base hue, separation, accent.",
    "palette": "Hue walk used by the spectrum and chase looks.",
    "hit_style": "What a beat does to colour.",
}

PAGE = r"""<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="dark">
<title>rigby</title>
<style>
:root{
  --bg:#0a0c10; --surface:#111419; --surface-2:#161a22; --raised:#1c212b;
  --line:#232936; --line-2:#2e3646;
  --fg:#e9ecf3; --dim:#8d95a8; --faint:#5d6577;
  --accent:#5ee7f5; --accent-ink:#04222a; --accent-2:#a78bfa;
  --ok:#4ade80; --warn:#fbbf24; --bad:#f87171;
  --r:10px; --r-sm:7px;
  --sp:8px;
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,monospace;
  --sans:system-ui,-apple-system,"Segoe UI",Inter,Roboto,sans-serif;
}
*{box-sizing:border-box;min-width:0}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 var(--sans);
  -webkit-font-smoothing:antialiased;overflow-x:hidden}
h1,h2,h3{margin:0;font-weight:600;letter-spacing:-.01em}
button,input,select{font:inherit;color:inherit}
button{cursor:pointer}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:4px}
@media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}

/* ---------- header ---------- */
header{position:sticky;top:0;z-index:30;display:flex;align-items:center;gap:18px;
  padding:10px 20px;background:rgba(10,12,16,.86);backdrop-filter:blur(12px);
  border-bottom:1px solid var(--line);flex-wrap:wrap}
.brand{display:flex;align-items:center;gap:9px;font-weight:650;letter-spacing:.16em;
  font-size:12px;text-transform:uppercase}
.brand .dot{width:9px;height:9px;border-radius:50%;background:var(--accent);
  box-shadow:0 0 10px var(--accent);flex:none}
.pills{display:flex;gap:6px;align-items:center}
.pill{font:11px/1 var(--mono);color:var(--dim);background:var(--surface-2);
  border:1px solid var(--line);padding:5px 8px;border-radius:20px;white-space:nowrap}
.pill.beat{color:var(--accent-ink);background:var(--accent);border-color:var(--accent);
  opacity:0;transition:opacity .12s}
.pill.beat.on{opacity:1}
.spacer{flex:1}
.seg{display:flex;background:var(--surface-2);border:1px solid var(--line);
  border-radius:var(--r);padding:3px;gap:2px}
.seg button{background:none;border:0;color:var(--dim);padding:6px 14px;
  border-radius:var(--r-sm);font-size:13px;transition:color .12s,background .12s}
.seg button:hover{color:var(--fg)}
.seg button[aria-selected=true]{background:var(--raised);color:var(--fg);
  box-shadow:0 1px 2px rgba(0,0,0,.4)}
.quick{display:flex;align-items:center;gap:10px}
.quick label{display:flex;align-items:center;gap:7px;font-size:12px;color:var(--dim)}
.quick input[type=range]{width:96px}
.btn{background:var(--surface-2);border:1px solid var(--line);color:var(--fg);
  padding:7px 12px;border-radius:var(--r-sm);font-size:13px;
  transition:background .12s,border-color .12s}
.btn:hover{background:var(--raised);border-color:var(--line-2)}
.btn.on{background:var(--accent);border-color:var(--accent);color:var(--accent-ink);
  font-weight:600}
.btn.sm{padding:4px 9px;font-size:12px}
.btn.tiny{padding:2px 7px;font-size:11px;font-family:var(--mono)}
.btn.ghost{background:none;border-color:transparent;color:var(--dim)}
.btn.ghost:hover{color:var(--fg);background:var(--surface-2)}

/* ---------- layout ---------- */
main{padding:18px 20px 56px;max-width:1400px;margin:0 auto}
.cols{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(310px,1fr));
  align-items:start}
.row2{display:grid;gap:14px;grid-template-columns:minmax(0,1fr) minmax(0,1fr);
  align-items:start;margin-bottom:14px}
.row4{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(268px,1fr));
  align-items:start}
@media(max-width:860px){.row2{grid-template-columns:1fr}}
.card{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);
  overflow:hidden}
.card>header{position:static;background:none;backdrop-filter:none;padding:12px 15px;
  border-bottom:1px solid var(--line);gap:10px}
.card h2{font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--dim)}
.card .body{padding:14px 15px}
.hint{font-size:12px;color:var(--faint)}
.tab[hidden]{display:none}

/* ---------- rig monitor ---------- */
#rigcard{margin-bottom:14px}
#rig{display:flex;flex-wrap:wrap;gap:14px;padding:14px 15px;align-items:flex-start}
.fx{background:var(--surface-2);border:1px solid var(--line);border-radius:var(--r-sm);
  padding:10px 12px}
.fx-head{display:flex;align-items:center;gap:8px;margin-bottom:8px}
.fx-name{font:11px/1 var(--mono);color:var(--dim);letter-spacing:.08em}
.fx-meta{font:10px/1 var(--mono);color:var(--faint)}
kbd{font:10px/1 var(--mono);border:1px solid var(--line-2);border-bottom-width:2px;
  border-radius:4px;padding:2px 4px;margin-left:5px;color:var(--dim);
  background:var(--bg);vertical-align:middle}
.btn.on kbd{color:var(--accent-ink);border-color:rgba(4,34,42,.35);background:rgba(255,255,255,.25)}
.led{stroke:#2b3140;stroke-width:1;transition:none}
.paintable .led{cursor:crosshair}
.paintable .led:hover{stroke:var(--accent);stroke-width:2}
.strip{display:flex;flex-wrap:wrap;gap:3px;max-width:340px}
.cell{width:16px;height:16px;border-radius:4px;border:1px solid #2b3140;background:#000}
.paintable .cell{cursor:crosshair}
.paintable .cell:hover{border-color:var(--accent)}
.cell.gap{border-color:transparent;background:none;cursor:default}
.grid{display:grid;gap:2px}

/* ---------- controls ---------- */
.field{margin-bottom:14px}
.field:last-child{margin-bottom:0}
.field>label{display:block;font-size:12px;color:var(--dim);margin-bottom:5px}
select{width:100%;background:var(--surface-2);border:1px solid var(--line);
  border-radius:var(--r-sm);padding:8px 10px;font-size:13px;appearance:none;
  background-image:linear-gradient(45deg,transparent 50%,var(--dim) 50%),
    linear-gradient(135deg,var(--dim) 50%,transparent 50%);
  background-position:calc(100% - 16px) 50%,calc(100% - 11px) 50%;
  background-size:5px 5px,5px 5px;background-repeat:no-repeat}
select:hover{border-color:var(--line-2)}
.help{font-size:11px;color:var(--faint);margin-top:5px;line-height:1.45}
.slider{margin-bottom:15px}
.slider:last-child{margin-bottom:0}
.slider .row{display:flex;justify-content:space-between;align-items:baseline;gap:10px}
.slider .lab{font-size:12px;color:var(--dim)}
.slider .val{font:12px/1 var(--mono);color:var(--fg)}
input[type=range]{-webkit-appearance:none;appearance:none;width:100%;height:22px;
  background:none;margin:2px 0}
input[type=range]::-webkit-slider-runnable-track{height:4px;border-radius:3px;
  background:linear-gradient(var(--accent),var(--accent)) 0/var(--fill,0%) 100% no-repeat,
    var(--line-2)}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:14px;height:14px;
  border-radius:50%;background:var(--fg);margin-top:-5px;border:0;
  box-shadow:0 1px 3px rgba(0,0,0,.5)}
input[type=range]:hover::-webkit-slider-thumb{background:var(--accent)}
input[type=range]::-moz-range-track{height:4px;border-radius:3px;background:var(--line-2)}
input[type=range]::-moz-range-progress{height:4px;border-radius:3px;background:var(--accent)}
input[type=range]::-moz-range-thumb{width:14px;height:14px;border:0;border-radius:50%;
  background:var(--fg)}
input[type=number],input[type=text]{background:var(--surface-2);border:1px solid var(--line);
  border-radius:var(--r-sm);padding:6px 8px;font:13px/1 var(--mono);width:100%}
input[type=number]:hover,input[type=text]:hover{border-color:var(--line-2)}
input[type=color]{width:44px;height:32px;padding:2px;background:var(--surface-2);
  border:1px solid var(--line);border-radius:var(--r-sm)}

/* ---------- analysis ---------- */
.bands{display:flex;gap:3px;height:64px;align-items:flex-end;margin-bottom:14px}
.bands i{flex:1;background:linear-gradient(var(--accent),var(--accent-2));
  border-radius:3px 3px 0 0;height:2px;min-height:2px}
.meter{display:grid;grid-template-columns:82px 1fr 46px;gap:9px;align-items:center;
  margin-bottom:7px}
.meter:last-child{margin-bottom:0}
.meter .lab{font-size:12px;color:var(--dim)}
.meter .track{height:7px;background:var(--surface-2);border-radius:4px;overflow:hidden}
.meter .track i{display:block;height:100%;width:0;background:var(--accent);border-radius:4px}
.meter .num{font:12px/1 var(--mono);color:var(--fg);text-align:right}

/* ---------- devices ---------- */
#d_zones{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(430px,1fr));
  align-items:start}
@media(max-width:900px){#d_zones{grid-template-columns:1fr}}
.dev{margin-bottom:0}
.dev:last-child{margin-bottom:0}
.zone{border:1px solid var(--line);border-radius:var(--r-sm);background:var(--surface-2);
  padding:12px;margin-bottom:9px}
.zone-head{display:flex;align-items:baseline;gap:9px;flex-wrap:wrap;margin-bottom:10px}
.zone-name{font-size:13px;font-weight:550}
.zone-sub{font:11px/1 var(--mono);color:var(--faint)}
.tag{font:10px/1 var(--mono);padding:3px 6px;border-radius:4px;background:var(--raised);
  color:var(--dim);border:1px solid var(--line)}
.tag.ok{color:var(--ok);border-color:rgba(74,222,128,.3)}
.ctrl-row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px}
.ctrl-row:last-child{margin-bottom:0}
.ctrl-row label{font-size:11px;color:var(--dim);display:flex;flex-direction:column;gap:3px}
.ctrl-row input{width:78px}
.splits{display:flex;gap:4px;flex-wrap:wrap}
.slots{display:flex;gap:6px;flex-wrap:wrap;margin:8px 0}
.slot{display:flex;flex-direction:column;gap:3px}
.slot span{font:10px/1 var(--mono);color:var(--faint)}
.slot input{width:56px;text-align:center}
.fxrow{display:grid;grid-template-columns:120px 60px 66px 1fr 44px auto;gap:10px;
  align-items:center;padding:7px 0;border-bottom:1px solid var(--line)}
.fxrow:last-child{border-bottom:0}
.fxrow .nm{font:12px/1 var(--mono)}

/* ---------- playground ---------- */
.tools{display:flex;gap:9px;align-items:center;flex-wrap:wrap}
.swatches{display:flex;gap:4px}
.sw{width:22px;height:22px;border-radius:5px;border:1px solid var(--line);cursor:pointer}
.sw:hover{border-color:var(--fg)}

/* ---------- toast + banner ---------- */
#toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%) translateY(14px);
  background:var(--raised);border:1px solid var(--line-2);border-radius:var(--r);
  padding:10px 16px;font-size:13px;opacity:0;pointer-events:none;z-index:60;
  transition:opacity .18s,transform .18s;box-shadow:0 8px 28px rgba(0,0,0,.5)}
#toast.on{opacity:1;transform:translateX(-50%) translateY(0)}
#err{display:none;background:#3b1113;border-bottom:1px solid #7f1d1d;color:#fecaca;
  padding:9px 20px;font:12px/1.5 var(--mono)}
pre{font:11px/1.6 var(--mono);color:var(--dim);white-space:pre-wrap;margin:0}
@media(max-width:640px){
  header{gap:10px;padding:9px 13px}
  main{padding:13px 13px 48px}
  .quick label{display:none}
  .fxrow{grid-template-columns:100px 1fr auto;gap:7px}
}
</style>

<div id="err" role="alert"></div>

<header>
  <div class="brand"><span class="dot" id="live"></span> rigby</div>
  <div class="pills">
    <span class="pill" id="p_fps">--</span>
    <span class="pill" id="p_in">--</span>
    <span class="pill beat" id="p_beat">beat</span>
  </div>
  <div class="spacer"></div>
  <nav class="seg" role="tablist" aria-label="Section">
    <button role="tab" data-tab="show" aria-selected="true">Show</button>
    <button role="tab" data-tab="devices" aria-selected="false">Devices</button>
    <button role="tab" data-tab="playground" aria-selected="false">Playground</button>
  </nav>
  <div class="quick">
    <label for="q_master">Master <input type="range" id="q_master" min="0" max="1" step="0.01"></label>
    <button class="btn" id="q_black">Blackout</button>
  </div>
</header>

<main>
  <section class="card" id="rigcard">
    <header><h2>Rig</h2><span class="hint" id="righint"></span><div class="spacer"></div>
      <span class="hint" id="rigcount"></span></header>
    <div id="rig"></div>
  </section>

  <div class="tab" id="tab-show">
    <div class="row2">
      <section class="card">
        <header><h2>Look</h2></header>
        <div class="body" id="lookfields"></div>
      </section>
      <section class="card">
        <header><h2>Analysis</h2><div class="spacer"></div>
          <span class="hint" id="an_note"></span></header>
        <div class="body">
          <div class="bands" id="bands" aria-hidden="true"></div>
          <div class="meter"><span class="lab">Level</span><div class="track"><i id="b_level"></i></div><span class="num" id="v_level">-</span></div>
          <div class="meter"><span class="lab">Dynamics</span><div class="track"><i id="b_dynamics"></i></div><span class="num" id="v_dynamics">-</span></div>
          <div class="meter"><span class="lab">Swell</span><div class="track"><i id="b_swell"></i></div><span class="num" id="v_swell">-</span></div>
          <div class="meter"><span class="lab">Pulse</span><div class="track"><i id="b_pulse"></i></div><span class="num" id="v_pulse">-</span></div>
          <div class="meter"><span class="lab">Output</span><div class="track"><i id="b_out"></i></div><span class="num" id="v_out">-</span></div>
        </div>
      </section>
    </div>
    <div class="row4" id="slidercards"></div>
  </div>

  <div class="tab" id="tab-devices" hidden>
    <section class="card" style="margin-bottom:14px">
      <header><h2>Calibration</h2><span class="hint">the show is paused while you're here</span>
        <div class="spacer"></div>
        <button class="btn sm" id="d_save">Save config</button></header>
      <div class="body"><div id="d_zones"></div></div>
    </section>
    <div class="cols">
      <section class="card">
        <header><h2>Fixtures</h2><span class="hint">spin, rotation, identify</span></header>
        <div class="body"><div id="d_fixtures"></div></div>
      </section>
      <section class="card">
        <header><h2>Log</h2></header>
        <div class="body"><pre id="d_notes"></pre></div>
      </section>
    </div>
  </div>

  <div class="tab" id="tab-playground" hidden>
    <section class="card">
      <header><h2>Paint</h2><span class="hint">click or drag on the rig above &middot; colours are exact</span></header>
      <div class="body">
        <div class="tools">
          <input type="color" id="pick" value="#ff3b30" aria-label="Colour">
          <div class="swatches" id="swatches"></div>
          <label class="hint" for="pbright">Brightness</label>
          <input type="range" id="pbright" min="0" max="1" step="0.01" value="1" style="width:110px">
          <span class="num" id="pbrightv" style="font:12px/1 var(--mono)">1.00</span>
          <span style="width:1px;height:22px;background:var(--line)"></span>
          <button class="btn sm on" id="t_paint">Paint <kbd>P</kbd></button>
          <button class="btn sm" id="t_erase">Erase <kbd>E</kbd></button>
          <button class="btn sm" id="fill">Fill all</button>
          <button class="btn sm" id="clear">Clear</button>
          <button class="btn sm" id="grab">Capture show</button>
        </div>
      </div>
    </section>
  </div>
</main>

<div id="toast" role="status"></div>

<script>
const SL=__SLIDERS__, OPTS=__OPTS__, OPT_HELP=__OPT_HELP__, GROUPS=__GROUPS__;
const $=id=>document.getElementById(id);
const SWATCHES=['#ff3b30','#ff9f0a','#ffd60a','#30d158','#5ee7f5','#0a84ff','#bf5af2','#ffffff'];
let params=null, geo=null, devState=null, mode='show', tool='paint';
let painting=false, pending={}, flushT=null, built=false, toastT=null;

function fail(e){ const b=$('err'); b.textContent='ui error: '+(e&&e.message?e.message:e);
  b.style.display='block'; console.error(e); }
function toast(msg){ const t=$('toast'); t.textContent=msg; t.classList.add('on');
  clearTimeout(toastT); toastT=setTimeout(()=>t.classList.remove('on'),1900); }
function fmt(v,st){ const d=st>=1?0:(st>=0.1?1:2); return (+v).toFixed(d); }
function hex2rgb(h){return [1,3,5].map(i=>parseInt(h.slice(i,i+2),16)/255);}
function fillTrack(el){ const p=(el.value-el.min)/(el.max-el.min)*100;
  el.style.setProperty('--fill',p+'%'); }

async function send(o){
  try{ const r=await fetch('/set',{method:'POST',body:JSON.stringify(o)});
       params=await r.json(); paintControls(); }catch(e){ fail(e); }
}
async function devCmd(o){
  try{ const r=await fetch('/devices',{method:'POST',body:JSON.stringify(o)});
       devState=await r.json(); renderDevices(); geo=null; }catch(e){ fail(e); }
}
async function canvasOp(o){
  try{ await fetch('/canvas',{method:'POST',body:JSON.stringify(o)}); }catch(e){ fail(e); }
}
function queue(fx,i){ (pending[fx]=pending[fx]||{})[i]=brush();
  if(!flushT) flushT=setTimeout(flushPaint,40); }
async function flushPaint(){ flushT=null; const cells=pending; pending={};
  if(Object.keys(cells).length) canvasOp({op:'set',cells}); }
function brush(){ if(tool==='erase') return [0,0,0];
  const b=+$('pbright').value; return hex2rgb($('pick').value).map(c=>c*b); }

/* ---------------------------------------------------------------- build */
function build(){
  // look selects
  const lf=$('lookfields');
  lf.innerHTML=Object.keys(OPTS).map(k=>`<div class="field">
      <label for="sel_${k}">${k.replace('_',' ')}</label>
      <select id="sel_${k}">${OPTS[k].map(v=>`<option>${v}</option>`).join('')}</select>
      <div class="help">${OPT_HELP[k]||''}</div></div>`).join('');
  Object.keys(OPTS).forEach(k=>$('sel_'+k).onchange=()=>send({[k]:$('sel_'+k).value}));

  // slider cards, grouped
  $('slidercards').innerHTML=GROUPS.map(g=>{
    const keys=Object.keys(SL).filter(k=>SL[k].group===g);
    if(!keys.length) return '';
    return `<section class="card"><header><h2>${g}</h2></header><div class="body">`+
      keys.map(k=>{const s=SL[k];return `<div class="slider">
        <div class="row"><span class="lab">${s.label}</span>
          <span class="val" id="n_${k}">--</span></div>
        <input type="range" id="s_${k}" min="${s.min}" max="${s.max}" step="${s.step}"
          aria-label="${s.label}">
        <div class="help">${s.help}</div></div>`}).join('')+`</div></section>`;
  }).join('');
  Object.keys(SL).forEach(k=>{ const el=$('s_'+k); if(!el) return;
    el.oninput=()=>{ fillTrack(el); $('n_'+k).textContent=fmt(el.value,SL[k].step)+SL[k].unit;
                     send({[k]:+el.value}); }; });

  $('bands').innerHTML='<i></i>'.repeat(8);
  $('swatches').innerHTML=SWATCHES.map(c=>`<button class="sw" style="background:${c}"
      data-c="${c}" aria-label="${c}"></button>`).join('');
  $('swatches').querySelectorAll('[data-c]').forEach(b=>b.onclick=()=>{
    $('pick').value=b.dataset.c; setTool('paint'); });

  document.querySelectorAll('[data-tab]').forEach(b=>b.onclick=()=>setMode(b.dataset.tab));
  $('q_master').oninput=()=>{ fillTrack($('q_master')); send({master:+$('q_master').value}); };
  $('q_black').onclick=()=>send({blackout:!params.blackout});
  $('pbright').oninput=()=>{ fillTrack($('pbright'));
    $('pbrightv').textContent=(+$('pbright').value).toFixed(2); };
  $('t_paint').onclick=()=>setTool('paint');
  $('t_erase').onclick=()=>setTool('erase');
  $('fill').onclick=()=>{canvasOp({op:'fill',rgb:brush()});toast('filled');};
  $('clear').onclick=()=>{canvasOp({op:'clear'});toast('cleared');};
  $('grab').onclick=async()=>{ const d=await (await fetch('/state')).json();
    const fr=d.telemetry.frame||{}, cells={};
    for(const k in fr) cells[k]=fr[k].map(hex2rgb);
    canvasOp({op:'load',cells}); toast('captured the current show'); };
  $('d_save').onclick=()=>{devCmd({op:'save'});toast('config saved');};

  document.addEventListener('pointerup',()=>painting=false);
  window.addEventListener('hashchange',applyHash);
  document.addEventListener('keydown',e=>{
    if(/^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement.tagName)) return;
    const k=e.key.toLowerCase();
    if(k==='1')setMode('show'); else if(k==='2')setMode('devices');
    else if(k==='3')setMode('playground');
    else if(k==='p')setTool('paint'); else if(k==='e')setTool('erase');
    else if(k==='b')send({blackout:!params.blackout});
  });
  built=true; applyHash();
}
function setTool(t){ tool=t;
  $('t_paint').classList.toggle('on',t==='paint');
  $('t_erase').classList.toggle('on',t==='erase'); }

/* ----------------------------------------------------------------- mode */
function setMode(m,skipHash){
  mode=m;
  if(!skipHash) location.hash = m==='show'?'':m;
  ['show','devices','playground'].forEach(t=>{
    $('tab-'+t).hidden = t!==m;
    const b=document.querySelector(`[data-tab="${t}"]`);
    if(b) b.setAttribute('aria-selected', String(t===m));
  });
  $('rig').classList.toggle('paintable', m==='playground');
  $('righint').textContent = m==='playground' ? 'click or drag to paint'
    : (m==='devices' ? 'paused for calibration' : 'live');
  send({playground:m==='playground', pause:m==='devices'});
  if(m==='devices') loadDevices();
}
function applyHash(){
  const h=(location.hash||'').replace('#','');
  setMode(['devices','playground'].includes(h)?h:'show', true);
}

/* ------------------------------------------------------------- controls */
function paintControls(){
  if(!params||!built) return;
  for(const k of Object.keys(OPTS)){ const el=$('sel_'+k);
    if(el && document.activeElement!==el) el.value=params[k]; }
  for(const k of Object.keys(SL)){ const el=$('s_'+k); if(!el) continue;
    if(document.activeElement!==el){ el.value=params[k]; fillTrack(el); }
    $('n_'+k).textContent=fmt(params[k],SL[k].step)+SL[k].unit; }
  const qm=$('q_master');
  if(document.activeElement!==qm){ qm.value=params.master; fillTrack(qm); }
  $('q_black').classList.toggle('on',!!params.blackout);
}

/* ------------------------------------------------------------------ rig */
function buildRig(){
  const box=$('rig'); box.innerHTML='';
  let total=0;
  for(const [name,g] of Object.entries(geo)){
    total+=g.n;
    const d=document.createElement('div'); d.className='fx';
    d.innerHTML=`<div class="fx-head"><span class="fx-name">${name}</span>
      <span class="fx-meta">${g.n}${g.mirror>1?'x'+g.mirror:''} ${g.kind}${g.slow?' · i2c':''}</span></div>`;
    if(g.kind==='ring'){
      const big=g.n>12, R=big?54:34, pad=13, S=(R+pad)*2, r=big?6:8;
      const ns='http://www.w3.org/2000/svg';
      const svg=document.createElementNS(ns,'svg');
      svg.setAttribute('width',S); svg.setAttribute('height',S);
      svg.setAttribute('role','img'); svg.setAttribute('aria-label',name);
      g.angle.forEach((a,i)=>{
        const th=a*2*Math.PI-Math.PI/2, c=document.createElementNS(ns,'circle');
        c.setAttribute('cx',S/2+R*Math.cos(th)); c.setAttribute('cy',S/2+R*Math.sin(th));
        c.setAttribute('r',r); c.setAttribute('class','led'); c.setAttribute('fill','#000');
        c.dataset.fx=name; c.dataset.i=i; svg.appendChild(c);
      });
      d.appendChild(svg);
    } else if(g.matrix){
      const gr=document.createElement('div'); gr.className='grid';
      gr.style.gridTemplateColumns=`repeat(${g.matrix[0].length},14px)`;
      g.matrix.forEach(row=>row.forEach(v=>{
        const c=document.createElement('div');
        c.className='cell'+(v===null?' gap':'');
        c.style.width='14px'; c.style.height='14px';
        if(v!==null){ c.dataset.fx=name; c.dataset.i=v; }
        gr.appendChild(c);
      }));
      d.appendChild(gr);
    } else {
      const st=document.createElement('div'); st.className='strip';
      for(let i=0;i<g.n;i++){ const c=document.createElement('div');
        c.className='cell'; c.dataset.fx=name; c.dataset.i=i; st.appendChild(c); }
      d.appendChild(st);
    }
    box.appendChild(d);
  }
  $('rigcount').textContent=`${Object.keys(geo).length} fixtures · ${total} leds`;
  box.onpointerdown=e=>{ if(mode!=='playground') return;
    const t=e.target; if(!t.dataset||!t.dataset.fx) return;
    painting=true; e.preventDefault(); queue(t.dataset.fx,t.dataset.i); };
  box.onpointerover=e=>{ if(!painting||mode!=='playground') return;
    const t=e.target; if(t.dataset&&t.dataset.fx) queue(t.dataset.fx,t.dataset.i); };
}
function paintRig(frame){
  if(!geo||!frame) return;
  const box=$('rig');
  for(const [name,cols] of Object.entries(frame)){
    for(let i=0;i<cols.length;i++){
      const el=box.querySelector(`[data-fx="${name}"][data-i="${i}"]`);
      if(!el) continue;
      if(el.tagName==='circle') el.setAttribute('fill',cols[i]);
      else el.style.background=cols[i];
    }
  }
}

/* -------------------------------------------------------------- devices */
async function loadDevices(){
  try{ devState=await (await fetch('/devices')).json(); renderDevices(); }
  catch(e){ fail(e); }
}
function renderDevices(){
  const d=devState; if(!d) return;
  const byDev={};
  d.zones.forEach(z=>(byDev[z.device]=byDev[z.device]||[]).push(z));
  let h='';
  for(const [dev,zs] of Object.entries(byDev)){
    h+=`<div class="dev"><div class="zone-head"><span class="zone-name">${dev}</span>
      ${zs[0].virtual?'<span class="tag">virtual · skipped</span>':''}</div>`;
    zs.forEach((z,i)=>{
      const g=z.group||{}, idx=d.zones.indexOf(z);
      const splits=(z.leds<6?[]:(z.splits||[])).map(([k,per])=>
        `<button class="btn tiny" data-sp="${z.key}" data-r="${k}" data-p="${per}">${k}×${per}</button>`).join('');
      h+=`<div class="zone">
        <div class="zone-head"><span class="zone-name">${z.zone}</span>
          <span class="zone-sub">${z.leds} leds</span>
          ${z.patched?'<span class="tag ok">in patch</span>':'<span class="tag">unused</span>'}</div>
        <div class="ctrl-row">
          <label>Chain length<input type="number" min="0" max="512" value="${z.leds}"
            id="z_${idx}" ${z.resizable?'':'disabled'}></label>
          ${z.resizable?`<button class="btn sm" data-z="${idx}" data-k="${z.key}">Set</button>`:''}
          ${z.leds>=6?`<label>Segments<input type="number" min="0" max="64" placeholder="—"
            value="${g.rings||''}" id="gr_${idx}"></label>
          <label>LEDs each<input type="number" min="0" max="256" placeholder="—"
            value="${g.leds_per_ring||''}" id="gp_${idx}"></label>
          <label>Name<input type="text" placeholder="auto" value="${g.name||''}" id="gn_${idx}"></label>
          <button class="btn sm" data-g="${idx}" data-k="${z.key}">Apply</button>`:''}
        </div>
        ${splits?`<div class="ctrl-row"><span class="hint">divides evenly:</span>
          <div class="splits">${splits}</div></div>`:''}
        ${orderBlock(z)}
      </div>`;
    });
    h+='</div>';
  }
  $('d_zones').innerHTML=h;
  wireZones(d);

  $('d_fixtures').innerHTML=Object.entries(d.fixtures).map(([n,x])=>`
    <div class="fxrow">
      <span class="nm">${n}</span>
      <span class="hint">${x.n}${x.mirror>1?'×'+x.mirror:''} ${x.kind}</span>
      <button class="btn sm" data-sp2="${n}" data-v="${x.spin>=0?-1:1}">${x.spin>=0?'CW':'CCW'}</button>
      <input type="range" min="0" max="0.99" step="0.01" value="${x.rotate}" data-rot="${n}"
        aria-label="${n} rotation">
      <span class="num" style="font:12px/1 var(--mono)">${(+x.rotate).toFixed(2)}</span>
      <button class="btn sm" data-id="${n}">Identify</button>
    </div>`).join('') || '<span class="hint">no fixtures</span>';
  $('d_fixtures').querySelectorAll('[data-sp2]').forEach(b=>b.onclick=()=>
    devCmd({op:'calibrate',fixture:b.dataset.sp2,spin:+b.dataset.v}));
  $('d_fixtures').querySelectorAll('[data-id]').forEach(b=>b.onclick=()=>{
    devCmd({op:'identify',fixture:b.dataset.id,seconds:3}); toast('identifying '+b.dataset.id); });
  $('d_fixtures').querySelectorAll('[data-rot]').forEach(r=>{ fillTrack(r);
    r.onchange=()=>devCmd({op:'calibrate',fixture:r.dataset.rot,rotate:+r.value}); });

  $('d_notes').textContent=(d.notes||[]).join('\n')+'\n\n'+(d.patch||'');
}
function orderBlock(z){
  const g=z.group||{}, cnt=+(g.rings||0);
  if(cnt<2) return '';
  const ord=z.order||[], base=g.name||'fixture';
  let h=`<div class="ctrl-row" style="align-items:flex-start;flex-direction:column">
    <span class="hint">mounting order — which wired segment sits in each physical slot</span>
    <div class="slots">`;
  for(let s=0;s<cnt;s++) h+=`<label class="slot"><span>${base}_${String.fromCharCode(97+s)}</span>
    <input type="number" min="0" max="${cnt-1}" value="${ord[s]!==undefined?ord[s]:s}"
      data-o="${z.key}" data-slot="${s}"></label>`;
  h+=`</div><div class="ctrl-row"><button class="btn sm" data-oa="${z.key}">Apply order</button>
    <button class="btn sm" data-oi="${z.key}">Identity</button>
    <button class="btn sm" data-or="${z.key}">Reverse</button>
    <span class="hint">light segment:</span><div class="splits">`;
  for(let s=0;s<cnt;s++) h+=`<button class="btn tiny" data-seg="${s}" data-sk="${z.key}">${s}</button>`;
  return h+'</div></div></div>';
}
function wireZones(d){
  const box=$('d_zones');
  box.querySelectorAll('[data-z]').forEach(b=>b.onclick=()=>
    devCmd({op:'resize',key:b.dataset.k,leds:+$('z_'+b.dataset.z).value}));
  box.querySelectorAll('[data-g]').forEach(b=>b.onclick=()=>{
    const i=b.dataset.g;
    devCmd({op:'group',key:b.dataset.k,rings:+$('gr_'+i).value||0,
      leds_per_ring:+$('gp_'+i).value||0,name:$('gn_'+i).value});
    toast('patch rebuilt'); });
  box.querySelectorAll('[data-sp]').forEach(b=>b.onclick=()=>
    devCmd({op:'group',key:b.dataset.sp,rings:+b.dataset.r,leds_per_ring:+b.dataset.p}));
  const read=k=>Array.from(box.querySelectorAll(`[data-o="${k}"]`))
    .sort((a,b)=>a.dataset.slot-b.dataset.slot).map(i=>+i.value);
  box.querySelectorAll('[data-oa]').forEach(b=>b.onclick=()=>
    devCmd({op:'order',key:b.dataset.oa,order:read(b.dataset.oa)}));
  box.querySelectorAll('[data-oi]').forEach(b=>b.onclick=()=>
    devCmd({op:'order',key:b.dataset.oi,order:'identity'}));
  box.querySelectorAll('[data-or]').forEach(b=>b.onclick=()=>
    devCmd({op:'order',key:b.dataset.or,order:'reverse'}));
  box.querySelectorAll('[data-seg]').forEach(b=>b.onclick=()=>{
    const z=d.zones.find(x=>x.key===b.dataset.sk)||{};
    const ord=z.order||[], base=(z.group||{}).name||'fixture';
    const slot=ord.indexOf(+b.dataset.seg);
    if(slot<0) return;
    const fx=`${base}_${String.fromCharCode(97+slot)}`;
    devCmd({op:'identify',fixture:fx,seconds:3}); toast('segment '+b.dataset.seg+' → '+fx); });
}

/* ----------------------------------------------------------------- poll */
function bar(id,v){ $('b_'+id).style.width=Math.max(0,Math.min(1,v))*100+'%';
  $('v_'+id).textContent=(+v).toFixed(2); }
async function tick(){
  try{
    const d=await (await fetch('/state')).json();
    if(!params){ params=d.params; build(); }
    else if(!/^(INPUT|SELECT)$/.test(document.activeElement.tagName)){
      params=d.params; paintControls(); }
    const t=d.telemetry||{};
    $('p_fps').textContent=(t.fps||0).toFixed(0)+' fps';
    $('p_in').textContent=(t.dbfs==null?'--':(t.dbfs).toFixed(0)+' dBFS');
    $('p_beat').classList.toggle('on',!!t.onset);
    $('an_note').textContent = (t.dbfs!=null&&t.dbfs<-90)?'no signal':'';
    ['level','dynamics','swell','pulse','out'].forEach(k=>bar(k,t[k]||0));
    const bs=$('bands').children, b=t.bands||[];
    for(let i=0;i<bs.length;i++) bs[i].style.height=(2+(b[i]||0)*62)+'px';
    if(!geo){ geo=await (await fetch('/patch')).json(); buildRig(); }
    paintRig(t.frame);
    $('live').style.background=(t.fps>1)?'var(--accent)':'var(--warn)';
    $('err').style.display='none';
  }catch(e){ fail(e); }
}
setInterval(tick,100); tick();
</script>
"""


def page() -> str:
    return (PAGE.replace("__SLIDERS__", json.dumps(SLIDERS))
                .replace("__OPTS__", json.dumps(OPTS))
                .replace("__OPT_HELP__", json.dumps(OPT_HELP))
                .replace("__GROUPS__", json.dumps(GROUPS)))
