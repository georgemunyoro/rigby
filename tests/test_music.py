"""Regression cases for musical meaning, clocks, and physical output intent."""

import io
from types import SimpleNamespace
import unittest

import numpy as np

from rigby.analyze import Analyzer, Features, Onset, RATE
from rigby.music import BeatClock, Director
from rigby.show import Auto, Duotone, Rain, Spectrum, Prism, _Drops
from rigby.fx import drive, encode_rgb, mix_layers, crossfade
from rigby.sink import Sink
from bench import fake_fixtures, interval_mask, score


def feature(**kwargs):
    args = dict(
        bands=np.ones(8) * 0.3,
        bands_slow=np.ones(8) * 0.3,
        level=0.5,
        dynamics=0.6,
        rms=0.1,
        bass=0.3,
        onset=False,
        flux=0.0,
    )
    args.update(kwargs)
    return Features(**args)


def tone(an, hz, seconds, amplitude=0.1):
    last = None
    for _ in range(round(seconds / an.dt)):
        samples = np.arange(an.hop) + round(an._time * RATE)
        last = an.feed(
            (amplitude * np.sin(2 * np.pi * hz * samples / RATE)).astype(np.float32)
        )
    return last


class SignalTests(unittest.TestCase):
    def test_silence_is_not_a_swell(self):
        an = Analyzer("file:test")
        f = tone(an, 0, 4)
        self.assertEqual((f.dynamics, f.swell, f.presence, f.pulse), (0, 0, 0, 0))
        for cls in (Auto, Rain, Duotone, Spectrum, Prism):
            look = cls(fake_fixtures())
            look.step(4, f)
            self.assertTrue(
                all(not encode_rgb(v).any() for v in look.render(f).values())
            )

    def test_mid_tone_has_no_bass(self):
        an = Analyzer("file:test")
        f = tone(an, 1000, 3)
        self.assertLess(f.bass, 0.01)
        self.assertLess(float(f.bands[:3].max()), 0.01)
        self.assertGreater(f.bands[4], 0.5)
        self.assertGreater(f.mid_share, 0.99)

    def test_bass_uses_its_own_reference(self):
        f = tone(Analyzer("file:test"), 100, 2)
        self.assertGreater(f.bass, 0.6)
        self.assertLess(f.bands[-1], 0.01)

    def test_quiet_signal_remains_visible_without_noise_amplification(self):
        f = tone(Analyzer("file:test"), 1000, 2, 0.002)
        self.assertGreater(f.presence, 0.9)
        self.assertLess(f.bass, 0.01)
        quiet = tone(Analyzer("file:test"), 1000, 2, 1e-6)
        self.assertLess(quiet.presence, 0.01)

    def test_long_silence_forgets_reference(self):
        an = Analyzer("file:test")
        tone(an, 100, 1)
        f = tone(an, 0, 7)
        self.assertLess(f.dynamics, 0.001)
        self.assertEqual(an._ref_time, 0)
        self.assertEqual(f.pulse, 0)
        f = tone(an, 100, 1, 0.002)
        self.assertGreater(f.dynamics, 0.3)

    def test_nonfinite_audio_recovers(self):
        an = Analyzer("file:test")
        an.feed(np.full(an.hop, np.nan))
        f = tone(an, 100, 1)
        self.assertTrue(np.isfinite(f.bands).all())
        self.assertGreater(f.bass, 0.5)

    def test_sustained_vibrato_does_not_invent_a_drumbeat(self):
        an = Analyzer("file:test")
        n = np.arange(RATE * 4) / RATE
        samples = 0.1 * np.sin(2 * np.pi * 440 * n + 0.4 * np.sin(2 * np.pi * 5 * n))
        events = []
        for i in range(0, len(samples), an.hop):
            f = an.feed(samples[i : i + an.hop])
            events.extend(f.events)
        self.assertLess(len(events), 4)
        self.assertLess(f.pulse, 0.2)


