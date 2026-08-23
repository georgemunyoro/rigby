"""Fixture patch and geometry.

Effects address *fixtures* -- named groups that know their own shape -- never
raw device or LED indices. A fan is not a strip: it is a ring, and it has an
angle, a spin direction and a centre. Effects that know that can rotate, split
a ring in half, or offset one fan against the next.

Nothing here hardcodes a device name. Every zone OpenRGB reports becomes a
fixture, and the config says how to carve the interesting ones up -- an
Adalight strip carrying seven fans is seven rings, and no amount of guessing
from a device name would work out that it is anything but a strip.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np

LINE = "line"
RING = "ring"

# i2c/SMBus devices must be rate-limited; everything else takes 60fps.
SLOW_TYPES = {"DRAM", "GPU", "MOTHERBOARD_SLOW"}


@dataclass
class Fixture:
    name: str
    dev_idx: int
    zone_idx: int
    n: int
    offset: int           # first LED index within the device's flat LED list
    pos: np.ndarray       # (n,) 0..1 along the fixture
    slow: bool            # i2c/SMBus -- must be rate-limited
    kind: str = LINE
    angle: np.ndarray | None = None   # (n,) 0..1 turns around a ring
    origin: tuple[float, float] = (0.5, 0.5)
    spin: float = 1.0     # +1 / -1, so neighbouring rings can counter-rotate
    reverse: bool = False
    matrix: list | None = None
    mirror: int = 1       # data repeated down the wire by a splitter hub

    @property
    def is_ring(self) -> bool:
        return self.kind == RING


# Friendly names for zones worth recognising. Everything else is named from
# the device and zone it came from, which is always at least unambiguous.
KNOWN = [
    (r"aura mainboard", "mobo"),
    (r"gpu", "gpu"),
    (r"dram", "ram"),
    (r"keyboard", "kbd"),
]

ORIGINS = {
    "mobo": (0.55, 0.50), "gpu": (0.55, 0.30),
    "ram_a": (0.62, 0.72), "ram_b": (0.68, 0.72),
    "kbd": (0.50, 0.05), "aio": (0.50, 0.85),
}


def slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")
    return s or "zone"


def zone_key(dev, zone_idx: int) -> str:
    return f"{dev.name}:{zone_idx}"


def ring_angles(n: int) -> np.ndarray:
    return np.arange(n, dtype=np.float32) / max(n, 1)


def _base_name(dev, zone, zone_idx: int, used: set) -> str:
    hay = f"{dev.name} {zone.name}".lower()
    for pat, name in KNOWN:
        if re.search(pat, hay):
            if name == "ram":                     # two sticks, same name
                for suffix in "abcdefgh":
                    cand = f"ram_{suffix}"
                    if cand not in used:
                        return cand
            return name
    base = slug(zone.name if zone.name.lower() not in ("led strip", "virtual zone")
                else dev.name)
    cand, i = base, 2
    while cand in used:
        cand, i = f"{base}_{i}", i + 1
    return cand


def segment_order(order, count: int) -> list[int]:
    """Physical slot -> electrical segment. Falls back to identity.

    Anything that isn't a clean permutation is rejected outright rather than
    partially applied: a half-valid order would silently double-drive one
    segment and leave another dark, which is far harder to spot than no effect.
    """
    if isinstance(order, (list, tuple)) and len(order) >= count:
        try:
            seq = [int(x) for x in list(order)[:count]]
        except (TypeError, ValueError):
            return list(range(count))
        if sorted(seq) == list(range(count)):
            return seq
    return list(range(count))


def _fan_origin(i: int, total: int) -> tuple[float, float]:
    if total <= 1:
        return (0.12, 0.5)
    return (0.12, 0.90 - 0.80 * (i / (total - 1)))


def resolve(devices, groups: dict | None = None, overrides: dict | None = None,
            **_legacy) -> tuple[dict[str, Fixture], list[str]]:
    """Turn every zone OpenRGB reports into fixtures.

    `groups` carves a zone into rings: {"<device>:<zone>": {"rings": 7,
    "leds_per_ring": 11, "name": "cfan", "mirror": 1, "skip": false}}.

    `order` is a permutation mapping each *physical* slot to the *electrical*
    segment sitting there, because the sequence fans are wired in is rarely the
    sequence they are mounted in. Fixture names follow physical order, so a
    sweep across cfan_a..cfan_j crosses the case in a straight line whatever
    the cabling does.
    """
    groups = groups or {}
    overrides = overrides or {}
    fixtures: dict[str, Fixture] = {}
    notes: list[str] = []

    for di, dev in enumerate(devices):
        if dev.type.name == "VIRTUAL":
            # Virtual devices are remaps of LEDs we already drive; writing to
            # both would fight over the same hardware.
            continue
        slow = dev.type.name in SLOW_TYPES
        for zi, zone in enumerate(dev.zones):
            n = len(zone.leds)
            if n == 0:
                continue
            offset = sum(len(z.leds) for z in dev.zones[:zi])
            spec = groups.get(zone_key(dev, zi)) or {}
            if spec.get("skip"):
                continue

            rings = int(spec.get("rings", 0) or 0)
            per = int(spec.get("leds_per_ring", 0) or 0)
            mirror = max(1, int(spec.get("mirror", 1) or 1))

            if rings > 0 and per > 0:
                base = spec.get("name") or _base_name(dev, zone, zi, set(fixtures))
                fit = n // (per * mirror)
                count = max(1, min(rings, fit)) if fit else 1
                if count < rings:
                    notes.append(f"{zone_key(dev, zi)}: only room for {count} "
                                 f"x {per} (zone has {n})")
                seq = segment_order(spec.get("order"), count)
                if spec.get("order") and seq == list(range(count)) and \
                        list(spec["order"])[:count] != list(range(count)):
                    notes.append(f"{zone_key(dev, zi)}: order ignored, not a "
                                 f"permutation of 0..{count - 1}")
                for k in range(count):
                    name = f"{base}_{chr(ord('a') + k)}" if count > 1 else base
                    fixtures[name] = Fixture(
                        name=name, dev_idx=di, zone_idx=zi, n=per,
                        offset=offset + seq[k] * per * mirror,
                        pos=np.linspace(0.0, 1.0, per, dtype=np.float32),
                        slow=slow, kind=RING, angle=ring_angles(per),
                        origin=_fan_origin(k, count),
                        spin=-1.0 if k % 2 else 1.0, mirror=mirror)
                continue

            name = spec.get("name") or _base_name(dev, zone, zi, set(fixtures))
            kind = spec.get("kind") or (RING if spec.get("ring") else LINE)
            mm = getattr(zone, "matrix_map", None)
            matrix = ([[(None if v is None or v >= n else int(v)) for v in row]
                       for row in mm]
                      if mm and all(isinstance(r, (list, tuple)) for r in mm)
                      else None)
            fixtures[name] = Fixture(
                name=name, dev_idx=di, zone_idx=zi, n=n, offset=offset,
                pos=(np.linspace(0.0, 1.0, n, dtype=np.float32) if n > 1
                     else np.array([0.5], dtype=np.float32)),
                slow=slow, kind=kind,
                angle=ring_angles(n) if kind == RING else None,
                origin=ORIGINS.get(name, (0.5, 0.5)), matrix=matrix,
                mirror=mirror)

    # Calibration: which way a ring turns, and where its first LED sits.
    for name, fx in fixtures.items():
        o = overrides.get(name) or {}
        if "spin" in o:
            fx.spin = 1.0 if float(o["spin"]) >= 0 else -1.0
        if o.get("rotate") and fx.angle is not None:
            fx.angle = (fx.angle + float(o["rotate"])) % 1.0
        if o.get("reverse") and fx.angle is not None:
            fx.angle = (-fx.angle) % 1.0
            fx.reverse = True

    return fixtures, notes
