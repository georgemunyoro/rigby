"""Serve the typefaces the UI is designed in.

The page asks for specific faces rather than whatever the OS calls its UI font,
because "system-ui" is a different design on every machine and a layout tuned
to one of them is wrong on the rest.

Fonts are read from wherever fontconfig says they live and served by the app
itself. Nothing is copied into this repository -- no redistribution, no
megabytes of base64 in a source file -- and a phone on the same network gets
the same design as the machine running the show. If a face isn't installed the
CSS falls through to a stack that degrades sensibly.
"""

from __future__ import annotations

import gzip
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

# url key -> fontconfig pattern
WANTED = {
    "sans-400": "Fira Sans:weight=regular",
    "sans-500": "Fira Sans:weight=medium",
    "sans-700": "Fira Sans:weight=bold",
    "mono-400": "Google Sans Code",
}


@lru_cache(maxsize=1)
def resolve() -> dict[str, Path]:
    """Map each key to a real file, dropping anything fontconfig can't find."""
    if not shutil.which("fc-match"):
        return {}
    found: dict[str, Path] = {}
    for key, pattern in WANTED.items():
        try:
            out = subprocess.run(["fc-match", "-f", "%{file}", pattern],
                                 capture_output=True, text=True, timeout=4)
        except (OSError, subprocess.SubprocessError):
            continue
        path = Path(out.stdout.strip())
        # fc-match always answers, so check we got the family we asked for
        # rather than its idea of a substitute.
        family = pattern.split(":")[0].replace(" ", "").lower()
        if path.is_file() and family[:8] in path.name.replace(" ", "").lower():
            found[key] = path
    return found


@lru_cache(maxsize=8)
def payload(key: str) -> tuple[bytes, bool] | None:
    """(body, is_gzipped) for a font key, or None if we don't have it."""
    path = resolve().get(key)
    if path is None:
        return None
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    # TTF compresses by roughly half, which matters over wifi and costs
    # nothing over loopback.
    packed = gzip.compress(raw, 6)
    return (packed, True) if len(packed) < len(raw) else (raw, False)


def css() -> str:
    """@font-face rules for whatever was actually found."""
    have = resolve()
    out = []
    for key in ("sans-400", "sans-500", "sans-700"):
        if key in have:
            out.append("@font-face{font-family:'Rigby Sans';font-style:normal;"
                       f"font-weight:{key.split('-')[1]};font-display:swap;"
                       f"src:url('/font/{key}') format('truetype')}}")
    if "mono-400" in have:
        # Variable: one file covers the whole weight range.
        out.append("@font-face{font-family:'Rigby Mono';font-style:normal;"
                   "font-weight:300 700;font-display:swap;"
                   "src:url('/font/mono-400') format('truetype-variations')}")
    return "".join(out)
