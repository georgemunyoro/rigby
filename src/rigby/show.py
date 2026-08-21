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

    # A full turn of the wheel in ~5 minutes: slow enough that you never catch
    # it moving, fast enough that a track doesn't end on the colour it started.
    HUE_DRIFT = 1.0 / 300.0
    SEP_DRIFT = 1.0 / 170.0

    def __init__(self, fixtures: dict[str, Fixture], palette: str = "sunset",
                 gain: float = 1.6, curve: float = 0.45, duo: str = "ember",
                 hue_drift: float = 1.0):
        self.fixtures = fixtures
        self.palette = palette
        self.gain = gain
        self.curve = curve
        self.duo = duo
        self._h_base, self._sep, self._acc = fx.DUOS.get(duo, fx.DUOS["ember"])
        self._hue_t = 0.0
        self._sep_t = 0.0
        self._drift = hue_drift
        self.t = 0.0          # global time in turns
        self.chase = 0.0      # chase phase in turns
        self._hit = 0.0       # onset flash envelope
        self._lvl = 0.0       # heavily smoothed level, for chase rate

    def tones(self) -> tuple[float, float, float]:
        """Current (primary, secondary, accent) hues.

        The separation breathes as well as the base drifting, so the pair keeps
        changing character -- near-complementary at one moment, a tighter
        analogous pair a minute later -- without ever collapsing together.
        """
        base = (self._h_base + self._hue_t) % 1.0
        wob = 0.5 + 0.5 * np.sin(2 * np.pi * self._sep_t)
        sep = self._sep * (0.78 + 0.34 * wob)
        return base, (base + sep) % 1.0, (base + self._acc) % 1.0

    def step(self, dt: float, f: Features) -> None:
        # Rate rides on energy -- but on a *heavily* smoothed level. Driving it
        # from the raw level made the chase speed jitter frame to frame, which
        # reads as judder rather than movement.
        self._lvl += (f.level - self._lvl) * 0.04
        self._hue_t += dt * self.HUE_DRIFT * self._drift
        self._sep_t += dt * self.SEP_DRIFT * self._drift
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


