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
__FONTCSS__
:root{
  /* Swiss discipline: the chrome is monochrome so every hue on screen
     belongs to the rig. One signal colour, used only for state. */
  --ink:#0a0a0b; --ink-2:#101012; --ink-3:#161619; --ink-4:#1d1d21;
  --rule:#26262b; --rule-2:#33333a;
  --paper:#f4f4f5; --paper-2:#9a9aa4; --paper-3:#61616b;
  --signal:#ff3b25;
  --grid:20px;
  --sans:'Rigby Sans','Fira Sans','Helvetica Neue',Helvetica,Arial,sans-serif;
  --mono:'Rigby Mono','Google Sans Code','JetBrains Mono',ui-monospace,monospace;
}
*{box-sizing:border-box;min-width:0}
body{margin:0;background:var(--ink);color:var(--paper);font:400 14px/1.5 var(--sans);
  -webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility;overflow-x:hidden;
  font-feature-settings:"kern" 1}
h1,h2,h3{margin:0;font-weight:500}
button,input,select{font:inherit;color:inherit}
button{cursor:pointer;border:0;background:none}
:focus-visible{outline:1px solid var(--signal);outline-offset:2px}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
.mono,.num{font-family:var(--mono);font-variant-numeric:tabular-nums;
  font-feature-settings:"tnum" 1}

/* type scale -- few sizes, clear steps */
.eyebrow{font:500 10px/1 var(--sans);letter-spacing:.22em;text-transform:uppercase;
  color:var(--paper-3)}
.lede{font:400 13px/1.55 var(--sans);color:var(--paper-2)}
.micro{font:400 11px/1.5 var(--sans);color:var(--paper-3)}

/* ---------- header: a wordmark, a rule, nothing else ---------- */
header{position:sticky;top:0;z-index:30;display:flex;align-items:center;
  gap:var(--grid);padding:0 var(--grid);height:56px;background:var(--ink);
  border-bottom:1px solid var(--rule)}
.wordmark{font:700 16px/1 var(--sans);letter-spacing:-.02em;text-transform:lowercase}
.wordmark b{color:var(--signal);font-weight:700}
.stats{display:flex;gap:18px;align-items:baseline}
.stat{display:flex;align-items:baseline;gap:5px}
.stat .v{font:500 13px/1 var(--mono);font-variant-numeric:tabular-nums}
.stat .k{font:500 9px/1 var(--sans);letter-spacing:.18em;text-transform:uppercase;
  color:var(--paper-3)}
.beat{width:7px;height:7px;background:var(--rule-2);flex:none;
  transition:background .08s}
.beat.on{background:var(--signal)}
.spacer{flex:1}
nav{display:flex;height:100%}
nav button{padding:0 18px;height:100%;font-size:13px;color:var(--paper-3);
  border-bottom:2px solid transparent;transition:color .12s,border-color .12s}
nav button:hover{color:var(--paper)}
nav button[aria-selected=true]{color:var(--paper);border-bottom-color:var(--signal)}
.quick{display:flex;align-items:center;gap:12px}
.quick label{display:flex;align-items:center;gap:8px}
.quick input[type=range]{width:82px}
.btn{border:1px solid var(--rule);padding:6px 12px;font-size:12px;color:var(--paper-2);
  transition:border-color .12s,color .12s,background .12s}