class TimingTests(unittest.TestCase):
    def test_analysis_does_not_depend_on_render_fps(self):
        results = [
            tone(Analyzer("file:test", fps=fps), 100, 2) for fps in (30, 60, 120)
        ]
        for f in results[1:]:
            np.testing.assert_array_equal(f.bands, results[0].bands)
            self.assertEqual(f.dynamics, results[0].dynamics)
            self.assertEqual(f.beat_position, results[0].beat_position)

    def test_events_delivered_once_and_only_after_detection(self):
        an = Analyzer("file:test")
        e = Onset(0.985, 0.7, 0.8, "low")
        an._history.extend(
            [
                feature(timestamp=1.0),
                feature(timestamp=1.01, events=(e,), onset=True),
                feature(timestamp=1.02),
            ]
        )
        self.assertFalse(an.sample(1.005).events)
        self.assertEqual(an.sample(1.015).events, (e,))
        self.assertFalse(an.sample(1.015).events)
        self.assertFalse(an.sample(1.02).events)
        # Moving the offset backward must not replay the hit.
        self.assertFalse(an.sample(1.01).events)

    def test_render_stall_discards_old_hits(self):
        an = Analyzer("file:test")
        an._history.extend(
            [
                feature(timestamp=1, events=(Onset(0.97, 1, 1, "low"),)),
                feature(timestamp=2),
            ]
        )
        self.assertFalse(an.sample(2).events)

    def test_stale_capture_fades_and_loses_confidence(self):
        an = Analyzer("file:test")
        an._history.append(feature(timestamp=1, pulse=0.9))
        stale = an.sample(1.01, stale_age=10)
        self.assertLess(stale.dynamics, 0.001)
        self.assertLess(stale.pulse, 0.01)

    def test_resuming_stalled_capture_does_not_restore_a_stale_peak(self):
        an = Analyzer("file:test")
        tone(an, 100, 0.5)
        an._pending_event = (Onset(an._time, 1.0, 1.0, "low"), 1.0, an._time)
        an._after_gap(3.0)
        f = an.feed(np.zeros(an.hop))
        self.assertFalse(f.events)
        self.assertLess(f.dynamics, 0.01)
        self.assertLess(f.bands.max(), 0.01)
        self.assertEqual(f.pulse, 0.0)

    def test_feature_interpolation(self):
        an = Analyzer("file:test")
        an._history.extend(
            [feature(timestamp=1, level=0), feature(timestamp=1.01, level=1)]
        )
        self.assertAlmostEqual(an.sample(1.005).level, 0.5)

    def test_partial_final_hop_is_retained(self):
        an = Analyzer("file:test")
        an.proc = SimpleNamespace(
            stdout=io.BytesIO(np.ones(71, dtype=np.float32).tobytes()),
            stderr=io.BytesIO(),
        )
        c = an._next_chunk()
        self.assertEqual(len(c), an.hop)
        self.assertEqual(c.sum(), 71)

    def test_history_is_bounded(self):
        an = Analyzer("file:test")
        tone(an, 0, 5)
        self.assertLessEqual(len(an._history), 400)

    def test_clock_locks_and_holds_through_a_short_break(self):
        clock = BeatClock()
        for i in range(1000):
            t = (i + 1) / 100
            hit = i % 50 == 0
            e = (Onset(t, 0.7, 0.9, "low"),) if hit else ()
            clock.step(t, 1.0 if hit else 0.0, e)
        self.assertAlmostEqual(clock.bpm, 120, delta=2)
        self.assertGreater(clock.confidence, 0.6)
        self.assertLess(clock.bar_confidence, 0.2)  # no fabricated downbeat
        before = clock.position
        for i in range(100):
            clock.step(10 + (i + 1) / 100, 0.0, (), False)
        self.assertAlmostEqual(clock.position - before, 2.0, delta=0.15)
        for i in range(700):
            clock.step(11 + (i + 1) / 100, 0.0, (), False)
        self.assertEqual(clock.confidence, 0)
        self.assertEqual(clock.bpm, 0)

    def test_fills_do_not_advance_clock_as_extra_beats(self):
        clock = BeatClock()
        for i in range(1500):
            t = (i + 1) / 100
            hit = i % 50 == 0
            fill = i > 1000 and i % 200 in (125, 150, 175)
            events = (Onset(t, 0.8 if hit else 0.3, 0.9, "low"),) if hit or fill else ()
            clock.step(t, 1 if hit else 0.3 if fill else 0, events)
        self.assertAlmostEqual(clock.bpm, 120, delta=4)


