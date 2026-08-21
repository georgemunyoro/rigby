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
    print(sink.describe())

    look = LOOKS[args.look](sink.fixtures, palette=args.palette)

    an = None
    if not args.no_audio:
        try:
            an = Analyzer(args.source, fps=args.fps, offset_ms=args.offset_ms)
            an.start()
            print(f"listening: {an.source}"
                  + (f"  (+{args.offset_ms}ms offset)" if args.offset_ms else ""))
        except Exception as e:
            print(f"audio unavailable ({e}); falling back to --no-audio",
                  file=sys.stderr)
            an = None

    print(f"look={args.look} palette={args.palette}  -- ctrl-c to blackout")

    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    silent = Features(bands=np.zeros(8, dtype=np.float32), level=0.0,
                      bass=0.0, onset=False, flux=0.0)
    dt = 1.0 / args.fps
    started = time.monotonic()

    try:
        while running:
            if an is not None:
                f = an.read()
                if f is None:
                    if an.eof:
                        break                     # source ended
                    continue                      # still filling the delay line
            else:
                f = silent
                time.sleep(dt)

            look.step(dt, f)
            sink.write(look.render(f))

            if args.seconds and time.monotonic() - started >= args.seconds:
                break
    finally:
        if an is not None:
            an.stop()
        sink.blackout()
        print("\nblackout.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
