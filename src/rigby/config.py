"""Persisted rig configuration.

Calibration is a thing you do once, by eye, and then want to keep: which
header carries what, how long each chain is, which way a fan's ring turns and
where its first LED sits. As CLI flags all of that is lost on exit, so it lives
in a file the control UI can write.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


def default_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / "rigby" / "config.json"


@dataclass
class RigConfig:
    fan_mode: str = "mirrored"
    fans: int = 3
    leds_per_fan: int = 6
    swap_headers: bool = False
    # "<device substring>:<zone index>" -> LED count
    zones: dict = field(default_factory=dict)
    # fixture name -> {"spin": 1|-1, "rotate": 0..1}
    fixtures: dict = field(default_factory=dict)
    # "<device>:<zone>" -> how to carve that zone into fixtures, e.g.
    # {"rings": 7, "leds_per_ring": 11, "name": "cfan"}
    groups: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | None = None) -> "RigConfig":
        path = path or default_path()
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return cls()
        cfg = cls()
        for k in ("fan_mode", "fans", "leds_per_fan", "swap_headers",
                  "zones", "fixtures", "groups"):
            if k in raw:
                setattr(cfg, k, raw[k])
        return cfg

    def save(self, path: Path | None = None) -> Path:
        path = path or default_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "fan_mode": self.fan_mode,
            "fans": int(self.fans),
            "leds_per_fan": int(self.leds_per_fan),
            "swap_headers": bool(self.swap_headers),
            "zones": self.zones,
            "fixtures": self.fixtures,
            "groups": self.groups,
        }, indent=2) + "\n")
        tmp.replace(path)              # atomic, so a crash can't truncate it
        return path

    def as_dict(self) -> dict:
        return {"fan_mode": self.fan_mode, "fans": self.fans,
                "leds_per_fan": self.leds_per_fan,
                "swap_headers": self.swap_headers,
                "zones": dict(self.zones), "fixtures": dict(self.fixtures),
                "groups": dict(self.groups)}


def apply_zone_sizes(client, zones: dict) -> list[str]:
    """Resize zones to the configured counts. Returns human-readable notes."""
    notes = []
    for key, want in (zones or {}).items():
        try:
            match, zi = key.rsplit(":", 1)
            zi = int(zi)
            want = int(want)
        except (ValueError, AttributeError):
            continue
        dev = next((d for d in client.devices
                    if match.lower() in d.name.lower()), None)
        if dev is None or zi >= len(dev.zones):
            notes.append(f"{key}: no such zone")
            continue
        zone = dev.zones[zi]
        if len(zone.leds) == want:
            continue
        try:
            zone.resize(want)
            dev.update()
            got = len(dev.zones[zi].leds)
            notes.append(f"{key}: {got} leds"
                         + ("" if got == want else f" (asked {want})"))
        except Exception as e:
            notes.append(f"{key}: resize failed ({e})")
    return notes
