"""Small causal musical clock and arrangement director; no model downloads.

Beat position is continuous. Four-beat bar position is only a hypothesis, with
separate confidence: a uniform click track contains no downbeat information.
"""

from __future__ import annotations

from collections import deque
import math

import numpy as np


def alpha(dt: float, tau: float) -> float:
    return -math.expm1(-max(0.0, dt) / max(tau, 1e-6))


class BeatClock:
    def __init__(self, hz: int = 100):
        self.hz = hz
        self.history: deque[float] = deque(maxlen=hz * 6)
        self.period = 0.5
        self.position = 0.0
        self.confidence = 0.0
        self.bar_confidence = 0.0
        self.bar_offset = 0
        self._accent = np.zeros(4)
        self._counts = np.zeros(4)
        self._ticks = 0
        self._locked = False
        self._last_event = -100.0
        self._events = deque(maxlen=80)
        self._last_beat = -1
        self._silence = 0.0
        self._correction = 0.0

    @property
    def bpm(self) -> float:
        return 60.0 / self.period if self._locked else 0.0

    def step(self, time: float, novelty: float, events=(), present=True) -> None:
        dt = 1.0 / self.hz
        # Slew phase corrections instead of snapping a moving fixture.
        correction = float(np.clip(self._correction, -dt * 0.25, dt * 0.25))
        self._correction -= correction
        self.position += dt / self.period + correction
        self.history.append(max(0.0, novelty) if present else 0.0)
        self._ticks += 1
        self._silence = 0.0 if present else self._silence + dt

        rhythmic = [e for e in events if e.kind != "high" and e.confidence >= 0.5]
        for event in rhythmic:
            self._last_event = time
            self._events.append(event)
            if self._locked:
                at = self.position - (time - event.time) / self.period
                error = round(at) - at
                if abs(error) < 0.20:
                    self._correction = float(
                        np.clip(self._correction + error * 0.22, -0.3, 0.3)
                    )
                    beat = round(at)
                    if beat != self._last_beat:
                        self._last_beat = beat
                        slot = beat % 4
                        self._accent[slot] = (
                            0.85 * self._accent[slot] + 0.15 * event.strength
                        )
                        self._counts[slot] += 1
                        if self._counts.min() >= 3:
                            order = np.argsort(self._accent)
                            contrast = self._accent[order[-1]] - self._accent[order[-2]]
                            self.bar_offset = int(order[-1])
                            self.bar_confidence = (
                                float(np.clip(contrast / 0.22, 0, 1)) * self.confidence
                            )

        if self._ticks % max(1, self.hz // 5) == 0 and len(self.history) >= self.hz * 2:
            self._estimate(time)
        if time - self._last_event > 2.0:
            self.confidence *= math.exp(-dt / 1.5)
            self.bar_confidence *= math.exp(-dt / 1.5)
        if self._silence >= 6.0:
            self.history.clear()
            self._events.clear()
            self.confidence = self.bar_confidence = 0.0
            self._counts[:] = self._accent[:] = 0
            self._locked = False
            self._correction = 0.0

    def _estimate(self, time: float) -> None:
        x = np.asarray(self.history)
        # Remove the local baseline; sustained timbre changes should not be a clock.
        n = max(3, int(0.35 * self.hz))
        baseline = np.convolve(
            np.pad(x, (n // 2, n - 1 - n // 2), mode="edge"),
            np.ones(n) / n,
            mode="valid",
        )
        x = np.maximum(x - baseline, 0)
        x -= x.mean()
        energy = float(x @ x)
        if energy < 1e-8:
            self.confidence *= 0.85
            return
        fft_n = 1 << (2 * len(x) - 1).bit_length()
        fft = np.fft.rfft(x, fft_n)
        ac = np.fft.irfft(fft * fft.conj())[: len(x)] / energy
        lags = np.arange(round(self.hz * 60 / 180), round(self.hz * 60 / 55) + 1)
        # Favor repeated support and a moderate tempo, with continuity once locked.
        scores = ac[lags].copy()
        for multiple, weight in ((2, 0.30), (3, 0.15)):
            indices = lags * multiple
            valid = indices < len(ac)
            scores[valid] += weight * ac[indices[valid]]
        scores *= np.exp(-0.18 * np.log2((60 * self.hz / lags) / 115) ** 2)
        if self._locked and self.confidence > 0.25:
            scores *= 0.8 + 0.2 * np.exp(
                -0.5 * (np.log(lags / (self.period * self.hz)) / 0.12) ** 2
            )
        best = int(np.argmax(scores))
        lag = int(lags[best])
        salience = float(ac[lag])
        target = float(np.clip((salience - 0.18) / 0.48, 0, 1))
        # A periodic envelope needs attacks consistently near the proposed grid.
        recent = [e for e in self._events if time - e.time < 6.0]
        if len(recent) >= 4:
            weights = np.array([e.strength * e.confidence for e in recent])
            phases = np.array([e.time for e in recent]) * (self.hz / lag)
            coherence = abs(np.sum(weights * np.exp(2j * np.pi * phases))) / max(
                weights.sum(), 1e-9
            )
            target *= float(np.clip((coherence - 0.2) / 0.5, 0, 1))
        else:
            target = 0.0
        if time - self._last_event > 2.0:
            target = 0.0
        self.confidence += (target - self.confidence) * alpha(
            0.2, 0.9 if target > self.confidence else 1.8
        )
        if target < 0.3:
            return
        # Parabolic refinement avoids quantizing tempo to whole analysis hops.
        denom = ac[lag - 1] - 2 * ac[lag] + ac[lag + 1]
        fine = (
            float(np.clip(0.5 * (ac[lag - 1] - ac[lag + 1]) / denom, -0.5, 0.5))
            if abs(denom) > 1e-9
            else 0.0
        )
        period = (lag + fine) / self.hz
        if not self._locked:
            self.period = period
            # Strongest phase of the onset envelope, not the start of capture.
            positive = np.maximum(np.asarray(self.history), 0)
            phases = np.arange(len(positive)) % lag
            sums = np.bincount(phases, weights=positive, minlength=lag)
            phase = ((len(positive) - 1 - int(sums.argmax())) % lag) / lag
            self.position = math.floor(self.position) + phase
            self._locked = True
        else:
            self.period += (period - self.period) * alpha(0.2, 1.5)
            # Tempo changes can leave the old phase outside the event PLL's
            # capture range. Refit the grid from recent attacks, then slew to it.
            if len(recent) >= 4 and target > 0.4:
                phases = np.array([e.time for e in recent]) / period
                z = np.sum(weights * np.exp(-2j * np.pi * phases))
                desired = (time / period + np.angle(z) / (2 * np.pi)) % 1
                error = (desired - self.position + 0.5) % 1 - 0.5
                self._correction = float(np.clip(error * 0.5, -0.3, 0.3))


class Director:
    """Conservative scene decisions; names describe behavior, not song labels."""

    def __init__(self):
        self.scene = "sparse"
        self.candidate = self.scene
        self.pending_for = 0.0
        self.held = 0.0
        self.changed = False
        self._last_beat = -1
        self.expansion = 0.0

    def step(self, dt, f, energy=0.0):
        self.changed = False
        self.held += dt
        if f.presence < 0.1:
            wanted = "sparse"
        elif f.energy_slope > 0.06 and f.swell > 0.42:
            wanted = "build"
        elif (
            f.swell > (0.54 if self.scene == "full" else 0.64)
            and f.dynamics > 0.65
        ) or energy > (0.55 if self.scene == "full" else 0.7):
            wanted = "full"
        elif (
            f.pulse > (0.32 if self.scene == "groove" else 0.52) and f.density > 0.08
        ) or energy > 0.3:
            wanted = "groove"
        else:
            wanted = "sparse"
        if wanted != self.candidate:
            self.candidate, self.pending_for = wanted, 0.0
        self.pending_for += dt
        beat = math.floor(f.beat_position)
        boundary = beat != self._last_beat
        self._last_beat = beat
        # With credible bar evidence use it. Otherwise only promise a beat boundary.
        if f.bar_confidence >= 0.5:
            boundary = boundary and (beat - f.bar_offset) % 4 == 0
        ready = self.pending_for >= 1.2 and self.held >= 5.0
        if wanted != self.scene and ready and (f.pulse < 0.35 or boundary):
            self.scene, self.held, self.changed = wanted, 0.0, True
        # Spectral novelty can refresh a motif, but never once per random hit.
        elif (
            self.held >= 12
            and f.spectral_change > 0.22
            and (boundary or f.pulse < 0.35)
        ):
            self.held, self.changed = 0.0, True
        target = {"sparse": 0.18, "groove": 0.48, "build": 0.70, "full": 1.0}[
            self.scene
        ]
        target *= f.presence
        self.expansion += (target - self.expansion) * alpha(dt, 1.0)
