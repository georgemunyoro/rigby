"""Looks: the layered thing an operator actually builds.

A look is base wash + movement + hits, merged HTP, exactly like stacking
playbacks on a desk. Add new ones by writing a function and registering it in
LOOKS -- they all get the same (fixtures, features, phase) contract.
"""

from __future__ import annotations

import numpy as np

from . import fx
from .analyze import Features
from .patch import Fixture


class Look:
    """Base class. Subclasses fill in render()."""

    name = "look"

    def __init__(self, fixtures: dict[str, Fixture], palette: str = "sunset",
                 gain: float = 1.6, curve: float = 0.45):
        self.fixtures = fixtures
        self.palette = palette
        self.gain = gain
        self.curve = curve
        self.t = 0.0          # global time in turns
        self.chase = 0.0      # chase phase in turns
        self._hit = 0.0       # onset flash envelope
        self._lvl = 0.0       # heavily smoothed level, for chase rate

    def step(self, dt: float, f: Features) -> None:
        # Rate rides on energy -- but on a *heavily* smoothed level. Driving it
        # from the raw level made the chase speed jitter frame to frame, which
        # reads as judder rather than movement.
        self._lvl += (f.level - self._lvl) * 0.04
        self.t += dt * 0.05
        self.chase += dt * (0.22 + self._lvl * 1.1)
        self._hit = max(self._hit * 0.82, 1.0 if f.onset else 0.0)

    @staticmethod
    def _hit_scale(f: Features) -> float:
        """Flashes still land in quiet passages, just not at full power."""
        return 0.35 + 0.65 * f.dynamics

    def drive(self, x):
        """Every look pushes its brightness through here before hsv()."""
        return fx.drive(x, self.gain, self.curve)

    def render(self, f: Features) -> dict[str, np.ndarray]:
        raise NotImplementedError


class Spectrum(Look):
    """Bands spread along the trusses, mirrored; bass in the RAM; hits flash white."""

    name = "spectrum"

    def render(self, f: Features) -> dict[str, np.ndarray]:
        out: dict[str, np.ndarray] = {}
        nb = len(f.bands)

        for key, fix in self.fixtures.items():
            if fix.slow or key == "mobo":
                continue

            # The wash comes off the SLOW envelope and is smeared across
            # neighbouring LEDs; the fast envelope only adds a little accent on
            # top. Driving the wash from the fast envelope is what made the
            # between-beat picture look haywire.
            # The slow envelope lags transients and so reads lower than the
            # fast one; lift it back up rather than losing overall brightness.
            b = np.interp(fix.pos, np.linspace(0, 1, nb), f.bands_slow)
            b = np.clip(fx.blur(b, 2) * 1.35, 0.0, 1.0)
            accent = np.interp(fix.pos, np.linspace(0, 1, nb), f.bands)
            accent = fx.blur(accent, 1)

            # Movement layer: a travelling bump so it never sits still.
            mv = fx.wave("bump", self.chase, fix.pos, spread=1.0, size=1.0)

            v = fx.htp(b, accent * 0.45, mv * 0.30 * (0.3 + self._lvl))
            v = self.drive(v) * f.dynamics
            v = np.maximum(v, self._hit * 0.9 * self._hit_scale(f))

            hue = (fx.palette_hue(self.palette, self.t) + fix.pos * 0.22) % 1.0
            sat = np.clip(1.0 - self._hit * 0.85, 0.0, 1.0)
            out[key] = fx.hsv(hue, sat, v)

        if (fix := self.fixtures.get("mobo")) is not None:
            v = self.drive(np.full(fix.n, 0.18 + f.level * 0.82)) * f.dynamics
            v = np.maximum(v, self._hit * self._hit_scale(f))
            hue = fx.palette_hue(self.palette, self.t + 0.25)
            out["mobo"] = fx.hsv(hue, 1.0 - self._hit * 0.8, v)

        # RAM sits on SMBus at 12fps -- give it slow bass wash, not detail.
        for key in ("ram_a", "ram_b"):
            if (fix := self.fixtures.get(key)) is None:
                continue
            sweep = fx.wave("sine", self.chase * 0.5, fix.pos, spread=0.5, size=1.0)
            v = self.drive(np.clip(0.10 + f.bass * 0.90 * (0.55 + sweep * 0.45),
                                   0, 1)) * f.dynamics
            hue = fx.palette_hue(self.palette, self.t + (0.0 if key == "ram_a" else 0.08))
            out[key] = fx.hsv(hue, 0.95, v)

        if (fix := self.fixtures.get("gpu")) is not None:
            hue = fx.palette_hue(self.palette, self.t + 0.5)
            out["gpu"] = fx.hsv(hue, 0.9,
                                self.drive(np.full(1, 0.12 + f.level * 0.88))
                                * f.dynamics)

        return out


