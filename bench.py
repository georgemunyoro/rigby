"""Deterministic music/output scoring, including device cadence and event timing."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from rigby.analyze import Analyzer
from rigby.patch import Fixture
from rigby.show import LOOKS
from rigby.fx import encode_rgb


def to_led(v, g=2.2, master=1.0, min_lit=3):
    return encode_rgb(v, g, master, min_lit)


def fake_fixtures():
    """Representative small rig with fast rings and slow lines."""
    from rigby.patch import RING, LINE, ORIGINS

    def ring(name, n, spin=1.0):
        return Fixture(
            name,
            0,
            0,
            n,
            0,
            np.linspace(0, 1, n, dtype=np.float32),
            False,
            kind=RING,
            angle=(np.arange(n, dtype=np.float32) / n),
            origin=ORIGINS.get(name, (0.5, 0.5)),
            spin=spin,
        )

    def line(name, n, slow=False):
        pos = (
            np.linspace(0, 1, n, dtype=np.float32)
            if n > 1
            else np.array([0.5], dtype=np.float32)
        )
        return Fixture(
            name, 0, 0, n, 0, pos, slow, kind=LINE, origin=ORIGINS.get(name, (0.5, 0.5))
        )

    # One fan ring, not three: the hub mirrors it to every port.
    return {
        "fans": ring("fans", 6),
        "aio": ring("aio", 18, -1.0),
        "mobo": line("mobo", 4),
        "ram_a": line("ram_a", 8, slow=True),
        "ram_b": line("ram_b", 8, slow=True),
        "gpu": line("gpu", 1, slow=True),
    }


def run(
    path,
    gt,
    look_name="auto",
    fps=60,
    trace=False,
    master=1.0,
    gamma=2.2,
    min_lit=3,
    noise_floor_db=-72.0,
    offset_ms=0,
    **kw,
):
    analyzer = Analyzer(
        f"file:{path}", fps=fps, noise_floor_db=noise_floor_db, offset_ms=offset_ms
    )
    fixtures = fake_fixtures()
    look = LOOKS[look_name](fixtures, **kw)
    onsets, bright, frames, telemetry = [], [], [], []
    output = {k: np.zeros((f.n, 3), np.uint8) for k, f in fixtures.items()}
    due = {k: 0.0 for k in fixtures}
    analyzer.start()
    analyzer._paced = False
    try:
        while not analyzer.eof:
            f = analyzer.read()
            if f is None:
                continue
            look.step(1 / fps, f)
            frame = look.render(f)
            t = f.timestamp
            # Low/mid/high evidence from one physical attack may arrive on adjacent hops.
            for event in sorted(f.events, key=lambda e: e.time):
                if not onsets or event.time - onsets[-1] > 0.06:
                    onsets.append(event.time)
            for key, fix in fixtures.items():
                if t + 1e-9 >= due[key]:
                    output[key] = to_led(
                        frame.get(key, np.zeros((fix.n, 3))), gamma, master, min_lit
                    )
                    period = 1 / (12 if fix.slow else 60)
                    due[key] = (
                        t + period
                        if fix.slow
                        else (np.floor(t / period + 1e-9) + 1) * period
                    )
            led = np.concatenate(list(output.values())).astype(np.float32)
            frames.append(led)
            bright.append((t, float(led.mean())))
            telemetry.append(
                dict(
                    time=t,
                    bpm=f.bpm,
                    pulse=f.pulse,
                    phase=f.beat_position % 1,
                    presence=f.presence,
                    scene=look.director.scene,
                    level=f.level,
                    dynamics=f.dynamics,
                    swell=f.swell,
                    energy_slope=f.energy_slope,
                    spectral_change=f.spectral_change,
                    density=f.density,
                    bands=f.bands.tolist(),
                    render_time=analyzer._render_tick / fps,
                    events=[
                        dict(
                            time=e.time,
                            kind=e.kind,
                            confidence=e.confidence,
                            strength=e.strength,
                            delivered=analyzer._render_tick / fps,
                        )
                        for e in f.events
                    ],
                )
            )
    finally:
        analyzer.stop()
    result = np.array(onsets), np.array(bright), np.array(frames)
    return (*result, telemetry) if trace else result


def interval_mask(times, intervals):
    mask = np.zeros(len(times), dtype=bool)
    if not intervals:
        return mask
    if isinstance(intervals[0], (int, float)):
        intervals = [intervals]
    for start, end in intervals:
        mask |= (times >= start) & (times < end)
    return mask


def match_events(onsets, truth, tolerance):
    # Globally nearest one-to-one pairs, including empty/silent ground truth.
    candidates = sorted(
        (abs(o - h), i, j)
        for i, o in enumerate(onsets)
        for j, h in enumerate(truth)
        if abs(o - h) <= tolerance
    )
    used_o, used_h, errors = set(), set(), []
    for _, i, j in candidates:
        if i not in used_o and j not in used_h:
            used_o.add(i)
            used_h.add(j)
            errors.append(float(onsets[i] - truth[j]))
    return used_o, used_h, errors


def score(onsets, bright, frames, gt, tol=0.06, telemetry=None):
    primary_hits = np.asarray(gt.get("hits", []))
    hits = np.asarray(sorted(set(gt.get("hits", []) + gt.get("texture_hits", []))))
    used_o, used_h, errors = match_events(onsets, hits, tol)
    tp = len(used_o)
    precision, recall = tp / max(len(onsets), 1), tp / max(len(hits), 1)
    result = dict(
        onsets=len(onsets),
        truth=len(hits),
        tp=tp,
        fp=len(onsets) - tp,
        fn=len(hits) - tp,
        precision=precision,
        recall=recall,
        f1=2 * precision * recall / max(precision + recall, 1e-9),
        timing_bias_ms=float(np.mean(errors) * 1000) if errors else None,
        timing_p95_ms=float(np.percentile(np.abs(errors), 95) * 1000)
        if errors
        else None,
    )
    primary_matched, _, _ = match_events(onsets, primary_hits, tol)
    result["primary_recall"] = len(primary_matched) / max(len(primary_hits), 1)
    if not len(bright):
        return result
    times, values = bright.T
    quiet = interval_mask(times, gt.get("quiet"))
    silence = interval_mask(times, gt.get("silence"))
    loud = interval_mask(times, gt.get("loud", gt.get("belt")))
    if not loud.any() and quiet.any():
        loud = ~quiet & ~silence
    result.update(
        loud_mean=float(values[loud].mean()) if loud.any() else None,
        quiet_mean=float(values[quiet].mean()) if quiet.any() else None,
        loud_vs_quiet=(
            float(values[loud].mean() / max(values[quiet].mean(), 1e-9))
            if loud.any() and quiet.any()
            else None
        ),
        clipping_fraction=float((frames >= 254).mean()),
        frac_fully_dark=float((frames.max(axis=(1, 2)) == 0).mean()),
        silence_brightness=float(values[silence].mean()) if silence.any() else None,
    )
    delta = np.abs(np.diff(frames, axis=0)).mean(axis=(1, 2))
    mask = np.ones(len(delta), dtype=bool)
    for onset in onsets:
        mask &= np.abs(times[1:] - onset) > 0.15
    result["motion_delta"] = float(delta[mask].mean()) if mask.any() else None
    if telemetry:
        phase_errors, delivery_errors = [], []
        stable = [r["time"] for r in telemetry if r["pulse"] > 0.5]
        result["first_lock_s"] = stable[0] if stable else None
        sustained = gt.get("sustained", [])
        sustained_events = [
            e["time"]
            for r in telemetry
            for e in r["events"]
            if interval_mask(np.array([e["time"]]), sustained).any()
        ]
        result["sustained_false_events"] = len(sustained_events)
        recoveries = []
        for section in gt.get("sections", []):
            start, end, bpm = section["start"], section["end"], section["bpm"]
            good_since = None
            recovery = None
            for row in telemetry:
                if not start <= row["time"] < end:
                    continue
                if row["pulse"] > 0.4 and abs(row["bpm"] / bpm - 1) < 0.05:
                    if good_since is None:
                        good_since = row["time"]
                    if row["time"] - good_since >= 1:
                        recovery = good_since - start
                        break
                else:
                    good_since = None
            recoveries.append(recovery)
        result["tempo_recovery_s"] = recoveries
        beat_times = np.asarray(gt.get("beats", []))
        for row in telemetry:
            t = row["time"]
            if row["pulse"] > 0.4 and t > 5:
                phase = None
                if len(beat_times) >= 2 and beat_times[0] <= t < beat_times[-1]:
                    i = int(np.searchsorted(beat_times, t, side="right") - 1)
                    phase = (t - beat_times[i]) / (beat_times[i + 1] - beat_times[i])
                elif gt.get("bpm") and not len(beat_times):
                    phase = (t - gt.get("beat_offset", 0)) * gt["bpm"] / 60
                if phase is not None:
                    phase_errors.append(abs((row["phase"] - phase + 0.5) % 1 - 0.5))
            for event in row["events"]:
                if len(hits):
                    nearest = hits[np.argmin(np.abs(hits - event["time"]))]
                    if abs(nearest - event["time"]) <= tol:
                        delivery_errors.append(event["delivered"] - nearest)
        result.update(
            beat_phase_mae=float(np.mean(phase_errors)) if phase_errors else None,
            delivery_p95_ms=float(np.percentile(delivery_errors, 95) * 1000)
            if delivery_errors
            else None,
            mean_pulse=float(np.mean([r["pulse"] for r in telemetry])),
            scene_changes=sum(
                a["scene"] != b["scene"] for a, b in zip(telemetry, telemetry[1:])
            ),
        )
    return {k: round(v, 4) if isinstance(v, float) else v for k, v in result.items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio", nargs="?")
    parser.add_argument("annotations", nargs="?")
    parser.add_argument("--look", choices=LOOKS, default="auto")
    parser.add_argument("--fps", type=int, default=60)
    parser.add_argument("--gain", type=float, default=1.6)
    parser.add_argument("--curve", type=float, default=0.45)
    parser.add_argument("--master", type=float, default=1.0)
    parser.add_argument("--gamma", type=float, default=2.2)
    parser.add_argument("--min-lit", type=int, default=3)
    parser.add_argument("--noise-floor-db", type=float, default=-72.0)
    parser.add_argument("--offset-ms", type=int, default=0)
    parser.add_argument(
        "--corpus",
        type=Path,
        help="JSON list of {audio, annotations}, paths relative to manifest",
    )
    parser.add_argument(
        "--trace", type=Path, help="write timestamped feature/event trace as JSON"
    )
    args = parser.parse_args()
    if args.corpus:
        entries = json.loads(args.corpus.read_text())
        entries = [
            (args.corpus.parent / r["audio"], args.corpus.parent / r["annotations"])
            for r in entries
        ]
    elif args.audio and args.annotations:
        entries = [(Path(args.audio), Path(args.annotations))]
    else:
        parser.error("supply audio and annotations, or --corpus")
    reports, traces = {}, {}
    for path, annotation in entries:
        gt = json.loads(annotation.read_text())
        onsets, bright, frames, telemetry = run(
            path,
            gt,
            args.look,
            args.fps,
            trace=True,
            gain=args.gain,
            curve=args.curve,
            master=args.master,
            gamma=args.gamma,
            min_lit=args.min_lit,
            noise_floor_db=args.noise_floor_db,
            offset_ms=args.offset_ms,
        )
        reports[str(path)] = score(onsets, bright, frames, gt, telemetry=telemetry)
        traces[str(path)] = telemetry
    print(json.dumps(reports, indent=2, allow_nan=False))
    if args.trace:
        args.trace.write_text(json.dumps(traces, allow_nan=False) + "\n")


if __name__ == "__main__":
    main()