.btn:hover{border-color:var(--rule-2);color:var(--paper)}
.btn.on{background:var(--signal);border-color:var(--signal);color:#fff}
.btn.sm{padding:4px 9px;font-size:11px}
.btn.tiny{padding:2px 6px;font:400 10px/1.4 var(--mono)}

/* ---------- page grid ---------- */
main{padding:0 var(--grid) 80px;max-width:1440px;margin:0 auto}
section.block{border-bottom:1px solid var(--rule);padding:calc(var(--grid)*1.4) 0}
section.block:last-child{border-bottom:0}
.blockhead{display:flex;align-items:baseline;gap:14px;margin-bottom:var(--grid)}
.cols{display:grid;gap:calc(var(--grid)*1.6)}
.c2{grid-template-columns:minmax(0,1fr) minmax(0,1fr)}
.c4{grid-template-columns:repeat(auto-fit,minmax(224px,1fr))}
@media(max-width:900px){.c2{grid-template-columns:1fr}}
.tab[hidden]{display:none}

/* ---------- rig ---------- */
#rig{display:flex;flex-wrap:wrap;gap:calc(var(--grid)*1.2);align-items:flex-start}
.fx{display:flex;flex-direction:column;gap:9px}
.fx-name{font:500 10px/1 var(--mono);letter-spacing:.1em;color:var(--paper-2)}
.fx-meta{font:400 9px/1 var(--mono);color:var(--paper-3);letter-spacing:.06em}
.led{stroke:none}
.paintable .led{cursor:crosshair}
.paintable .led:hover{stroke:var(--paper);stroke-width:1.5}
.strip{display:flex;flex-wrap:wrap;gap:2px;max-width:326px}
.cell{width:14px;height:14px;background:#000;border:1px solid #1c1c20}
.paintable .cell{cursor:crosshair}
.paintable .cell:hover{border-color:var(--paper)}
.cell.gap{border-color:transparent;background:none;cursor:default}
.grid{display:grid;gap:2px}

/* ---------- controls ---------- */
.field{margin-bottom:var(--grid)}
.field:last-child{margin-bottom:0}
.field>label{display:block;margin-bottom:6px}
select{width:100%;background:var(--ink-2);border:1px solid var(--rule);
  padding:9px 11px;font-size:13px;appearance:none;border-radius:0;
  background-image:linear-gradient(45deg,transparent 50%,var(--paper-3) 50%),
    linear-gradient(135deg,var(--paper-3) 50%,transparent 50%);
  background-position:calc(100% - 15px) 52%,calc(100% - 11px) 52%;
  background-size:4px 4px,4px 4px;background-repeat:no-repeat}
select:hover{border-color:var(--rule-2)}
.slider{margin-bottom:var(--grid)}
.slider:last-child{margin-bottom:0}
.slider .row{display:flex;justify-content:space-between;align-items:baseline;gap:10px;
  margin-bottom:2px}
.slider .val{font:400 12px/1 var(--mono);font-variant-numeric:tabular-nums}
input[type=range]{-webkit-appearance:none;appearance:none;width:100%;height:18px;
  background:none;margin:0}
input[type=range]::-webkit-slider-runnable-track{height:3px;
  background:linear-gradient(var(--paper),var(--paper)) 0/var(--fill,0%) 100% no-repeat,
    var(--ink-4)}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:3px;height:14px;
  background:var(--paper);margin-top:-5px;border:0;border-radius:0}
input[type=range]:hover::-webkit-slider-thumb{background:var(--signal);width:3px}
input[type=range]::-moz-range-track{height:3px;background:var(--ink-4)}
input[type=range]::-moz-range-progress{height:3px;background:var(--paper)}
input[type=range]::-moz-range-thumb{width:3px;height:13px;border:0;border-radius:0;
  background:var(--paper)}
input[type=number],input[type=text]{background:var(--ink-2);border:1px solid var(--rule);
  padding:6px 8px;font:400 12px/1.2 var(--mono);width:100%;border-radius:0}
input[type=color]{width:38px;height:30px;padding:2px;background:var(--ink-2);
  border:1px solid var(--rule);border-radius:0}

/* ---------- analysis ---------- */
.bands{display:flex;gap:2px;height:76px;align-items:flex-end;margin-bottom:var(--grid)}
.bands i{flex:1;background:var(--paper);height:1px;min-height:1px}
.meter{display:grid;grid-template-columns:74px 1fr 44px;gap:12px;align-items:center;
  padding:6px 0;border-top:1px solid var(--rule)}
.meter:first-of-type{border-top:0}
.meter .track{height:3px;background:var(--ink-4)}
.meter .track i{display:block;height:3px;width:0;background:var(--paper)}
.meter .num{font:400 12px/1 var(--mono);text-align:right;font-variant-numeric:tabular-nums}

/* ---------- devices ---------- */
#d_zones{display:grid;gap:calc(var(--grid)*1.6);
  grid-template-columns:repeat(auto-fit,minmax(400px,1fr));align-items:start}
