"""Fixed 100 Hz audio analysis, timestamped events, and independent rendering.

The capture worker processes every audio hop. Renderers sample bounded feature
history; they never re-analyse stale windows or replay a backlog of old hits.
The same sample clock is used for live capture and deterministic file analysis.
"""

from __future__ import annotations

import collections
import math
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, replace

import numpy as np

from .music import BeatClock, alpha

RATE = 48000
ANALYSIS_HZ = 100


@dataclass(frozen=True)
class Onset:
    time: float  # estimated audio attack time, seconds
    strength: float
    confidence: float
    kind: str  # low, mid, high; not an instrument classifier


@dataclass
class Features:
    bands: np.ndarray
    bands_slow: np.ndarray
    level: float
    dynamics: float
    rms: float
    bass: float
    onset: bool
    flux: float
    pulse: float = 0.0
    swell: float = 0.0
    onset_strength: float = 0.0
    timestamp: float = 0.0
    presence: float = 1.0  # hand-built/no-audio features remain visible
    balance: np.ndarray | None = None
    events: tuple[Onset, ...] = ()
    bpm: float = 0.0
    beat_position: float = 0.0
    bar_confidence: float = 0.0
    bar_offset: int = 0
    density: float = 0.0
    energy_slope: float = 0.0
    spectral_change: float = 0.0
    mid_share: float = 0.0
    analysis_age_ms: float = 0.0  # host arrival age, not physical end-to-end latency


def default_monitor() -> str:
    sink = subprocess.run(
        ["pactl", "get-default-sink"], capture_output=True, text=True
    ).stdout.strip()
    if not sink:
        raise RuntimeError("no default PipeWire sink")
    return sink + ".monitor"


