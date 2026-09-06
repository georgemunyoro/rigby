"""Orchestrator HTTP routes, shared by the live desk and standalone editor."""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit, unquote

from .arrangement import validate_project
from .control import geometry_of
from .editor_ui import PAGE
from .rig_layout_ui import PAGE as LAYOUT_PAGE
from .orchestrator import Orchestrator, virtual_fixtures


def route(handler, editor, method):
    url = urlsplit(handler.path)
    if url.path not in (
        "/orchestrator",
        "/orchestrator/",
        "/rig-layout",
        "/rig-layout/",
        "/api/rig-layout",
    ) and not url.path.startswith(("/api/orchestrator/", "/api/rig-layout/")):
        return False

    def send(code, value):
        handler._send(
            code, json.dumps(value, allow_nan=False).encode(), "application/json"
        )

    try:
        if url.path.rstrip("/") == "/rig-layout":
            handler._send(200, LAYOUT_PAGE.encode(), "text/html; charset=utf-8")
            return True
        if url.path.rstrip("/") == "/orchestrator":
            handler._send(200, PAGE.encode(), "text/html; charset=utf-8")
            return True
        action = url.path.removeprefix("/api/orchestrator/")
        if url.path.startswith("/api/rig-layout"):
            action = "layout" + url.path.removeprefix("/api/rig-layout").rstrip("/")
        query = parse_qs(url.query)

        def layout_config(config):
            return {
                key: config[key]
                for key in ("layout", "revision", "can_identify", "error")
            }

        if method == "GET":
            if action == "layout":
                send(200, layout_config(editor.ai.config()))
            elif action == "layout/geometry":
                send(200, geometry_of(editor.fixtures))
            elif action == "state":
                send(200, editor.state())
            elif action == "ai/config":
                send(200, editor.ai.config())
            elif action == "ai/job":
                send(200, editor.ai.status(query.get("id", [None])[0]))
            elif action == "ai/preview":
                proposed_track = editor.ai.proposal_track(query.get("id", [""])[0])
                send(
                    200,
                    editor.preview(
                        proposed_track["id"],
                        float(query.get("time", ["0"])[0]),
                        proposed_track,
                    ),
                )
            elif action == "geometry":
                send(200, geometry_of(editor.fixtures))
            elif action == "analysis":
                send(200, editor.metadata_for(query.get("track", [""])[0]))
            elif action == "preview":
                send(
                    200,
                    editor.preview(
                        query.get("track", [""])[0], float(query.get("time", ["0"])[0])
                    ),
                )
            elif action == "audio":
                with editor.lock:
                    track = editor.find_track(query.get("track", [""])[0])
                    path = editor.directory / (track["audio"] + ".wav")
                size = path.stat().st_size
                start, end = 0, size - 1
                requested = handler.headers.get("Range")
                if requested:
                    import re

                    match = re.fullmatch(r"bytes=(\d+)-(\d*)", requested)
                    if not match:
                        raise ValueError("Unsupported audio byte range")
                    start = int(match[1])
                    end = min(int(match[2]) if match[2] else end, size - 1)
                    if start > end:
                        handler.send_response(416)
                        handler.send_header("Content-Range", f"bytes */{size}")
                        handler.send_header("Content-Length", "0")
                        handler.end_headers()
                        return True
                handler.send_response(206 if requested else 200)
                handler.send_header("Content-Type", "audio/wav")
                handler.send_header("Accept-Ranges", "bytes")
                handler.send_header("Content-Length", str(end - start + 1))
                if requested:
                    handler.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                handler.end_headers()
                with path.open("rb") as stream:
                    stream.seek(start)
                    left = end - start + 1
                    while left:
                        chunk = stream.read(min(left, 65536))
                        if not chunk:
                            break
                        handler.wfile.write(chunk)
                        left -= len(chunk)
            else:
                send(404, {"error": "Unknown editor endpoint"})
        elif method == "POST":
            length = int(handler.headers.get("Content-Length", "0"))
            if action == "upload":
                editor.import_audio(
                    handler.rfile,
                    length,
                    unquote(handler.headers.get("X-Filename", "Track")),
                )
                send(202, {"accepted": True})
                return True
            if not 0 <= length <= 4 * 1024 * 1024:
                raise ValueError("Arrangement request too large")
            msg = json.loads(handler.rfile.read(length) or b"{}")
            if not isinstance(msg, dict):
                raise ValueError("Expected an object")
            if action == "layout":
                # Layout-only editing must preserve musical preferences.
                send(
                    200,
                    layout_config(
                        editor.ai.save_settings(
                            {k: msg[k] for k in ("layout", "revision") if k in msg}
                        )
                    ),
                )
            elif action in ("ai/identify", "layout/identify"):
                send(200, editor.ai.identify(msg))
            elif action == "ai/settings":
                send(200, editor.ai.save_settings(msg))
            elif action == "ai/generate":
                send(202, editor.ai.start(msg))
            elif action == "ai/cancel":
                send(200, editor.ai.cancel(msg.get("id")))
            elif action == "ai/apply":
                send(200, editor.ai.apply(msg.get("id")))
            elif action == "project":
                send(200, editor.replace(msg.get("project"), msg.get("revision")))
            elif action == "proposal":
                proposed = validate_project(msg.get("project"))
                # Validation is read-only. Applying a proposal uses the normal
                # revision-checked project endpoint, after local review.
                send(
                    200,
                    {
                        "project": proposed,
                        "tracks": len(proposed["tracks"]),
                        "clips": sum(
                            len(t.get("clips", [])) for t in proposed["tracks"]
                        ),
                    },
                )
            elif action == "transport":
                send(200, editor.transport(msg))
            elif action == "generate":
                from .arrangement import generated_clips

                state = editor.state()
                if msg.get("revision") != state["revision"]:
                    raise RuntimeError("Set changed; reload first")
                track = next(
                    (
                        t
                        for t in state["project"]["tracks"]
                        if t["id"] == msg.get("track")
                    ),
                    None,
                )
                if track is None:
                    raise ValueError("Unknown track")
                metadata = editor.metadata_for(track["id"])
                if not metadata:
                    raise ValueError("Analysis is not ready")
                track["clips"] = generated_clips(metadata, track["clips"])
                send(200, editor.replace(state["project"], state["revision"]))
            else:
                send(404, {"error": "Unknown editor endpoint"})
        else:
            send(405, {"error": "Method not allowed"})
    except (BrokenPipeError, ConnectionResetError):
        return True
    except RuntimeError as e:
        send(409, {"error": str(e)})
    except (ValueError, TypeError, KeyError, OSError) as e:
        handler.close_connection = True
        send(400, {"error": str(e)})
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Rigby arrangement editor (virtual fixtures, no OpenRGB required)"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8722)
    parser.add_argument("--directory", default=None)
    args = parser.parse_args()
    try:
        editor = Orchestrator(virtual_fixtures(), args.directory)
    except RuntimeError as e:
        parser.error(str(e))

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.split("?")[0] == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
            if self.path == "/":
                self.path = "/orchestrator"
            if not route(self, editor, "GET"):
                self._send(404, b"Not found", "text/plain")

        def do_POST(self):
            if not route(self, editor, "POST"):
                self._send(404, b"Not found", "text/plain")

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    print(
        f"Orchestrator: http://{args.host}:{server.server_address[1]}/orchestrator",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        editor.close()


if __name__ == "__main__":
    main()
