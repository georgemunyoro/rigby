"""Portable arrangements and deterministic LED/property layering.

Clip times and keyframes are seconds in track/local clip time respectively.
Later clips win only for the LEDs and properties they own. JSON is the editing
interface for both the browser and prospective language-model tooling.
"""

from __future__ import annotations

import copy
import math
import re
import uuid

import numpy as np

from . import fx
from .show import LOOKS

VERSION = 1
PROPERTIES = {
    "brightness": (0.0, 1.0),
    "movement": (0.0, 2.0),
    "coverage": (0.0, 1.0),
    "accent": (0.0, 2.0),
    "hue": (-4.0, 4.0),
    "position": (-100.0, 100.0),
    "opacity": (0.0, 1.0),
    "red": (0.0, 1.0),
    "green": (0.0, 1.0),
    "blue": (0.0, 1.0),
    "saturation": (0.0, 1.0),
    "direction": (-360.0, 360.0),
    "width": (0.02, 2.0),
    "origin_x": (0.0, 1.0),
    "origin_y": (0.0, 1.0),
}
SPATIAL_PATTERNS = {"sweep", "ripple", "mirror", "spatial_gradient"}
PATTERNS = {"solid", "gradient", "arc", "chase", "path"} | SPATIAL_PATTERNS


def new_project():
    return {"version": VERSION, "name": "Untitled set", "tracks": [], "presets": []}


def number(value, lo, hi):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Expected a number")
    if not math.isfinite(value) or not lo <= value <= hi:
        raise ValueError(f"Number must be between {lo} and {hi}")
    return float(value)


