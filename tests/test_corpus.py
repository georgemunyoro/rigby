"""Signal-level acceptance tests using annotated, reproducible arrangements."""

from pathlib import Path
import json
import tempfile
import unittest
import wave

import numpy as np

import tracks
from bench import match_events
from rigby.analyze import Analyzer


class CorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.results = {}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for make, name in (
                (tracks.dyn, "dyn"),
                (tracks.hard, "hard"),
                (tracks.ballad, "ballad2"),
                (tracks.timing, "timing"),
                (tracks.sustained, "sustained"),
            ):
                make(root)
                with wave.open(str(root / (name + ".wav"))) as wav:
                    audio = (
                        np.frombuffer(wav.readframes(wav.getnframes()), "<i2") / 32768.0
                    )
                an = Analyzer("file:test")
                features = [
                    an.feed(audio[i : i + an.hop])
                    for i in range(0, len(audio) - an.hop + 1, an.hop)
                ]
                events = sorted({e.time for f in features for e in f.events})
                # The score matcher accounts for nearby evidence from different roles.
                truth = json.loads((root / (name + ".json")).read_text())
                cls.results[name] = features, events, truth

    def test_primary_rhythm_recall_and_phase(self):
        for name in ("dyn", "hard"):
            with self.subTest(track=name):
                features, events, truth = self.results[name]
                _, matched, _ = match_events(events, truth["hits"], 0.06)
                self.assertGreaterEqual(len(matched) / len(truth["hits"]), 0.93)
                errors = [
                    abs(
                        (f.beat_position - f.timestamp * truth["bpm"] / 60 + 0.5) % 1
                        - 0.5
                    )
                    for f in features
                    if f.timestamp > 8 and f.pulse > 0.4
                ]
                self.assertTrue(errors)
                self.assertLess(np.mean(errors), 0.06)

    def test_ballad_does_not_lock_to_pitch_motion(self):
        features, events, _ = self.results["ballad2"]
        self.assertLessEqual(len(events), 20)
        self.assertLess(np.mean([f.pulse for f in features]), 0.1)

    def test_sustained_music_only_accents_annotated_ticks(self):
        features, events, truth = self.results["sustained"]
        used, matched, _ = match_events(events, truth["hits"], 0.06)
        self.assertEqual(len(used), len(events))
        self.assertEqual(len(matched), len(truth["hits"]))
        self.assertLess(max(f.pulse for f in features), 0.2)
        self.assertLess(features[-1].dynamics, 0.001)

    def test_tempo_change_recovers_phase_and_silence(self):
        features, _, truth = self.results["timing"]
        beat_times = np.array(truth["beats"])
        errors = []
        for f in features:
            if 27 < f.timestamp < 33:
                i = np.searchsorted(beat_times, f.timestamp, side="right") - 1
                expected = (f.timestamp - beat_times[i]) / (
                    beat_times[i + 1] - beat_times[i]
                )
                errors.append(abs((f.beat_position - expected + 0.5) % 1 - 0.5))
        self.assertLess(np.mean(errors), 0.1)
        self.assertLess(features[-1].dynamics, 0.001)
        self.assertGreater(features[3000].pulse, 0.4)