class EffectTests(unittest.TestCase):
    def test_extra_onsets_do_not_change_gesture_or_beat_count(self):
        a, b = Duotone(fake_fixtures()), Duotone(fake_fixtures())
        for i in range(300):
            args = dict(
                timestamp=i / 60, bpm=120, beat_position=i / 30, pulse=0.9, density=0.3
            )
            a.step(1 / 60, feature(**args))
            b.step(1 / 60, feature(**args, onset=True, onset_strength=0.5))
        self.assertEqual(a.beat, b.beat)
        self.assertEqual(a._g.name, b._g.name)
        self.assertAlmostEqual(a.spin, b.spin)

    def test_attack_strength_changes_accent_amplitude(self):
        weak, strong = Duotone(fake_fixtures()), Duotone(fake_fixtures())
        for look, strength in ((weak, 0.2), (strong, 1)):
            look.step(
                1 / 60, feature(timestamp=1, events=(Onset(1, strength, 1, "mid"),))
            )
        self.assertLess(weak._flash, strong._flash * 0.5)

    def test_rain_only_accents_one_fast_fixture(self):
        look = Rain(fake_fixtures())
        look.step(
            0.01, feature(timestamp=1, pulse=0.9, events=(Onset(1, 1, 1, "mid"),))
        )
        self.assertEqual(sum(np.sum(d.tone == 2) for d in look.drops.values()), 1)
        self.assertFalse(
            any(
                np.any(look.drops[k].tone == 2)
                for k, f in look.fixtures.items()
                if f.slow
            )
        )

    def test_steady_syncopated_music_reaches_full_without_a_swell(self):
        look = Duotone(fake_fixtures())
        for i in range(900):
            look.step(1 / 60, feature(timestamp=i / 60, density=.48,
                                     dynamics=.8, swell=.5, pulse=.15))
        self.assertGreater(look._energy, .9)
        self.assertEqual(look.director.scene, "full")
        self.assertGreater(look.director.expansion, .95)
        self.assertGreaterEqual(look._g.rate, 2.)

    def test_sustained_loudness_alone_does_not_create_rhythmic_activity(self):
        look = Duotone(fake_fixtures())
        for _ in range(900):
            look.step(1 / 60, feature(dynamics=1., swell=.8, density=.05))
        self.assertEqual(look._energy, 0.)
        # Arrangement expansion is still allowed on a sustained chorus.
        self.assertEqual(look.director.scene, "full")

    def test_even_groove_refreshes_motifs_on_sixteen_beat_boundaries(self):
        look = Duotone(fake_fixtures())
        changes = []
        for i in range(1800):
            old = look._g
            look.step(1 / 60, feature(timestamp=i / 60, beat_position=i / 30,
                                     bpm=120, pulse=.9, density=.48,
                                     dynamics=.8, swell=.5))
            if look._g is not old and not look.director.changed:
                changes.append(i / 30)
        self.assertGreaterEqual(len(changes), 2)
        for beat in changes:
            self.assertAlmostEqual(beat % 16, 0., delta=.04)

    def test_energetic_rain_has_immediate_local_splashes(self):
        look = Rain(fake_fixtures())
        for i in range(600):
            look.step(1 / 60, feature(timestamp=i / 60, density=.48,
                                     dynamics=.8, swell=.5, pulse=.15))
        look.step(1 / 60, feature(timestamp=10, density=.48, dynamics=.8,
                                 events=(Onset(9.96, .8, .8, "low"),)))
        accented = [k for k, d in look.drops.items() if np.any(d.tone == 2)]
        self.assertEqual(len(accented), 2)
        for key in accented:
            self.assertFalse(look.fixtures[key].slow)
            self.assertGreater(look.drops[key].render()[2].max(), .4)

    def test_spectrum_hits_remain_visible_above_a_bright_wash(self):
        look = Spectrum(fake_fixtures())
        args = dict(density=.48, dynamics=.8, swell=.5,
                    bands=np.full(8, .7), bands_slow=np.full(8, .7))
        for _ in range(600):
            look.step(1 / 60, feature(**args))
        before = look.render(feature(**args))["fans"]
        look.step(1 / 60, feature(**args, timestamp=1,
                                 events=(Onset(.96, .8, .8, "low"),)))
        after = look.render(feature(**args))["fans"]
        self.assertGreater(after[0].max() - before[0].max(), .15)
        self.assertLessEqual(after.max(), 1.)

    def test_prism_preserves_spectral_brightness_while_moving_colour(self):
        prism, spectrum = Prism(fake_fixtures()), Spectrum(fake_fixtures())
        colours = []
        for i in range(120):
            f = feature(timestamp=i / 60, dynamics=.8, swell=.6, density=.48,
                        bpm=120, pulse=.9, beat_position=i / 30)
            prism.step(1 / 60, f)
            spectrum.step(1 / 60, f)
            # Compare at the same spatial phase: prism intentionally moves
            # more slowly, but must retain spectrum's full-energy brightness.
            spectrum._motion = prism._prism_phase
            a, b = prism.render(f), spectrum.render(f)
            for key in a:
                np.testing.assert_allclose(a[key].max(axis=1), b[key].max(axis=1),
                                           atol=1e-6)
                self.assertTrue(np.isfinite(a[key]).all())
            rgb = a["fans"]
            colours.append(rgb / np.maximum(rgb.max(axis=1, keepdims=True), 1e-9))
        self.assertGreater(np.abs(colours[-1] - colours[0]).max(), .1)

    def test_prism_large_accents_shift_colour_but_hats_and_weak_hits_do_not(self):
        for kind, strength, shifts in (("high", 1., False), ("low", .2, False),
                                       ("low", 1., True)):
            look = Prism(fake_fixtures())
            look.step(1 / 60, feature(timestamp=1, onset_strength=strength,
                                     dynamics=.8, swell=.6,
                                     events=(Onset(.96, strength, 1., kind),)))
            self.assertEqual(look._colour_target > 0., shifts)
            self.assertLessEqual(look._slow_colour, look._colour)
        # One sustained lift is one palette change, even across many frames.
        look = Prism(fake_fixtures())
        for i in range(600):
            look.step(1 / 60, feature(swell=.8, energy_slope=.1))
        self.assertAlmostEqual(look._colour_target, .19)

    def test_prism_pulls_back_in_breakdowns_despite_lingering_density(self):
        look = Prism(fake_fixtures())
        loud = feature(dynamics=.8, swell=.6, density=.48, bpm=120, pulse=.9)
        quiet = feature(dynamics=.45, swell=.30, density=.48, bpm=120, pulse=.9)
        for _ in range(600):
            look.step(1 / 60, loud)
        phase = look._prism_phase
        for _ in range(60):
            look.step(1 / 60, loud)
        loud_motion = look._prism_phase - phase
        for _ in range(60):
            look.step(1 / 60, quiet)
        self.assertLess(look._engagement, .12)
        phase = look._prism_phase
        for _ in range(60):
            look.step(1 / 60, quiet)
        self.assertLess(look._prism_phase - phase, loud_motion * .2)
        quiet_level = np.concatenate(list(look.render(quiet).values())).max()
        for _ in range(30):
            look.step(1 / 60, loud)
        self.assertGreater(look._engagement, .9)
        self.assertGreater(np.concatenate(list(look.render(loud).values())).max(),
                           quiet_level * 1.8)

    def test_prism_quiet_accents_do_not_spend_a_palette_swing(self):
        look = Prism(fake_fixtures())
        for i in range(600):
            look.step(1 / 60, feature(timestamp=i / 60, dynamics=.45, swell=.3,
                                     onset_strength=1.,
                                     events=(Onset(i / 60, 1., 1., "low"),)))
        self.assertEqual(look._colour_target, 0.)
        look.step(1 / 60, feature(timestamp=10., dynamics=.9, swell=.65,
                                 onset_strength=1., events=(Onset(10., 1., 1., "low"),)))
        self.assertGreater(look._colour_target, 0.)

    def test_prism_is_selectable_in_the_ui_and_registry(self):
        from rigby.show import LOOKS
        from rigby.ui import LOOKS_LIST
        self.assertIs(LOOKS["prism"], Prism)
        self.assertIn("prism", LOOKS_LIST)

    def test_zero_rate_does_not_spawn(self):
        d = _Drops(6, 1, True)
        d.step(30, 0, 1)
        self.assertEqual(len(d.pos), 0)

    def test_drop_schedule_is_frame_rate_independent(self):
        results = []
        for fps in (30, 120):
            d = _Drops(6, 1, True)
            for _ in range(fps * 2):
                d.step(1 / fps, 4, 0.5)
            results.append(d)
        np.testing.assert_allclose(results[0].pos, results[1].pos)
        np.testing.assert_allclose(results[0].age, results[1].age, atol=1e-5)

    def test_director_waits_for_stability_and_a_boundary(self):
        d = Director()
        for i in range(600):
            f = feature(
                timestamp=i / 100,
                pulse=0.9,
                bpm=120,
                beat_position=i / 50,
                swell=0.75,
                dynamics=0.85,
                density=0.4,
            )
            d.step(0.01, f)
            if d.changed:
                self.assertAlmostEqual(f.beat_position % 1, 0, delta=0.03)
        self.assertEqual(d.scene, "full")

    def test_short_swell_does_not_change_scene(self):
        d = Director()
        for _ in range(100):
            d.step(0.01, feature(swell=1, dynamics=1))
        self.assertEqual(d.scene, "sparse")


