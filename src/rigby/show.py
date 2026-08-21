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
                 gain: float = 1.0, curve: float = 0.55):
        self.fixtures = fixtures
        self.palette = palette
        self.gain = gain
        self.curve = curve
        self.t = 0.0          # global time in turns
        self.chase = 0.0      # chase phase in turns
        self._hit = 0.0       # onset flash envelope

    def step(self, dt: float, f: Features) -> None:
        # Rate rides on energy -- the show speeds up when the track does. On a
        # desk you'd tap-tempo this instead; see the timecode note in README.
        self.t += dt * 0.05
        self.chase += dt * (0.25 + f.level * 1.6)
        self._hit = max(self._hit * 0.82, 1.0 if f.onset else 0.0)

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

            # Map each LED's position onto a band, then interpolate so the
            # spectrum reads as a continuous ribbon rather than 8 blocks.
            b = np.interp(fix.pos, np.linspace(0, 1, nb), f.bands)

            # Movement layer: a travelling bump so it never sits still.
            mv = fx.wave("bump", self.chase, fix.pos, spread=1.0, size=1.0)

            v = fx.htp(b * 0.95, mv * 0.35 * (0.3 + f.level))
            v = self.drive(v)
            v = np.maximum(v, self._hit * 0.9)

            hue = (fx.palette_hue(self.palette, self.t) + fix.pos * 0.22) % 1.0
            sat = np.clip(1.0 - self._hit * 0.85, 0.0, 1.0)
            out[key] = fx.hsv(hue, sat, v)

        if (fix := self.fixtures.get("mobo")) is not None:
            v = self.drive(np.full(fix.n, 0.18 + f.level * 0.82))
            v = np.maximum(v, self._hit)
            hue = fx.palette_hue(self.palette, self.t + 0.25)
            out["mobo"] = fx.hsv(hue, 1.0 - self._hit * 0.8, v)

        # RAM sits on SMBus at 12fps -- give it slow bass wash, not detail.
        for key in ("ram_a", "ram_b"):
            if (fix := self.fixtures.get(key)) is None:
                continue
            sweep = fx.wave("sine", self.chase * 0.5, fix.pos, spread=0.5, size=1.0)
            v = self.drive(np.clip(0.10 + f.bass * 0.90 * (0.55 + sweep * 0.45), 0, 1))
            hue = fx.palette_hue(self.palette, self.t + (0.0 if key == "ram_a" else 0.08))
            out[key] = fx.hsv(hue, 0.95, v)

        if (fix := self.fixtures.get("gpu")) is not None:
            hue = fx.palette_hue(self.palette, self.t + 0.5)
            out["gpu"] = fx.hsv(hue, 0.9,
                                self.drive(np.full(1, 0.12 + f.level * 0.88)))

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
