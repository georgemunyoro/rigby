"""The FX primitive, borrowed wholesale from real lighting desks.

On a grandMA/Hog an "effect" is four things: a waveform, a rate, a size, and a
phase spread across the fixture group. That single primitive generates most of
what you've seen at a show -- sine on intensity with 360 degrees of spread
across a strip is the classic wave; step on hue with zero spread is a colour
chase.

Everything here returns float arrays in 0..1 shaped (n,), ready to be merged.
"""

from __future__ import annotations

import numpy as np

# ---------------------------------------------------------------- waveforms --

def w_sine(p):     return 0.5 - 0.5 * np.cos(2 * np.pi * p)
def w_ramp(p):     return p % 1.0
def w_saw(p):      return 1.0 - (p % 1.0)
def w_triangle(p): return 1.0 - np.abs(((p % 1.0) * 2.0) - 1.0)
def w_step(p):     return (((p % 1.0) < 0.5)).astype(np.float32)

def w_bump(p, width: float = 0.18):
    """A travelling dot -- the workhorse for chases."""
    d = np.abs(((p % 1.0) + 0.5) % 1.0 - 0.5)
    return np.clip(1.0 - d / width, 0.0, 1.0)

SHAPES = {"sine": w_sine, "ramp": w_ramp, "saw": w_saw,
          "triangle": w_triangle, "step": w_step, "bump": w_bump}


def wave(shape: str, phase: float, pos: np.ndarray, spread: float = 1.0,
         size: float = 1.0, offset: float = 0.0) -> np.ndarray:
    """Evaluate `shape` across a fixture.

    phase  -- global effect phase in turns (advance it by rate/fps each frame)
    pos    -- fixture positions 0..1
    spread -- how many full cycles are laid across the fixture
    size   -- output depth; 1.0 = full swing, 0.0 = flat at `offset`
    """
    fn = SHAPES[shape]
    v = fn(phase + pos * spread)
    return np.clip(offset + (v - 0.5) * size + 0.5 * size, 0.0, 1.0)


def blur(x: np.ndarray, radius: int = 2) -> np.ndarray:
    """Box-blur along a fixture.

    Neighbouring LEDs on a strip are a few millimetres apart, so per-LED
    independence reads as noise rather than detail. Smearing slightly is most
    of what makes a strip look like a considered wash instead of a VU meter
    having a fit.
    """
    if radius <= 0 or x.size < 3:
        return x
    k = np.ones(2 * radius + 1, dtype=np.float32)
    k /= k.sum()
    return np.convolve(np.pad(x, radius, mode="edge"), k, mode="valid")


# ------------------------------------------------------------------- merge --

def htp(*layers: np.ndarray) -> np.ndarray:
    """Highest-takes-precedence, as a desk merges intensity."""
    out = layers[0]
    for l in layers[1:]:
        out = np.maximum(out, l)
    return out


# -------------------------------------------------------------- geometry --

def wrap_delta(a, b):
    """Shortest signed distance from b to a, in turns, in (-0.5, 0.5]."""
    return ((np.asarray(a, dtype=np.float32) - b + 0.5) % 1.0) - 0.5


def arc(angle: np.ndarray, centre: float, width: float = 0.22,
        softness: float = 1.0) -> np.ndarray:
    """A lit arc of a ring, centred at `centre` turns, `width` turns wide.

    This is the ring equivalent of a travelling bump, and it's the primitive
    that makes fans read as rotating objects rather than as blinking dots.
    """
    d = np.abs(wrap_delta(angle, centre))
    v = np.clip(1.0 - d / max(width, 1e-4), 0.0, 1.0)
    return v ** max(softness, 1e-3)


def half(angle: np.ndarray, centre: float, feather: float = 0.06) -> np.ndarray:
    """The half of a ring centred on `centre`, with a soft edge.

    Used to flash one side of a fan on a beat while the other side keeps
    running its own effect.
    """
    d = np.abs(wrap_delta(angle, centre))
    return np.clip((0.25 - d) / max(feather, 1e-4) + 0.5, 0.0, 1.0)


# ------------------------------------------------------------------ colour --

def hsv(h, s, v) -> np.ndarray:
    """Vectorised HSV->RGB. Inputs broadcastable, all 0..1. Returns (n,3)."""
    h, s, v = (np.atleast_1d(np.asarray(x, dtype=np.float32)) for x in (h, s, v))
    # Any of the three may be scalar or per-LED -- settle on one common shape
    # rather than assuming hue is the widest.
    shape = np.broadcast_shapes(h.shape, s.shape, v.shape)
    h = np.broadcast_to(h, shape) % 1.0
    s = np.broadcast_to(s, shape)
    v = np.broadcast_to(v, shape)

    i = np.floor(h * 6.0).astype(np.int32)
    f = h * 6.0 - i
    p, q, t = v * (1 - s), v * (1 - f * s), v * (1 - (1 - f) * s)

    i = i % 6
    r = np.select([i == 0, i == 1, i == 2, i == 3, i == 4, i == 5],
                  [v, q, p, p, t, v])
    g = np.select([i == 0, i == 1, i == 2, i == 3, i == 4, i == 5],
                  [t, v, v, q, p, p])
    b = np.select([i == 0, i == 1, i == 2, i == 3, i == 4, i == 5],
                  [p, p, t, v, v, q])
    return np.clip(np.stack([r, g, b], axis=-1), 0.0, 1.0)


