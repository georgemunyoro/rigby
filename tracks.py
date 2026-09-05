"""Generate the synthetic tracks the benchmarks score against.

bench.py is useless without fixtures, and audio files don't belong in git, so
they're regenerated deterministically instead. Every source of randomness is
seeded; running this twice gives byte-identical files.

    uv run python tracks.py [outdir]
"""

from __future__ import annotations

import json
import sys
import wave
from pathlib import Path

import numpy as np

SR = 48000


def _env(ph, d):
    return np.where(ph >= 0, np.exp(-np.maximum(ph, 0) / d), 0.0)


def _write(path: Path, x: np.ndarray) -> None:
    x = np.clip(x / max(float(np.abs(x).max()), 1e-9) * 0.88, -1, 1)
    with wave.open(str(path), "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((x * 32767).astype("<i2").tobytes())


def dyn(out: Path):
    """128bpm, kick/snare/hats, with an 18dB quiet section from 8-16s."""
    bpm, dur = 128, 24.0
    beat = 60 / bpm
    t = np.arange(int(SR * dur)) / SR
    x = np.zeros_like(t)
    hits = []
    for i in range(int(dur / beat)):
        tb = i * beat
        ph = t - tb
        if i % 2 == 0:
            x += 1.0 * np.sin(2 * np.pi * 52 * np.maximum(ph, 0)) * _env(ph, 0.10)
        else:
            x += 0.5 * np.random.RandomState(i).randn(t.size) * _env(ph, 0.05)
            x += 0.35 * np.sin(2 * np.pi * 190 * np.maximum(ph, 0)) * _env(ph, 0.06)
        hits.append(tb)
    for i in range(int(dur / (beat / 2))):
        ph = t - i * beat / 2
        x += 0.13 * np.random.RandomState(1000 + i).randn(t.size) * _env(ph, 0.010)
    x += 0.22 * np.sin(2 * np.pi * 110 * t) * (0.6 + 0.4 * np.sin(2 * np.pi * t / 3))
    x += 0.15 * np.sin(2 * np.pi * 330 * t)
    g = np.ones_like(t)
    g[(t >= 8) & (t < 16)] = 10 ** (-18 / 20)
    _write(out / "dyn.wav", x * g)
    (out / "dyn.json").write_text(json.dumps(
        {"hits": hits, "bpm": bpm, "quiet": [8, 16],
         "texture_hits": [i * beat / 2 for i in range(int(dur / (beat / 2)))]}))


def hard(out: Path):
    """140bpm, syncopation, dense pad and 16th hats over a noise floor."""
    bpm, dur = 140, 24.0
    beat = 60 / bpm
    t = np.arange(int(SR * dur)) / SR
    rs = np.random.RandomState(7)
    x = np.zeros_like(t)
    hits = []
    for i in range(int(dur / beat)):
        tb = i * beat
        ph = t - tb
        x += 0.9 * np.sin(2 * np.pi * 50 * np.maximum(ph, 0)) * _env(ph, 0.09)
        hits.append(tb)
        if i % 4 == 2:
            tb2 = tb + beat * 0.5
            ph2 = t - tb2
            x += 0.7 * np.sin(2 * np.pi * 50 * np.maximum(ph2, 0)) * _env(ph2, 0.07)
            hits.append(tb2)
        if i % 2 == 1:
            x += 0.55 * rs.randn(t.size) * _env(ph, 0.05)
    for f0 in (110, 164.8, 220, 329.6):
        x += 0.16 * np.sin(2 * np.pi * f0 * t + rs.rand())
    for i in range(int(dur / (beat / 4))):
        ph = t - i * beat / 4
        x += 0.10 * rs.randn(t.size) * _env(ph, 0.008)
    x += 0.10 * rs.randn(t.size)
    _write(out / "hard.wav", x)
    (out / "hard.json").write_text(json.dumps(
        {"hits": sorted(hits), "bpm": bpm, "quiet": [0, 0],
         "texture_hits": [i * beat / 4 for i in range(int(dur / (beat / 4)))]}))


def ballad(out: Path):
    """Sustained, 4 soft ticks in 30s, belt 16-24s.

    Modulation comes from smoothed noise, never an LFO: a periodic LFO lands in
    the beat-period range and the tempo-salience measure reads it as a beat,
    which silently invalidates the whole not-beaty test.
    """
    dur = 30.0
    t = np.arange(int(SR * dur)) / SR
    rs = np.random.RandomState(11)
    x = np.zeros_like(t)
    ticks = []
    for tb in (4.3, 11.9, 19.1, 26.4):
        x += 0.09 * rs.randn(t.size) * _env(t - tb, 0.02)
        ticks.append(tb)

    def wobble(scale, seed):
        n = np.random.RandomState(seed)
        r = n.randn(int(dur * 4))
        k = np.hanning(9)
        k /= k.sum()
        r = np.convolve(r, k, mode="same")
        return np.interp(t, np.linspace(0, dur, len(r)), r) * scale

    for f0, amp, sd in ((146.8, 0.20, 1), (220.0, 0.15, 2), (293.7, 0.12, 3)):
        x += amp * np.sin(2 * np.pi * f0 * t + wobble(0.8, sd))
    vox = (0.5 * np.sin(2 * np.pi * 392 * t * (1 + 0.015 * wobble(1.0, 4)))
           + 0.22 * np.sin(2 * np.pi * 784 * t * (1 + 0.015 * wobble(1.0, 5))))
    g = np.interp(t, [0, 9, 10, 16, 17, 23, 24, 30],
                  [0.10, 0.12, 0.30, 0.95, 1.00, 0.95, 0.25, 0.06])
    _write(out / "ballad2.wav", (x * 0.5 + vox * g) * g + 0.004 * rs.randn(t.size))
    (out / "ballad2.json").write_text(json.dumps(
        {"hits": ticks, "belt": [16, 24], "quiet": [0, 10]}))


def timing(out: Path):
    """Tempo change, subdivisions, a short breakdown, and digital silence."""
    dur = 38
    t = np.arange(SR * dur) / SR
    rng = np.random.default_rng(30)
    x = np.zeros_like(t)
    beats = np.r_[np.arange(2, 18, .5), np.arange(18, 34, .6)]
    hits, texture = [], []
    for i, tb in enumerate(beats):
        if 12 <= tb < 14:
            continue
        ph = t - tb
        decay = _env(ph, .09)
        x += .8 * np.sin(2*np.pi*60*np.maximum(ph, 0)) * decay
        if i % 2:
            x += .2 * rng.standard_normal(t.size) * _env(ph, .035)
        hits.append(float(tb))
        if i % 8 == 7:
            for shift in (.125, .25, .375):
                tt = tb + shift
                x += .035 * rng.standard_normal(t.size) * _env(t-tt, .007)
                texture.append(float(tt))
    # The silence intervals are truly zero, not an exponential tail.
    x[(t<2) | ((t>=12)&(t<14)) | (t>=34)] = 0
    _write(out/'timing.wav', x)
    (out/'timing.json').write_text(json.dumps(dict(
        hits=hits, texture_hits=texture, beats=beats.tolist(),
        silence=[[0,2],[12.8,14],[34.8,38]],
        sections=[dict(start=2,end=12,bpm=120),dict(start=18,end=34,bpm=100)])))


def sustained(out: Path):
    """Pitched vibrato, plucked strings, quiet ticks, and a loud sustained lift."""
    t = np.arange(SR * 16) / SR
    x = np.zeros_like(t)
    # Integrate frequency to phase: f(t)*t would introduce unintended chirps.
    frequency = 220 * (1 + .012 * np.sin(2*np.pi*5*t))
    phase = 2*np.pi*np.cumsum(frequency) / SR
    envelope = np.interp(t, [0,2,2.3,7,7.7,11,11.5,16], [0,0,.12,.12,.65,.65,0,0])
    x += envelope * (np.sin(phase) + .25*np.sin(2*phase) + .1*np.sin(3*phase))
    hits = [4., 9.]
    rng = np.random.default_rng(12)
    for tb in hits:
        x += .10*rng.standard_normal(t.size)*_env(t-tb,.015)
    _write(out/'sustained.wav', x)
    (out/'sustained.json').write_text(json.dumps(dict(
        hits=hits, quiet=[2.5,7], loud=[8,11], silence=[[0,2],[12.5,16]],
        sustained=[[2.5,3.9],[4.2,7],[8,8.9],[9.2,11]])))


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    out.mkdir(parents=True, exist_ok=True)
    for fn in (dyn, hard, ballad, timing, sustained):
        fn(out)
        print(f"  wrote {fn.__name__}")

    (out / "corpus.json").write_text(json.dumps([
        {"audio": name + ".wav", "annotations": name + ".json"}
        for name in ("dyn", "hard", "ballad2", "timing", "sustained")], indent=2) + "\n")
