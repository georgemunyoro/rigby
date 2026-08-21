"""Audio analysis: PipeWire monitor tap -> perceptual bands + onsets.

Two things here matter more than FFT quality:

  1. Log-spaced bands with per-band rolling normalisation. Linear FFT bins mean
     bass eats everything and quiet passages go dark.
  2. Fast-attack / slow-release envelopes. This is the whole difference between
     "cheap PC RGB" and something that reads as intentional.

Capture is decoupled from rendering. A reader thread keeps a ring buffer
current and the render loop samples the newest window on a wall clock. That
matters because PulseAudio hands over audio in ~340ms bursts by default: reading
one hop per rendered frame turns those bursts into visible stutter, and any
backlog becomes permanent latency that keeps animating after the music stops.
Offline file rendering (--no-play) keeps the sequential path, where processing
every hop matters more than staying current.
"""

from __future__ import annotations

import collections
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass

import numpy as np

RATE = 48000


@dataclass
class Features:
    bands: np.ndarray   # (n_bands,) normalised 0..1, envelope-shaped
    level: float        # broadband loudness 0..1, auto-gained
    rms: float          # absolute input RMS, for metering only
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
                 window: int = 2048, play: bool = False):
        self.source = source or default_monitor()
        self.play = play
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
        self._level_peak = 1e-4

        self._prev_mag = None
        self._flux_hist: collections.deque[float] = collections.deque(maxlen=fps * 2)

        # Compensates for output latency (Bluetooth sinks are ~150-250ms).
        # We delay the *features*, which is equivalent to delaying the light
        # frames but far cheaper.
        self._delay: collections.deque[Features] = collections.deque(
            maxlen=max(1, int(offset_ms / 1000 * fps) + 1))
        self._delay_frames = int(offset_ms / 1000 * fps)

        self.proc: subprocess.Popen | None = None
        self._realtime = True  # sample newest audio; False = process every hop
        self._paced = False    # file sources decode faster than realtime
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pumped = 0       # hops the reader thread has ingested
        self._src_eof = False
        self._last_pumped = 0
        self._stale = 0        # consecutive frames with no fresh audio
        self._fclock = 0.0
        self.eof = False
        self.error: str | None = None
        self.frames = 0        # hops actually delivered

    # -- envelope coefficients (per frame) ------------------------------------
    ATTACK = 0.55   # how fast a band rises to a new peak
    RELEASE = 0.10  # how slowly it falls back

    def start(self) -> None:
        if self.source.startswith("file:"):
            path = self.source[5:]
            if not shutil.which("ffmpeg"):
                raise RuntimeError("ffmpeg not found; needed to decode audio files")
            # ffmpeg handles mp3/flac/opus/m4a/wav alike and resamples for us.
            # Streaming it keeps a long set from being read into memory.
            cmd = ["ffmpeg", "-v", "error", "-i", path,
                   "-f", "f32le", "-acodec", "pcm_f32le",
                   "-ac", "1", "-ar", str(RATE), "pipe:1"]
            if self.play:
                # A second output on the same decoder: one process, so sound
                # and lights cannot drift apart. The pulse muxer runs in real
                # time, which also paces the analysis pipe for us.
                cmd += ["-f", "pulse", "-ac", "2", "-ar", str(RATE), "rigby"]

            self.proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                bufsize=self.hop * 4 * 4)

            # Playing: pulse runs the decoder in real time and we sample the
            # newest audio. Not playing: this is offline programming, so walk
            # the file hop by hop and analyse all of it.
            self._realtime = self.play
            self._paced = not self.play
            self._fclock = time.monotonic()
            if self._realtime:
                self._start_pump()
            return
        if not shutil.which("parecord"):
            raise RuntimeError("parecord not found (install pulseaudio-utils / "
                               "pipewire-pulse)")
        # parecord, NOT pw-record. `pw-record --target=<sink>.monitor` does not
        # resolve PulseAudio-style monitor names: it silently attaches to some
        # other source and records digital silence forever, which looks exactly
        # like "the effects are broken". parecord resolves monitors correctly.
        # --raw is essential too, or we'd get a WAV header parsed as samples.
        self.proc = subprocess.Popen(
            ["parecord", f"--device={self.source}", f"--rate={RATE}",
             "--channels=1", "--format=float32le", "--raw",
             # Without this parecord delivers ~340ms bursts; 20ms keeps the
             # reader thread fed smoothly and bounds capture latency.
             "--latency-msec=20", "--stream-name=rigby"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=self.hop * 4 * 4)
        self._realtime = True
        self._fclock = time.monotonic()
        self._start_pump()

    def _start_pump(self) -> None:
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        """Reader thread: keep the ring current so no backlog can accumulate."""
        assert self.proc and self.proc.stdout
        nbytes = self.hop * 4
        while not self._stop.is_set():
            buf = self.proc.stdout.read(nbytes)
            if not buf or len(buf) < nbytes:
                if self.proc.stderr is not None:
                    err = self.proc.stderr.read().decode(errors="replace").strip()
                    if err:
                        self.error = err.splitlines()[-1]
                self._src_eof = True
                return
            c = np.frombuffer(buf, dtype=np.float32)
            if not np.isfinite(c).all():
                c = np.nan_to_num(c, nan=0.0, posinf=0.0, neginf=0.0)
            with self._lock:
                self._ring = np.roll(self._ring, -self.hop)
                self._ring[-self.hop:] = c
                self._pumped += 1

    def stop(self) -> None:
        self._stop.set()
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1)
        self._thread = None

    def _next_chunk(self) -> np.ndarray | None:
        """One hop of mono f32, or None at end of stream."""
        assert self.proc and self.proc.stdout
        want = self.hop * 4
        buf = self.proc.stdout.read(want)
        if not buf or len(buf) < want:
            self.eof = True
            if self.proc.stderr is not None:
                # Don't gate on poll() -- at first read the child often hasn't
                # been reaped yet, which silently swallowed the real message.
                err = self.proc.stderr.read().decode(errors="replace").strip()
                if err:
                    self.error = err.splitlines()[-1]
            return None

        chunk = np.frombuffer(buf, dtype=np.float32)
        self.frames += 1
        if self._paced:
            # A decoder runs far faster than realtime; hold it to playback speed
            # so the show renders at the tempo it will actually be watched at.
            self._fclock += 1.0 / self.fps
            lag = self._fclock - time.monotonic()
            if lag > 0:
                time.sleep(lag)

        return chunk

    def read(self) -> Features | None:
        """One rendered frame's worth of features, or None if nothing yet."""
        if self._realtime:
            # Pace on the wall clock, not on the audio pipe. Capture bursts
            # then no longer translate into bursts of rendered frames.
            self._fclock += 1.0 / self.fps
            lag = self._fclock - time.monotonic()
            if lag > 0:
                time.sleep(lag)
            elif lag < -0.25:
                self._fclock = time.monotonic()   # resync after a long stall

            with self._lock:
                pumped = self._pumped
                ring = self._ring.copy() if pumped else None

            if ring is None:
                if self._src_eof:
                    self.eof = True
                return None
            if pumped == self._last_pumped:
                self._stale += 1
                if self._src_eof:
                    self.eof = True               # source ended and drained
                    return None
                # A few stale frames are just capture jitter -- re-analysing is
                # harmless. A sustained stall (suspended sink, paused stream)
                # is not: without this the show would animate on stale audio
                # forever instead of fading out.
                if self._stale > 3:
                    return self._delayed(self._idle())
            else:
                self._stale = 0

            self._last_pumped = pumped
            self.frames += 1
            return self._delayed(self._analyse(ring))

        chunk = self._next_chunk()
        if chunk is None:
            return None
        # Cheap insurance: one non-finite sample would stick in _peak forever.
        if not np.isfinite(chunk).all():
            chunk = np.nan_to_num(chunk, nan=0.0, posinf=0.0, neginf=0.0)
        self._ring = np.roll(self._ring, -self.hop)
        self._ring[-self.hop:] = chunk
        return self._delayed(self._analyse(self._ring))

    def _analyse(self, ring: np.ndarray) -> Features:
        mag = np.abs(np.fft.rfft(ring * self._han))

        # Spectral flux over the low half -> kick/snare transients.
        low = mag[: len(mag) // 2]
        flux = (0.0 if self._prev_mag is None
                else float(np.sum(np.maximum(0.0, low - self._prev_mag))))
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

        hop = ring[-self.hop:]
        rms = float(np.sqrt(np.mean(hop.astype(np.float64) ** 2)))
        # Auto-gain against a rolling reference, exactly like the bands. An
        # absolute scale here silently collapses whenever the sink volume is
        # low -- monitors are post-volume, so a quiet sink means a tiny tap.
        self._level_peak = max(rms, self._level_peak * 0.9995)
        lvl = min(1.0, rms / max(self._level_peak, 1e-9))
        self._level += (lvl - self._level) * (self.ATTACK if lvl > self._level
                                              else self.RELEASE)

        return Features(bands=self._env.copy(), level=self._level, rms=rms,
                        bass=self._bass_env, onset=onset, flux=flux)

    def _idle(self) -> Features:
        """No fresh audio: release toward zero instead of holding a stale look."""
        self._env *= (1.0 - self.RELEASE)
        self._bass_env *= (1.0 - self.RELEASE)
        self._level *= (1.0 - self.RELEASE)
        self._prev_mag = None
        return Features(bands=self._env.copy(), level=self._level, rms=0.0,
                        bass=self._bass_env, onset=False, flux=0.0)

    def _delayed(self, f: Features) -> Features | None:
        """Hold features back by --offset-ms to match output latency."""
        if self._delay_frames <= 0:
            return f
        self._delay.append(f)
        return self._delay[0] if len(self._delay) > self._delay_frames else None
