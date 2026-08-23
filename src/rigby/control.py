"""Live control surface: a small HTTP server and a single-page UI.

Tuning by editing a flag and restarting loses the thing you were listening to,
so every knob here is applied to the running show between frames. The page also
shows the analysis -- bands, dynamics, swell, pulse -- because most of the
tuning questions are really "what does the analyser think is happening", and
answering that from a number is far quicker than inferring it from the lights.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field, fields
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .fonts import payload as font_payload
from .ui import SLIDERS, page


# Changing these means building a new Look; the rest are live attributes.
REBUILD = {"look", "duo"}


@dataclass
class Params:
    look: str = "auto"
    duo: str = "ember"
    palette: str = "sunset"
    hit_style: str = "swing"
    master: float = 1.0
    gain: float = 1.6
    curve: float = 0.45
    gamma: float = 2.2
    saturation: float = 0.88
    hot: float = 0.5
    hue_drift: float = 1.0
    dynamics_db: float = 15.0
    onset_k: float = 1.7
    swing_min_beats: int = 6
    offset_ms: int = 0
    blackout: bool = False
    playground: bool = False
    pause: bool = False

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _dirty: set = field(default_factory=set, repr=False)

    def snapshot(self) -> dict:
        # Not dataclasses.asdict: it deep-copies every field, and this one
        # holds a lock, which cannot be copied.
        with self._lock:
            return {f.name: getattr(self, f.name) for f in fields(self)
                    if not f.name.startswith("_")}

    def update(self, changes: dict) -> set:
        """Apply changes, returning the set of names that actually changed."""
        changed = set()
        with self._lock:
            for k, v in changes.items():
                if k.startswith("_") or not hasattr(self, k):
                    continue
                cur = getattr(self, k)
                try:
                    v = type(cur)(v) if not isinstance(cur, bool) else bool(v)
                except (TypeError, ValueError):
                    continue
                if v != cur:
                    setattr(self, k, v)
                    changed.add(k)
            self._dirty |= changed
        return changed

    def take_dirty(self) -> set:
        with self._lock:
            d, self._dirty = self._dirty, set()
            return d


class Canvas:
    """Hand-set LED colours, used instead of the look while in playground mode.

    Stored per fixture as plain 0..1 floats so it round-trips through JSON
    without conversion, and so it can be handed to the sink unchanged.
    """

    def __init__(self, geometry: dict):
        self._lock = threading.Lock()
        self.cells: dict[str, list[list[float]]] = {
            name: [[0.0, 0.0, 0.0] for _ in range(g["n"])]
            for name, g in geometry.items()}

    def snapshot(self) -> dict:
        with self._lock:
            return {k: [list(c) for c in v] for k, v in self.cells.items()}

    def apply(self, msg: dict) -> None:
        op = msg.get("op")
        with self._lock:
            if op == "clear":
                for v in self.cells.values():
                    for c in v:
                        c[0] = c[1] = c[2] = 0.0
                return
            if op == "fill":
                rgb = [float(x) for x in msg.get("rgb", [0, 0, 0])][:3]
                names = msg.get("fixtures") or list(self.cells)
                for n in names:
                    for c in self.cells.get(n, []):
                        c[:] = rgb
                return
            if op == "set":
                # {"op":"set","cells":{"fan_a":{"0":[r,g,b], ...}, ...}}
                for name, idxs in (msg.get("cells") or {}).items():
                    cells = self.cells.get(name)
                    if cells is None:
                        continue
                    for i, rgb in idxs.items():
                        try:
                            j = int(i)
                        except (TypeError, ValueError):
                            continue
                        if 0 <= j < len(cells):
                            cells[j][:] = [max(0.0, min(1.0, float(x)))
                                           for x in rgb[:3]]
                return
            if op == "load":
                # Adopt a whole frame, e.g. captured from the running show.
                for name, arr in (msg.get("cells") or {}).items():
                    cells = self.cells.get(name)
                    if cells is None:
                        continue
                    for j, rgb in enumerate(arr[:len(cells)]):
                        cells[j][:] = [max(0.0, min(1.0, float(x)))
                                       for x in rgb[:3]]


class Rig:
    """Mutable holder for things a rebuild replaces.

    Handlers must dereference this per request rather than closing over the
    canvas. HTTP/1.1 keep-alive means one handler instance serves a browser for
    its whole session, so swapping RequestHandlerClass only affects *new*
    connections -- the open one keeps writing into an orphaned canvas and the
    playground goes quietly dead after any rebuild.
    """

    def __init__(self, geometry: dict, canvas: "Canvas"):
        self.geometry = geometry
        self.canvas = canvas


class Telemetry:
    """Latest analysis values, written by the render loop, read by the UI.

    Versioned and condition-backed so the event stream can block until there
    is genuinely something new, rather than the browser asking ten times a
    second and mostly being told nothing changed.
    """

    def __init__(self):
        self._cv = threading.Condition()
        self.data: dict = {}
        self.version = 0

    def set(self, **kw) -> None:
        with self._cv:
            self.data.update(kw)
            self.version += 1
            self._cv.notify_all()

    def get(self) -> dict:
        with self._cv:
            return dict(self.data)

    def wait(self, since: int, timeout: float = 1.0) -> tuple[int, dict]:
        with self._cv:
            if self.version == since:
                self._cv.wait(timeout)
            return self.version, dict(self.data)


def _handler(params: Params, telem: Telemetry, patch_text: str,
             rig: Rig, devices):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):        # keep the terminal for the show
            pass

        def _send(self, code, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _stream(self):
            """Server-sent events: one connection, pushed on change.

            SSE rather than websockets because this channel is one-way --
            controls stay ordinary POSTs -- and EventSource reconnects on its
            own. Chunked because HTTP/1.1 needs a framing the response length
            can't provide.
            """
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

            def chunk(payload: bytes) -> None:
                self.wfile.write(b"%X\r\n" % len(payload) + payload + b"\r\n")
                self.wfile.flush()

            seen = -1
            try:
                while True:
                    ver, data = telem.wait(seen, 1.0)
                    if ver == seen:
                        chunk(b": ping\n\n")      # keep proxies from idling us out
                        continue
                    seen = ver
                    body = json.dumps({"telemetry": data,
                                       "params": params.snapshot()}).encode()
                    chunk(b"data: " + body + b"\n\n")
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass                                # the tab went away

        def _font(self, key: str):
            got = font_payload(key)
            if got is None:
                return self._send(404, b"no such font", "text/plain")
            body, gz = got
            self.send_response(200)
            self.send_header("Content-Type", "font/ttf")
            if gz:
                self.send_header("Content-Encoding", "gzip")
            # Immutable: the file only changes if the system font changes.
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.startswith("/font/"):
                return self._font(self.path[6:].split("?")[0])
            if self.path.startswith("/events"):
                return self._stream()
            if self.path.startswith("/state"):
                body = json.dumps({"params": params.snapshot(),
                                   "telemetry": telem.get(),
                                   "patch": patch_text}).encode()
                return self._send(200, body, "application/json")
            if self.path.startswith("/devices"):
                return self._send(200, json.dumps(devices.state()).encode(),
                                  "application/json")
            if self.path.startswith("/patch"):
                return self._send(200, json.dumps(rig.geometry).encode(),
                                  "application/json")
            if self.path.startswith("/canvas"):
                return self._send(200, json.dumps(rig.canvas.snapshot()).encode(),
                                  "application/json")
            if self.path.split("?")[0] in ("/", "/index.html"):
                return self._send(200, page().encode(), "text/html; charset=utf-8")
            self._send(404, b"not found", "text/plain")

        def do_POST(self):
            if self.path.startswith("/devices"):
                n = int(self.headers.get("Content-Length", 0) or 0)
                try:
                    msg = json.loads(self.rfile.read(n) or b"{}")
                except json.JSONDecodeError:
                    return self._send(400, b"bad json", "text/plain")
                out = devices.command(msg if isinstance(msg, dict) else {})
                return self._send(200, json.dumps(out).encode(),
                                  "application/json")
            if self.path.startswith("/canvas"):
                n = int(self.headers.get("Content-Length", 0) or 0)
                try:
                    msg = json.loads(self.rfile.read(n) or b"{}")
                except json.JSONDecodeError:
                    return self._send(400, b"bad json", "text/plain")
                if isinstance(msg, dict):
                    rig.canvas.apply(msg)
                return self._send(200, b"{}", "application/json")
            if not self.path.startswith("/set"):
                return self._send(404, b"not found", "text/plain")
            n = int(self.headers.get("Content-Length", 0) or 0)
            try:
                changes = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                return self._send(400, b"bad json", "text/plain")
            params.update(changes if isinstance(changes, dict) else {})
            self._send(200, json.dumps(params.snapshot()).encode(),
                       "application/json")

    return H


class ControlServer:
    """Unauthenticated by design -- bind it to localhost unless you mean it.

    Anyone who can reach the port can drive the lights, so the default is
    127.0.0.1 and reaching it from a phone is an explicit opt-in.
    """

    def __init__(self, params: Params, telem: Telemetry, patch_text: str,
                 rig: Rig, devices,
                 host: str = "127.0.0.1", port: int = 8721):
        self.params, self.telem, self.rig = params, telem, rig
        self._patch_text, self._devices = patch_text, devices
        self.httpd = ThreadingHTTPServer(
            (host, port), _handler(params, telem, patch_text, rig, devices))
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self._t = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self) -> None:
        self._t.start()

    def stop(self) -> None:
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except Exception:
            pass


def geometry_of(fixtures) -> dict:
    """Shape of each fixture, so the UI can draw the rig as it physically is."""
    out = {}
    for name, f in fixtures.items():
        out[name] = {
            "n": f.n,
            "kind": f.kind,
            "origin": list(f.origin),
            "slow": bool(f.slow),
            "angle": ([float(a) for a in f.angle] if f.angle is not None
                      else None),
            "matrix": getattr(f, "matrix", None),
        }
    return out
