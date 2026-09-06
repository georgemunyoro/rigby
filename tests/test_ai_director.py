"""AI provider contract, locked/range boundaries, preview isolation and settings."""

import copy
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import wave

import numpy as np

from rigby.ai_director import (
    compile_proposal,
    default_layout,
    gemini_generate,
    rig_png,
    validate_layout,
)
from rigby.arrangement import new_project
from rigby.control import geometry_of
from rigby.orchestrator import FPS, Orchestrator, virtual_fixtures
from test_arrangement import features, track, clip


def model_result(start=2, end=6):
    return {
        "summary": "Build contrast with a restrained blue passage.",
        "cues": [
            {
                "name": "Blue breath",
                "start": start,
                "end": end,
                "mode": "authored",
                "pattern": "solid",
                "look": "inherit",
                "colour": "#0000ff",
                "colour2": "#00ffff",
                "ownership": "rgb",
                "audio": "none",
                "fade_in": 0,
                "fade_out": 0,
                "targets": [{"fixture": "fan_1", "leds": [0, 1]}],
                "curves": [
                    {
                        "property": "position",
                        "keys": [{"time": 0, "value": 0, "ease": "hold"}],
                    }
                ],
            }
        ],
    }


class CompileTests(unittest.TestCase):
    def setUp(self):
        self.fixtures = virtual_fixtures()
        self.project = new_project()
        self.project["tracks"] = [
            track(
                [
                    clip(id="outside", start=0, end=1),
                    clip(id="crossing", start=1, end=3),
                    clip(id="inside", start=3, end=5),
                    clip(id="locked", start=8, end=10, locked=True),
                ]
            )
        ]

    def compile(self, result, start=2, end=7, beats=()):
        return compile_proposal(
            self.project, "track", start, end, result, self.fixtures, beats
        )

    def test_replaces_only_contained_clips_and_preserves_locked_and_crossing(self):
        before = copy.deepcopy(self.project)
        proposed, summary, added, removed = self.compile(model_result())
        self.assertEqual(before, self.project)
        self.assertEqual((added, removed), (1, 1))
        self.assertEqual(
            [c["id"] for c in proposed["tracks"][0]["clips"][:-1]],
            ["outside", "crossing", "locked"],
        )
        self.assertEqual(
            proposed["tracks"][0]["clips"][2], before["tracks"][0]["clips"][3]
        )
        self.assertIn("blue", summary)

    def test_invalid_target_locked_range_and_empty_responses(self):
        variants = [
            model_result(8, 9),
            model_result(1, 6),
            model_result(2, 8),
            {"summary": "none", "cues": []},
        ]
        bad = model_result()
        bad["cues"][0]["targets"][0]["leds"] = [999]
        variants.append(bad)
        bad = model_result()
        bad["cues"][0]["targets"][0]["fixture"] = "invented"
        variants.append(bad)
        bad = model_result()
        bad["cues"][0]["curves"][0]["keys"][0]["value"] = float("nan")
        variants.append(bad)
        for result in variants:
            with self.subTest(result=result), self.assertRaises(ValueError):
                self.compile(result)
        with self.assertRaisesRegex(ValueError, "locked"):
            self.compile(model_result(8, 9), 0, 12)

    def test_snap_scales_local_keys_and_does_not_cross_protection(self):
        result = model_result(2.06, 6.08)
        result["cues"][0]["curves"][0]["keys"].append(
            {"time": 4.02, "value": 1, "ease": "linear"}
        )
        proposed, *_ = self.compile(result, beats=[2, 6])
        cue = proposed["tracks"][0]["clips"][-1]
        self.assertEqual((cue["start"], cue["end"]), (2, 6))
        self.assertAlmostEqual(cue["curves"]["position"][-1]["time"], 4)
        proposed, *_ = self.compile(model_result(6, 7.99), 0, 12, beats=[8.05])
        self.assertEqual(proposed["tracks"][0]["clips"][-1]["end"], 7.99)

    def test_layout_validation_and_png(self):
        layout = default_layout(self.fixtures)
        self.assertEqual(validate_layout(layout, self.fixtures), layout)
        diagram, palette = rig_png(layout, geometry_of(self.fixtures))
        self.assertTrue(diagram.startswith(b"\x89PNG"))
        self.assertEqual(set(palette), set(layout))
        layout["fan_1"]["x"] = float("nan")
        with self.assertRaises(ValueError):
            validate_layout(layout, self.fixtures)


class DirectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.editor = Orchestrator(virtual_fixtures(), self.temp.name)
        self.editor.project["tracks"] = [track()]
        self.editor.features["a" * 64] = [features() for _ in range(12 * FPS)]
        self.editor.metadata["a" * 64] = {
            "duration": 12,
            "beats": [2, 4, 6, 8],
            "sections": [],
        }
        offsets, total = {}, 0
        for k, f in self.editor.fixtures.items():
            offsets[k] = slice(total, total + f.n)
            total += f.n
        self.editor.bases[("a" * 64, "prism")] = (
            np.full((12 * FPS, total, 3), 0.4),
            offsets,
        )
        with wave.open(str(Path(self.temp.name) / ("a" * 64 + ".wav")), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(24000)
            wav.writeframes(
                (np.sin(np.arange(12 * 24000) / 30) * 10000).astype("<i2").tobytes()
            )
        self.calls = []

        def provider(audio, diagram, context, model, key):
            self.calls.append((audio, diagram, context, model, key))
            return model_result()

        self.editor.ai.provider = provider
        self.env = patch.dict(os.environ, {"GEMINI_API_KEY": "fake-test-key"})
        self.env.start()

    def tearDown(self):
        self.editor.close()
        self.editor.ai.executor.shutdown(wait=True)
        self.env.stop()
        self.temp.cleanup()

    def start(self, **kw):
        return self.editor.ai.start(
            {
                "track": "track",
                "revision": self.editor.revision,
                "start": 2,
                "end": 7,
                "instruction": "Blue and quiet",
                **kw,
            }
        )

    def wait(self):
        self.assertTrue(self.editor.ai.job["_done"].wait(10))
        job = self.editor.ai.status()
        self.assertEqual(job["status"], "ready", job)
        return job

    def test_actual_excerpt_context_review_apply_and_revision(self):
        before = copy.deepcopy(self.editor.project)
        job = self.start()
        job = self.wait()
        audio, diagram, context, _, _ = self.calls[0]
        self.assertGreater(len(audio), 1000)
        self.assertTrue(diagram.startswith(b"\x89PNG"))
        self.assertEqual(context["edit_range"], [2, 7])
        self.assertEqual(context["audio_offset"], 0)
        self.assertTrue(context["measurements"])
        self.assertEqual(set(context["physical_layout"]), set(self.editor.fixtures))
        self.assertNotIn("fake-test-key", json.dumps(job))
        self.assertNotIn("fake-test-key", json.dumps(self.editor.ai.config()))
        self.assertEqual(self.editor.project, before)
        proposed = self.editor.ai.proposal_track(job["id"])
        original = self.editor.preview("track", 3)
        audition = self.editor.preview("track", 3, proposed)
        self.assertNotEqual(original["fan_1"][0], audition["fan_1"][0])
        self.assertEqual(self.editor.project, before)
        state = self.editor.ai.apply(job["id"])
        self.assertEqual(state["revision"], 1)
        self.assertEqual(state["project"], job["project"])
        self.start(parent=job["id"], instruction="Keep blue but make it slower")
        self.wait()
        self.assertEqual(self.calls[-1][2]["previous_proposal"], job["project"])
        self.assertEqual(len(self.calls[-1][2]["conversation"]), 1)

    def test_subrange_revision_retains_the_rest_of_an_unapplied_draft(self):
        self.start()
        first = self.wait()
        first_clip = copy.deepcopy(first["project"]["tracks"][0]["clips"][0])
        self.editor.ai.provider = lambda *args: model_result(3, 4)
        self.start(
            parent=first["id"], start=3, end=4, instruction="Just change this beat"
        )
        second = self.wait()
        self.assertTrue(second["based_on_draft"])
        self.assertEqual(second["project"]["tracks"][0]["clips"][0], first_clip)
        self.assertEqual(len(second["project"]["tracks"][0]["clips"]), 2)
        self.assertEqual(self.editor.revision, 0)
        self.editor.ai.apply(second["id"])
        self.assertEqual(len(self.editor.project["tracks"][0]["clips"]), 2)

    def test_generated_spatial_clip_snapshots_physical_layout(self):
        result = model_result()
        result["cues"][0]["pattern"] = "sweep"
        self.editor.ai.provider = lambda *args: result
        self.start()
        job = self.wait()
        cue = job["project"]["tracks"][0]["clips"][0]
        self.assertEqual(len(cue["spatial"]["fan_1"]), 12)
        self.assertEqual(cue["spatial"]["fan_1"], self.editor.ai.positions["fan_1"])

    def test_live_draft_audition_switches_while_paused_without_saving(self):
        self.start()
        job = self.wait()
        original = copy.deepcopy(self.editor.project)
        request = {
            "track": "track",
            "position": 3,
            "playing": False,
            "active": True,
            "client": "audition",
            "sequence": 1,
            "proposal": job["id"],
        }
        result = self.editor.transport(request)
        self.assertEqual(result["proposal"], job["id"])
        draft = self.editor.live_frame()["fan_1"][0]
        np.testing.assert_allclose(draft, [0, 0, 1])
        self.assertEqual(self.editor.project, original)
        self.assertEqual(self.editor.revision, 0)
        self.editor.transport(dict(request, proposal=None, sequence=2))
        np.testing.assert_allclose(
            self.editor.live_frame()["fan_1"][0], [0.4, 0.4, 0.4]
        )
        self.editor.transport(request)  # Delayed B packet must not switch back.
        self.assertIsNone(self.editor.proposal_id)
        self.editor.transport(dict(request, sequence=3))
        np.testing.assert_allclose(self.editor.live_frame()["fan_1"][0], draft)
        self.editor.transport(dict(request, sequence=4, active=False))
        self.assertIsNone(self.editor.live_frame())

    def test_discard_layout_change_and_apply_resolve_live_draft_correctly(self):
        def audition():
            self.start()
            job = self.wait()
            self.editor.transport(
                {"track": "track", "position": 3, "active": True, "proposal": job["id"]}
            )
            return job

        job = audition()
        self.editor.ai.cancel(job["id"])
        np.testing.assert_allclose(
            self.editor.live_frame()["fan_1"][0], [0.4, 0.4, 0.4]
        )
        self.assertIsNone(self.editor.proposal_id)
        job = audition()
        config = self.editor.ai.config()
        config["layout"]["fan_1"]["x"] = 0.75
        self.editor.ai.save_settings(config)
        np.testing.assert_allclose(
            self.editor.live_frame()["fan_1"][0], [0.4, 0.4, 0.4]
        )
        job = audition()
        self.editor.ai.apply(job["id"])
        np.testing.assert_allclose(self.editor.live_frame()["fan_1"][0], [0, 0, 1])
        self.assertIsNone(self.editor.proposal_id)

    def test_live_draft_retains_disconnect_blackout_and_rejects_stale_edits(self):
        self.start()
        job = self.wait()
        request = {
            "track": "track",
            "position": 3,
            "playing": True,
            "active": True,
            "proposal": job["id"],
        }
        self.editor.transport(request)
        self.editor.last_transport -= 3
        self.assertFalse(
            any(frame.any() for frame in self.editor.live_frame().values())
        )
        self.editor.transport(dict(request, playing=False))
        np.testing.assert_allclose(self.editor.live_frame()["fan_1"][0], [0, 0, 1])
        self.editor.replace(self.editor.project, self.editor.revision)
        np.testing.assert_allclose(
            self.editor.live_frame()["fan_1"][0], [0.4, 0.4, 0.4]
        )
        self.assertIsNone(self.editor.transport(request)["proposal"])

    def test_conflicts_and_layout_changes_do_not_apply(self):
        self.start()
        job = self.wait()
        config = self.editor.ai.config()
        config["layout"]["fan_1"]["x"] = 0.8
        self.editor.ai.save_settings(config)
        with self.assertRaisesRegex(RuntimeError, "layout"):
            self.editor.ai.apply(job["id"])
        self.start()
        job = self.wait()
        self.editor.replace(self.editor.project, self.editor.revision)
        with self.assertRaises(RuntimeError):
            self.editor.ai.apply(job["id"])
        with self.assertRaises(RuntimeError):
            self.editor.ai.proposal_track(job["id"])

    def test_cancel_suppresses_late_provider_result(self):
        entered, release = threading.Event(), threading.Event()

        def provider(*args):
            entered.set()
            release.wait(5)
            return model_result()

        self.editor.ai.provider = provider
        try:
            job = self.start()
            self.assertTrue(entered.wait(5))
            self.editor.ai.cancel(job["id"])
            with self.assertRaises(RuntimeError):
                self.start()
        finally:
            release.set()
        self.assertTrue(self.editor.ai.job["_done"].wait(5))
        self.assertEqual(self.editor.ai.status()["status"], "cancelled")
        self.assertNotIn("project", self.editor.ai.status())

    def test_one_repair_attempt_and_no_invalid_proposal_applied(self):
        attempts = []

        def provider(audio, diagram, context, model, key):
            attempts.append(context)
            return model_result(0, 6) if len(attempts) == 1 else model_result()

        self.editor.ai.provider = provider
        self.start()
        self.wait()
        self.assertEqual(len(attempts), 2)
        self.assertIn("validation_error", attempts[1])
        self.assertEqual(self.editor.revision, 0)
        self.editor.ai.provider = lambda *args: model_result(0, 6)
        self.start()
        self.assertTrue(self.editor.ai.job["_done"].wait(10))
        self.assertEqual(self.editor.ai.status()["status"], "error")
        self.assertNotIn("project", self.editor.ai.status())
        self.assertEqual(self.editor.revision, 0)

    def test_identify_validates_device_and_led(self):
        calls = []
        self.editor.identify = lambda name, led: calls.append((name, led))
        self.editor.ai.identify({"fixture": "fan_1", "led": 0})
        self.assertEqual(calls, [("fan_1", 0)])
        with self.assertRaises(ValueError):
            self.editor.ai.identify({"fixture": "fan_1", "led": 999})
        self.assertEqual(len(calls), 1)

    def test_preferences_persist_and_missing_key_is_actionable(self):
        config = self.editor.ai.config()
        config["preferences"] = "Only blue"
        self.editor.ai.save_settings(config)
        self.assertEqual(
            json.loads((Path(self.temp.name) / "ai-settings.json").read_text())[
                "preferences"
            ],
            "Only blue",
        )
        with self.assertRaises(RuntimeError):
            self.editor.ai.save_settings(config)
        with patch.dict(os.environ, {"GEMINI_API_KEY": ""}):
            self.assertFalse(self.editor.ai.config()["configured"])
            with self.assertRaisesRegex(ValueError, "GEMINI_API_KEY"):
                self.start()


class ProviderTests(unittest.TestCase):
    def test_wire_request_contains_audio_diagram_schema_and_server_key(self):
        response = {
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {"parts": [{"text": json.dumps(model_result())}]},
                }
            ]
        }
        with patch(
            "urllib.request.urlopen",
            return_value=io.BytesIO(json.dumps(response).encode()),
        ) as send:
            result = gemini_generate(
                b"audio", b"png", {"audio_offset": 8}, "test-model", "secret"
            )
        request = send.call_args.args[0]
        self.assertNotIn("secret", request.full_url)
        self.assertEqual(request.get_header("X-goog-api-key"), "secret")
        payload = json.loads(request.data)
        self.assertEqual(
            payload["contents"][0]["parts"][1]["inlineData"]["mimeType"], "audio/mpeg"
        )
        self.assertIn("responseSchema", payload["generationConfig"])
        self.assertEqual(result, model_result())

    def test_provider_permissions_details_are_actionable_and_redacted(self):
        payload = {
            "error": {
                "message": "Requests from this referrer are blocked. Key secret. See https://example.test?key=secret",
                "status": "PERMISSION_DENIED",
                "details": [
                    {
                        "reason": "API_KEY_HTTP_REFERRER_BLOCKED",
                        "metadata": {"private": "do not display"},
                    }
                ],
            }
        }
        error = urllib.error.HTTPError(
            "https://example.test",
            403,
            "Forbidden",
            {},
            io.BytesIO(json.dumps(payload).encode()),
        )
        with (
            patch("urllib.request.urlopen", side_effect=error),
            self.assertRaises(ValueError) as raised,
        ):
            gemini_generate(b"a", b"b", {}, "test-model", "secret")
        message = str(raised.exception)
        self.assertIn("HTTP 403", message)
        self.assertIn("test-model", message)
        self.assertIn("PERMISSION_DENIED", message)
        self.assertIn("referrer are blocked", message)
        self.assertIn("Rigby server", message)
        self.assertNotIn("secret", message)
        self.assertNotIn("https://", message)
        self.assertNotIn("do not display", message)

    def test_malformed_provider_error_uses_fallback(self):
        for body in (
            b"[]",
            b'{"error":null}',
            b'{"error":{"details":null}}',
            b"x" * 65537,
        ):
            error = urllib.error.HTTPError(
                "https://example.test", 403, "Forbidden", {}, io.BytesIO(body)
            )
            with (
                self.subTest(body=body[:50]),
                patch("urllib.request.urlopen", side_effect=error),
                self.assertRaisesRegex(ValueError, "API access denied"),
            ):
                gemini_generate(b"a", b"b", {}, "test", "secret")

    def test_errors_and_truncation_are_not_accepted(self):
        err = urllib.error.HTTPError(
            "https://example.test?key=secret", 429, "secret", {}, io.BytesIO(b"secret")
        )
        with (
            patch("urllib.request.urlopen", side_effect=err),
            self.assertRaisesRegex(ValueError, "quota") as raised,
        ):
            gemini_generate(b"a", b"b", {}, "test", "secret")
        self.assertNotIn("secret", str(raised.exception))
        with (
            patch(
                "urllib.request.urlopen",
                return_value=io.BytesIO(
                    b'{"candidates":[{"finishReason":"MAX_TOKENS"}]}'
                ),
            ),
            self.assertRaisesRegex(ValueError, "complete"),
        ):
            gemini_generate(b"a", b"b", {}, "test", "secret")


if __name__ == "__main__":
    unittest.main()
