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
    pulse: float = 0.0      # 0..1 confidence the track has a real beat
    swell: float = 0.0      # 0..1 sustained loudness -- the "belting" ramp
    onset_strength: float = 0.0   # 0..1 size of this hit vs recent hits


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
        # Vocal-ish range, for the sustained "belting" measure.
        self._vox_bin = (int(np.searchsorted(freqs, 200)),
                         int(np.searchsorted(freqs, 4000)))

        # Rolling per-band peak, so each band self-normalises.
        self._peak = np.full(n_bands, 1e-4, dtype=np.float32)
        self._env = np.zeros(n_bands, dtype=np.float32)
        self._env_slow = np.zeros(n_bands, dtype=np.float32)
        self._rms_slow = 0.0
        self._loud_ref = 0.0
        self._dyn = 0.0
        self._since_onset = 99.0
        self._swell = 0.0
        self._ref_n = 0
        self._pulse = 0.0
        self._pulse_hist: collections.deque[float] = collections.deque(
            maxlen=self.PULSE_WIN)
        self._pulse_tick = 0
        self._onset_peaks: collections.deque[float] = collections.deque(maxlen=24)
        self._onset_strength = 0.0
        self._ref_coef = 1.0 / (self.REF_TAU_S * fps)
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
    DYN_RANGE_DB = 15.0  # how far below the reference reads as fully dark
    DYN_HEAD_DB = 5.0    # headroom above the reference, so builds have somewhere to go
    DYN_FLOOR = 0.10     # never quite black, so the rig doesn't look switched off
    REF_TAU_S = 25.0     # the song's "typical level" settles over this long

    # Beat confidence, from the periodicity of the onset envelope. A decaying
    # max reference reads 1.0 through an entire slow build, which is useless for
    # ballads; a symmetric average of the song's own level is what gives a swell
    # somewhere to rise from.
    SWELL_SPAN_DB = 12.0 # swell has its own, wider window than brightness:
                         # a ballad's verse *should* be dim, but the same
                         # mapping would dim every beaty track along with it.

    PULSE_WIN = 360      # 6s of onset envelope
    PULSE_EVERY = 10     # recompute every N frames; the FFT is cheap but not free
    PULSE_LO, PULSE_HI = 20, 90     # lags = 40..180 BPM
    PULSE_FLOOR, PULSE_CEIL = 0.35, 0.68

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

        # Programme dynamics, in dB against the song's own typical level. This
        # is a symmetric slow average, NOT a decaying max: against a max, a slow
        # build tracks its own reference upward and reads 1.0 the whole way, so
        # a ballad's verse and its chorus come out identical.
        self._rms_slow += (rms - self._rms_slow) * 0.05
        # Warm-up: behave as a running mean until enough history exists, then
        # settle into the EMA. Seeding the reference from the first frame
        # instead pins it near silence, and everything reads as "loud" for the
        # next half minute.
        self._ref_n += 1
        coef = max(self._ref_coef, 1.0 / max(self._ref_n, 1))
        self._loud_ref += (self._rms_slow - self._loud_ref) * coef
        db = 20.0 * np.log10(max(self._rms_slow, 1e-9) /
                             max(self._loud_ref, 1e-9))
        span = self.DYN_RANGE_DB + self.DYN_HEAD_DB
        d = float(np.clip((db + self.DYN_RANGE_DB) / span, 0.0, 1.0))
        self._dyn += (d - self._dyn) * 0.08          # keep it from stepping
        dynamics = self.DYN_FLOOR + (1.0 - self.DYN_FLOOR) * self._dyn

        # Sustained loudness -> the belting ramp. Weighted by how much of the
        # energy sits in the vocal range, as a *share* of the whole spectrum:
        # a share is scale-invariant, where any peak-normalised measure would
        # ride its own reference upward through a build and read flat.
        va, vb = self._vox_bin
        total = float(np.mean(mag[1:])) if mag.size > 1 else 0.0
        vox_share = (float(np.mean(mag[va:vb])) / max(total, 1e-9)
                     if vb > va else 0.0)
        vox_w = float(np.clip(vox_share / 2.0, 0.0, 1.0))
        swell_d = float(np.clip((db + self.SWELL_SPAN_DB) /
                                (2.0 * self.SWELL_SPAN_DB), 0.0, 1.0))
        target = swell_d * (0.55 + 0.45 * vox_w)
        self._swell += (target - self._swell) * (0.030 if target > self._swell
                                                 else 0.012)

        self._update_pulse(flux)

        return Features(bands=self._env.copy(),
                        bands_slow=self._env_slow.copy(),
                        level=self._level, dynamics=dynamics, rms=rms,
                        bass=self._bass_env, onset=onset, flux=flux,
                        pulse=self._pulse, swell=self._swell,
                        onset_strength=(self._onset_strength if onset else 0.0))

    def _update_pulse(self, flux: float) -> None:
        """Beat confidence = periodicity of the onset envelope.

        Autocorrelating the raw envelope does not work: it is dominated by slow
        drift, and every track scores ~0.7. Detrending against a local moving
        average and half-wave rectifying first is what makes a beat grid
        distinguishable from a sustained arrangement.
        """
        self._pulse_hist.append(flux)
        self._pulse_tick += 1
        if (self._pulse_tick % self.PULSE_EVERY
                or len(self._pulse_hist) < self.PULSE_WIN):
            return

        seg = np.asarray(self._pulse_hist, dtype=np.float64)
        ma = 24
        k = np.ones(ma) / ma
        base = np.convolve(np.pad(seg, (ma // 2, ma // 2), mode="edge"),
                           k, mode="valid")[:len(seg)]
        seg = np.maximum(seg - base, 0.0)
        if seg.std() < 1e-12:
            target = 0.0
        else:
            seg = seg - seg.mean()
            n = 1 << int(np.ceil(np.log2(len(seg) * 2)))
            F = np.fft.rfft(seg, n)
            ac = np.fft.irfft(F * np.conj(F))[:len(seg)]
            ac /= max(ac[0], 1e-12)
            sal = float(ac[self.PULSE_LO:self.PULSE_HI].max())
            target = float(np.clip(
                (sal - self.PULSE_FLOOR) / (self.PULSE_CEIL - self.PULSE_FLOOR),
                0.0, 1.0))
        # Very slow: flipping regime mid-phrase looks like a glitch.
        self._pulse += (target - self._pulse) * 0.05

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

        # How big is this hit compared with the hits around it? Measured
        # against recent accepted peaks rather than an absolute scale, so it
        # still means something in a quiet passage.
        if len(self._onset_peaks) >= 4:
            med = float(np.median(self._onset_peaks))
            self._onset_strength = float(np.clip(prev1 / max(med * 2.0, 1e-9),
                                                 0.0, 1.0))
        else:
            self._onset_strength = 0.5
        self._onset_peaks.append(prev1)

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
        self._swell *= (1.0 - 0.012)
        return Features(bands=self._env.copy(),
                        bands_slow=self._env_slow.copy(),
                        level=self._level,
                        dynamics=self.DYN_FLOOR + (1.0 - self.DYN_FLOOR) * self._dyn,
                        rms=0.0, bass=self._bass_env, onset=False, flux=0.0,
                        pulse=self._pulse, swell=self._swell)

    def _delayed(self, f: Features) -> Features | None:
        """Hold features back by --offset-ms to match output latency."""
        if self._delay_frames <= 0:
            return f
        self._delay.append(f)
        return self._delay[0] if len(self._delay) > self._delay_frames else None
