"""rigby -- audio-reactive lighting desk for OpenRGB."""

from __future__ import annotations

import argparse
import signal
import sys
import time
from pathlib import Path

import numpy as np

from .analyze import Analyzer, Features, default_monitor
from .config import RigConfig, apply_zone_sizes, default_path
from .control import (REBUILD, Canvas, ControlServer, Params, Rig,
                       Telemetry, geometry_of)
from .devices import Devices
from .show import LOOKS
from .sink import Sink


_METER_N = [0]


def _meter(f, frame) -> None:
    """One line per ~8 frames: what came in, and what the rig would emit."""
    _METER_N[0] += 1
    if _METER_N[0] % 8:
        return
    db = 20 * np.log10(max(f.rms, 1e-9))
    bars = "".join("_.:-=+*#"[min(7, int(b * 8))] for b in f.bands)
    out_max = max((float(v.max()) for v in frame.values()), default=0.0)
    print(f"\rin {db:7.1f} dBFS  dyn {f.dynamics:4.2f}  swell {f.swell:4.2f}  "
          f"pulse {f.pulse:4.2f}  bands [{bars}]  -> out {out_max:4.2f} "
          f"{'HIT' if f.onset else '   '}", end="", flush=True)


def _frame_hex(frame) -> dict:
    """The frame as hex per fixture, so the UI can mirror the real rig."""
    out = {}
    for name, arr in frame.items():
        a = np.clip(np.asarray(arr, dtype=np.float32), 0.0, 1.0)
        if a.ndim != 2 or a.shape[0] == 0:
            out[name] = []
            continue
        v = (a * 255.0 + 0.5).astype(np.uint8)
        out[name] = ["#%02x%02x%02x" % tuple(int(c) for c in px) for px in v]
    return out


def _apply_live(look, an, sink, params, dirty) -> None:
    """Push changed knobs onto the running objects, no restart."""
    p = params.snapshot()
    for name in dirty:
        val = p[name]
        if name in ("gain", "curve", "saturation", "hot", "hit_style"):
            for obj in _look_tree(look):
                setattr(obj, name, val)
        elif name == "hue_drift":
            for obj in _look_tree(look):
                obj._drift = val
        elif name == "swing_min_beats":
            for obj in _look_tree(look):
                obj.swing_min_beats = max(1, int(val))
                obj.swing_max_beats = max(1, int(val)) * 4
        elif name == "palette":
            for obj in _look_tree(look):
                obj.palette = val
        elif name == "master":
            sink.master = val
        elif name == "gamma":
            sink.gamma = val
            sink._last.clear()          # force a redraw at the new curve
        elif name == "dynamics_db" and an is not None:
            an.DYN_RANGE_DB = float(val)
        elif name == "onset_k" and an is not None:
            an.ONSET_K = float(val)
        elif name == "offset_ms" and an is not None:
            an.set_offset(int(val))


def _look_tree(look):
    """A look, plus the sub-looks `auto` mixes between."""
    yield look
    for attr in ("beaty", "calm"):
        sub = getattr(look, attr, None)
        if sub is not None:
            yield sub