class Chase(Look):
    """No spectrum -- pure movement. Useful for checking the rig with no audio."""

    name = "chase"

    def render(self, f: Features) -> dict[str, np.ndarray]:
        out: dict[str, np.ndarray] = {}
        for key, fix in self.fixtures.items():
            v = fx.wave("bump", self.chase, fix.pos, spread=1.0, size=1.0)
            v = np.maximum(v, 0.06)
            hue = (fx.palette_hue(self.palette, self.t) + fix.pos * 0.3) % 1.0
            out[key] = fx.hsv(hue, 1.0, v)
        return out


class Duotone(Look):
    """Two rotating tones per ring, with beats as events rather than flashes.

    The point of this look is that the movement is *intrinsic* -- the rings
    rotate whether or not anything is playing, and the music modulates that
    rotation. Amplitude-drives-brightness is what makes a rig read as a
    visualiser; phase relationships between fixtures are what make it read as
    a lighting rig.

    Three things carry that here:
      * counter-rotation -- fan_b and the AIO spin against the others, so three
        identical rings stop reading as one object,
      * a phase spread of a third of a turn per fan, straight off a desk,
      * beats land on *one* ring's *half*, cycling round, while everything else
        keeps running. A global flash is the thing that gets boring fastest.
    """

    name = "duotone"

    def __init__(self, fixtures, palette="sunset", gain=1.6, curve=0.45,
                 duo="ember"):
        super().__init__(fixtures, palette=palette, gain=gain, curve=curve)
        self.h1, self.h2, self.hacc = fx.DUOS.get(duo, fx.DUOS["ember"])
        self.duo = duo
        self.spin = 0.0
        self.dir = 1.0
        self.beat = 0
        self._flash = 0.0
        self._flash_key = None
        self._flash_centre = 0.0
        self._rings = [k for k, f in fixtures.items() if f.is_ring]

    def step(self, dt: float, f: Features) -> None:
        super().step(dt, f)
        self.spin += dt * (0.10 + self._lvl * 0.42) * self.dir

        if f.onset:
            self.beat += 1
            if self._rings:
                # Cycle which ring takes the hit, and alternate which half.
                self._flash_key = self._rings[self.beat % len(self._rings)]
                self._flash_centre = 0.0 if self.beat % 2 else 0.5
            self._flash = 1.0
            # Structure: every fourth beat the whole rig reverses. Small change,
            # but it's what stops a loop of four bars looking like one bar.
            if self.beat % 4 == 0:
                self.dir *= -1.0
        self._flash = max(self._flash * 0.80, 0.0)

    def _ring_rgb(self, key, fix, f) -> np.ndarray:
        ang = fix.angle
        idx = self._rings.index(key) if key in self._rings else 0
        phase = self.spin * fix.spin + idx / max(len(self._rings), 1)

        nb = len(f.bands_slow)
        # Each ring listens to a different part of the spectrum, so they move
        # independently instead of pumping in unison.
        lo = float(f.bands_slow[min(idx, nb - 1)])
        hi = float(f.bands_slow[min(nb - 1 - idx, nb - 1)])

        w1 = 0.16 + 0.16 * lo
        w2 = 0.13 + 0.13 * hi
        a1 = fx.arc(ang, phase, w1, softness=1.3) * (0.35 + 0.65 * lo)
        a2 = fx.arc(ang, phase + 0.5, w2, softness=1.3) * (0.25 + 0.60 * hi)

        rgb = (fx.hsv(self.h1, 1.0, self.drive(a1)) +
               fx.hsv(self.h2, 0.95, self.drive(a2)))

        if self._flash > 0.01 and key == self._flash_key:
            m = fx.half(ang, self._flash_centre) * self._flash
            rgb = rgb + fx.hsv(self.hacc, 0.45, m * self._hit_scale(f))

        return np.clip(rgb * f.dynamics, 0.0, 1.0)

    def _line_rgb(self, key, fix, f) -> np.ndarray:
        # Lines get the same two tones split along their length, with a slow
        # travelling seam so they aren't static blocks.
        seam = (self.spin * 0.5) % 1.0
        d = np.abs(fx.wrap_delta(fix.pos * 0.5, seam))
        mix = np.clip(d * 4.0, 0.0, 1.0)
        wash = np.interp(fix.pos, np.linspace(0, 1, len(f.bands_slow)),
                         f.bands_slow)
        wash = fx.blur(wash, 1) if fix.n > 3 else wash
        v = self.drive(np.clip(0.14 + wash * 0.9, 0, 1))
        rgb = (fx.hsv(self.h1, 1.0, v * (1.0 - mix)) +
               fx.hsv(self.h2, 0.95, v * mix))
        if self._flash > 0.01:
            rgb = rgb + fx.hsv(self.hacc, 0.5,
                               np.full(fix.n, self._flash * 0.35
                                       * self._hit_scale(f)))
        return np.clip(rgb * f.dynamics, 0.0, 1.0)

    def render(self, f: Features) -> dict[str, np.ndarray]:
        out = {}
        for key, fix in self.fixtures.items():
            out[key] = (self._ring_rgb(key, fix, f) if fix.is_ring
                        else self._line_rgb(key, fix, f))
        return out




