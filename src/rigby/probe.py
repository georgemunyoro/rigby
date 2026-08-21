"""Discover what is physically on an ARGB header.

OpenRGB cannot see past the header: it reports whatever LED count the zone is
configured for, and writes to LEDs beyond the real chain simply vanish. So the
count, the number of fans, and whether a hub mirrors or chains can only be
established by lighting things and looking.

Every probe restores the zone's original size on the way out.
"""

from __future__ import annotations

import time

from openrgb.utils import RGBColor

WHITE = RGBColor(120, 120, 120)
OFF = RGBColor(0, 0, 0)


def _zone_of(client, zone_arg: str):
    """Accept 'ASUS TUF:1' or a bare zone index on the motherboard."""
    if ":" in zone_arg:
        match, idx = zone_arg.rsplit(":", 1)
    else:
        match, idx = "ASUS TUF", zone_arg
    dev = next((d for d in client.devices if match.lower() in d.name.lower()),
               None)
    if dev is None:
        raise SystemExit(f"no device matching {match!r}")
    i = int(idx)
    if i >= len(dev.zones):
        raise SystemExit(f"{dev.name} has no zone {i}")
    return dev, dev.zones[i]


def _paint(dev, zone, values) -> None:
    """Write a list of colours into one zone, leaving other zones dark."""
    start = sum(len(z.leds) for z in dev.zones[:zone.id])
    buf = [OFF] * len(dev.leds)
    for i, c in enumerate(values):
        if start + i < len(buf):
            buf[start + i] = c
    dev.set_colors(buf, fast=True)


def run(client, zone_arg: str, size: int, step: float, mode: str) -> int:
    dev, zone = _zone_of(client, zone_arg)
    original = len(zone.leds)
    print(f"\nprobing {dev.name} / {zone.name!r}  (currently {original} LEDs)")

    names = [m.name for m in dev.modes]
    if "Direct" in names and dev.active_mode != names.index("Direct"):
        dev.set_mode("Direct")

    try:
        if size != original:
            print(f"resizing zone to {size} LEDs (restored on exit)")
            zone.resize(size)
            dev.update()
            zone = dev.zones[zone.id]

        n = len(zone.leds)

        if mode in ("all", "both"):
            print(f"\n[1] lighting all {n} LEDs at once.")
            print("    Count how many actually light, and note where the lit")
            print("    run stops -- that is your real chain length.")
            _paint(dev, zone, [WHITE] * n)
            time.sleep(6.0)

        if mode in ("walk", "both"):
            print(f"\n[2] walking one LED at a time, {step:.2f}s each.")
            print("    Watch which fan lights. If the SAME position lights on")
            print("    every fan, the hub is a splitter. If the lit LED moves")
            print("    from fan to fan, it is chained -- note the index where")
            print("    it jumps to the next fan; that is your LEDs-per-fan.\n")
            for i in range(n):
                _paint(dev, zone, [WHITE if j == i else OFF for j in range(n)])
                print(f"\r    LED {i:4d} / {n - 1}", end="", flush=True)
                time.sleep(step)
            print()

        _paint(dev, zone, [OFF] * n)
        return 0
    finally:
        try:
            cur = dev.zones[zone.id]
            _paint(dev, cur, [OFF] * len(cur.leds))
            if len(cur.leds) != original:
                cur.resize(original)
                print(f"\nrestored {zone.name!r} to {original} LEDs")
        except Exception as e:
            print(f"\nwarning: could not restore zone size: {e}")
