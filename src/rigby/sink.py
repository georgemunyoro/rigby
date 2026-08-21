"""OpenRGB output stage.

Per-device tick rates are the whole point of this module. DRAM and GPU sit on
SMBus/i2c; driving them at 60fps makes the bus the bottleneck and stutters
everything else. They get ~12fps and a dirty check, the HID devices get the
full rate.
"""

from __future__ import annotations

import time

import numpy as np
from openrgb import OpenRGBClient
from openrgb.utils import RGBColor

from openrgb.utils import ModeFlags

from .fx import gamma
from .patch import Fixture, resolve

FAST_HZ = 60.0
SLOW_HZ = 12.0

# With gamma 2.2 on 8-bit LEDs, every perceptual value below ~0.11 quantises to
# zero -- so quiet passages don't dim, they switch off. Any non-zero intent gets
# at least this raw value so the bottom of the range stays visible.
MIN_LIT = 3


class Sink:
    def __init__(self, host: str = "127.0.0.1", port: int = 6742,
                 gamma_val: float = 2.2, master: float = 1.0,
                 swap_headers: bool = False, raw: bool = False,
                 fan_mode: str = "mirrored"):
        self.client = OpenRGBClient(host, port, "rigby")
        self.fixtures, self.missing = resolve(self.client.devices,
                                              swap_headers=swap_headers,
                                              fan_mode=fan_mode)
        self.gamma = gamma_val
        self.master = master
        # Playground mode writes what you picked. Gamma is right for shaping an
        # effect's brightness, but when hand-setting a colour it just means the
        # LED doesn't match the swatch.
        self.raw = raw

        # dev_idx -> next allowed write time / last frame written
        self._next: dict[int, float] = {}
        self._last: dict[int, np.ndarray] = {}

        self._dev_fixtures: dict[int, list[Fixture]] = {}
        for f in self.fixtures.values():
            self._dev_fixtures.setdefault(f.dev_idx, []).append(f)

    @staticmethod
    def _sanitize(mode) -> None:
        """Clamp mode fields into their own declared ranges.

        Some drivers report values outside the range they advertise -- the
        Palit GPU says brightness=255 with a range of 0..100 -- and
        openrgb-python's client-side validator refuses to pack that. Clamping
        is harmless for well-behaved devices and unbreaks the rest.
        """
        flags = ModeFlags(int(mode.flags))
        if ModeFlags.HAS_BRIGHTNESS in flags:
            lo, hi = mode.brightness_min, mode.brightness_max
            if lo is not None and hi is not None:
                lo, hi = min(lo, hi), max(lo, hi)
                cur = hi if mode.brightness is None else mode.brightness
                mode.brightness = max(lo, min(hi, cur))
        if ModeFlags.HAS_SPEED in flags:
            lo, hi = mode.speed_min, mode.speed_max
            if lo is not None and hi is not None:
                lo, hi = min(lo, hi), max(lo, hi)
                cur = (lo + hi) // 2 if mode.speed is None else mode.speed
                mode.speed = max(lo, min(hi, cur))

    def set_direct(self) -> None:
        """Direct mode = host drives every frame. Without it the device keeps
        running its own firmware effect and ignores you."""
        for dev_idx in self._dev_fixtures:
            dev = self.client.devices[dev_idx]
            names = [m.name for m in dev.modes]
            for want in ("Direct", "Custom", "Static"):
                if want not in names:
                    continue
                idx = names.index(want)
                if dev.active_mode != idx:
                    self._sanitize(dev.modes[idx])
                    try:
                        dev.set_mode(want)
                    except Exception as e:
                        print(f"  warn: {dev.name}: could not set {want} mode ({e})")
                break

    def describe(self) -> str:
        lines = []
        for name, f in self.fixtures.items():
            dev = self.client.devices[f.dev_idx]
            rep = f" x{f.mirror}" if f.mirror > 1 else "   "
            lines.append(f"  {name:<8} {f.n:>4} led {f.kind:<4}{rep} "
                         f"{'i2c ' + str(int(SLOW_HZ)) + 'fps' if f.slow else 'hid ' + str(int(FAST_HZ)) + 'fps'}"
                         f"   [{dev.name} / {dev.zones[f.zone_idx].name}]")
        if self.missing:
            lines.append(f"  (not present: {', '.join(self.missing)})")
        return "\n".join(lines)

    def write(self, frame: dict[str, np.ndarray]) -> None:
        """frame maps fixture name -> (n,3) float RGB in 0..1."""
        now = time.monotonic()

        for dev_idx, fixtures in self._dev_fixtures.items():
            slow = fixtures[0].slow
            if now < self._next.get(dev_idx, 0.0):
                continue

            dev = self.client.devices[dev_idx]
            buf = np.zeros((len(dev.leds), 3), dtype=np.float32)

            for f in fixtures:
                rgb = frame.get(f.name)
                if rgb is None:
                    continue
                block = rgb[: f.n]
                if f.mirror > 1:
                    # Write every repeat, so it works whether the hub mirrors
                    # or the chain is simply shorter than it claims.
                    block = np.tile(block, (f.mirror, 1))
                end = min(f.offset + len(block), len(buf))
                buf[f.offset:end] = block[: end - f.offset]

            if self.raw:
                out = np.clip(buf * self.master * 255.0 + 0.5,
                              0, 255).astype(np.uint8)
            else:
                lit = buf > 1e-4
                buf = gamma(buf * self.master, self.gamma)
                scaled = buf * (255.0 - MIN_LIT) + MIN_LIT
                out = np.clip(np.where(lit, scaled, 0.0) + 0.5,
                              0, 255).astype(np.uint8)

            prev = self._last.get(dev_idx)
            if prev is not None and np.array_equal(prev, out):
                # Nothing changed -- don't spend bus time saying so.
                self._next[dev_idx] = now + 1.0 / (SLOW_HZ if slow else FAST_HZ)
                continue

            try:
                dev.set_colors([RGBColor(int(r), int(g), int(b)) for r, g, b in out],
                               fast=True)
            except Exception:
                # A device can vanish mid-show (unplugged keyboard). Back off
                # rather than taking the whole rig down.
                self._next[dev_idx] = now + 2.0
                continue

            self._last[dev_idx] = out
            self._next[dev_idx] = now + 1.0 / (SLOW_HZ if slow else FAST_HZ)

    def blackout(self) -> None:
        black = {n: np.zeros((f.n, 3), dtype=np.float32)
                 for n, f in self.fixtures.items()}
        self._last.clear()
        self._next.clear()
        self.write(black)
        time.sleep(0.15)
        self._last.clear()
        self._next.clear()
        self.write(black)
