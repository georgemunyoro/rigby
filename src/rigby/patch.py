"""Fixture patch and geometry.

Effects address *fixtures* -- named groups that know their own shape -- never
raw device or LED indices. A fan is not a strip: it is a ring, and it has an
angle, a spin direction and a centre. Effects that know that can rotate, split
a ring in half, or offset one fan against the next; effects that only see a
flat list of LEDs can do none of those things and end up looking like a VU
meter no matter how good the analysis is.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

LINE = "line"
RING = "ring"


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
    origin: tuple[float, float] = (0.5, 0.5)   # rough place in the case
    spin: float = 1.0     # +1 / -1, so neighbouring rings can counter-rotate
    reverse: bool = False
    # Rows of LED indices with None for gaps, when the device reports one.
    # A keyboard is a grid, not a 126-long strip, and drawing it as one makes
    # it unusable by hand.
    matrix: list | None = None
    # How many times this fixture's data is repeated down the wire. A passive
    # ARGB splitter hub feeds every port the same signal, so three fans on one
    # header are one ring shown three times, not three rings.
    mirror: int = 1

    @property
    def is_ring(self) -> bool:
        return self.kind == RING


# Which ARGB header carries what. `--swap-headers` flips these when the cables
# are the other way round; `--identify` walks the LEDs so you can check.
HUB_ZONE = 1   # 3 fans x 6 on a hub
AIO_ZONE = 2   # AIO pump head, one 18-LED ring

FANS_PER_HUB = 3
LEDS_PER_FAN = 6

# Rough positions in a unit square looking at the case from the side, so
# effects can sweep across the whole rig rather than per-fixture.
ORIGINS = {
    "fans":  (0.12, 0.50),
    "aio":   (0.50, 0.85),
    "mobo":  (0.55, 0.50),
    "ram_a": (0.62, 0.72), "ram_b": (0.68, 0.72),
    "gpu":   (0.55, 0.30),
    "kbd":   (0.50, 0.05),
}

# (fixture, device-name substring, zone, occurrence, slow)
LINE_SPEC: list[tuple[str, str, int, int, bool]] = [
    ("mobo",  "ASUS TUF", 0, 0, False),
    ("ram_a", "ENE DRAM", 0, 0, True),
    ("ram_b", "ENE DRAM", 0, 1, True),
    ("gpu",   "GeForce",  0, 0, True),
    ("kbd",   "EVision",  0, 0, False),
]


def _ring_angles(n: int) -> np.ndarray:
    """0..1 turns around the ring, one step per LED."""
    return (np.arange(n, dtype=np.float32) / max(n, 1))


def fan_name(i: int) -> str:
    """fan_a .. fan_z, then fan_27 onward. Ten fans is not unusual."""
    return f"fan_{chr(ord('a') + i)}" if i < 26 else f"fan_{i + 1}"


def _fan_origin(i: int, total: int) -> tuple[float, float]:
    """Stack the chain down the left of the case, first fan at the top."""
    if total <= 1:
        return (0.12, 0.5)
    return (0.12, 0.90 - 0.80 * (i / (total - 1)))


def _find(devices, match: str, occurrence: int = 0) -> int | None:
    hits = [i for i, d in enumerate(devices) if match.lower() in d.name.lower()]
    return hits[occurrence] if occurrence < len(hits) else None


def resolve(devices, swap_headers: bool = False,
            fans: int = FANS_PER_HUB, leds_per_fan: int = LEDS_PER_FAN,
            fan_mode: str = "mirrored", overrides: dict | None = None
            ) -> tuple[dict[str, Fixture], list[str]]:
    """Map the spec onto live devices. Returns (fixtures, missing_names)."""
    fixtures: dict[str, Fixture] = {}
    missing: list[str] = []
    overrides = overrides or {}

    hub_zone, aio_zone = (AIO_ZONE, HUB_ZONE) if swap_headers else (HUB_ZONE, AIO_ZONE)

    mb = _find(devices, "ASUS TUF")
    if mb is None:
        missing += ["fan_a", "fan_b", "fan_c", "aio"]
    else:
        zones = devices[mb].zones

        def zone_offset(zi: int) -> int:
            # openrgb-python 0.3.x has no zone.start_idx; zones are contiguous.
            return sum(len(z.leds) for z in zones[:zi])

        # --- the fan hub ---------------------------------------------------
        if (fan_mode == "mirrored" and hub_zone < len(zones)
                and len(zones[hub_zone].leds) >= leds_per_fan):
            # One ring, repeated across every port by the hub. Modelling this
            # as N independent rings means N-1 of them are computed and sent
            # into a void, and every phase offset between fans is invisible.
            base = zone_offset(hub_zone)
            reps = max(1, len(zones[hub_zone].leds) // leds_per_fan)
            fixtures["fans"] = Fixture(
                name="fans", dev_idx=mb, zone_idx=hub_zone, n=leds_per_fan,
                offset=base,
                pos=np.linspace(0.0, 1.0, leds_per_fan, dtype=np.float32),
                slow=False, kind=RING, angle=_ring_angles(leds_per_fan),
                origin=ORIGINS["fans"], spin=1.0, mirror=reps)
        elif hub_zone < len(zones) and len(zones[hub_zone].leds) >= fans * leds_per_fan:
            base = zone_offset(hub_zone)
            # However many actually fit on the chain, not however many were
            # asked for -- a wrong count silently addresses LEDs that aren't
            # there.
            room = len(zones[hub_zone].leds) // leds_per_fan
            count = max(1, min(fans, room))
            for i in range(count):
                name = fan_name(i)
                fixtures[name] = Fixture(
                    name=name, dev_idx=mb, zone_idx=hub_zone, n=leds_per_fan,
                    offset=base + i * leds_per_fan,
                    pos=np.linspace(0.0, 1.0, leds_per_fan, dtype=np.float32),
                    slow=False, kind=RING, angle=_ring_angles(leds_per_fan),
                    origin=_fan_origin(i, count),
                    # Alternate spin down the chain: identical rings turning in
                    # unison read as one object, opposed they read as many.
                    spin=-1.0 if i % 2 else 1.0)
        else:
            missing += (["fans"] if fan_mode == "mirrored"
                        else [fan_name(i) for i in range(fans)])

        # --- the AIO pump head: one fine-grained ring ----------------------
        if aio_zone < len(zones) and len(zones[aio_zone].leds) > 0:
            n = len(zones[aio_zone].leds)
            fixtures["aio"] = Fixture(
                name="aio", dev_idx=mb, zone_idx=aio_zone, n=n,
                offset=zone_offset(aio_zone),
                pos=np.linspace(0.0, 1.0, n, dtype=np.float32),
                slow=False, kind=RING, angle=_ring_angles(n),
                origin=ORIGINS["aio"], spin=-1.0)
        else:
            missing.append("aio")

    for name, match, zone_idx, occurrence, slow in LINE_SPEC:
        dev_idx = _find(devices, match, occurrence)
        if dev_idx is None or zone_idx >= len(devices[dev_idx].zones):
            missing.append(name)
            continue
        zones = devices[dev_idx].zones
        n = len(zones[zone_idx].leds)
        if n == 0:
            missing.append(name)
            continue
        mm = getattr(zones[zone_idx], "matrix_map", None)
        matrix = ([[(None if v is None or v >= n else int(v)) for v in row]
                   for row in mm]
                  if mm and all(isinstance(r, (list, tuple)) for r in mm)
                  else None)

        fixtures[name] = Fixture(
            name=name, dev_idx=dev_idx, zone_idx=zone_idx, n=n,
            offset=sum(len(z.leds) for z in zones[:zone_idx]),
            pos=(np.linspace(0.0, 1.0, n, dtype=np.float32) if n > 1
                 else np.array([0.5], dtype=np.float32)),
            slow=slow, kind=LINE, origin=ORIGINS.get(name, (0.5, 0.5)),
            matrix=matrix)

    # Calibration: which way a ring turns, and where its first LED sits.
    # Fans mounted mirrored run backwards, and LED 0 lands at whatever clock
    # position the fan's own wiring puts it -- neither is knowable in advance.
    for name, fx in fixtures.items():
        o = overrides.get(name) or {}
        if "spin" in o:
            fx.spin = 1.0 if float(o["spin"]) >= 0 else -1.0
        if o.get("rotate") and fx.angle is not None:
            fx.angle = (fx.angle + float(o["rotate"])) % 1.0
        if o.get("reverse") and fx.angle is not None:
            fx.angle = (-fx.angle) % 1.0
            fx.reverse = True

    return fixtures, missing