class _Gesture:
    """One way for a ring to move.

    A single behaviour plus a direction flip is still a single behaviour -- it
    reads as "swirling back and forth" and stops being interesting after about
    a minute. What keeps a rig alive is a vocabulary of gestures and something
    that changes between them on musical structure, the way an operator would
    change look at a phrase boundary rather than continuously.
    """

    NAMES = ("spin", "lobes", "pingpong", "breathe", "wipe", "sparkle",
             "converge")

    def __init__(self, name: str, rng: np.random.Generator, n_rings: int):
        self.name = name
        self.rate = float(rng.uniform(0.6, 1.5))
        self.width = float(rng.uniform(0.13, 0.24))
        self.lobes = int(rng.integers(2, 4))
        self.dir = 1.0 if rng.random() < 0.5 else -1.0
        self.spread = float(rng.choice([0.0, 1.0 / max(n_rings, 1), 0.5]))
        self._seed = int(rng.integers(0, 1 << 30))

    def field(self, ang, phase, t, idx, lo, hi):
        """Returns (tone0, tone1) intensity across the ring."""
        ph = phase * self.rate * self.dir + idx * self.spread
        w = self.width

        if self.name == "spin":
            return (fx.arc(ang, ph, w, 1.3) * (0.35 + 0.65 * lo),
                    fx.arc(ang, ph + 0.5, w * 0.85, 1.3) * (0.25 + 0.6 * hi))

        if self.name == "lobes":
            a = np.zeros_like(ang)
            b = np.zeros_like(ang)
            for k in range(self.lobes):
                c = ph + k / self.lobes
                a = np.maximum(a, fx.arc(ang, c, w * 0.8, 1.4))
                b = np.maximum(b, fx.arc(ang, c + 0.5 / self.lobes, w * 0.55, 1.4))
            return a * (0.3 + 0.7 * lo), b * (0.2 + 0.6 * hi)

        if self.name == "pingpong":
            # Sweeps a limited arc instead of going all the way round.
            c = 0.25 * np.sin(2 * np.pi * ph)
            return (fx.arc(ang, c, w, 1.2) * (0.35 + 0.65 * lo),
                    fx.arc(ang, -c, w * 0.8, 1.2) * (0.25 + 0.6 * hi))

        if self.name == "breathe":
            # No rotation at all: the ring holds two static halves and pulses.
            g = 0.5 + 0.5 * np.sin(2 * np.pi * ph)
            return (fx.half(ang, 0.0, 0.16) * (0.25 + 0.75 * lo) * g,
                    fx.half(ang, 0.5, 0.16) * (0.2 + 0.7 * hi) * (1.0 - 0.6 * g))

        if self.name == "wipe":
            # A filled arc grows from a point, then snaps back and refills.
            grow = (ph % 1.0)
            d = np.abs(fx.wrap_delta(ang, 0.0))
            m = np.clip((grow * 0.5 - d) * 8.0, 0.0, 1.0)
            return (m * (0.3 + 0.7 * lo),
                    (1.0 - m) * (0.15 + 0.5 * hi))

        if self.name == "sparkle":
            # Deterministic per-LED twinkle: no particles, just phase offsets.
            off = ((np.arange(ang.size) * 2654435761 + self._seed) % 1000) / 1000.0
            v = 0.5 + 0.5 * np.sin(2 * np.pi * (ph * 2.0 + off))
            v = np.clip(v, 0, 1) ** 3.0
            return v * (0.3 + 0.7 * lo), (1.0 - v) * 0.5 * (0.2 + 0.6 * hi)

        # converge: two arcs walk toward each other and apart again
        c = 0.25 * (0.5 + 0.5 * np.sin(2 * np.pi * ph))
        return (fx.arc(ang, c, w, 1.3) * (0.35 + 0.65 * lo),
                fx.arc(ang, -c, w, 1.3) * (0.3 + 0.6 * hi))


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
                 duo="ember", hue_drift=1.0):
        super().__init__(fixtures, palette=palette, gain=gain, curve=curve,
                         duo=duo, hue_drift=hue_drift)
        self.spin = 0.0
        self.dir = 1.0
        self.beat = 0
        self._flash = 0.0
        self._flash_key = None
        self._flash_centre = 0.0
        self._rings = [k for k, f in fixtures.items() if f.is_ring]
        self._rng = np.random.default_rng(20260821)
        self._g = _Gesture("spin", self._rng, len(self._rings))
        self._g_prev = self._g
        self._xf = 1.0          # crossfade into the current gesture
        self._since_change = 0.0

    PHRASE_BEATS = 16     # change gesture on a phrase boundary...
    IDLE_CHANGE_S = 14.0  # ...or on a timer when there is no beat to count
    XFADE_S = 0.7

    def _new_gesture(self) -> None:
        choices = [g for g in _Gesture.NAMES if g != self._g.name]
        name = str(self._rng.choice(choices))
        self._g_prev, self._g = self._g, _Gesture(name, self._rng,
                                                  len(self._rings))
        self._xf = 0.0
        self._since_change = 0.0

    def step(self, dt: float, f: Features) -> None:
        super().step(dt, f)
        self.spin += dt * (0.10 + self._lvl * 0.42) * self.dir
        self._since_change += dt
        self._xf = min(1.0, self._xf + dt / self.XFADE_S)

        # On beaty material change at a phrase boundary; with no usable beat,
        # fall back to a timer so it still evolves.
        if f.pulse < 0.35 and self._since_change > self.IDLE_CHANGE_S:
            self._new_gesture()

        if f.onset:
            self.beat += 1
            if self.beat % self.PHRASE_BEATS == 0:
                self._new_gesture()
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

        a1, a2 = self._g.field(ang, phase, self._since_change, idx, lo, hi)
        if self._xf < 1.0:
            p1, p2 = self._g_prev.field(ang, phase, self._since_change, idx,
                                        lo, hi)
            a1 = a1 * self._xf + p1 * (1.0 - self._xf)
            a2 = a2 * self._xf + p2 * (1.0 - self._xf)

        h1, h2, hacc = self.tones()
        rgb = (fx.hsv(h1, 1.0, self.drive(a1)) +
               fx.hsv(h2, 0.95, self.drive(a2)))

        if self._flash > 0.01 and key == self._flash_key:
            m = fx.half(ang, self._flash_centre) * self._flash
            rgb = rgb + fx.hsv(hacc, 0.45, m * self._hit_scale(f))

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
        h1, h2, hacc = self.tones()
        v = self.drive(np.clip(0.14 + wash * 0.9, 0, 1))
        rgb = (fx.hsv(h1, 1.0, v * (1.0 - mix)) +
               fx.hsv(h2, 0.95, v * mix))
        if self._flash > 0.01:
            rgb = rgb + fx.hsv(hacc, 0.5,
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

    ATTACK_S = 0.13   # a drop that appears at full brightness in one frame
    TAU_S = 0.62      # is a pop, not a raindrop
    CULL_S = 2.6

    def __init__(self, n: int, seed: int, wrap: bool):
        self.n = n
        self.wrap = wrap
        self.rng = np.random.default_rng(seed)
        self.pos = np.zeros(0, dtype=np.float32)
        self.amp = np.zeros(0, dtype=np.float32)
        self.age = np.zeros(0, dtype=np.float32)
        self.hjit = np.zeros(0, dtype=np.float32)
        self.tone = np.zeros(0, dtype=np.int8)
        self._idx = np.arange(n, dtype=np.float32)
        self._next = 0.0
        # Peak of the attack/decay envelope, so `amp` still means peak height.
        a = np.linspace(0.0, self.CULL_S, 512)
        self._norm = float(((1 - np.exp(-a / self.ATTACK_S))
                            * np.exp(-a / self.TAU_S)).max())

    def _env(self, age):
        return ((1.0 - np.exp(-age / self.ATTACK_S))
                * np.exp(-age / self.TAU_S)) / self._norm

    def spawn(self, amp: float, tone: int | None = None) -> None:
        self.pos = np.append(self.pos, self.rng.random() * self.n)
        self.amp = np.append(self.amp, amp)
        self.age = np.append(self.age, 0.0)
        # A little hue wander per drop, so a shower isn't one flat colour.
        self.hjit = np.append(self.hjit, self.rng.normal(0.0, 0.028))
        t = self.rng.integers(0, 2) if tone is None else tone
        self.tone = np.append(self.tone, np.int8(t))

    def step(self, dt: float, rate: float, amp: float) -> None:
        # Exponential inter-arrival times -- real Poisson scatter. A fractional
        # carry spawns at exactly even spacing, which at low rates reads as a
        # metronome rather than as rain.
        self._next -= dt * max(rate, 1e-6)
        while self._next <= 0.0:
            self._next += float(self.rng.exponential(1.0))
            self.spawn(amp * float(self.rng.uniform(0.55, 1.0)))
        self.age = self.age + dt
        keep = self.age < self.CULL_S
        self.pos, self.amp, self.age, self.tone, self.hjit = (
            self.pos[keep], self.amp[keep], self.age[keep],
            self.tone[keep], self.hjit[keep])

    def render(self, width: float = 0.85):
        """Returns (tone0, tone1, hue_offset) fields."""
        out = [np.zeros(self.n, dtype=np.float32),
               np.zeros(self.n, dtype=np.float32)]
        hue = np.zeros(self.n, dtype=np.float32)
        if self.pos.size:
            d = np.abs(self._idx[None, :] - self.pos[:, None])
            if self.wrap:
                d = np.minimum(d, self.n - d)
            g = (np.exp(-(d ** 2) / (2.0 * width ** 2))
                 * (self.amp * self._env(self.age))[:, None])
            for t in (0, 1):
                m = self.tone == t
                if m.any():
                    out[t] = g[m].sum(axis=0).astype(np.float32)
            tot = g.sum(axis=0)
            hue = np.where(tot > 1e-6,
                           (g * self.hjit[:, None]).sum(axis=0) / np.maximum(tot, 1e-6),
                           0.0).astype(np.float32)
        return out[0], out[1], hue


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
                 duo="ember", hue_drift=1.0):
        super().__init__(fixtures, palette=palette, gain=gain, curve=curve,
                         duo=duo, hue_drift=hue_drift)
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
            d.step(dt, r, amp)
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
        h1, h2, _ = self.tones()
        for key, fixt in self.fixtures.items():
            # Wider blobs on the coarse 6-LED fan rings, or a drop is just one
            # LED blinking on and off.
            w = 0.7 if fixt.n >= 12 else 1.05
            t0, t1, hj = self.drops[key].render(width=w)
            v0 = self.drive(np.clip(t0 + ambient, 0, 1))
            v1 = self.drive(np.clip(t1 + ambient * 0.6, 0, 1))
            rgb = (fx.hsv((h1 + hj) % 1.0, 0.95, v0) +
                   fx.hsv((h2 + hj) % 1.0, 0.90, v1))
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
                 duo="ember", hue_drift=1.0):
        super().__init__(fixtures, palette=palette, gain=gain, curve=curve,
                         duo=duo, hue_drift=hue_drift)
        self.beaty = Duotone(fixtures, palette, gain, curve, duo, hue_drift)
        self.calm = Rain(fixtures, palette, gain, curve, duo, hue_drift)
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