class Analyzer:
    DYN_RANGE_DB = 15.0
    DYN_HEAD_DB = 5.0
    REF_TAU_S = 25.0
    ONSET_K = 1.7
    MAX_OFFSET_MS = 2000
    EVENT_MAX_AGE = 0.15

    def __init__(
        self,
        source=None,
        fps=60,
        n_bands=8,
        fmin=40.0,
        fmax=16000.0,
        offset_ms=0,
        window=2048,
        play=False,
        dynamics_db=None,
        onset_k=None,
        noise_floor_db=-72.0,
    ):
        if fps <= 0 or fps > 240:
            raise ValueError("fps must be between 1 and 240")
        if (
            window < RATE // ANALYSIS_HZ
            or n_bands < 1
            or not 0 < fmin < fmax <= RATE / 2
        ):
            raise ValueError("invalid analysis window or frequency bands")
        self.source = source or default_monitor()
        self.play, self.fps = play, fps
        self.hop = RATE // ANALYSIS_HZ
        self.dt = self.hop / RATE
        self.window, self.n_bands = window, n_bands
        self.DYN_RANGE_DB = max(
            0.1, dynamics_db if dynamics_db is not None else self.DYN_RANGE_DB
        )
        self.ONSET_K = max(0.1, onset_k if onset_k is not None else self.ONSET_K)
        self.noise_floor_db = noise_floor_db
        self._ring = np.zeros(window, dtype=np.float32)
        self._han = np.hanning(window).astype(np.float32)
        frequencies = np.fft.rfftfreq(window, 1 / RATE)

        def bins(lo, hi):
            a = max(1, int(np.searchsorted(frequencies, lo)))
            return a, min(
                len(frequencies), max(a + 1, int(np.searchsorted(frequencies, hi)))
            )

        edges = np.geomspace(fmin, fmax, n_bands + 1)
        self._bins = [bins(a, b) for a, b in zip(edges[:-1], edges[1:])]
        self._bass_bin = bins(40, 120)
        self._mid_bin = bins(200, 4000)
        self._roles = [bins(40, 400), bins(400, 4000), bins(4000, 16000)]
        # Semitone-spaced spectral groups make the maximum filter cover a
        # similar pitch excursion at low and high frequencies (SuperFlux idea).
        self._onset_edges = np.unique(
            np.searchsorted(frequencies, np.geomspace(40, 16000, 105))
        )
        centers = (
            frequencies[self._onset_edges[:-1]] + frequencies[self._onset_edges[1:]]
        ) / 2
        self._onset_roles = [
            (int(np.searchsorted(centers, lo)), int(np.searchsorted(centers, hi)))
            for lo, hi in ((40, 400), (400, 4000), (4000, 16001))
        ]
        self._env = np.zeros(n_bands, dtype=np.float32)
        self._env_slow = self._env.copy()
        self._balance_ref = self._env.copy()
        self._balance_short = self._env.copy()
        self._peak = np.zeros(n_bands)
        self._bass_peak = self._bass_env = 0.0
        self._level_peak = self._level = 0.0
        self._rms_slow = self._loud_ref = self._dyn = self._swell = 0.0
        self._energy_short = self._energy_long = self._density = 0.0
        self._presence = 0.0
        self._active = False
        self._quiet = self._ref_time = 0.0
        self._noise = 10 ** (noise_floor_db / 20)
        self._prev_mag = None
        self._flux_hist = collections.deque(maxlen=ANALYSIS_HZ * 2)
        self._power_history = collections.deque(maxlen=4)
        self._peak_hist = [collections.deque(maxlen=32) for _ in range(3)]
        self._last_onsets = np.full(3, -100.0)
        self._pending_event = None
        self._last_event_time = -100.0
        self._clock = BeatClock(ANALYSIS_HZ)
        self._time = 0.0
        self._latest = None

        self.proc = None
        self._realtime = True
        self._paced = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._history = collections.deque(maxlen=ANALYSIS_HZ * 4)
        self._delivered = -1.0
        self._render_tick = 0
        self._fclock = 0.0
        self._arrival = 0.0
        self._src_eof = False
        self._input_eof = False
        self.eof = False
        self.error = None
        self.frames = 0
        self.set_offset(offset_ms)

    def set_offset(self, offset_ms):
        value = min(self.MAX_OFFSET_MS, max(0, int(offset_ms))) / 1000
        with self._lock:
            self._offset = value

    def start(self):
        if self.source.startswith("file:"):
            if not shutil.which("ffmpeg"):
                raise RuntimeError("ffmpeg is needed to decode audio files")
            cmd = [
                "ffmpeg",
                "-v",
                "error",
                "-i",
                self.source[5:],
                "-f",
                "f32le",
                "-acodec",
                "pcm_f32le",
                "-ac",
                "1",
                "-ar",
                str(RATE),
                "pipe:1",
            ]
            if self.play:
                cmd += ["-f", "pulse", "-ac", "2", "-ar", str(RATE), "rigby"]
            self._realtime, self._paced = self.play, not self.play
        else:
            if not shutil.which("parecord"):
                raise RuntimeError("parecord not found (install pulseaudio-utils)")
            cmd = [
                "parecord",
                f"--device={self.source}",
                f"--rate={RATE}",
                "--channels=1",
                "--format=float32le",
                "--raw",
                "--latency-msec=20",
                "--stream-name=rigby",
            ]
        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=self.hop * 16
        )
        self._fclock = time.monotonic()
        if self._realtime:
            self._thread = threading.Thread(target=self._pump, daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()
        proc = self.proc
        if proc:
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        if self._thread:
            self._thread.join(timeout=2)
        if proc:
            for pipe in (proc.stdout, proc.stderr):
                if pipe:
                    pipe.close()
        self.proc = self._thread = None

    def _next_chunk(self):
        assert self.proc and self.proc.stdout
        buf = self.proc.stdout.read(self.hop * 4)
        if len(buf) < self.hop * 4:
            self._input_eof = True
            if self.proc.stderr:
                err = self.proc.stderr.read().decode(errors="replace").strip()
                if err:
                    self.error = err.splitlines()[-1]
            if not buf:
                return None
            # Preserve the last partial hop, padding only the final samples.
            samples = np.frombuffer(buf[: len(buf) // 4 * 4], dtype=np.float32)
            return np.pad(samples, (0, self.hop - len(samples)))
        return np.frombuffer(buf, dtype=np.float32)

    def _pump(self):
        try:
            while not self._stop.is_set():
                chunk = self._next_chunk()
                if chunk is None:
                    return
                now = time.monotonic()
                if self._arrival and now - self._arrival > self.EVENT_MAX_AGE:
                    self._after_gap(now - self._arrival)
                self.feed(chunk, arrival=now)
                if self._input_eof:
                    return
        except (OSError, ValueError) as exc:
            if not self._stop.is_set():
                self.error = str(exc)
        finally:
            self._src_eof = True

    def _after_gap(self, elapsed):
        """Resume from the faded state shown by the renderer, not a stale peak."""
        self._pending_event = None
        release = math.exp(-elapsed / 0.28)
        self._presence *= release
        self._env *= release
        self._env_slow *= release
        self._bass_env *= release
        self._level *= release
        self._dyn *= release
        self._swell *= release
        self._clock.confidence *= math.exp(-max(0.0, elapsed - 0.5) / 1.5)
        self._prev_mag = None
        self._flux_hist.clear()
        self._power_history.clear()
        if elapsed > 2.0:
            self._clock = BeatClock(ANALYSIS_HZ)
        if elapsed > 6.0:
            self._ref_time = 0.0
            self._peak[:] = 0.0
            self._bass_peak = self._level_peak = 0.0

    def feed(self, chunk, arrival=None):
        """Process exactly one analysis hop. Also used by deterministic tests."""
        chunk = np.asarray(chunk, dtype=np.float32)
        if chunk.shape != (self.hop,):
            raise ValueError(f"expected {self.hop} mono samples")
        chunk = np.nan_to_num(chunk, nan=0.0, posinf=0.0, neginf=0.0)
        self._ring[: -self.hop] = self._ring[self.hop :]
        self._ring[-self.hop :] = chunk
        f = self._analyse(self._ring)
        self.frames += 1
        with self._lock:
            self._latest = f
            self._arrival = time.monotonic() if arrival is None else arrival
            self._history.append(f)
        return f

    def read(self):
        self._render_tick += 1
        if self._realtime:
            self._fclock += 1 / self.fps
            now = time.monotonic()
            if self._fclock > now:
                time.sleep(self._fclock - now)
            elif now - self._fclock > 0.1:
                self._fclock = now
            now = time.monotonic()
            with self._lock:
                latest, arrival, offset = self._latest, self._arrival, self._offset
            if latest is None:
                self.eof = self._src_eof
                return None
            age = max(0.0, now - arrival)
            # Once EOF arrives, let the latency buffer finish before stopping.
            target = latest.timestamp + min(age, offset + self.dt) - offset
            if self._src_eof and self._delivered >= latest.timestamp:
                self.eof = True
                return None
            return self.sample(target, stale_age=age)

        target = self._render_tick / self.fps
        with self._lock:
            offset = self._offset
        while self._time < target and not self._input_eof:
            chunk = self._next_chunk()
            if chunk is None:
                break
            self.feed(chunk)
        if self._paced:
            self._fclock += 1 / self.fps
            lag = self._fclock - time.monotonic()
            if lag > 0:
                time.sleep(lag)
        if self._input_eof and target - offset > self._time + 1 / self.fps:
            self.eof = True
            return None
        return self.sample(target - offset)

    def sample(self, target, stale_age=0.0):
        """Interpolate features and deliver each recent event once, at availability.

        Events carry their original attack time, but aren't exposed before the
        peak-picker has observed them. Old events are discarded after a stall.
        """
        with self._lock:
            history = list(self._history)
        if not history or target < history[0].timestamp:
            return None
        before = history[0]
        after = before
        for item in history:
            if item.timestamp > target:
                after = item
                break
            before = after = item
        fraction = (
            (target - before.timestamp) / (after.timestamp - before.timestamp)
            if after.timestamp > before.timestamp
            else 0.0
        )
        continuous = (
            "level",
            "dynamics",
            "rms",
            "bass",
            "pulse",
            "swell",
            "presence",
            "density",
            "energy_slope",
            "spectral_change",
            "mid_share",
        )
        values = {
            k: getattr(before, k) + fraction * (getattr(after, k) - getattr(before, k))
            for k in continuous
        }
        for k in ("bands", "bands_slow", "balance"):
            a, b = getattr(before, k), getattr(after, k)
            values[k] = a + fraction * (b - a) if a is not None and b is not None else a
        beat = (
            before.beat_position + max(0.0, target - before.timestamp) * before.bpm / 60
        )
        events = tuple(
            e
            for item in history
            if self._delivered < item.timestamp <= target
            for e in item.events
            if 0 <= target - e.time <= self.EVENT_MAX_AGE
        )
        self._delivered = max(self._delivered, min(target, history[-1].timestamp))
        if stale_age > 0.08:
            release = math.exp(-(stale_age - 0.08) / 0.3)
            for k in (
                "level",
                "dynamics",
                "bass",
                "swell",
                "presence",
                "density",
                "bands",
                "bands_slow",
            ):
                values[k] = values[k] * release
            values["pulse"] *= math.exp(-max(0.0, stale_age - 0.5) / 1.5)
            events = ()
        strongest = max(events, key=lambda e: e.strength * e.confidence, default=None)
        return replace(
            before,
            **values,
            timestamp=target,
            beat_position=beat,
            events=events,
            onset=bool(events),
            onset_strength=strongest.strength if strongest else 0.0,
            analysis_age_ms=stale_age * 1000,
        )

    def _analyse(self, ring):
        self._time += self.dt
        rms = float(np.sqrt(np.mean(ring[-self.hop :].astype(np.float64) ** 2)))
        floor = 10 ** (self.noise_floor_db / 20)
        # Learn only the background below the configured gate, never a quiet song.
        if rms < floor * 1.5:
            self._noise += (rms - self._noise) * alpha(self.dt, 3.0)
        threshold = max(floor, self._noise * 2.0)
        self._active = rms > threshold * (0.7 if self._active else 1.4)
        self._quiet = 0.0 if self._active else self._quiet + self.dt
        self._presence += (float(self._active) - self._presence) * alpha(
            self.dt, 0.025 if self._active else 0.28
        )
        if self._presence < 1e-4:
            self._presence = 0.0

        mag = np.abs(np.fft.rfft(ring * self._han))
        power = mag**2
        total = max(float(power[1:].sum()), 1e-20)
        energies = np.array([power[a:b].sum() for a, b in self._bins])
        balance = energies / total
        # RMS-like band levels with a common scale preserve spectral balance.
        raw = np.sqrt(energies) / max(float(self._han.sum()), 1.0)
        broad = math.sqrt(total) / max(float(self._han.sum()), 1.0)
        if self._active:
            self._peak = np.maximum(raw, self._peak * math.exp(-self.dt / 15.0))
        # Gain cannot exceed 4x the broadband reference; discard insignificant energy.
        reference = np.maximum(self._peak, max(broad * 0.25, floor))
        audible = np.clip((balance - 0.0005) / 0.008, 0, 1)
        norm = np.clip(raw / reference, 0, 1) * audible * self._presence
        for env, attack, release in (
            (self._env, 0.025, 0.16),
            (self._env_slow, 0.12, 0.4),
        ):
            env += (norm - env) * np.where(
                norm > env, alpha(self.dt, attack), alpha(self.dt, release)
            )
        a, b = self._bass_bin
        bass_power = float(power[a:b].sum())
        bass_raw = math.sqrt(bass_power) / max(float(self._han.sum()), 1.0)
        if self._active:
            self._bass_peak = max(bass_raw, self._bass_peak * math.exp(-self.dt / 15.0))
        bass = min(1.0, bass_raw / max(self._bass_peak, broad * 0.25, floor))
        bass *= (
            float(np.clip((bass_power / total - 0.0005) / 0.008, 0, 1)) * self._presence
        )
        self._bass_env += (bass - self._bass_env) * alpha(
            self.dt, 0.025 if bass > self._bass_env else 0.16
        )
        self._level_peak = (
            max(rms, self._level_peak * math.exp(-self.dt / 15.0))
            if self._active
            else self._level_peak
        )
        level = min(1.0, rms / max(self._level_peak, floor)) * self._presence
        self._level += (level - self._level) * alpha(
            self.dt, 0.04 if level > self._level else 0.2
        )

        self._rms_slow += (rms - self._rms_slow) * alpha(self.dt, 0.35)
        if self._active:
            # Seed from actual input, not an envelope still rising from zero.
            if self._ref_time == 0:
                self._loud_ref = rms
            self._ref_time += self.dt
            coefficient = max(alpha(self.dt, self.REF_TAU_S), self.dt / self._ref_time)
            self._loud_ref += (rms - self._loud_ref) * coefficient
        db = 20 * math.log10(max(self._rms_slow, floor) / max(self._loud_ref, floor))
        target = float(
            np.clip(
                (db + self.DYN_RANGE_DB) / (self.DYN_RANGE_DB + self.DYN_HEAD_DB), 0, 1
            )
        )
        self._dyn += (target * self._presence - self._dyn) * alpha(self.dt, 0.2)
        a, b = self._mid_bin
        mid_share = float(power[a:b].sum() / total)
        target = float(np.clip(0.5 + db / 24, 0, 1)) * self._presence
        self._swell += (target - self._swell) * alpha(
            self.dt, 0.55 if target > self._swell else 1.1
        )
        self._energy_short += (self._dyn - self._energy_short) * alpha(self.dt, 0.4)
        self._energy_long += (self._dyn - self._energy_long) * alpha(self.dt, 3.0)
        self._balance_short += (balance - self._balance_short) * alpha(self.dt, 0.45)
        change = float(np.abs(self._balance_short - self._balance_ref).sum()) / 2
        self._balance_ref += (self._balance_short - self._balance_ref) * alpha(
            self.dt, 4.0
        )
        events, novelty = self._onsets(mag)
        self._density += (
            (min(len(events), 1) / self.dt / 6.0) - self._density
        ) * alpha(self.dt, 2.0)
        self._clock.step(self._time, novelty, events, self._active)
        if self._quiet > 2.0:
            self._prev_mag = None
            self._flux_hist.clear()
        if self._quiet > 6.0:
            self._peak[:] = 0.0
            self._bass_peak = self._level_peak = self._ref_time = 0.0
            for h in self._peak_hist:
                h.clear()
        return Features(
            self._env.copy(),
            self._env_slow.copy(),
            self._level,
            (0.10 + 0.90 * self._dyn) * self._presence,
            rms,
            self._bass_env,
            bool(events),
            novelty,
            pulse=self._clock.confidence,
            swell=self._swell,
            onset_strength=max((e.strength for e in events), default=0.0),
            timestamp=self._time,
            presence=self._presence,
            balance=balance,
            events=events,
            bpm=self._clock.bpm,
            beat_position=self._clock.position,
            bar_confidence=self._clock.bar_confidence,
            bar_offset=self._clock.bar_offset,
            density=min(1.0, self._density),
            energy_slope=self._energy_short - self._energy_long,
            spectral_change=change * self._presence,
            mid_share=mid_share,
        )

    def _onsets(self, mag):
        # Log compression and a frequency maximum suppress small vibrato shifts.
        # A common scale for both spectra retains amplitude attacks.
        flux = np.zeros(3)
        powers = np.array([np.sum(mag[a:b] ** 2) for a, b in self._roles])
        mag = np.sqrt(np.add.reduceat(mag**2, self._onset_edges)[:-1])
        if self._prev_mag is not None and self._active:
            scale = max(float(mag.max()), float(self._prev_mag.max()), 1e-9) / 20
            current = np.log1p(mag / scale)
            previous = np.log1p(self._prev_mag / scale)
            padded = np.pad(previous, (2, 2), mode="edge")
            previous = np.maximum.reduce(
                [padded[i : i + len(previous)] for i in range(5)]
            )
            diff = np.maximum(current - previous, 0)
            for i, (a, b) in enumerate(self._onset_roles):
                share = float(powers[i]) / max(float(powers.sum()), 1e-12)
                flux[i] = float(diff[a:b].sum() / max(current[a:b].sum(), 1e-9))
                flux[i] *= min(1.0, share / 0.003)
                if self._power_history:
                    old = self._power_history[0]
                    broad_rise = max(
                        0.0, 1.0 - float(old.sum()) / max(float(powers.sum()), 1e-12)
                    )
                    role_rise = max(0.0, 1.0 - old[i] / max(powers[i], 1e-12))
                    # Pitch motion is spectral novelty, but a percussive accent
                    # also needs an attack in energy. High bands can reveal a
                    # quiet tick against a loud sustained arrangement.
                    support = max(broad_rise, role_rise * (0.7 if i == 2 else 0.15))
                    flux[i] *= min(1.0, support / 0.12)
        self._power_history.append(powers)
        self._prev_mag = mag
        self._flux_hist.append(flux)
        events = []
        saliences = []
        if len(self._flux_hist) >= 10 and self._active:
            h = np.asarray(self._flux_hist)
            peak = h[-2]
            base = h[:-2]
            thresholds = base.mean(axis=0) + self.ONSET_K * base.std(axis=0)
            for i, kind in enumerate(("low", "mid", "high")):
                threshold = max(float(thresholds[i]), (0.035, 0.065, 0.08)[i])
                if (
                    peak[i] <= h[-3, i]
                    or peak[i] < h[-1, i]
                    or peak[i] <= threshold
                    or self._time - self._last_onsets[i] < (0.12, 0.14, 0.10)[i]
                ):
                    continue
                confidence = float(
                    np.clip(0.35 + (peak[i] / threshold - 1) * 0.35, 0, 1)
                )
                if confidence < (0.40 if i == 0 else 0.45):
                    continue
                history = self._peak_hist[i]
                reference = float(np.median(history)) if history else float(peak[i])
                strength = float(np.clip(peak[i] / max(reference * 1.6, 0.08), 0.1, 1))
                history.append(float(peak[i]))
                self._last_onsets[i] = self._time
                # One hop of lookahead plus the Hann window's center delay.
                at = max(0.0, self._time - self.dt - self.window / (2 * RATE))
                events.append(Onset(at, strength, confidence, kind))
                saliences.append(
                    float(peak[i]) * self._power_history[-2][i] * (3.0, 1.0, 1.0)[i]
                )
        # Coalesce adjacent-band peaks for one physical attack. One extra hop
        # lets a low-band peak catch up with a slightly earlier high-band peak.
        if events:
            best = int(np.argmax(saliences))
            event, salience = events[best], saliences[best]
            if self._pending_event is not None:
                old, old_salience, detected = self._pending_event
                if salience > old_salience:
                    self._pending_event = (event, salience, detected)
            elif event.time - self._last_event_time >= 0.07:
                self._pending_event = (event, salience, self._time)
        events = []
        if self._pending_event is not None:
            event, _, detected = self._pending_event
            if self._time - detected >= self.dt - 1e-8:
                events = [event]
                self._last_event_time = event.time
                self._pending_event = None
        novelty = float(flux[0] + 0.65 * flux[1] + 0.05 * flux[2])
        return tuple(events), novelty
