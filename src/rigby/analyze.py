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
    bands: np.ndarray       # fast envelope: transient detail
    bands_slow: np.ndarray  # slow envelope: the sustained wash
    level: float            # broadband loudness 0..1, auto-gained
    dynamics: float         # loudness vs a long reference -> master intensity
    rms: float              # absolute input RMS, for metering only
    bass: float             # 40-120Hz, envelope-shaped
    onset: bool             # peak-picked percussive transient
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
                 window: int = 2048, play: bool = False,
                 dynamics_db: float | None = None, onset_k: float | None = None):
        self.source = source or default_monitor()
        self.play = play
        if dynamics_db is not None:
            self.DYN_RANGE_DB = dynamics_db
        if onset_k is not None:
            self.ONSET_K = onset_k
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
        # Flux only over the percussive low band. Measuring it across half the
        # spectrum meant sustained pads and hi-hats counted as transients, which
        # is most of why detection fired ~11x too often.
        self._flux_bin = (int(np.searchsorted(freqs, 40)),
                          int(np.searchsorted(freqs, 400)))

        # Rolling per-band peak, so each band self-normalises.
        self._peak = np.full(n_bands, 1e-4, dtype=np.float32)
        self._env = np.zeros(n_bands, dtype=np.float32)
        self._env_slow = np.zeros(n_bands, dtype=np.float32)
        self._rms_slow = 0.0
        self._loud_ref = 1e-4
        self._dyn = 0.0
        self._since_onset = 99.0
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
    ATTACK = 0.55        # fast envelope: rise to a new peak
    RELEASE = 0.10       # fast envelope: fall back
    SLOW_ATTACK = 0.16   # wash envelope -- deliberately lazy, this is what
    SLOW_RELEASE = 0.05  # stops the between-beat flicker

    # Auto-gain normalises every band against its own recent peak, which is
    # what makes the spectrum readable -- and also what destroys dynamics, since
    # a quiet passage gets amplified right back up. `dynamics` measures loudness
    # against a long reference and is applied as a master intensity instead, so
    # shape and loudness are separate concerns.
    DYN_RANGE_DB = 22.0  # how far below the reference reads as fully dark
    DYN_FLOOR = 0.10     # never quite black, so the rig doesn't look switched off
    DYN_REF_DECAY = 0.99975   # ~45s half-life

    ONSET_K = 1.7        # threshold = mean + K * std of recent flux
    REFRACTORY_S = 0.11  # min gap between onsets (allows 16ths at 128bpm)

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

        # Spectral flux over the percussive low band only.
        fa, fb = self._flux_bin
        low = mag[fa:fb]
        flux = (0.0 if self._prev_mag is None
                else float(np.sum(np.maximum(0.0, low - self._prev_mag))))
        self._prev_mag = low
        self._flux_hist.append(flux)
        self._since_onset += 1.0 / self.fps

        onset = self._pick_onset()

        raw = np.array([float(np.mean(mag[a:b])) for a, b in self._bins],
                       dtype=np.float32)

        # Rolling peak with slow decay = per-band auto-gain.
        self._peak = np.maximum(raw, self._peak * 0.9995)
        norm = np.clip(raw / np.maximum(self._peak, 1e-9), 0.0, 1.0)

        rising = norm > self._env
        coef = np.where(rising, self.ATTACK, self.RELEASE).astype(np.float32)
        self._env += (norm - self._env) * coef

        slow_coef = np.where(norm > self._env_slow,
                             self.SLOW_ATTACK, self.SLOW_RELEASE).astype(np.float32)
        self._env_slow += (norm - self._env_slow) * slow_coef

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

        # Programme dynamics, in dB against a slowly-decaying reference. Uses a
        # ~330ms smoothed loudness so a single kick can't define "loud".
        self._rms_slow += (rms - self._rms_slow) * 0.05
        self._loud_ref = max(self._rms_slow, self._loud_ref * self.DYN_REF_DECAY)
        db = 20.0 * np.log10(max(self._rms_slow, 1e-9) /
                             max(self._loud_ref, 1e-9))
        d = float(np.clip(1.0 + db / self.DYN_RANGE_DB, 0.0, 1.0))
        self._dyn += (d - self._dyn) * 0.08          # keep it from stepping
        dynamics = self.DYN_FLOOR + (1.0 - self.DYN_FLOOR) * self._dyn

        return Features(bands=self._env.copy(),
                        bands_slow=self._env_slow.copy(),
                        level=self._level, dynamics=dynamics, rms=rms,
                        bass=self._bass_env, onset=onset, flux=flux)

    def _pick_onset(self) -> bool:
        """Peak-pick the flux one frame late.

        The old test -- flux above 2.2x the running median -- fired on every
        frame of a rising transient and on sustained content too. Requiring an
        actual local maximum, plus a refractory gap, is what separates one kick
        from twenty consecutive 'onsets'.
        """
        h = self._flux_hist
        if len(h) < 8:
            return False
        prev2, prev1, cur = h[-3], h[-2], h[-1]
        if not (prev1 > prev2 and prev1 >= cur):
            return False                       # not a local max
        if self._since_onset < self.REFRACTORY_S:
            return False
        window = list(h)[:-2]                  # exclude the peak being tested
        if len(window) < 4:
            return False
        mean = float(np.mean(window))
        std = float(np.std(window))
        if prev1 <= mean + self.ONSET_K * std or prev1 <= 1e-6:
            return False
        self._since_onset = 0.0
        return True

    def _idle(self) -> Features:
        """No fresh audio: release toward zero instead of holding a stale look."""
        self._env *= (1.0 - self.RELEASE)
        self._env_slow *= (1.0 - self.SLOW_RELEASE)
        self._bass_env *= (1.0 - self.RELEASE)
        self._level *= (1.0 - self.RELEASE)
        self._dyn *= (1.0 - 0.08)
        self._prev_mag = None
        return Features(bands=self._env.copy(),
                        bands_slow=self._env_slow.copy(),
                        level=self._level,
                        dynamics=self.DYN_FLOOR + (1.0 - self.DYN_FLOOR) * self._dyn,
                        rms=0.0, bass=self._bass_env, onset=False, flux=0.0)

    def _delayed(self, f: Features) -> Features | None:
        """Hold features back by --offset-ms to match output latency."""
        if self._delay_frames <= 0:
            return f
        self._delay.append(f)
        return self._delay[0] if len(self._delay) > self._delay_frames else None
