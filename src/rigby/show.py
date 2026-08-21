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

        for key in ("truss_l", "truss_r", "kbd"):
            fix = self.fixtures.get(key)
            if fix is None:
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


LOOKS = {c.name: c for c in (Spectrum, Chase)}