class _Drops:
    """A small particle pool: sparse points that appear and fade in place.

    Rain is the one pattern that reads as *intentional* when the music has no
    beat to hang anything off, because its structure comes from sparseness
    rather than from timing. Density and brightness are what respond, so a
    verse is a few scattered points and a chorus fills in.
    """

    def __init__(self, n: int, seed: int, wrap: bool):
        self.n = n
        self.wrap = wrap
        self.rng = np.random.default_rng(seed)
        self.pos = np.zeros(0, dtype=np.float32)
        self.amp = np.zeros(0, dtype=np.float32)
        self.tone = np.zeros(0, dtype=np.int8)
        self._idx = np.arange(n, dtype=np.float32)
        self._carry = 0.0

    def spawn(self, amp: float, tone: int | None = None) -> None:
        self.pos = np.append(self.pos, self.rng.random() * self.n)
        self.amp = np.append(self.amp, amp)
        t = self.rng.integers(0, 2) if tone is None else tone
        self.tone = np.append(self.tone, np.int8(t))

    def step(self, dt: float, rate: float, amp: float, decay: float = 2.4) -> None:
        # Fractional carry, so a rate below one drop per frame still produces
        # an even scatter instead of nothing.
        self._carry += rate * dt
        while self._carry >= 1.0:
            self._carry -= 1.0
            self.spawn(amp * float(self.rng.uniform(0.55, 1.0)))
        self.amp = self.amp * float(np.exp(-decay * dt))
        keep = self.amp > 0.015
        self.pos, self.amp, self.tone = (self.pos[keep], self.amp[keep],
                                         self.tone[keep])

    def render(self, width: float = 0.85) -> tuple[np.ndarray, np.ndarray]:
        """Returns (tone0, tone1) intensity arrays."""
        out = [np.zeros(self.n, dtype=np.float32),
               np.zeros(self.n, dtype=np.float32)]
        if self.pos.size:
            d = np.abs(self._idx[None, :] - self.pos[:, None])
            if self.wrap:
                d = np.minimum(d, self.n - d)
            g = np.exp(-(d ** 2) / (2.0 * width ** 2)) * self.amp[:, None]
            for t in (0, 1):
                m = self.tone == t
                if m.any():
                    out[t] = g[m].sum(axis=0).astype(np.float32)
        return out[0], out[1]