def colour(value):
    if not isinstance(value, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        raise ValueError("Colours must be #RRGGBB")
    return np.array([int(value[i : i + 2], 16) / 255 for i in (1, 3, 5)])


def validate_clip(c, duration):
    if not isinstance(c, dict):
        raise ValueError("Clip must be an object")
    if not isinstance(c.get("name", "Clip"), str) or len(c.get("name", "Clip")) > 200:
        raise ValueError("Clip name must be text under 200 characters")
    if not isinstance(c.get("id"), str) or not c["id"]:
        raise ValueError("Clip needs an id")
    number(c.get("start"), 0, duration)
    number(c.get("end"), 0, duration)
    if c["end"] <= c["start"]:
        raise ValueError("Clip end must follow start")
    if c.get("mode", "guided") not in {"automatic", "guided", "authored"}:
        raise ValueError("Unknown clip mode")
    if c.get("look") is not None and c["look"] not in LOOKS:
        raise ValueError("Unknown look")
    if c.get("pattern") is not None and c["pattern"] not in PATTERNS:
        raise ValueError("Unknown pattern")
    if c.get("ownership", "rgb") not in {"rgb", "colour", "brightness"}:
        raise ValueError("Unknown LED ownership")
    if c.get("audio", "none") not in {"none", "bass", "level", "hit"}:
        raise ValueError("Unknown audio modulation")
    for key in ("colour", "colour2"):
        if key in c:
            colour(c[key])
    for key in ("fade_in", "fade_out"):
        number(c.get(key, 0), 0, c["end"] - c["start"])
    targets = c.get("targets", {})
    if not isinstance(targets, dict) or len(targets) > 256:
        raise ValueError("Targets must map fixture names to LED index lists")
    for name, indices in targets.items():
        if not isinstance(name, str) or not isinstance(indices, list):
            raise ValueError("Invalid fixture selection")
        if len(indices) > 10000 or any(type(i) is not int or i < 0 for i in indices):
            raise ValueError("LED indices must be nonnegative integers")
        if len(set(indices)) != len(indices):
            raise ValueError("LED selection contains duplicates")
    spatial = c.get("spatial", {})
    if not isinstance(spatial, dict) or len(spatial) > 256:
        raise ValueError("Invalid spatial map")
    for name, points in spatial.items():
        if (
            not isinstance(name, str)
            or not isinstance(points, list)
            or len(points) > 10000
        ):
            raise ValueError("Invalid spatial fixture")
        for point in points:
            if not isinstance(point, list) or len(point) != 2:
                raise ValueError("Spatial coordinates need x and y")
            for value in point:
                number(value, -2, 3)
    curves = c.get("curves", {})
    if not isinstance(curves, dict):
        raise ValueError("Curves must be an object")
    for prop, keys in curves.items():
        if prop not in PROPERTIES or not isinstance(keys, list) or len(keys) > 2000:
            raise ValueError("Invalid animation curve")
        previous = -1.0
        for k in keys:
            if not isinstance(k, dict):
                raise ValueError("Keyframe must be an object")
            t = number(k.get("time"), 0, c["end"] - c["start"])
            number(k.get("value"), *PROPERTIES[prop])
            if t <= previous or k.get("ease", "linear") not in {
                "linear",
                "smooth",
                "hold",
            }:
                raise ValueError("Keyframes must be ordered, unique, with valid easing")
            previous = t
    for key in ("enabled", "locked"):
        if key in c and type(c[key]) is not bool:
            raise ValueError(f"{key} must be boolean")


def validate_project(project):
    if not isinstance(project, dict) or project.get("version") != VERSION:
        raise ValueError("Unsupported arrangement version")
    if not isinstance(project.get("name"), str) or len(project["name"]) > 200:
        raise ValueError("Set needs a name under 200 characters")
    project = copy.deepcopy(project)
    project.setdefault("presets", [])
    tracks = project.get("tracks")
    if not isinstance(tracks, list) or len(tracks) > 100:
        raise ValueError("A set supports up to 100 tracks")
    ids = set()
    for track in tracks:
        if not isinstance(track, dict):
            raise ValueError("Track must be an object")
        tid = track.get("id")
        if not isinstance(tid, str) or tid in ids:
            raise ValueError("Track ids must be unique")
        ids.add(tid)
        if not re.fullmatch(r"[a-f0-9]{64}", track.get("audio", "")):
            raise ValueError("Track needs its exact audio fingerprint")
        if not isinstance(track.get("title", "Track"), str):
            raise ValueError("Track title must be text")
        track.setdefault("title", "Track")
        track.setdefault("look", "prism")
        track.setdefault("clips", [])
        duration = number(track.get("duration"), 0.01, 1800)
        if track.get("look", "prism") not in LOOKS:
            raise ValueError("Unknown track look")
        clips = track.get("clips", [])
        if not isinstance(clips, list) or len(clips) > 2000:
            raise ValueError("Too many clips")
        cids = set()
        for c in clips:
            validate_clip(c, duration)
            if c["id"] in cids:
                raise ValueError("Clip ids must be unique within a track")
            c.setdefault("curves", {})
            c.setdefault("name", "Clip")
            cids.add(c["id"])
    presets = project.get("presets", [])
    if not isinstance(presets, list) or len(presets) > 200:
        raise ValueError("Too many presets")
    for c in presets:
        validate_clip(c, 1800)
    return copy.deepcopy(project)


def curve(keys, t, default):
    if not keys:
        return default
    if t <= keys[0]["time"]:
        return keys[0]["value"]
    for a, b in zip(keys, keys[1:]):
        if t < b["time"]:
            w = (t - a["time"]) / (b["time"] - a["time"])
            ease = a.get("ease", "linear")
            if ease == "hold":
                w = 0.0
            elif ease == "smooth":
                w = w * w * (3 - 2 * w)
            return a["value"] * (1 - w) + b["value"] * w
    return keys[-1]["value"]


def selected(c, key, n):
    targets = c.get("targets", {})
    if not targets:
        return np.arange(n)
    if key not in targets:
        return np.array([], dtype=int)
    return np.array([i for i in targets[key] if i < n], dtype=int)


def compose(track, t, fixtures, base, features, positions=None):
    """base(look, time) is a seekable, prepared automatic frame provider."""
    out = {k: v.copy() for k, v in base(track.get("look", "prism"), t).items()}
    for c in track.get("clips", []):
        if not c.get("enabled", True) or not c["start"] <= t < c["end"]:
            continue
        local = t - c["start"]
        get = lambda name, default: curve(c.get("curves", {}).get(name), local, default)
        weight = get("opacity", 1.0)
        for length, distance in (
            (c.get("fade_in", 0), local),
            (c.get("fade_out", 0), c["end"] - t),
        ):
            if length:
                weight *= min(1.0, distance / length)
        source = base(c["look"], t) if c.get("look") else None
        mode = c.get("mode", "guided")
        path_offsets, path_length = {}, 0
        if c.get("pattern") == "path":
            for name in c.get("targets") or fixtures:
                if name in fixtures:
                    path_offsets[name] = path_length
                    path_length += len(selected(c, name, fixtures[name].n))
        for key, fix in fixtures.items():
            ix = selected(c, key, fix.n)
            if not len(ix):
                continue
            old = out[key][ix].copy()
            rgb = (source[key][ix] if source is not None else old).copy()
            if mode != "automatic":
                movement = get("movement", 1.0)
                if movement != 1.0:
                    # Slow/hold the source's animation locally; spectral
                    # brightness remains tied to the current audio frame.
                    look = c.get("look", track.get("look", "prism"))
                    at = c["start"] + local * movement
                    moving = base(look, min(track["duration"] - 1e-6, at))[key][ix]
                    peak = moving.max(axis=1, keepdims=True)
                    rgb = (
                        moving / np.maximum(peak, 1e-9) * rgb.max(axis=1, keepdims=True)
                    )
                if c.get("pattern"):
                    # Index order is intentional: custom chase paths need not
                    # follow physical numbering. Unselected LEDs are untouched.
                    pos = np.arange(len(ix)) / max(len(ix), 1)
                    position = get("position", local / 4)
                    a = colour(c.get("colour", "#ff6600"))
                    b = colour(c.get("colour2", "#6633ff"))
                    pattern = c["pattern"]
                    mask = None
                    if pattern in SPATIAL_PATTERNS:
                        points = c.get("spatial", {}).get(key) or (positions or {}).get(
                            key
                        )
                        if points is None or len(points) != fix.n:
                            # Old portable clips without a map still have device origins.
                            points = [[fix.origin[0], 1 - fix.origin[1]]] * fix.n
                        xy = np.asarray(points, dtype=float)[ix]
                        angle = math.radians(get("direction", 0))
                        axis = np.array([math.cos(angle), math.sin(angle)])
                        projection = (xy - 0.5) @ axis + 0.5
                        phase = position % 1
                        if pattern == "spatial_gradient":
                            w = ((projection - phase) % 1)[:, None]
                            authored = a * (1 - w) + b * w
                        else:
                            if pattern == "ripple":
                                centre = np.array(
                                    [get("origin_x", 0.5), get("origin_y", 0.5)]
                                )
                                coordinate = np.linalg.norm(
                                    xy - centre, axis=1
                                ) / math.sqrt(2)
                            elif pattern == "mirror":
                                coordinate = np.abs(projection - 0.5) * 2
                            else:
                                coordinate = projection
                            mask = np.exp(
                                -(
                                    (
                                        (coordinate - phase)
                                        / max(0.01, get("width", 0.12) / 2)
                                    )
                                    ** 2
                                )
                            )
                            authored = mask[:, None] * a
                    elif pattern == "path":
                        pos = (path_offsets[key] + np.arange(len(ix))) / max(
                            path_length, 1
                        )
                        distance = np.abs((pos - position + 0.5) % 1 - 0.5)
                        mask = np.exp(
                            -((distance / max(0.01, get("width", 0.12) / 2)) ** 2)
                        )
                        authored = mask[:, None] * a
                    elif pattern == "gradient":
                        w = (pos - position) % 1
                        authored = (
                            a[None, :] * (1 - w[:, None]) + b[None, :] * w[:, None]
                        )
                    elif pattern in {"arc", "chase"}:
                        distance = np.abs((pos - position + 0.5) % 1 - 0.5)
                        mask = (
                            np.exp(-((distance / 0.16) ** 2))
                            if pattern == "arc"
                            else (distance < 0.5 / max(len(ix), 1)).astype(float)
                        )
                        authored = mask[:, None] * a
                    else:
                        authored = np.broadcast_to(a, rgb.shape).copy()
                    ownership = c.get("ownership", "rgb")
                    if ownership == "colour":
                        value = rgb.max(axis=1, keepdims=True)
                        peak = authored.max(axis=1, keepdims=True)
                        coloured = np.where(
                            peak > 1e-9, authored / np.maximum(peak, 1e-9) * value, rgb
                        )
                        blend = mask[:, None] if mask is not None else 1.0
                        authored = rgb * (1 - blend) + coloured * blend
                        authored *= value / np.maximum(
                            authored.max(axis=1, keepdims=True), 1e-9
                        )
                    elif ownership == "brightness":
                        authored = (
                            rgb
                            / np.maximum(rgb.max(axis=1, keepdims=True), 1e-9)
                            * authored.max(axis=1, keepdims=True)
                        )
                    rgb = authored
                before_keys = rgb.copy()
                rgb_keys = any(
                    c.get("curves", {}).get(prop) for prop in ("red", "green", "blue")
                )
                for channel, prop in enumerate(("red", "green", "blue")):
                    if c.get("curves", {}).get(prop):
                        rgb[:, channel] = get(prop, 0.0)
                if rgb_keys and c.get("ownership") == "colour":
                    value = before_keys.max(axis=1, keepdims=True)
                    peak = rgb.max(axis=1, keepdims=True)
                    rgb = np.where(
                        peak > 1e-9, rgb / np.maximum(peak, 1e-9) * value, before_keys
                    )
                elif rgb_keys and c.get("ownership") == "brightness":
                    rgb = (
                        before_keys
                        / np.maximum(before_keys.max(axis=1, keepdims=True), 1e-9)
                        * rgb.max(axis=1, keepdims=True)
                    )
                hue = get("hue", 0.0)
                saturation = get("saturation", 1.0)
                if hue or saturation != 1.0:
                    # Hue-only overlay retains the base's value and saturation.
                    import colorsys

                    hsv = np.array([colorsys.rgb_to_hsv(*pixel) for pixel in rgb])
                    rgb = fx.hsv(
                        (hsv[:, 0] + hue) % 1, hsv[:, 1] * saturation, hsv[:, 2]
                    )
                rgb *= get("brightness", 1.0)
                coverage = get("coverage", 1.0)
                if coverage < 1:
                    mask = np.clip(coverage * len(ix) - np.arange(len(ix)), 0.0, 1.0)
                    rgb *= mask[:, None]
                audio = c.get("audio", "none")
                if audio != "none":
                    signal = {
                        "bass": features.bass,
                        "level": features.level,
                        "hit": features.onset_strength,
                    }[audio]
                    rgb *= np.clip(signal, 0.0, 1.0)
                accent = get("accent", 1.0)
                rgb *= max(0.0, 1.0 + (accent - 1.0) * features.onset_strength)
            out[key][ix] = np.clip(old * (1 - weight) + rgb * weight, 0.0, 1.0)
    return out


def generated_clips(analysis, old_clips):
    """Rebuild only unlocked automatic suggestions; hand edits always survive."""
    preserved = [
        copy.deepcopy(c)
        for c in old_clips
        if c.get("origin") != "generated" or c.get("locked", False)
    ]
    result = []
    for section in analysis["sections"]:
        start, end = section["start"], section["end"]
        if any(c["start"] < end and c["end"] > start for c in preserved):
            continue
        kind = section["label"]
        level = {"breakdown": 0.55, "build": 0.8, "groove": 0.9, "full": 1.0}[kind]
        motion = {"breakdown": 0.15, "build": 0.7, "groove": 0.65, "full": 1.0}[kind]
        curves = {
            "brightness": [{"time": 0, "value": level}],
            "movement": [{"time": 0, "value": motion}],
        }
        if kind == "build":
            curves["coverage"] = [
                {"time": 0, "value": 0.4, "ease": "smooth"},
                {"time": end - start, "value": 0.85},
            ]
        if kind == "full":
            curves["hue"] = [{"time": 0, "value": (section.get("theme", 0) % 4) * 0.12}]
        result.append(
            {
                "id": uuid.uuid4().hex,
                "name": kind.title(),
                "start": start,
                "end": end,
                "mode": "guided",
                "origin": "generated",
                "locked": False,
                "curves": curves,
            }
        )
    return result + preserved
