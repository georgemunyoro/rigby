"""Device calibration, driven from the control UI.

Everything here is a thing you can only settle by looking at the case: how many
LEDs are really on a header, which physical fan is `fan_c`, which way its ring
turns and where its first LED sits. The UI asks, the rig answers by lighting
something, and the result is written to the config file.
"""

from __future__ import annotations

import threading
import time

from .config import RigConfig, apply_zone_sizes
from .patch import zone_key

MAX_ZONE = 512      # the driver clamps well below this; just a sanity bound


class Devices:
    """Mediates config edits between the HTTP thread and the render loop."""

    def __init__(self, sink, cfg: RigConfig, cfg_path=None):
        self.sink = sink
        self.cfg = cfg
        self.cfg_path = cfg_path
        self._lock = threading.Lock()
        self.notes: list[str] = []
        self.rebuild_requested = False
        # (fixture, led_index or None, until_monotonic) -- rendered by the loop
        self.highlight: tuple | None = None

    # -- read ---------------------------------------------------------------
    def state(self) -> dict:
        with self._lock:
            zones = []
            for di, dev in enumerate(self.sink.client.devices):
                for zi, z in enumerate(dev.zones):
                    n = len(z.leds)
                    key = zone_key(dev, zi)
                    zones.append({
                        "key": key,
                        "device": dev.name,
                        "zone": z.name,
                        "leds": n,
                        "resizable": z.type.name != "SINGLE",
                        "virtual": dev.type.name == "VIRTUAL",
                        "group": self.cfg.groups.get(key) or {},
                        # Ways this zone divides evenly, so a fan count can be
                        # picked off a list instead of worked out by hand.
                        "splits": [[k, n // k] for k in range(2, 33)
                                   if n % k == 0 and n // k >= 3],
                        "patched": any(f.dev_idx == di and f.zone_idx == zi
                                       for f in self.sink.fixtures.values()),
                    })
            fixtures = {}
            for name, f in self.sink.fixtures.items():
                o = self.cfg.fixtures.get(name, {})
                fixtures[name] = {
                    "n": f.n, "kind": f.kind, "mirror": f.mirror,
                    "spin": float(f.spin),
                    "rotate": float(o.get("rotate", 0.0)),
                    "reverse": bool(o.get("reverse", False)),
                }
            return {"zones": zones, "fixtures": fixtures,
                    "config": self.cfg.as_dict(),
                    "patch": self.sink.describe(),
                    "notes": self.notes[-6:]}

    # -- write --------------------------------------------------------------
    def command(self, msg: dict) -> dict:
        op = msg.get("op")
        with self._lock:
            if op == "resize":
                self._resize(msg)
            elif op == "chain":
                self._chain(msg)
            elif op == "group":
                self._group(msg)
            elif op == "calibrate":
                self._calibrate(msg)
            elif op == "identify":
                secs = float(msg.get("seconds", 2.0))
                idx = msg.get("led")
                self.highlight = (msg.get("fixture"),
                                  None if idx is None else int(idx),
                                  time.monotonic() + max(0.2, min(secs, 20.0)))
            elif op == "save":
                try:
                    p = self.cfg.save(self.cfg_path)
                    self._note(f"saved {p}")
                except OSError as e:
                    self._note(f"save failed: {e}")
        return self.state()

    def _note(self, s: str) -> None:
        self.notes.append(s)
        del self.notes[:-20]

    def _resize(self, msg: dict) -> None:
        key, want = msg.get("key"), msg.get("leds")
        try:
            want = max(0, min(int(want), MAX_ZONE))
        except (TypeError, ValueError):
            return
        self.cfg.zones[key] = want
        for n in apply_zone_sizes(self.sink.client, {key: want}):
            self._note(n)
        # A zone that grew or shrank changes every offset after it.
        self.rebuild_requested = True

    def _chain(self, msg: dict) -> None:
        for k, cast in (("fan_mode", str), ("fans", int),
                        ("leds_per_fan", int), ("swap_headers", bool)):
            if k in msg:
                try:
                    setattr(self.cfg, k, cast(msg[k]))
                except (TypeError, ValueError):
                    pass
        self.cfg.fans = max(1, self.cfg.fans)
        self.cfg.leds_per_fan = max(1, self.cfg.leds_per_fan)
        self.rebuild_requested = True

    def _group(self, msg: dict) -> None:
        key = msg.get("key")
        if not key:
            return
        spec = dict(self.cfg.groups.get(key) or {})
        for field, cast in (("rings", int), ("leds_per_ring", int),
                            ("mirror", int), ("name", str), ("kind", str),
                            ("skip", bool)):
            if field in msg:
                try:
                    spec[field] = cast(msg[field])
                except (TypeError, ValueError):
                    pass
        if not spec.get("rings"):
            spec.pop("rings", None)
            spec.pop("leds_per_ring", None)
        if spec:
            self.cfg.groups[key] = spec
        else:
            self.cfg.groups.pop(key, None)
        self.rebuild_requested = True

    def _calibrate(self, msg: dict) -> None:
        name = msg.get("fixture")
        if name is None:
            return
        cur = dict(self.cfg.fixtures.get(name, {}))
        if "spin" in msg:
            cur["spin"] = 1 if float(msg["spin"]) >= 0 else -1
        if "rotate" in msg:
            cur["rotate"] = float(msg["rotate"]) % 1.0
        if "reverse" in msg:
            cur["reverse"] = bool(msg["reverse"])
        self.cfg.fixtures[name] = cur
        self.rebuild_requested = True

    # -- render loop side ---------------------------------------------------
    def take_rebuild(self) -> bool:
        with self._lock:
            r, self.rebuild_requested = self.rebuild_requested, False
            return r

    def active_highlight(self):
        with self._lock:
            if self.highlight is None:
                return None
            name, led, until = self.highlight
            if time.monotonic() > until:
                self.highlight = None
                return None
            return name, led
