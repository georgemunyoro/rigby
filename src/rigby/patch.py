"""Fixture patch.

Effects address *fixtures* (named groups with geometry), never raw device or LED
indices. That way replugging hardware, or OpenRGB reordering its device list,
doesn't rewrite a single effect.

Fixtures are resolved against the live device list at startup. Anything that
isn't plugged in right now is simply skipped.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class Fixture:
    name: str
    dev_idx: int          # index into OpenRGBClient.devices
    zone_idx: int         # index into device.zones
    n: int                # LED count
    offset: int           # first LED index within the *device's* flat LED list
    pos: np.ndarray       # (n,) normalised 0..1 position along the fixture
    slow: bool            # True => i2c/SMBus, must be rate-limited hard
    reverse: bool = False # flip `pos` so mirrored pairs sweep outward together


# (fixture, device-name substring, zone index, which match, slow)
#
# `slow` is the important column. DRAM and GPU sit on SMBus/i2c at ~100kHz and
# will stutter the whole show if driven at video rates -- they're wash fixtures.
# The Aura headers and keyboard are on HID and can take 60fps.
PATCH_SPEC: list[tuple[str, str, int, int, bool]] = [
    ("mobo",    "ASUS TUF", 0, 0, False),   # 4  - board glow
    ("truss_l", "ASUS TUF", 1, 0, False),   # 18 - ARGB header 1
    ("truss_r", "ASUS TUF", 2, 0, False),   # 18 - ARGB header 2
    ("ram_a",   "ENE DRAM", 0, 0, True),    # 8
    ("ram_b",   "ENE DRAM", 0, 1, True),    # 8  - second DIMM
    ("gpu",     "GeForce",  0, 0, True),    # 1
    ("kbd",     "EVision",  0, 0, False),   # 126 - per-key, when plugged in
]

REVERSED = {"truss_r"}


def resolve(devices) -> tuple[dict[str, Fixture], list[str]]:
    """Map the spec onto live devices. Returns (fixtures, missing_names)."""
    fixtures: dict[str, Fixture] = {}
    missing: list[str] = []

    for name, match, zone_idx, occurrence, slow in PATCH_SPEC:
        hits = [i for i, d in enumerate(devices) if match.lower() in d.name.lower()]
        if occurrence >= len(hits):
            missing.append(name)
            continue

        dev_idx = hits[occurrence]
        zones = devices[dev_idx].zones
        if zone_idx >= len(zones):
            missing.append(name)
            continue

        n = len(zones[zone_idx].leds)
        if n == 0:
            missing.append(name)
            continue

        # openrgb-python 0.3.x exposes no zone.start_idx -- zones are laid out
        # contiguously in the device's flat LED list, so sum what precedes.
        offset = sum(len(z.leds) for z in zones[:zone_idx])

        pos = np.linspace(0.0, 1.0, n) if n > 1 else np.array([0.5])
        if name in REVERSED:
            pos = pos[::-1].copy()

        fixtures[name] = Fixture(name, dev_idx, zone_idx, n, offset, pos, slow,
                                 reverse=name in REVERSED)

    return fixtures, missing