@media(max-width:880px){#d_zones{grid-template-columns:1fr}}
.devname{font:500 10px/1 var(--sans);letter-spacing:.22em;text-transform:uppercase;
  color:var(--paper-3);padding-bottom:8px;border-bottom:1px solid var(--rule-2);
  margin-bottom:14px}
.zone{padding-bottom:16px;margin-bottom:16px;border-bottom:1px solid var(--rule)}
.zone:last-child{border-bottom:0;margin-bottom:0;padding-bottom:0}
.zone-head{display:flex;align-items:baseline;gap:9px;flex-wrap:wrap;margin-bottom:11px}
.zone-name{font:500 14px/1 var(--sans)}
.tag{font:400 9px/1 var(--mono);letter-spacing:.1em;text-transform:uppercase;
  color:var(--paper-3)}
.tag.ok{color:var(--paper)}
.ctrl-row{display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap;margin-bottom:9px}
.ctrl-row:last-child{margin-bottom:0}
.ctrl-row label{font:400 9px/1 var(--sans);letter-spacing:.16em;text-transform:uppercase;
  color:var(--paper-3);display:flex;flex-direction:column;gap:4px}
.ctrl-row input{width:74px}
.splits{display:flex;gap:4px;flex-wrap:wrap}
.slots{display:flex;gap:5px;flex-wrap:wrap;margin:9px 0}
.slot{display:flex;flex-direction:column;gap:4px}
.slot span{font:400 9px/1 var(--mono);color:var(--paper-3)}
.slot input{width:52px;text-align:center}
.fxrow{display:grid;grid-template-columns:118px 56px 58px 1fr 40px auto;gap:12px;
  align-items:center;padding:9px 0;border-top:1px solid var(--rule)}
.fxrow:first-child{border-top:0}
.fxrow .nm{font:400 12px/1 var(--mono)}

/* ---------- playground ---------- */
.tools{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.swatches{display:flex;gap:3px}
.sw{width:20px;height:20px;border:1px solid var(--rule)}
.sw:hover{border-color:var(--paper)}
kbd{font:400 9px/1 var(--mono);color:var(--paper-3);margin-left:5px;letter-spacing:.1em}
.btn.on kbd{color:rgba(255,255,255,.7)}

/* ---------- feedback ---------- */
#toast{position:fixed;left:var(--grid);bottom:var(--grid);background:var(--paper);
  color:var(--ink);padding:9px 15px;font:500 12px/1 var(--sans);opacity:0;
  pointer-events:none;z-index:60;transition:opacity .16s;letter-spacing:.01em}
#toast.on{opacity:1}
#err{display:none;background:var(--signal);color:#fff;padding:9px var(--grid);
  font:400 12px/1.5 var(--mono)}
pre{font:400 11px/1.7 var(--mono);color:var(--paper-3);white-space:pre-wrap;margin:0}
@media(max-width:640px){
  header{height:auto;padding:10px var(--grid);flex-wrap:wrap;gap:12px}
  nav button{padding:0 12px;height:38px}
  .quick label{display:none}
  .fxrow{grid-template-columns:96px 1fr auto;gap:8px}
  .stats{gap:12px}
}
</style>

<div id="err" role="alert"></div>

<header>
  <div class="wordmark">rig<b>by</b></div>
  <div class="stats">
    <span class="stat"><span class="v" id="p_fps">--</span><span class="k">fps</span></span>
    <span class="stat"><span class="v" id="p_in">--</span><span class="k">dbfs</span></span>
    <span class="beat" id="p_beat" title="beat"></span>
  </div>
  <div class="spacer"></div>
  <nav role="tablist" aria-label="Section">
    <button role="tab" data-tab="show" aria-selected="true">Show</button>
    <button role="tab" data-tab="devices" aria-selected="false">Devices</button>
    <button role="tab" data-tab="playground" aria-selected="false">Playground</button>
  </nav>
  <div class="quick">
    <label for="q_master"><span class="eyebrow">Master</span>
      <input type="range" id="q_master" min="0" max="1" step="0.01"></label>
    <button class="btn" id="q_black">Blackout</button>
  </div>
</header>