class OutputTests(unittest.TestCase):
    def test_zero_master_is_black(self):
        self.assertFalse(encode_rgb(np.ones((6, 3)), master=0).any())

    def test_toe_preserves_absent_channels_and_fades(self):
        self.assertEqual(encode_rgb([[0.1, 0, 0]])[0, 1], 0)
        self.assertFalse(encode_rgb([[1e-7, 1e-7, 1e-7]]).any())
        self.assertGreaterEqual(
            encode_rgb([[0.05, 0, 0]])[0, 0],
            encode_rgb([[0.05, 0, 0]], min_lit=0)[0, 0],
        )

    def test_drive_has_headroom_and_no_early_plateau(self):
        v = drive(np.array([0.1, 0.3, 0.625, 0.8, 1.0]))
        self.assertTrue((np.diff(v) > 0).all())
        self.assertGreater(v[-1], 0.85)
        self.assertLess(v[-1], 0.95)

    def test_layer_overlap_does_not_clip(self):
        out = mix_layers(np.array([[0.8, 0.3, 0]]), np.array([[0, 0.3, 0.8]]))
        self.assertLess(out.max(), 0.9)
        self.assertGreater(out.min(), 0)

    def test_color_overlap_does_not_add_a_hidden_dimmer(self):
        a = np.array([[0.5, 0.0, 0.0]])
        b = np.array([[0.0, 0.0, 0.5]])
        combined = mix_layers(a, b)
        self.assertGreaterEqual(combined.max(), a.max())
        np.testing.assert_allclose(mix_layers(a, np.zeros_like(a)), a)
        self.assertFalse(mix_layers(np.zeros_like(a), np.zeros_like(a)).any())

    def test_auto_crossfade_preserves_emitted_light(self):
        a = np.array([[0.8, 0.0, 0.0]])
        b = np.array([[0.0, 0.0, 0.8]])
        mid = crossfade(a, b, 0.5)
        # A half-fade should retain half each endpoint's electrical light,
        # rather than quartering it through perceptual RGB interpolation.
        np.testing.assert_allclose(mid**2.2, 0.5 * (a**2.2 + b**2.2))
        np.testing.assert_allclose(crossfade(a, b, 0), b)
        np.testing.assert_allclose(crossfade(a, b, 1), a)

    def test_sink_uses_shared_conversion_and_cadence(self):
        sink = Sink.__new__(Sink)
        fix = fake_fixtures()["fans"]
        received = []
        dev = SimpleNamespace(
            leds=[0] * fix.n, set_colors=lambda colors, fast: received.append(colors)
        )
        sink.client = SimpleNamespace(devices=[dev])
        sink._dev_fixtures = {0: [fix]}
        sink._next = {}
        sink._last = {}
        sink.raw = False
        sink.master = 0
        sink.gamma = 2.2
        sink.min_lit = 3
        sink.write({"fans": np.ones((fix.n, 3))})
        self.assertFalse(sink._last[0].any())
        sink.write({"fans": np.ones((fix.n, 3))})
        self.assertEqual(len(received), 1)


