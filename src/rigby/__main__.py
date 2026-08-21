"""rigby -- audio-reactive lighting desk for OpenRGB."""

from __future__ import annotations

import argparse
import signal
import sys
import time

import numpy as np

from .analyze import Analyzer, Features, default_monitor
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
    print(f"\rin {db:7.1f} dBFS  lvl {f.level:4.2f}  bass {f.bass:4.2f}  "
          f"bands [{bars}]  -> out {out_max:4.2f} "
          f"{'HIT' if f.onset else '   '}", end="", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(prog="rigby", description=__doc__)
    ap.add_argument("--look", default="spectrum", choices=sorted(LOOKS))
    ap.add_argument("--palette", default="sunset",
                    choices=["sunset", "cyanmag", "acid", "ice"])
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
    ap.add_argument("--gain", type=float, default=1.0,
                    help="pre-curve drive on control signals")
    ap.add_argument("--curve", type=float, default=0.55,
                    help="brightness curve; <1 lifts the low end (0.5 = punchy)")
    ap.add_argument("--play", action=argparse.BooleanOptionalAction, default=None,
                    help="play a file: source out loud (default: on for files)")
    ap.add_argument("--meter", action="store_true",
                    help="print an input level meter instead of driving lights")
    ap.add_argument("--no-audio", action="store_true",
                    help="run on a wall clock with no audio (rig check)")
    ap.add_argument("--seconds", type=float, default=0.0,
                    help="run for N seconds then blackout (0 = forever)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=6742)
    args = ap.parse_args()

    try:
        sink = Sink(args.host, args.port, gamma_val=args.gamma, master=args.master)
    except Exception as e:
        print(f"cannot reach OpenRGB SDK at {args.host}:{args.port} -- "
              f"is `openrgb --server` running?\n  {e}", file=sys.stderr)
        return 1

    if not sink.fixtures:
        print("no fixtures resolved -- check `openrgb --list-devices`", file=sys.stderr)
        return 1

    sink.set_direct()
    print(f"patch ({sum(f.n for f in sink.fixtures.values())} leds live):")
    print(sink.describe(), flush=True)

    look = LOOKS[args.look](sink.fixtures, palette=args.palette,
                            gain=args.gain, curve=args.curve)

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
                          play=play)
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
    silent = Features(bands=np.zeros(8, dtype=np.float32), level=0.0, rms=0.0,
                      bass=0.0, onset=False, flux=0.0)
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

            look.step(dt, f)
            frame = look.render(f)

            if args.meter:
                _meter(f, frame)
            else:
                sink.write(frame)

            if args.seconds and time.monotonic() - started >= args.seconds:
                break
    finally:
        if an is not None:
            an.stop()
        sink.blackout()
        print("\nblackout.")

    return rc


if __name__ == "__main__":
    raise SystemExit(main())