<main>
  <section class="block">
    <div class="blockhead">
      <h2 class="eyebrow">Rig</h2>
      <span class="micro" id="righint"></span>
      <div class="spacer"></div>
      <span class="micro mono" id="rigcount"></span>
    </div>
    <div id="rig"></div>
  </section>

  <div class="tab" id="tab-show">
    <section class="block">
      <div class="cols c2">
        <div>
          <div class="blockhead"><h2 class="eyebrow">Programme</h2></div>
          <div id="lookfields"></div>
        </div>
        <div>
          <div class="blockhead"><h2 class="eyebrow">Analysis</h2>
            <span class="micro" id="an_note"></span></div>
          <div class="bands" id="bands" aria-hidden="true"></div>
          <div class="meter"><span class="micro">Level</span><div class="track"><i id="b_level"></i></div><span class="num" id="v_level">-</span></div>
          <div class="meter"><span class="micro">Dynamics</span><div class="track"><i id="b_dynamics"></i></div><span class="num" id="v_dynamics">-</span></div>
          <div class="meter"><span class="micro">Swell</span><div class="track"><i id="b_swell"></i></div><span class="num" id="v_swell">-</span></div>
          <div class="meter"><span class="micro">Pulse</span><div class="track"><i id="b_pulse"></i></div><span class="num" id="v_pulse">-</span></div>
          <div class="meter"><span class="micro">Output</span><div class="track"><i id="b_out"></i></div><span class="num" id="v_out">-</span></div>
        </div>
      </div>
    </section>
    <section class="block">
      <div class="cols c4" id="slidercards"></div>
    </section>
  </div>

  <div class="tab" id="tab-devices" hidden>
    <section class="block">
      <div class="blockhead"><h2 class="eyebrow">Calibration</h2>
        <span class="micro">the show is paused while you're here</span>
        <div class="spacer"></div>
        <button class="btn sm" id="d_save">Save config</button></div>
      <div id="d_zones"></div>
    </section>
    <section class="block">
      <div class="cols c2">
        <div>
          <div class="blockhead"><h2 class="eyebrow">Fixtures</h2>
            <span class="micro">spin, rotation, identify</span></div>
          <div id="d_fixtures"></div>
        </div>
        <div>
          <div class="blockhead"><h2 class="eyebrow">Log</h2></div>
          <pre id="d_notes"></pre>
        </div>
      </div>
    </section>
  </div>

  <div class="tab" id="tab-playground" hidden>
    <section class="block">
      <div class="blockhead"><h2 class="eyebrow">Paint</h2>
        <span class="micro">click or drag on the rig &middot; colours are written exactly</span></div>
      <div class="tools">
        <input type="color" id="pick" value="#ff3b25" aria-label="Colour">
        <div class="swatches" id="swatches"></div>
        <label class="eyebrow" for="pbright">Brightness</label>
        <input type="range" id="pbright" min="0" max="1" step="0.01" value="1" style="width:108px">
        <span class="num" id="pbrightv">1.00</span>
        <button class="btn sm on" id="t_paint">Paint<kbd>P</kbd></button>
        <button class="btn sm" id="t_erase">Erase<kbd>E</kbd></button>
        <button class="btn sm" id="fill">Fill all</button>
        <button class="btn sm" id="clear">Clear</button>
        <button class="btn sm" id="grab">Capture show</button>
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
let els={}, lastHex={}, es=null, geoPending=false;

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
       devState=await r.json(); renderDevices();
       geo=null; lastHex={}; }catch(e){ fail(e); }
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
      <label for="sel_${k}" class="eyebrow">${k.replace('_',' ')}</label>
      <select id="sel_${k}">${OPTS[k].map(v=>`<option>${v}</option>`).join('')}</select>
      <div class="micro" style="margin-top:6px">${OPT_HELP[k]||''}</div></div>`).join('');
  Object.keys(OPTS).forEach(k=>$('sel_'+k).onchange=()=>send({[k]:$('sel_'+k).value}));

  // slider cards, grouped
  $('slidercards').innerHTML=GROUPS.map(g=>{
    const keys=Object.keys(SL).filter(k=>SL[k].group===g);
    if(!keys.length) return '';
    return `<div><div class="blockhead"><h2 class="eyebrow">${g}</h2></div>`+
      keys.map(k=>{const s=SL[k];return `<div class="slider">
        <div class="row"><span class="micro" style="color:var(--paper-2)">${s.label}</span>
          <span class="val" id="n_${k}">--</span></div>
        <input type="range" id="s_${k}" min="${s.min}" max="${s.max}" step="${s.step}"
          aria-label="${s.label}">
        <div class="micro" style="margin-top:4px">${s.help}</div></div>`}).join('')+`</div>`;
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
    for(const k in fr){ const h=fr[k], a=[];
      for(let i=0;i+6<=h.length;i+=6) a.push(hex2rgb('#'+h.slice(i,i+6)));
      cells[k]=a; }
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
  els={}; lastHex={};
  let total=0;
  for(const [name,g] of Object.entries(geo)){
    total+=g.n;
    const d=document.createElement('div'); d.className='fx';
    d.innerHTML=`<div><div class="fx-name">${name}</div>
      <div class="fx-meta">${g.n}${g.mirror>1?'\u00d7'+g.mirror:''} ${g.kind}${g.slow?' i2c':''}</div></div>`;
    if(g.kind==='ring'){
      const big=g.n>12, R=big?54:34, pad=14, S=(R+pad)*2, r=big?6.5:8.5;
      const ns='http://www.w3.org/2000/svg';
      const svg=document.createElementNS(ns,'svg');
      svg.setAttribute('width',S); svg.setAttribute('height',S);
      svg.setAttribute('role','img'); svg.setAttribute('aria-label',name);
      g.angle.forEach((a,i)=>{
        const th=a*2*Math.PI-Math.PI/2, c=document.createElementNS(ns,'circle');
        c.setAttribute('cx',S/2+R*Math.cos(th)); c.setAttribute('cy',S/2+R*Math.sin(th));
        c.setAttribute('r',r); c.setAttribute('class','led'); c.setAttribute('fill','#000');
        c.dataset.fx=name; c.dataset.i=i; svg.appendChild(c);
        (els[name]=els[name]||[])[i]=c;
      });
      d.appendChild(svg);
    } else if(g.matrix){
      const gr=document.createElement('div'); gr.className='grid';
      gr.style.gridTemplateColumns=`repeat(${g.matrix[0].length},14px)`;
      g.matrix.forEach(row=>row.forEach(v=>{
        const c=document.createElement('div');
        c.className='cell'+(v===null?' gap':'');
        c.style.width='14px'; c.style.height='14px';
        if(v!==null){ c.dataset.fx=name; c.dataset.i=v; (els[name]=els[name]||[])[v]=c; }
        gr.appendChild(c);
      }));
      d.appendChild(gr);
    } else {
      const st=document.createElement('div'); st.className='strip';
      for(let i=0;i<g.n;i++){ const c=document.createElement('div');
        c.className='cell'; c.dataset.fx=name; c.dataset.i=i; st.appendChild(c);
        (els[name]=els[name]||[])[i]=c; }
      d.appendChild(st);
    }
    box.appendChild(d);
  }
  $('rigcount').textContent=`${Object.keys(geo).length} fixtures / ${total} leds`;
  box.onpointerdown=e=>{ if(mode!=='playground') return;
    const t=e.target; if(!t.dataset||!t.dataset.fx) return;
    painting=true; e.preventDefault(); queue(t.dataset.fx,t.dataset.i); };
  box.onpointerover=e=>{ if(!painting||mode!=='playground') return;
    const t=e.target; if(t.dataset&&t.dataset.fx) queue(t.dataset.fx,t.dataset.i); };
}
// Diff against the previous frame and touch only the LEDs that moved. A
// querySelector per LED per frame is ~200 lookups at 40Hz over a large DOM,
// which is its own source of stutter.
function paintRig(frame){
  if(!geo||!frame) return;
  for(const name in frame){
    const hex=frame[name], list=els[name];
    if(!list) continue;
    const prev=lastHex[name];
    if(prev===hex) continue;
    lastHex[name]=hex;
    for(let i=0;i<list.length;i++){
      const o=i*6, c=hex.slice(o,o+6);
      if(!c) break;
      if(prev && prev.slice(o,o+6)===c) continue;
      const el=list[i]; if(!el) continue;
      const css='#'+c;
      if(el.tagName==='circle') el.setAttribute('fill',css);
      else el.style.background=css;
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
    h+=`<div class="dev"><div class="devname">${dev}
      ${zs[0].virtual?'&middot; virtual, skipped':''}</div>`;
    zs.forEach((z,i)=>{
      const g=z.group||{}, idx=d.zones.indexOf(z);
      const splits=(z.leds<6?[]:(z.splits||[])).map(([k,per])=>
        `<button class="btn tiny" data-sp="${z.key}" data-r="${k}" data-p="${per}">${k}×${per}</button>`).join('');
      h+=`<div class="zone">
        <div class="zone-head"><span class="zone-name">${z.zone}</span>
          <span class="tag mono">${z.leds} leds</span>
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
        ${splits?`<div class="ctrl-row"><span class="micro">divides evenly</span>
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
      <span class="micro mono">${x.n}${x.mirror>1?'×'+x.mirror:''} ${x.kind}</span>
      <button class="btn sm" data-sp2="${n}" data-v="${x.spin>=0?-1:1}">${x.spin>=0?'CW':'CCW'}</button>
      <input type="range" min="0" max="0.99" step="0.01" value="${x.rotate}" data-rot="${n}"
        aria-label="${n} rotation">
      <span class="num">${(+x.rotate).toFixed(2)}</span>
      <button class="btn sm" data-id="${n}">Identify</button>
    </div>`).join('') || '<span class="micro">no fixtures</span>';
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
    <span class="micro">mounting order &mdash; which wired segment sits in each physical slot</span>
    <div class="slots">`;
  for(let s=0;s<cnt;s++) h+=`<label class="slot"><span>${base}_${String.fromCharCode(97+s)}</span>
    <input type="number" min="0" max="${cnt-1}" value="${ord[s]!==undefined?ord[s]:s}"
      data-o="${z.key}" data-slot="${s}"></label>`;
  h+=`</div><div class="ctrl-row"><button class="btn sm" data-oa="${z.key}">Apply order</button>
    <button class="btn sm" data-oi="${z.key}">Identity</button>
    <button class="btn sm" data-or="${z.key}">Reverse</button>
    <span class="micro">light segment</span><div class="splits">`;
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

/* ---------------------------------------------------------------- stream */
function bar(id,v){ $('b_'+id).style.width=Math.max(0,Math.min(1,v))*100+'%';
  $('v_'+id).textContent=(+v).toFixed(2); }

let frameT=null;
function apply(d){
  if(!params){ params=d.params; build(); }
  else if(!/^(INPUT|SELECT)$/.test(document.activeElement.tagName)){
    params=d.params; paintControls(); }
  const t=d.telemetry||{};
  $('p_fps').textContent=(t.fps||0).toFixed(0);
  $('p_in').textContent=(t.dbfs==null?'--':(t.dbfs).toFixed(0));
  $('p_beat').classList.toggle('on',!!t.onset);
  $('an_note').textContent=(t.dbfs!=null&&t.dbfs<-90)?'no signal':'';
  ['level','dynamics','swell','pulse','out'].forEach(k=>bar(k,t[k]||0));
  const bs=$('bands').children, b=t.bands||[];
  for(let i=0;i<bs.length;i++) bs[i].style.height=(2+(b[i]||0)*62)+'px';
  // One request, not one per pushed frame: at 30Hz an unguarded fetch here
  // opens thirty connections a second and starves the server's thread pool.
  if(!geo && !geoPending){ geoPending=true;
    fetch('/patch').then(r=>r.json()).then(g=>{geo=g;buildRig();})
      .catch(fail).finally(()=>{geoPending=false;}); }
  // Coalesce onto the display's own cadence: pushes can outrun a 60Hz screen,
  // and painting more often than it refreshes is wasted work.
  if(t.frame){ const fr=t.frame;
    if(frameT) cancelAnimationFrame(frameT);
    frameT=requestAnimationFrame(()=>{frameT=null;paintRig(fr);}); }

  $('err').style.display='none';
}

function connect(){
  // ?static renders one frame and stops. An open event stream is a request
  // that never finishes, which means headless capture never sees the page go
  // idle and screenshots hang -- so the UI has to be able to sit still.
  if(new URLSearchParams(location.search).has('static')) return;
  try{
    es=new EventSource('/events');
    es.onmessage=e=>{ try{ apply(JSON.parse(e.data)); }catch(err){ fail(err); } };
    es.onerror=()=>{};                       // EventSource retries by itself
  }catch(e){
    // No EventSource: fall back to polling so the page still works.
    setInterval(()=>fetch('/state').then(r=>r.json()).then(apply).catch(fail),100);
  }
}
fetch('/state').then(r=>r.json()).then(d=>{apply(d);connect();}).catch(e=>{fail(e);connect();});
</script>
"""


def page() -> str:
    from .fonts import css as font_css
    return (PAGE.replace("__FONTCSS__", font_css())
                .replace("__SLIDERS__", json.dumps(SLIDERS))
                .replace("__OPTS__", json.dumps(OPTS))
                .replace("__OPT_HELP__", json.dumps(OPT_HELP))
                .replace("__GROUPS__", json.dumps(GROUPS)))