class BenchmarkTests(unittest.TestCase):
    def test_empty_ground_truth_and_annotation_windows(self):
        times = np.arange(10.0)
        values = np.where(times < 3, 10.0, 100.0)
        frames = np.broadcast_to(values[:, None, None], (10, 2, 3))
        result = score(
            np.array([]),
            np.column_stack((times, values)),
            frames,
            {"hits": [], "quiet": [0, 3], "loud": [3, 10]},
        )
        self.assertEqual(result["loud_vs_quiet"], 10.0)
        self.assertIsNone(result["timing_bias_ms"])
        np.testing.assert_array_equal(
            interval_mask(times, [[0, 1], [8, 10]]),
            [True, False, False, False, False, False, False, False, True, True],
        )


class StreamIntegrationTests(unittest.TestCase):
    def test_offline_offset_drains_and_event_detection_is_fps_independent(self):
        traces = []
        for fps in (30, 60, 120):
            an = Analyzer("file:test", fps=fps, offset_ms=80)
            an._realtime = False
            t = np.arange(RATE * 3) / RATE
            ph = t % 0.5
            x = (0.4 * np.sin(2 * np.pi * 60 * t) * np.exp(-ph / 0.06)).astype(
                np.float32
            )
            an.proc = SimpleNamespace(
                stdout=io.BytesIO(x.tobytes()), stderr=io.BytesIO()
            )
            frames = []
            while not an.eof:
                f = an.read()
                if f is not None:
                    frames.append(f)
            events = [e.time for f in frames for e in f.events if e.kind == "low"]
            self.assertGreaterEqual(frames[-1].timestamp, 3 - 1 / fps)
            self.assertEqual(an.frames, 300)
            traces.append(events)
        self.assertGreater(len(traces[0]), 3)
        np.testing.assert_allclose(traces[0], traces[1])
        np.testing.assert_allclose(traces[0], traces[2])

    def test_capture_bursts_do_not_reanalyse_or_lose_recent_events(self):
        an = Analyzer("file:test")
        t = np.arange(RATE * 3) / RATE
        x = 0.4 * np.sin(2 * np.pi * 60 * t) * np.exp(-(t % 0.5) / 0.06)
        detected, delivered = [], []
        # Alternate 10/30 ms capture batches with repeated render samples.
        i = 0
        while i < len(x):
            batch = 3 if (i // an.hop) % 4 else 1
            for _ in range(batch):
                if i >= len(x):
                    break
                f = an.feed(x[i : i + an.hop])
                i += an.hop
                detected.extend(f.events)
            for _ in range(2):
                f = an.sample(an._time)
                if f:
                    delivered.extend(f.events)
        self.assertEqual(an.frames, 300)
        self.assertEqual(detected, delivered)

    def test_clock_recovers_after_tempo_change(self):
        clock = BeatClock()
        for i in range(3000):
            t = (i + 1) / 100
            hit = i % 50 == 0 if i < 1200 else (i - 1200) % 60 == 0
            clock.step(
                t, 1.0 if hit else 0.0, (Onset(t, 0.8, 1, "low"),) if hit else ()
            )
        self.assertAlmostEqual(clock.bpm, 100, delta=3)
        self.assertGreater(clock.confidence, 0.5)
        expected_phase = (30 - 12.01) / 0.6
        self.assertLess(abs((clock.position - expected_phase + 0.5) % 1 - 0.5), 0.08)


if __name__ == "__main__":
    unittest.main()
