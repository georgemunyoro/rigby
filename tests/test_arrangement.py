"""Authoring ownership, deterministic seeking, validation, and prepared playback."""

import copy
import json
from types import SimpleNamespace
import urllib.request
import urllib.error
from pathlib import Path
import tempfile
import time
import unittest
import wave

import numpy as np

from rigby.analyze import Features
from rigby.arrangement import (
    compose,
    curve,
    generated_clips,
    new_project,
    validate_project,
)
from rigby.orchestrator import FPS, Orchestrator, analyse, virtual_fixtures


def features():
    return Features(
        np.ones(8) * 0.5,
        np.ones(8) * 0.4,
        0.5,
        0.8,
        0.1,
        0.5,
        False,
        0.0,
        presence=1.0,
        swell=0.6,
    )


def track(clips=None):
    return {
        "id": "track",
        "audio": "a" * 64,
        "duration": 12.0,
        "look": "prism",
        "title": "Test",
        "clips": clips or [],
    }


def clip(**kwargs):
    result = dict(
        id="clip",
        name="LED hold",
        start=0.0,
        end=12.0,
        mode="authored",
        pattern="solid",
        colour="#000000",
        targets={"fan_1": [0]},
        curves={},
    )
    result.update(kwargs)
    return result


class LayerTests(unittest.TestCase):
    def setUp(self):
        self.fixtures = virtual_fixtures()
        self.base = {
            k: np.tile([0.8, 0.4, 0.2], (f.n, 1)) for k, f in self.fixtures.items()
        }

    def render(self, clips, t=1.0):
        return compose(track(clips), t, self.fixtures, lambda *_: self.base, features())

    def test_black_is_opaque_but_zero_opacity_is_transparent(self):
        black = self.render([clip()])
        self.assertFalse(black["fan_1"][0].any())
        np.testing.assert_array_equal(black["fan_1"][1:], self.base["fan_1"][1:])
        transparent = self.render([clip(curves={"opacity": [{"time": 0, "value": 0}]})])
        np.testing.assert_array_equal(transparent["fan_1"], self.base["fan_1"])

    def test_colour_and_brightness_can_be_owned_separately(self):
        colour = self.render([clip(colour="#0000ff", ownership="colour")])["fan_1"][0]
        np.testing.assert_allclose(colour, [0, 0, 0.8])
        brightness = self.render([clip(colour="#808080", ownership="brightness")])[
            "fan_1"
        ][0]
        np.testing.assert_allclose(brightness, np.array([1, 0.5, 0.25]) * 128 / 255)

    def test_colour_only_chase_preserves_unlit_pattern_background(self):
        result = self.render(
            [
                clip(
                    pattern="chase",
                    colour="#0000ff",
                    ownership="colour",
                    targets={"fan_1": [0, 1, 2, 3]},
                    curves={"position": [{"time": 0, "value": 0}]},
                )
            ]
        )
        np.testing.assert_allclose(result["fan_1"][:4].max(axis=1), 0.8)
        np.testing.assert_allclose(result["fan_1"][1:4], self.base["fan_1"][1:4])

    def test_rgb_keyframes_can_author_exact_colour_changes(self):
        curves = {
            "red": [{"time": 0, "value": 1}, {"time": 2, "value": 0}],
            "blue": [{"time": 0, "value": 0}, {"time": 2, "value": 1}],
        }
        at = self.render([clip(curves=curves)], 1.0)
        np.testing.assert_allclose(at["fan_1"][0], [0.5, 0, 0.5])

    def test_layer_order_and_half_open_clip_boundaries(self):
        result = self.render([clip(), clip(id="top", colour="#ff0000")])
        np.testing.assert_array_equal(result["fan_1"][0], [1, 0, 0])
        at_end = self.render([clip()], 12.0)
        np.testing.assert_array_equal(at_end["fan_1"], self.base["fan_1"])

    def test_custom_led_order_controls_chase(self):
        result = self.render(
            [
                clip(
                    pattern="chase",
                    colour="#ff0000",
                    targets={"fan_1": [4, 2, 7, 0]},
                    curves={"position": [{"time": 0, "value": 0.25}]},
                )
            ]
        )
        np.testing.assert_array_equal(result["fan_1"][2], [1, 0, 0])
        self.assertFalse(result["fan_1"][4].any())
        np.testing.assert_array_equal(result["fan_1"][1], self.base["fan_1"][1])

    def test_keyframe_easing_and_fade(self):
        keys = [{"time": 0, "value": 0, "ease": "hold"}, {"time": 2, "value": 1}]
        self.assertEqual(curve(keys, 1, 99), 0)
        self.assertEqual(curve(keys, 2, 99), 1)
        keys[0]["ease"] = "smooth"
        self.assertAlmostEqual(curve(keys, 0.5, 99), 0.15625)
        result = self.render([clip(fade_in=2.0)], 1.0)
        np.testing.assert_allclose(result["fan_1"][0], self.base["fan_1"][0] * 0.5)

    def test_regeneration_keeps_manual_and_locked_regions(self):
        old = [
            clip(origin="manual", start=0, end=3),
            clip(id="locked", origin="generated", locked=True, start=3, end=6),
            clip(id="replace", origin="generated", start=6, end=12),
        ]
        sections = {
            "sections": [
                {"start": i, "end": i + 3, "label": "full", "theme": 1}
                for i in (0, 3, 6, 9)
            ]
        }
        generated = generated_clips(sections, old)
        self.assertIn(old[0], generated)
        self.assertIn(old[1], generated)
        self.assertNotIn(old[2], generated)
        self.assertEqual(len(generated), 4)

    def test_invalid_keyframes_and_nan_rejected(self):
        for bad in [
            clip(start=float("nan")),
            clip(end=-1),
            clip(curves={"brightness": [{"time": 0, "value": 2}]}),
            clip(
                curves={"position": [{"time": 3, "value": 1}, {"time": 1, "value": 0}]}
            ),
            clip(targets={"fan_1": [1, 1]}),
        ]:
            project = new_project()
            project["tracks"] = [track([bad])]
            with self.assertRaises(ValueError):
                validate_project(project)


class EditorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.editor = Orchestrator(virtual_fixtures(), self.temp.name)
        project = new_project()
        project["tracks"] = [
            track(
                [
                    clip(
                        colour="#ff0000",
                        pattern="gradient",
                        curves={
                            "position": [
                                {"time": 0, "value": 0},
                                {"time": 12, "value": 1},
                            ]
                        },
                    )
                ]
            )
        ]
        self.editor.project = project
        self.editor.features["a" * 64] = [features() for _ in range(12 * FPS)]
        offsets, total = {}, 0
        for k, fix in self.editor.fixtures.items():
            offsets[k] = slice(total, total + fix.n)
            total += fix.n
        self.editor.bases[("a" * 64, "prism")] = (
            np.full((12 * FPS, total, 3), 0.4),
            offsets,
        )

    def tearDown(self):
        self.editor.close()
        self.temp.cleanup()

    def test_seeking_backward_and_forward_is_repeatable(self):
        first = self.editor.frame("track", 2.3)
        self.editor.frame("track", 10.0)
        self.editor.frame("track", 0.1)
        again = self.editor.frame("track", 2.3)
        for k in first:
            np.testing.assert_array_equal(first[k], again[k])
        self.assertNotEqual(
            first["fan_1"][0].tolist(),
            self.editor.frame("track", 8.0)["fan_1"][0].tolist(),
        )

    def test_revision_conflicts_do_not_overwrite_saved_edits(self):
        proposed = copy.deepcopy(self.editor.project)
        proposed["name"] = "My set"
        self.editor.replace(proposed, 0)
        with self.assertRaises(RuntimeError):
            self.editor.replace(new_project(), 0)
        self.assertEqual(
            json.loads((Path(self.temp.name) / "session.json").read_text())["name"],
            "My set",
        )

    def test_transport_pause_loop_and_end_blackout(self):
        self.editor.transport(
            {"track": "track", "position": 2, "playing": False, "active": True}
        )
        time.sleep(0.01)
        self.assertEqual(self.editor.current_time(), 2)
        self.editor.transport(
            {
                "track": "track",
                "position": 4,
                "playing": True,
                "active": True,
                "loop": [2, 4],
            }
        )
        self.assertGreaterEqual(self.editor.current_time(), 2)
        self.assertLess(self.editor.current_time(), 2.1)
        self.editor.transport({"track": "track", "position": 12, "active": True})
        self.assertFalse(any(v.any() for v in self.editor.live_frame().values()))

    def test_transport_waits_for_preparation_and_joins_at_current_time(self):
        cached = self.editor.bases.pop(("a" * 64, "prism"))
        result = self.editor.transport(
            {"track": "track", "position": 3.0, "playing": True, "active": True}
        )
        self.assertTrue(result["waiting"])
        self.assertFalse(any(v.any() for v in self.editor.live_frame().values()))
        self.editor.bases[("a" * 64, "prism")] = cached
        self.assertGreaterEqual(self.editor.current_time(), 3.0)
        self.assertTrue(any(v.any() for v in self.editor.live_frame().values()))
        result = self.editor.transport(
            {"track": "track", "position": 4.0, "playing": False, "active": True}
        )
        self.assertFalse(result["waiting"])
        self.assertEqual(self.editor.current_time(), 4.0)

    def test_stale_transport_messages_do_not_resume_paused_audio(self):
        msg = {"track": "track", "position": 2, "active": True, "client": "browser"}
        self.editor.transport(dict(msg, sequence=2, playing=False))
        self.editor.transport(dict(msg, sequence=1, playing=True))
        self.assertFalse(self.editor.playing)
        self.editor.transport(dict(msg, sequence=3, playing=True))
        self.editor.last_transport -= 3
        self.assertFalse(any(v.any() for v in self.editor.live_frame().values()))
        self.assertFalse(self.editor.playing)

    def test_preview_uses_output_master(self):
        self.editor.output_settings["master"] = 0
        self.assertFalse(
            any(np.asarray(v).any() for v in self.editor.preview("track", 1.0).values())
        )

    def test_store_rejects_a_second_writer(self):
        with self.assertRaises(RuntimeError):
            Orchestrator(virtual_fixtures(), self.temp.name)

    def test_http_routes_validate_proposals_and_detect_conflicts(self):
        from rigby.control import (
            Canvas,
            ControlServer,
            Params,
            Rig,
            Telemetry,
            geometry_of,
        )

        geometry = geometry_of(self.editor.fixtures)
        rig = Rig(geometry, Canvas(geometry))
        rig.editor = self.editor
        server = ControlServer(
            Params(),
            Telemetry(),
            "",
            rig,
            SimpleNamespace(state=lambda: {}, command=lambda msg: {}),
            port=0,
        )
        server.start()
        root = f"http://127.0.0.1:{server.port}/api/orchestrator/"

        def post(route, value):
            req = urllib.request.Request(
                root + route,
                data=json.dumps(value).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            return json.load(urllib.request.urlopen(req, timeout=3))

        try:
            original = self.editor.state()
            proposed = copy.deepcopy(original["project"])
            proposed["name"] = "Reviewed proposal"
            result = post("proposal", {"project": proposed})
            self.assertEqual(result["tracks"], 1)
            self.assertEqual(self.editor.project["name"], "Untitled set")
            post("project", {"project": proposed, "revision": original["revision"]})
            with self.assertRaises(urllib.error.HTTPError) as err:
                post("project", {"project": proposed, "revision": original["revision"]})
            self.assertEqual(err.exception.code, 409)
            cached = self.editor.bases.pop(("a" * 64, "prism"))
            waiting = post(
                "transport",
                {"track": "track", "position": 2.0, "playing": True, "active": True},
            )
            self.assertTrue(waiting["waiting"])
            self.editor.bases[("a" * 64, "prism")] = cached
            favicon = urllib.request.urlopen(
                root.replace("/api/orchestrator/", "/favicon.ico"), timeout=3
            )
            self.assertEqual(favicon.status, 204)
            favicon.close()
            preview = json.load(
                urllib.request.urlopen(root + "preview?track=track&time=2", timeout=3)
            )
            self.assertEqual(len(preview["fan_1"]), 12)
            bad = copy.deepcopy(proposed)
            bad["tracks"][0]["clips"][0]["curves"] = {"brightness": ["bad key"]}
            with self.assertRaises(urllib.error.HTTPError) as err:
                post("proposal", {"project": bad})
            self.assertEqual(err.exception.code, 400)
            self.assertEqual(self.editor.project["name"], "Reviewed proposal")
        finally:
            server.stop()

    def test_exact_audio_duration_checked(self):
        self.editor.metadata["a" * 64] = {"duration": 12}
        proposed = copy.deepcopy(self.editor.project)
        proposed["tracks"][0]["duration"] = 20
        with self.assertRaises(ValueError):
            self.editor.replace(proposed, 0)


class PreparedAnalysisTests(unittest.TestCase):
    def test_quiet_intro_is_relative_to_the_whole_track(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "track.wav"
            t = np.arange(48000 * 8) / 48000
            amp = np.where(t < 4, 0.015, 0.4)
            signal = (amp * np.sin(2 * np.pi * 110 * t) * 32767).astype("<i2")
            with wave.open(str(path), "wb") as out:
                out.setnchannels(1)
                out.setsampwidth(2)
                out.setframerate(48000)
                out.writeframes(signal.tobytes())
            frames, meta = analyse(path)
            self.assertEqual(len(frames), FPS * 8)
            self.assertLess(np.mean([f.dynamics for f in frames[FPS : 3 * FPS]]), 0.1)
            self.assertGreater(np.mean([f.dynamics for f in frames[6 * FPS :]]), 0.9)
            self.assertEqual(meta["duration"], 8)
            self.assertTrue(meta["waveform"])
            self.assertEqual(meta["sections"][0]["start"], 0)
            self.assertEqual(meta["sections"][-1]["end"], 8)


class SpatialPatternTests(unittest.TestCase):
    def setUp(self):
        self.fixtures = {
            name: SimpleNamespace(n=1, origin=(x, 0.5))
            for name, x in [("left", 0.2), ("centre", 0.5), ("right", 0.8)]
        }
        self.positions = {
            name: [[f.origin[0], 0.5]] for name, f in self.fixtures.items()
        }
        self.base = lambda look, t: {
            name: np.full((1, 3), 0.5) for name in self.fixtures
        }

    def render(self, pattern, position, **values):
        c = clip(
            pattern=pattern,
            targets={},
            colour="#ff0000",
            colour2="#0000ff",
            curves={
                name: [{"time": 0, "value": value}]
                for name, value in {
                    "position": position,
                    "width": 0.05,
                    **values,
                }.items()
            },
        )
        return compose(
            track([c]), 1, self.fixtures, self.base, features(), self.positions
        )

    def test_sweep_follows_global_positions_and_direction(self):
        frame = self.render("sweep", 0.2)
        self.assertGreater(frame["left"][0, 0], 0.99)
        self.assertLess(frame["right"][0, 0], 0.001)
        reverse = self.render("sweep", 0.2, direction=180)
        self.assertGreater(reverse["right"][0, 0], 0.99)
        self.assertLess(reverse["left"][0, 0], 0.001)

    def test_ripple_and_mirror_are_symmetric_across_devices(self):
        for pattern, phase in [("ripple", 0.3 / np.sqrt(2)), ("mirror", 0.6)]:
            with self.subTest(pattern=pattern):
                frame = self.render(pattern, phase)
                np.testing.assert_allclose(frame["left"], frame["right"])
                self.assertGreater(frame["left"][0, 0], 0.99)
                self.assertLess(frame["centre"][0, 0], 0.001)

    def test_spatial_gradient_and_global_ordered_path(self):
        frame = self.render("spatial_gradient", 0)
        self.assertGreater(frame["left"][0, 0], frame["right"][0, 0])
        self.assertLess(frame["left"][0, 2], frame["right"][0, 2])
        c = clip(
            pattern="path",
            colour="#ffffff",
            targets={"right": [0], "left": [0], "centre": [0]},
            curves={
                "position": [{"time": 0, "value": 1 / 3}],
                "width": [{"time": 0, "value": 0.05}],
            },
        )
        frame = compose(
            track([c]), 1, self.fixtures, self.base, features(), self.positions
        )
        self.assertGreater(frame["left"].max(), 0.99)
        self.assertLess(frame["right"].max(), 0.001)

    def test_saved_spatial_coordinates_survive_layout_changes_and_seeking(self):
        c = clip(
            pattern="sweep",
            targets={},
            colour="#ffffff",
            spatial=self.positions,
            curves={
                "position": [{"time": 0, "value": 0.2}, {"time": 12, "value": 0.8}]
            },
        )
        original = compose(
            track([c]), 2, self.fixtures, self.base, features(), self.positions
        )
        changed = {"left": [[0.9, 0.9]], "centre": [[0, 0]], "right": [[0.1, 0.1]]}
        compose(track([c]), 10, self.fixtures, self.base, features(), changed)
        again = compose(track([c]), 2, self.fixtures, self.base, features(), changed)
        for name in self.fixtures:
            np.testing.assert_array_equal(original[name], again[name])
        invalid = track([dict(c, spatial={"left": [[float("nan"), 0]]})])
        with self.assertRaises(ValueError):
            validate_project(dict(new_project(), tracks=[invalid]))