class Rain(Look):
    """Pitter-patter that ramps into a wash -- for music with no usable beat.

    Slow melodic material defeats beat detection: the onset envelope has no
    periodicity to lock onto, so anything driven by beats either sits still or
    invents a pulse that isn't there. This look ignores beats almost entirely
    and is driven by `swell` -- sustained loudness -- so a quiet verse is a few
    scattered drops and a belted chorus fills the rig in.
    """

    name = "rain"

    BASE_RATE = 0.45     # drops/sec/fixture at rest
    PEAK_RATE = 16.0     # ...and at full swell
    # `swell` sits near 0.5 at a song's average level, so the rate curve has to
    # be shifted and steep: at mid-swell this must be a handful of scattered
    # points, not a wash. Without the shift a quiet verse lights every LED.
    SWELL_KNEE = 0.38
    SWELL_EXP = 2.2

    def __init__(self, fixtures, palette="sunset", gain=1.6, curve=0.45,
                 duo="ember"):
        super().__init__(fixtures, palette=palette, gain=gain, curve=curve)
        self.h1, self.h2, self.hacc = fx.DUOS.get(duo, fx.DUOS["ember"])
        self.drops = {k: _Drops(f.n, seed=i * 977 + 13, wrap=f.is_ring)
                      for i, (k, f) in enumerate(fixtures.items())}
        self._sw = 0.0

    def step(self, dt: float, f: Features) -> None:
        super().step(dt, f)
        self._sw += (f.swell - self._sw) * 0.06
        s = self._eff(self._sw)
        rate = self.BASE_RATE + s * self.PEAK_RATE
        amp = 0.45 + 0.55 * s
        for key, d in self.drops.items():
            fixt = self.fixtures[key]
            r = rate * (0.45 if fixt.slow else 1.0)   # i2c fixtures stay calm
            d.step(dt, r, amp, decay=3.0)
            if f.onset:
                d.spawn(min(1.0, amp * 1.4), tone=2 % 2)

    def _eff(self, sw: float) -> float:
        """Swell shaped for density: flat until the music actually lifts."""
        x = (sw - self.SWELL_KNEE) / max(1.0 - self.SWELL_KNEE, 1e-6)
        return float(np.clip(x, 0.0, 1.0)) ** self.SWELL_EXP

    def render(self, f: Features) -> dict[str, np.ndarray]:
        out: dict[str, np.ndarray] = {}
        s = self._eff(self._sw)
        # As the swell rises the gaps fill in, so it crossfades from discrete
        # drops to a continuous glow without a mode change.
        ambient = (s ** 1.8) * 0.34
        for key, fixt in self.fixtures.items():
            t0, t1 = self.drops[key].render(width=0.7)
            v0 = self.drive(np.clip(t0 + ambient, 0, 1))
            v1 = self.drive(np.clip(t1 + ambient * 0.6, 0, 1))
            rgb = (fx.hsv(self.h1, 0.95, v0) + fx.hsv(self.h2, 0.90, v1))
            out[key] = np.clip(rgb * f.dynamics, 0.0, 1.0)
        return out


class Auto(Look):
    """Crossfade between beat-driven and swell-driven by beat confidence.

    Rather than classifying a track and switching, both looks run and `pulse`
    mixes them. A song that drifts in and out of having a beat drifts between
    the two, and nothing ever snaps.
    """

    name = "auto"

    def __init__(self, fixtures, palette="sunset", gain=1.6, curve=0.45,
                 duo="ember"):
        super().__init__(fixtures, palette=palette, gain=gain, curve=curve)
        self.beaty = Duotone(fixtures, palette, gain, curve, duo)
        self.calm = Rain(fixtures, palette, gain, curve, duo)
        self.mix = 0.0

    def step(self, dt: float, f: Features) -> None:
        self.beaty.step(dt, f)
        self.calm.step(dt, f)
        self.mix += (f.pulse - self.mix) * 0.03

    def render(self, f: Features) -> dict[str, np.ndarray]:
        w = float(np.clip(self.mix, 0.0, 1.0))
        a = self.beaty.render(f)
        b = self.calm.render(f)
        return {k: np.clip(a[k] * w + b[k] * (1.0 - w), 0.0, 1.0) for k in a}


LOOKS = {c.name: c for c in (Spectrum, Chase, Duotone, Rain, Auto)}