def _identify(sink) -> int:
    """Walk one LED at a time so the physical patch can be read off the case."""
    import numpy as _np
    print("\nlighting one LED at a time -- watch which fixture responds\n")
    for name, fix in sink.fixtures.items():
        print(f"  {name} ({fix.kind}, {fix.n} leds)", flush=True)
        for i in range(fix.n):
            frame = {k: _np.zeros((f.n, 3), dtype=_np.float32)
                     for k, f in sink.fixtures.items()}
            frame[name][i] = (1.0, 1.0, 1.0)
            sink._last.clear(); sink._next.clear()
            sink.write(frame)
            print(f"    led {i}", end="\r", flush=True)
            time.sleep(0.45)
        print("            ", end="\r")
    sink.blackout()
    print("done.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="rigby", description=__doc__)
    ap.add_argument("--look", default="spectrum", choices=sorted(LOOKS))
    ap.add_argument("--palette", default="sunset",
                    choices=["sunset", "cyanmag", "acid", "ice"])
    ap.add_argument("--duo", default="ember",
                    choices=["ember", "toxic", "vapor", "cobalt", "mono"],
                    help="two-tone pair for the duotone look")
    ap.add_argument("--config", default=None,
                    help=f"rig config file (default {default_path()})")
    ap.add_argument("--probe", metavar="ZONE",
                    help="discover a header's real layout, e.g. --probe 1 "
                         "(motherboard zone index, or 'Device name:zone')")
    ap.add_argument("--probe-size", type=int, default=60,
                    help="LED count to resize the zone to while probing")
    ap.add_argument("--probe-step", type=float, default=0.45,
                    help="seconds per LED during the walk")
    ap.add_argument("--probe-mode", default="both",
                    choices=["both", "all", "walk"])
    ap.add_argument("--identify", action="store_true",
                    help="walk the LEDs one at a time to map the patch")
    ap.add_argument("--source", default=None,
                    help="PipeWire source, or file:PATH.wav "
                         "(default: current sink's .monitor)")
    ap.add_argument("--fps", type=int, default=60)
    ap.add_argument("--offset-ms", type=int, default=0,
                    help="delay lights to match output latency; "
                         "try 200 on a Bluetooth sink")
    ap.add_argument("--master", type=float, default=1.0,
                    help="grand master 0..1")
    ap.add_argument("--gamma", type=float, default=2.2)
    ap.add_argument("--gain", type=float, default=1.6,
                    help="pre-curve drive on control signals")
    ap.add_argument("--curve", type=float, default=0.45,
                    help="brightness curve; <1 lifts the low end (0.5 = punchy)")
    ap.add_argument("--play", action=argparse.BooleanOptionalAction, default=None,
                    help="play a file: source out loud (default: on for files)")
    ap.add_argument("--dynamics-db", type=float, default=15.0,
                    help="dB below the running reference that reads as dark; "
                         "higher = flatter, lower = more dramatic")
    ap.add_argument("--hit-style", default="swing",
                    choices=["swing", "accent", "white"],
                    help="what a beat does to colour: swing to the opposite of "
                         "the wheel, jump to the accent hue, or flash white")
    ap.add_argument("--control", nargs="?", const=8721, type=int, default=None,
                    metavar="PORT",
                    help="serve a live control UI (default port 8721)")
    ap.add_argument("--control-host", default="127.0.0.1",
                    help="bind address for the control UI; 0.0.0.0 exposes it "
                         "to your network (no auth) so a phone can reach it")
    ap.add_argument("--saturation", type=float, default=0.88,
                    help="overall colour saturation, 0..1")
    ap.add_argument("--hot", type=float, default=0.5,
                    help="how far bright cores desaturate toward white; "
                         "0 keeps flat saturation")
    ap.add_argument("--swing-min-beats", type=int, default=6,
                    help="minimum beats between colour swings; ordinary beats "
                         "get the plain flash")
    ap.add_argument("--hue-drift", type=float, default=1.0,
                    help="speed of the slow colour drift; 0 pins the tones")
    ap.add_argument("--onset-k", type=float, default=1.7,
                    help="onset threshold in std devs; raise if it triggers "
                         "too eagerly")
    ap.add_argument("--meter", action="store_true",
                    help="print an input level meter instead of driving lights")
    ap.add_argument("--no-audio", action="store_true",
                    help="run on a wall clock with no audio (rig check)")
    ap.add_argument("--seconds", type=float, default=0.0,
                    help="run for N seconds then blackout (0 = forever)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=6742)
    args = ap.parse_args()

    cfg_path = Path(args.config) if args.config else default_path()
    cfg = RigConfig.load(cfg_path)
    # Explicit flags win over the saved file, so a one-off run can still
    # override calibration without editing it.
    given = {a.split("=")[0].lstrip("-").replace("-", "_") for a in sys.argv[1:]}
    try:
        sink = Sink(args.host, args.port, gamma_val=args.gamma,
                    master=args.master, groups=cfg.groups,
                    overrides=cfg.fixtures)
    except Exception as e:
        print(f"cannot reach OpenRGB SDK at {args.host}:{args.port} -- "
              f"is `openrgb --server` running?\n  {e}", file=sys.stderr)
        return 1

    if not sink.fixtures:
        print("no fixtures resolved -- check `openrgb --list-devices`", file=sys.stderr)
        return 1

    for note in apply_zone_sizes(sink.client, cfg.zones):
        print(f"  zone: {note}")
    if cfg.zones:
        sink.rebuild()

    sink.set_direct()
    print(f"patch ({sum(f.n for f in sink.fixtures.values())} leds live):")
    print(sink.describe(), flush=True)

    params = Params(look=args.look, duo=args.duo, palette=args.palette,
                    hit_style=args.hit_style, master=args.master,
                    gain=args.gain, curve=args.curve, gamma=args.gamma,
                    saturation=args.saturation, hot=args.hot,
                    hue_drift=args.hue_drift, dynamics_db=args.dynamics_db,
                    onset_k=args.onset_k,
                    swing_min_beats=args.swing_min_beats,
                    offset_ms=args.offset_ms)

    def build_look():
        kw = {"palette": params.palette, "gain": params.gain,
              "curve": params.curve}
        if params.look in ("duotone", "rain", "auto"):
            kw.update(duo=params.duo, hue_drift=params.hue_drift,
                      hit_style=params.hit_style,
                      swing_min_beats=params.swing_min_beats,
                      saturation=params.saturation, hot=params.hot)
        return LOOKS[params.look](sink.fixtures, **kw)

    look = build_look()

    if args.probe:
        from .probe import run as probe_run
        return probe_run(sink.client, args.probe, args.probe_size,
                         args.probe_step, args.probe_mode)

    if args.identify:
        return _identify(sink)

    an = None
    if not args.no_audio:
        try:
            is_file = bool(args.source and args.source.startswith("file:"))
            # Playing a file is almost always what you want when watching the
            # show; it's only unwanted when programming against a track silently.
            play = is_file if args.play is None else args.play
            if play and not is_file:
                print("--play only applies to file: sources; ignoring",
                      file=sys.stderr)
                play = False
            an = Analyzer(args.source, fps=args.fps, offset_ms=args.offset_ms,
                          play=play, dynamics_db=args.dynamics_db,
                          onset_k=args.onset_k)
            an.start()
            print(f"listening: {an.source}"
                  + ("  (playing)" if play else "")
                  + (f"  (+{args.offset_ms}ms offset)" if args.offset_ms else ""))
        except Exception as e:
            # An explicit --source is a request, not a preference. Silently
            # dropping to --no-audio just hides why it failed.
            if args.source:
                print(f"cannot read --source {args.source}: {e}", file=sys.stderr)
                return 1
            print(f"audio unavailable ({e}); falling back to --no-audio",
                  file=sys.stderr)
            an = None

    telem = Telemetry()
    geometry = geometry_of(sink.fixtures)
    rig = Rig(geometry, Canvas(geometry))
    devices = Devices(sink, cfg, cfg_path)
    control = None
    if args.control:
        try:
            control = ControlServer(params, telem, sink.describe(),
                                    rig, devices,
                                    host=args.control_host, port=args.control)
            control.start()
            where = ("localhost" if args.control_host in ("127.0.0.1", "localhost")
                     else args.control_host)
            print(f"control ui: http://{where}:{control.port}"
                  + ("  (exposed to your network, no auth)"
                     if args.control_host == "0.0.0.0" else ""), flush=True)
        except OSError as e:
            print(f"could not start control ui on port {args.control}: {e}",
                  file=sys.stderr)

    print(f"look={args.look} palette={args.palette}  -- ctrl-c to blackout",
          flush=True)

    running = True
    rc = 0

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    quiet_for = 0
    warned = False
    frames = 0
    last_report = time.monotonic()
    # --no-audio is for checking the rig and calibrating it, so it has to be
    # visible. dynamics=0 multiplies every look that respects it down to black,
    # which reads as "the device isn't working".
    silent = Features(bands=np.zeros(8, dtype=np.float32),
                      bands_slow=np.full(8, 0.35, dtype=np.float32),
                      level=0.35, dynamics=1.0, rms=0.0,
                      bass=0.25, onset=False, flux=0.0, swell=0.45)
    dt = 1.0 / args.fps
    started = time.monotonic()

    try:
        while running:
            if an is not None:
                f = an.read()
                if f is None:
                    if an.eof:
                        if an.error:
                            print(f"source error: {an.error}", file=sys.stderr)
                            rc = 1
                        elif an.frames == 0:
                            print(f"source produced no audio: {an.source}",
                                  file=sys.stderr)
                            rc = 1
                        break                     # source ended
                    continue                      # still filling the delay line
            else:
                f = silent
                time.sleep(dt)

            # A bogus PipeWire target doesn't fail -- it just delivers digital
            # silence forever. Say so rather than looking merely broken.
            if an is not None and not warned:
                quiet_for = quiet_for + 1 if f.rms <= 0.0 else 0
                if quiet_for > args.fps * 3:
                    print(f"\nno signal on {an.source} after 3s -- check the "
                          f"sink volume (monitors are post-volume) or run "
                          f"--meter", file=sys.stderr, flush=True)
                    warned = True

            if devices.take_rebuild():
                sink.rebuild(groups=cfg.groups, overrides=cfg.fixtures)
                rig.geometry = geometry_of(sink.fixtures)
                rig.canvas = Canvas(rig.geometry)
                look = build_look()
                print(f"\npatch rebuilt: "
                      f"{sum(f.n for f in sink.fixtures.values())} leds", flush=True)

            # Live knobs: applied between frames, so nothing has to restart.
            dirty = params.take_dirty()
            if dirty:
                if dirty & REBUILD:
                    look = build_look()
                else:
                    _apply_live(look, an, sink, params, dirty)

            if not params.pause:
                look.step(dt, f)
            if sink.raw != params.playground:
                sink.raw = params.playground
                sink._last.clear()          # curve changed; force a redraw
            if params.playground:
                # Hand control to the canvas entirely -- the look still steps,
                # so switching back resumes mid-gesture rather than restarting.
                cells = rig.canvas.snapshot()
                frame = {k: np.asarray(cells.get(k, []), dtype=np.float32)
                         .reshape(-1, 3)[:fx.n]
                         for k, fx in sink.fixtures.items()}
                look.render(f)
            else:
                frame = look.render(f)
            if params.blackout:
                frame = {k: v * 0.0 for k, v in frame.items()}

            hl = devices.active_highlight()
            if hl is not None:
                name, led = hl
                # Identify overrides everything: dark rig, one thing lit.
                frame = {k: v * 0.05 for k, v in frame.items()}
                if name in frame and frame[name].size:
                    if led is None:
                        frame[name][:] = 1.0
                    elif 0 <= led < frame[name].shape[0]:
                        frame[name][led] = (1.0, 1.0, 1.0)

            if args.meter:
                _meter(f, frame)
            else:
                sink.write(frame)

            if control is not None:
                frames += 1
                now = time.monotonic()
                if now - last_report >= 0.2:
                    telem.set(frame=_frame_hex(frame),
                              fps=frames / max(now - last_report, 1e-6),
                              dbfs=(20 * np.log10(max(f.rms, 1e-9))),
                              level=f.level, dynamics=f.dynamics,
                              swell=f.swell, pulse=f.pulse, onset=bool(f.onset),
                              bands=[float(x) for x in f.bands],
                              out=max((float(v.max()) for v in frame.values()),
                                      default=0.0))
                    frames, last_report = 0, now

            if args.seconds and time.monotonic() - started >= args.seconds:
                break
    finally:
        if control is not None:
            control.stop()
        if an is not None:
            an.stop()
        sink.blackout()
        print("\nblackout.")

    return rc


if __name__ == "__main__":
    raise SystemExit(main())
