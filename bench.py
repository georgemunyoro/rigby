"""Offline scoring harness: dynamics, onset accuracy, smoothness."""
import json, sys, numpy as np
from rigby.analyze import Analyzer
from rigby.patch import Fixture
from rigby.show import LOOKS
from rigby.fx import gamma as _gamma
from rigby.sink import MIN_LIT

def to_led(v, g=2.2, master=1.0):
    """Exactly what Sink does: gamma, then 8-bit quantisation."""
    lit = v > 1e-4
    b = _gamma(v*master, g)
    raw = b*(255.0-MIN_LIT)+MIN_LIT
    return np.clip(np.where(lit, raw, 0.0)+0.5, 0, 255).astype(np.uint8)

def fake_fixtures():
    def fx(name, n, slow=False, rev=False):
        pos = np.linspace(0,1,n) if n>1 else np.array([0.5])
        if rev: pos = pos[::-1].copy()
        return Fixture(name,0,0,n,0,pos,slow,rev)
    return {"mobo":fx("mobo",4), "truss_l":fx("truss_l",18),
            "truss_r":fx("truss_r",18,rev=True),
            "ram_a":fx("ram_a",8,slow=True), "ram_b":fx("ram_b",8,slow=True),
            "gpu":fx("gpu",1,slow=True)}

def run(path, gt, look_name="spectrum", **kw):
    an = Analyzer(f"file:{path}", fps=60, play=False); an.start()
    an._paced = False   # bench runs flat out
    look = LOOKS[look_name](fake_fixtures(), **kw)
    fps=60; i=0
    onsets=[]; bright=[]; frames=[]
    while True:
        f = an.read()
        if f is None:
            if an.eof: break
            continue
        look.step(1/fps, f)
        fr = look.render(f)
        t = i/fps
        if f.onset: onsets.append(t)
        led = to_led(fr["truss_l"]).astype(np.float32)
        v = float(led.mean())                       # 0..255 as the LED sees it
        bright.append((t, v)); frames.append(led)
        i+=1
    an.stop()
    return np.array(onsets), np.array(bright), np.array(frames)

def score(onsets, bright, frames, gt, tol=0.09):
    hits = np.array(gt["hits"])
    matched = set(); tp=0
    for o in onsets:
        d = np.abs(hits-o)
        j = int(np.argmin(d))
        if d[j] <= tol and j not in matched:
            matched.add(j); tp+=1
    fp = len(onsets)-tp; fn = len(hits)-tp
    prec = tp/max(len(onsets),1); rec = tp/max(len(hits),1)
    f1 = 2*prec*rec/max(prec+rec,1e-9)

    t = bright[:,0]; v = bright[:,1]
    loud = v[((t<8)|(t>=16))]; quiet = v[(t>=8)&(t<16)]
    ratio = float(np.mean(loud)/max(np.mean(quiet),1e-9))

    # smoothness: mean absolute frame-to-frame change, excluding onset frames
    d = np.abs(np.diff(frames,axis=0)).mean(axis=(1,2))
    onset_idx = set((onsets*60).astype(int))
    mask = np.array([i not in onset_idx and (i+1) not in onset_idx
                     for i in range(len(d))])
    jitter = float(d[mask].mean())
    dark = float((np.max(frames.reshape(len(frames),-1),axis=1) < 1).mean())
    return dict(onsets=len(onsets), truth=len(hits), tp=tp, fp=fp, fn=fn,
                precision=round(prec,3), recall=round(rec,3), f1=round(f1,3),
                loud_vs_quiet=round(ratio,2),
                loud_mean=round(float(loud.mean()),3),
                quiet_mean=round(float(quiet.mean()),3),
                jitter=round(jitter,3), frac_fully_dark=round(dark,3))

if __name__ == "__main__":
    gt = json.load(open(sys.argv[2]))
    o,b,f = run(sys.argv[1], gt)
    s = score(o,b,f,gt)
    for k,val in s.items(): print(f"  {k:14s} {val}")
