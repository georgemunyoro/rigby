"""Ahead-of-playback analysis, persistent sets, and a shared preview/playback engine."""

from __future__ import annotations

import copy
import gzip
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import uuid
import wave
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict

import numpy as np

from .analyze import Analyzer, Features, Onset, RATE
from .arrangement import compose, generated_clips, new_project, validate_project, number
from .fx import encode_rgb
from .patch import Fixture, RING
from .show import LOOKS

FPS = 50
CACHE_VERSION = 2


def atomic_json(path, value):
    path = Path(path)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, allow_nan=False))
    os.replace(tmp, path)


def virtual_fixtures():
    result = {}
    for i in range(3):
        name = f"fan_{i + 1}"
        result[name] = Fixture(
            name,
            0,
            i,
            12,
            0,
            np.linspace(0, 1, 12),
            False,
            kind=RING,
            angle=np.arange(12) / 12,
            origin=(0.2 + i * 0.3, 0.6),
            spin=1 if i % 2 == 0 else -1,
        )
    result["ram"] = Fixture(
        "ram", 0, 3, 8, 0, np.linspace(0, 1, 8), True, origin=(0.5, 0.2)
    )
    return result


def analyse(path, cancelled=lambda: False):
    """Whole-track relative dynamics and conservative section/repetition suggestions."""
    analyzer = Analyzer("file:prepared", fps=FPS)
    features, waveform = [], []
    with wave.open(str(path)) as wav:
        duration = wav.getnframes() / wav.getframerate()
        if not 0.01 <= duration <= 1800:
            raise ValueError("Audio must be between 0.01 seconds and 30 minutes")
        channels = wav.getnchannels()
        i = 0
        while raw := wav.readframes(analyzer.hop):
            if cancelled():
                raise ValueError("Analysis cancelled")
            audio = np.frombuffer(raw, "<i2").reshape(-1, channels).mean(axis=1) / 32768
            waveform.append(float(np.max(np.abs(audio))))
            if len(audio) < analyzer.hop:
                audio = np.pad(audio, (0, analyzer.hop - len(audio)))
            analyzer.feed(audio)
            i += 1
            if i % 2 == 0:
                features.append(analyzer.sample(analyzer._time))
        if i % 2:
            features.append(analyzer.sample(analyzer._time))
    # A fixed full-song reference preserves quiet intros and breakdowns.
    power, levels = 0.0, []
    for f in features:
        power += (f.rms * f.rms - power) * (1 - math.exp(-1 / FPS / 0.2))
        levels.append(math.sqrt(max(power, 0.0)))
    active = [
        level for f, level in zip(features, levels) if f.presence > 0.5 and level > 1e-6
    ]
    ref = max(float(np.percentile(active, 85)) if active else 1.0, 1e-6)
    dyn = swell = 0.0
    for f, level in zip(features, levels):
        db = 20 * math.log10(max(level, 1e-9) / ref)
        target = float(np.clip((db + 25) / 25, 0, 1)) * f.presence
        dyn += (target - dyn) * (1 - math.exp(-1 / FPS / 0.16))
        swell += (target - swell) * (1 - math.exp(-1 / FPS / 0.5))
        f.dynamics = dyn
        f.swell = swell * 0.7
    # Beat markers are estimates, emitted only where the tracker is confident.
    beats, last = [], None
    for i, f in enumerate(features):
        beat = math.floor(f.beat_position)
        if f.pulse > 0.45 and beat != last:
            beats.append(round(i / FPS, 3))
        last = beat
    # Coarse two-second windows, held for at least four seconds. Labels describe
    # lighting intent, not a claim of semantic verse/chorus recognition.
    sections = []
    held, label, pending, since = 0.0, "breakdown", "", 0.0
    for i in range(0, len(features), FPS * 2):
        group = features[i : i + FPS * 2]
        energy = float(np.mean([f.dynamics for f in group]))
        density = float(np.mean([f.density for f in group]))
        wanted = (
            "breakdown"
            if energy < 0.5
            else "full"
            if energy > 0.78 and density > 0.2
            else "groove"
        )
        t = i / FPS
        if wanted != pending:
            pending, since = wanted, t
        if not sections:
            label = wanted
            sections.append({"start": 0.0, "end": duration, "label": label})
        elif wanted != label and t - since >= 2 and t - held >= 4:
            boundary = since
            nearby = [b for b in beats if abs(b - boundary) < 0.4]
            if nearby:
                boundary = min(nearby, key=lambda b: abs(b - boundary))
            sections[-1]["end"] = boundary
            sections.append({"start": boundary, "end": duration, "label": wanted})
            label, held = wanted, boundary
    # Anticipate major arrivals only when preceded by enough space to build.
    expanded = []
    for idx, section in enumerate(sections):
        if (
            idx + 1 < len(sections)
            and sections[idx + 1]["label"] == "full"
            and section["label"] != "full"
            and section["end"] - section["start"] >= 8
        ):
            boundary = section["end"] - 4
            expanded.append(dict(section, end=boundary))
            expanded.append(dict(section, start=boundary, label="build"))
        else:
            expanded.append(section)
    themes = []
    for section in expanded:
        group = features[
            int(section["start"] * FPS) : max(
                int(section["start"] * FPS) + 1, int(section["end"] * FPS)
            )
        ]
        vector = np.mean([f.bands_slow for f in group], axis=0)
        vector /= max(float(np.linalg.norm(vector)), 1e-9)
        match = next(
            (
                j
                for j, (label, old) in enumerate(themes)
                if label == section["label"] and np.dot(vector, old) > 0.97
            ),
            None,
        )
        if match is None:
            match = len(themes)
            themes.append((section["label"], vector))
        section["theme"] = match
    stride = max(1, len(waveform) // 1800)
    peaks = [max(waveform[i : i + stride]) for i in range(0, len(waveform), stride)]
    metadata = {
        "duration": duration,
        "waveform": peaks,
        "beats": beats,
        "sections": expanded,
        "reference_rms": ref,
        "version": CACHE_VERSION,
    }
    return features, metadata


def feature_json(f):
    value = asdict(f)
    for k in ("bands", "bands_slow", "balance"):
        if value[k] is not None:
            value[k] = value[k].tolist()
    return value


def decode_feature(value):
    value = dict(value)
    for k in ("bands", "bands_slow", "balance"):
        if value[k] is not None:
            value[k] = np.asarray(value[k], dtype=np.float32)
    value["events"] = tuple(Onset(**e) for e in value["events"])
    return Features(**value)


class Orchestrator:
    def __init__(self, fixtures, directory=None):
        self.fixtures = dict(fixtures)
        self.directory = Path(
            directory or Path.home() / ".local/share/rigby/orchestrator"
        )
        self.directory.mkdir(parents=True, exist_ok=True)
        self.store_lock = (self.directory / "session.lock").open("a")
        try:
            fcntl.flock(self.store_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.store_lock.close()
            raise RuntimeError(
                "This arrangement directory is already open. Use its editor, or a different --directory."
            ) from None
        self.lock = threading.RLock()
        self.executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="arrangement"
        )
        self.project = new_project()
        self.revision = 0
        self.job = ""
        self.error = ""
        self.features = {}
        self.metadata = {}
        self.bases = {}
        self.pending = set()
        self.active = False
        self.playing = False
        self.track_id = None
        self.proposal_id = None
        self.position = 0.0
        self.anchor = time.monotonic()
        self.loop = None
        self.last_transport = time.monotonic()
        self.transport_sequences = {}
        self.transport_lost = False
        self.closed = False
        self.identify = None
        self.output_settings = {"g": 2.2, "master": 1.0, "min_lit": 3}
        saved = self.directory / "session.json"
        if saved.exists():
            try:
                self.project = validate_project(json.loads(saved.read_text()))
            except (ValueError, OSError) as e:
                self.error = f"Could not load saved set: {e}"
        self.prepare_project()
        from .ai_director import AIDirector

        self.ai = AIDirector(self)

    def state(self):
        with self.lock:
            warnings = set()
            for track in self.project["tracks"]:
                for clip in track.get("clips", []):
                    for name, indices in clip.get("targets", {}).items():
                        fix = self.fixtures.get(name)
                        if fix is None or any(i >= fix.n for i in indices):
                            warnings.add(
                                f"{clip.get('name', 'Clip')}: selection does not match fixture {name}"
                            )
            return {
                "project": copy.deepcopy(self.project),
                "revision": self.revision,
                "job": self.job,
                "error": self.error,
                "active": self.active,
                "warnings": sorted(warnings),
                "ready": {t["id"]: self.ready(t) for t in self.project["tracks"]},
                "transport": {
                    "track": self.track_id,
                    "proposal": self.proposal_id,
                    "position": self.current_time(),
                    "playing": self.playing,
                    "loop": self.loop,
                },
            }

    def _save(self):
        atomic_json(self.directory / "session.json", self.project)

    def replace(self, project, revision):
        proposed = validate_project(project)
        with self.lock:
            if revision != self.revision:
                raise RuntimeError(
                    "The set changed in another window. Reload before editing."
                )
            for t in proposed["tracks"]:
                meta = self.metadata.get(t["audio"])
                if meta and abs(t["duration"] - meta["duration"]) > 0.02:
                    raise ValueError(
                        "Track duration does not match the exact audio file"
                    )
            self.project = proposed
            self.revision += 1
            if self.track_id not in {t["id"] for t in proposed["tracks"]}:
                self.playing = self.active = False
                self.track_id = None
            self._save()
            self.prepare_project()
        return self.state()

    def find_track(self, tid):
        track = next((t for t in self.project["tracks"] if t["id"] == tid), None)
        if track is None:
            raise ValueError("Unknown track")
        return track

    def needed(self, track):
        return {track.get("look", "prism")} | {
            c["look"] for c in track.get("clips", []) if c.get("look")
        }

    def ready(self, track):
        return (
            track["audio"] in self.features
            and abs(
                track["duration"]
                - self.metadata.get(track["audio"], {}).get(
                    "duration", track["duration"]
                )
            )
            < 0.02
            and all((track["audio"], look) in self.bases for look in self.needed(track))
        )

    def prepare_project(self):
        for track in self.project["tracks"]:
            audio = track["audio"]
            for look in self.needed(track):
                key = (audio, look)
                if key not in self.bases and key not in self.pending:
                    self.pending.add(key)
                    self.executor.submit(self._prepare, audio, look)

    def _prepare(self, audio, look):
        try:
            with self.lock:
                self.job = f"Preparing {look} preview…"
            if audio not in self.features:
                meta = json.loads((self.directory / f"{audio}.json").read_text())
                if meta["version"] != CACHE_VERSION:
                    raise ValueError("Analysis version changed; reimport audio")
                with gzip.open(self.directory / f"{audio}.features.gz", "rt") as stream:
                    frames = [decode_feature(v) for v in json.load(stream)]
                with self.lock:
                    self.features[audio], self.metadata[audio] = frames, meta
            frames = self.features[audio]
            offsets, total = {}, 0
            for name, fix in self.fixtures.items():
                offsets[name] = slice(total, total + fix.n)
                total += fix.n
            fingerprint = hashlib.sha256(
                repr([(k, asdict(v)) for k, v in self.fixtures.items()]).encode()
            ).hexdigest()[:16]
            path = self.directory / f"{audio}.{look}.{fingerprint}.v{CACHE_VERSION}.npy"
            if not path.exists():
                temp = path.with_suffix(".tmp.npy")
                cache = np.lib.format.open_memmap(
                    temp, mode="w+", dtype=np.float16, shape=(len(frames), total, 3)
                )
                effect = LOOKS[look](self.fixtures)
                for i, f in enumerate(frames):
                    if self.closed:
                        return
                    effect.step(1 / FPS, f)
                    for name, rgb in effect.render(f).items():
                        cache[i, offsets[name]] = rgb
                cache.flush()
                del cache
                os.replace(temp, path)
            cache = np.load(path, mmap_mode="r", allow_pickle=False)
            if cache.shape != (len(frames), total, 3):
                raise ValueError("Prepared frame cache has invalid shape")
            with self.lock:
                self.bases[(audio, look)] = (cache, offsets)
        except Exception as e:
            with self.lock:
                self.error = f"Could not prepare audio: {e}. Reimport the matching audio file if missing."
        finally:
            with self.lock:
                self.pending.discard((audio, look))
                if not self.pending:
                    self.job = ""

    def import_audio(self, stream, size, title):
        if not 0 < size <= 200 * 1024 * 1024:
            raise ValueError("Choose an audio file under 200 MB")
        fd, name = tempfile.mkstemp(dir=self.directory, suffix=".upload")
        try:
            digest = hashlib.sha256()
            with os.fdopen(fd, "wb") as out:
                left = size
                while left:
                    chunk = stream.read(min(left, 1024 * 1024))
                    if not chunk:
                        raise ValueError("Incomplete upload")
                    digest.update(chunk)
                    out.write(chunk)
                    left -= len(chunk)
            audio = digest.hexdigest()
            with self.lock:
                self.job = "Decoding and analysing the whole track…"
                self.error = ""
            self.executor.submit(self._import, Path(name), audio, str(title)[:200])
        except Exception:
            Path(name).unlink(missing_ok=True)
            raise

    def _import(self, upload, audio, title):
        try:
            dest = self.directory / f"{audio}.wav"
            meta_path = self.directory / f"{audio}.json"
            reusable = False
            if (
                meta_path.exists()
                and dest.exists()
                and (self.directory / f"{audio}.features.gz").exists()
            ):
                try:
                    reusable = (
                        json.loads(meta_path.read_text()).get("version")
                        == CACHE_VERSION
                    )
                except (ValueError, OSError):
                    pass
            if not reusable:
                probe = subprocess.run(
                    [
                        "ffprobe",
                        "-v",
                        "error",
                        "-show_entries",
                        "format=duration",
                        "-of",
                        "default=noprint_wrappers=1:nokey=1",
                        str(upload),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=True,
                )
                number(float(probe.stdout.strip()), 0.01, 1800)
                subprocess.run(
                    [
                        "ffmpeg",
                        "-v",
                        "error",
                        "-y",
                        "-i",
                        str(upload),
                        "-vn",
                        "-ac",
                        "2",
                        "-ar",
                        str(RATE),
                        "-c:a",
                        "pcm_s16le",
                        str(dest),
                    ],
                    capture_output=True,
                    timeout=180,
                    check=True,
                )
                features, meta = analyse(dest, lambda: self.closed)
                temp = self.directory / f"{audio}.features.tmp.gz"
                with gzip.open(temp, "wt") as stream:
                    json.dump(
                        [feature_json(f) for f in features], stream, allow_nan=False
                    )
                os.replace(temp, self.directory / f"{audio}.features.gz")
                atomic_json(meta_path, meta)
                with self.lock:
                    self.features[audio], self.metadata[audio] = features, meta
            else:
                meta = json.loads(meta_path.read_text())
            with self.lock:
                existing = next(
                    (t for t in self.project["tracks"] if t["audio"] == audio), None
                )
                if existing is None:
                    track = {
                        "id": uuid.uuid4().hex,
                        "title": title,
                        "audio": audio,
                        "duration": meta["duration"],
                        "look": "prism",
                        "clips": generated_clips(meta, []),
                    }
                    self.project["tracks"].append(track)
                    self.revision += 1
                    self._save()
                self.prepare_project()
        except Exception as e:
            with self.lock:
                self.error = f"Audio import failed: {e}"
        finally:
            upload.unlink(missing_ok=True)
            with self.lock:
                if not self.pending:
                    self.job = ""

    def metadata_for(self, tid):
        with self.lock:
            return self.metadata.get(self.find_track(tid)["audio"], {})

    def current_time(self):
        pos = self.position + (time.monotonic() - self.anchor if self.playing else 0.0)
        if self.loop and self.playing and pos >= self.loop[1]:
            pos = self.loop[0] + (pos - self.loop[0]) % (self.loop[1] - self.loop[0])
        return pos

    def transport(self, msg):
        with self.lock:
            client = msg.get("client")
            sequence = msg.get("sequence")
            if client is not None:
                if (
                    not isinstance(client, str)
                    or len(client) > 100
                    or type(sequence) is not int
                ):
                    raise ValueError("Invalid transport sequence")
                if sequence <= self.transport_sequences.get(client, -1):
                    return {"ignored": True}
            track = self.find_track(msg.get("track", self.track_id))
            proposal_id = msg.get("proposal")
            if proposal_id is not None and (
                not isinstance(proposal_id, str) or len(proposal_id) > 100
            ):
                raise ValueError("Invalid proposal id")
            proposed = self.ai.live_proposal(proposal_id, track["id"])
            # A discarded/stale draft falls back to the saved arrangement. This
            # also handles transport messages already in flight during Apply.
            proposal_id = proposal_id if proposed is not None else None
            position = number(msg.get("position", 0), 0, track["duration"])
            loop = msg.get("loop")
            if loop is not None:
                if not isinstance(loop, list) or len(loop) != 2:
                    raise ValueError("Loop needs start and end")
                a, b = (number(v, 0, track["duration"]) for v in loop)
                if b - a < 0.05:
                    raise ValueError("Loop must be at least 50 ms")
            # Audio can already be playing while a new look is preparing.
            # Keep accepting the transport clock; live_frame holds black until
            # all required frames are ready, then joins at the current position.
            if client is not None:
                if len(self.transport_sequences) > 100:
                    self.transport_sequences.clear()
                self.transport_sequences[client] = sequence
            self.last_transport = time.monotonic()
            self.transport_lost = False
            self.track_id, self.position, self.anchor = (
                track["id"],
                position,
                time.monotonic(),
            )
            self.proposal_id = proposal_id
            self.loop = loop
            self.playing = bool(msg.get("playing", False))
            self.active = bool(msg.get("active", False))
            return {
                "track": self.track_id,
                "position": position,
                "playing": self.playing,
                "active": self.active,
                "proposal": self.proposal_id,
                "waiting": self.active and not self.ready(proposed or track),
            }

    def frame(self, tid, position, track_override=None):
        with self.lock:
            track = (
                track_override if track_override is not None else self.find_track(tid)
            )
            if not self.ready(track):
                raise ValueError("Track is still preparing or its audio is missing")
            t = number(position, 0, track["duration"])
            frames = self.features[track["audio"]]
            f = frames[min(int(t * FPS), len(frames) - 1)]

            def base(look, at):
                cache, offsets = self.bases[(track["audio"], look)]
                index = min(max(int(at * FPS), 0), len(cache) - 1)
                return {
                    k: np.asarray(cache[index, section], dtype=np.float32)
                    for k, section in offsets.items()
                }

            return compose(track, t, self.fixtures, base, f, self.ai.positions)

    def live_frame(self):
        with self.lock:
            if not self.active or self.track_id is None:
                return None
            track = self.find_track(self.track_id)
            proposed = self.ai.live_proposal(self.proposal_id, self.track_id)
            if proposed is not None:
                track = proposed
            else:
                self.proposal_id = None
            pos = self.current_time()
            if self.playing and time.monotonic() - self.last_transport > 2.0:
                self.playing = False
                self.position = min(pos, track["duration"])
                self.transport_lost = True
            if self.transport_lost:
                return {k: np.zeros((f.n, 3)) for k, f in self.fixtures.items()}
            if pos >= track["duration"]:
                self.playing = False
                self.position = track["duration"]
                return {k: np.zeros((f.n, 3)) for k, f in self.fixtures.items()}
            if not self.ready(track):
                return {k: np.zeros((f.n, 3)) for k, f in self.fixtures.items()}
            return self.frame(self.track_id, pos, track)

    def preview(self, tid, position, track_override=None):
        # Slow fixture samples intentionally match their achievable 12 Hz rate.
        frame = self.frame(tid, position, track_override)
        if any(f.slow for f in self.fixtures.values()):
            slow = self.frame(tid, math.floor(position * 12) / 12, track_override)
            for key, fix in self.fixtures.items():
                if fix.slow:
                    frame[key] = slow[key]
        return {
            k: encode_rgb(v, **self.output_settings).tolist() for k, v in frame.items()
        }

    def close(self):
        self.closed = True
        self.ai.close()
        self.executor.shutdown(wait=True, cancel_futures=True)
        self.store_lock.close()