def drive(x, gain: float = 1.6, curve: float = 0.45, floor: float = 0.0):
    """Soft shoulder without a second master dimmer; mixers reserve headroom."""
    y = np.maximum(0.0, (np.asarray(x) - floor) / max(1e-6, 1.0 - floor))
    return (-np.expm1(-max(0., gain) * y)) ** max(.05, curve)


def mix_layers(*layers, ceiling=.88):
    """Blend chroma separately from intensity, preserving output headroom."""
    weights = [np.max(layer, axis=-1, keepdims=True) for layer in layers]
    total = sum(weights)
    color = sum(layers) / np.maximum(total, 1e-9)
    # Mixing different hues reduces the RGB peak. Restore unit chroma before
    # applying intensity, otherwise overlap secretly adds another dimmer.
    color = color / np.maximum(color.max(axis=-1, keepdims=True), 1e-9)
    intensity = 1.0 - np.prod([1.0 - np.clip(w, 0, 1) for w in weights], axis=0)
    return color * np.minimum(intensity, ceiling)


def crossfade(a, b, weight):
    """Blend approximate emitted light, avoiding a dark dip between patterns.

    Looks work in perceptual RGB. A fixed 2.2 blend curve approximates the
    default output transfer; user gamma remains a separate device calibration.
    """
    w = np.clip(weight, 0., 1.)
    return (np.clip(a, 0, 1) ** 2.2 * w + np.clip(b, 0, 1) ** 2.2 * (1-w)) ** (1/2.2)


def encode_rgb(rgb, g=2.2, master=1.0, min_lit=3, raw=False):
    """Shared hardware/benchmark conversion, including a smooth visibility toe.

    The toe follows the LED's color ratios and fades to zero. It cannot turn a
    zero master into a nonzero output or lift absent channels into white.
    """
    value = np.clip(np.nan_to_num(np.asarray(rgb), nan=0., posinf=1., neginf=0.)
                    * np.clip(master, 0, 1), 0, 1)
    if raw:
        return (value * 255 + .5).astype(np.uint8)
    peak = value.max(axis=-1, keepdims=True)
    toe = np.clip(peak / .08, 0, 1)
    toe = toe * toe * (3 - 2 * toe)
    floor = np.clip(min_lit, 0, 20) * toe * value / np.maximum(peak, 1e-9)
    output = gamma(value, max(.1, g)) * (255 - np.clip(min_lit, 0, 20)) + floor
    return np.clip(output + .5, 0, 255).astype(np.uint8)


def gamma(rgb: np.ndarray, g: float = 2.2) -> np.ndarray:
    """LEDs are linear, eyes are not. Without this, low end looks blown out."""
    return np.power(np.clip(rgb, 0.0, 1.0), g)


# --------------------------------------------------------------- palettes --

PALETTES = {
    "sunset":  [0.02, 0.08, 0.95, 0.75],
    "cyanmag": [0.50, 0.83, 0.58, 0.90],
    "acid":    [0.28, 0.16, 0.45, 0.36],
    "ice":     [0.55, 0.60, 0.48, 0.70],
}

# Two-tone pairs as (base hue, separation, accent offset) rather than two fixed
# hues. Storing the *relationship* instead of the colours is what lets the base
# drift right around the wheel while the pair stays as legible as it started --
# fixed hues are why a rig ends up looking like the same red and blue forever.
# Separations are kept wide: adjacent hues read as one muddy colour on a 6-LED
# ring.
DUOS = {
    "ember":   (0.03, 0.55, 0.07),   # warm / cool, gold accent
    "toxic":   (0.28, 0.52, 0.88),   # green / violet, magenta accent
    "vapor":   (0.86, 0.66, 0.09),   # magenta / cyan, warm accent
    "cobalt":  (0.60, 0.51, 0.90),   # blue / amber, rose accent
    "mono":    (0.00, 0.03, 0.05),   # near-single hue, brightness only
}


def palette_hue(name: str, t: float) -> float:
    """Walk a palette continuously; t in turns."""
    p = PALETTES[name]
    n = len(p)
    x = (t * n) % n
    i = int(x)
    a, b = p[i], p[(i + 1) % n]
    d = ((b - a + 0.5) % 1.0) - 0.5          # shortest way round the wheel
    return (a + d * (x - i)) % 1.0
