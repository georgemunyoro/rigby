"""Audio analysis: PipeWire monitor tap -> perceptual bands + onsets.

Two things here matter more than FFT quality:

  1. Log-spaced bands with per-band rolling normalisation. Linear FFT bins mean
     bass eats everything and quiet passages go dark.
  2. Fast-attack / slow-release envelopes. This is the whole difference between
     "cheap PC RGB" and something that reads as intentional.

The audio stream is also the clock -- we read exactly one hop per frame, so the
render loop self-clocks at `fps` with no drift.
"""

from __future__ import annotations

import collections
import shutil
import subprocess
import time
import wave
from dataclasses import dataclass

import numpy as np

RATE = 48000


@dataclass
class Features:
    bands: np.ndarray   # (n_bands,) normalised 0..1, envelope-shaped
    level: float        # broadband loudness 0..1
    bass: float         # 40-120Hz, envelope-shaped
    onset: bool         # spectral-flux transient this frame
    flux: float


def default_monitor() -> str:
    """The monitor source of whatever sink is currently default."""
    sink = subprocess.run(["pactl", "get-default-sink"],
                          capture_output=True, text=True).stdout.strip()
    if not sink:
        raise RuntimeError("no default PipeWire sink")
    return sink + ".monitor"


class Analyzer:
    def __init__(self, source: str | None = None, fps: int = 60, n_bands: int = 8,
                 fmin: float = 40.0, fmax: float = 16000.0, offset_ms: int = 0,
                 window: int = 2048):
        self.source = source or default_monitor()
        self.fps = fps
        self.hop = RATE // fps
        self.window = window
        self.n_bands = n_bands

        self._ring = np.zeros(window, dtype=np.float32)
        self._han = np.hanning(window).astype(np.float32)

        # Log-spaced band edges -> FFT bin ranges.
        edges = np.geomspace(fmin, fmax, n_bands + 1)
        freqs = np.fft.rfftfreq(window, 1.0 / RATE)
        self._bins = [
            (max(1, int(np.searchsorted(freqs, edges[i]))),
             max(int(np.searchsorted(freqs, edges[i])) + 1,
                 int(np.searchsorted(freqs, edges[i + 1]))))
            for i in range(n_bands)
        ]
        self._bass_bin = (int(np.searchsorted(freqs, 40)),
                          int(np.searchsorted(freqs, 120)))

        # Rolling per-band peak, so each band self-normalises.
        self._peak = np.full(n_bands, 1e-4, dtype=np.float32)
        self._env = np.zeros(n_bands, dtype=np.float32)
        self._bass_env = 0.0
        self._level = 0.0

        self._prev_mag = None
        self._flux_hist: collections.deque[float] = collections.deque(maxlen=fps * 2)

        # Compensates for output latency (Bluetooth sinks are ~150-250ms).
        # We delay the *features*, which is equivalent to delaying the light
        # frames but far cheaper.
        self._delay: collections.deque[Features] = collections.deque(
            maxlen=max(1, int(offset_ms / 1000 * fps) + 1))
        self._delay_frames = int(offset_ms / 1000 * fps)

        self.proc: subprocess.Popen | None = None
        self._file: np.ndarray | None = None   # set when source is file:PATH
        self._fpos = 0
        self._fclock = 0.0
        self.eof = False

    # -- envelope coefficients (per frame) ------------------------------------
    ATTACK = 0.55   # how fast a band rises to a new peak
    RELEASE = 0.10  # how slowly it falls back

    def start(self) -> None:
        if self.source.startswith("file:"):
            self._file = _load_wav(self.source[5:])
            self._fclock = time.monotonic()
            return
        if not shutil.which("pw-record"):
            raise RuntimeError("pw-record not found (install pipewire-audio)")
        # --container raw is essential: without it pw-record writes an AU
        # header to stdout, and the header's length field for an open-ended
        # stream is 0xFFFFFFFF -- which reinterpreted as float32 is NaN, which
        # then poisons the rolling peak for the rest of the run.
        self.proc = subprocess.Popen(
            ["pw-record", f"--target={self.source}", f"--rate={RATE}",
             "--channels=1", "--format=f32", "--container", "raw", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            bufsize=self.hop * 4 * 4)

    def stop(self) -> None:
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None

    def _next_chunk(self) -> np.ndarray | None:
        """One hop of mono f32, or None at end of stream."""
        if self._file is not None:
            if self._fpos + self.hop > self._file.size:
                self.eof = True
                return None
            chunk = self._file[self._fpos:self._fpos + self.hop]
            self._fpos += self.hop
            # Pace to real time so the show renders at playback speed.
            self._fclock += 1.0 / self.fps
            lag = self._fclock - time.monotonic()
            if lag > 0:
                time.sleep(lag)
            return chunk

        assert self.proc and self.proc.stdout
        want = self.hop * 4
        buf = self.proc.stdout.read(want)
        if not buf or len(buf) < want:
            self.eof = True
            return None
        return np.frombuffer(buf, dtype=np.float32)

    def read(self) -> Features | None:
        """Advance one hop, return delayed features (None while filling)."""
        chunk = self._next_chunk()
        if chunk is None:
            return None
        # Cheap insurance: one non-finite sample would stick in _peak forever.
        if not np.isfinite(chunk).all():
            chunk = np.nan_to_num(chunk, nan=0.0, posinf=0.0, neginf=0.0)
        self._ring = np.roll(self._ring, -self.hop)
        self._ring[-self.hop:] = chunk

        mag = np.abs(np.fft.rfft(self._ring * self._han))

        # Spectral flux over the low half -> kick/snare transients.
        low = mag[: len(mag) // 2]
        if self._prev_mag is None:
            flux = 0.0
        else:
            flux = float(np.sum(np.maximum(0.0, low - self._prev_mag)))
        self._prev_mag = low
        self._flux_hist.append(flux)

        onset = False
        if len(self._flux_hist) > self.fps // 2:
            med = float(np.median(self._flux_hist))
            onset = flux > med * 2.2 and flux > 1e-3

        raw = np.array([float(np.mean(mag[a:b])) for a, b in self._bins],
                       dtype=np.float32)

        # Rolling peak with slow decay = per-band auto-gain.
        self._peak = np.maximum(raw, self._peak * 0.9995)
        norm = np.clip(raw / np.maximum(self._peak, 1e-9), 0.0, 1.0)

        rising = norm > self._env
        coef = np.where(rising, self.ATTACK, self.RELEASE).astype(np.float32)
        self._env += (norm - self._env) * coef

        a, b = self._bass_bin
        bass_raw = float(np.mean(mag[a:b])) if b > a else 0.0
        bass_n = min(1.0, bass_raw / max(float(self._peak[0]), 1e-9))
        c = self.ATTACK if bass_n > self._bass_env else self.RELEASE
        self._bass_env += (bass_n - self._bass_env) * c

        rms = float(np.sqrt(np.mean(chunk.astype(np.float64) ** 2)))
        lvl = min(1.0, rms * 8.0)
        self._level += (lvl - self._level) * (self.ATTACK if lvl > self._level
                                              else self.RELEASE)

        f = Features(bands=self._env.copy(), level=self._level,
                     bass=self._bass_env, onset=onset, flux=flux)

        if self._delay_frames <= 0:
            return f
        self._delay.append(f)
        return self._delay[0] if len(self._delay) > self._delay_frames else None


def _load_wav(path: str) -> np.ndarray:
    """Read a WAV to mono float32 at RATE (nearest-neighbour resample)."""
    with wave.open(path, "rb") as w:
        n, ch, sw, sr = (w.getnframes(), w.getnchannels(),
                         w.getsampwidth(), w.getframerate())
        raw = w.readframes(n)

    dtype = {1: np.uint8, 2: np.int16, 4: np.int32}.get(sw)
    if dtype is None:
        raise RuntimeError(f"unsupported WAV sample width: {sw} bytes")

    a = np.frombuffer(raw, dtype=dtype).astype(np.float32)
    a = (a - 128.0) / 128.0 if sw == 1 else a / float(np.iinfo(dtype).max)
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    if sr != RATE:
        idx = (np.arange(int(a.size * RATE / sr)) * sr / RATE).astype(np.int64)
        a = a[np.clip(idx, 0, a.size - 1)]
    return np.ascontiguousarray(a, dtype=np.float32)
