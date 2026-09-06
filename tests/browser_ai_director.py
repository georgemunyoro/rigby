"""Real HTTP/browser AI workflow with an isolated store and a simulated provider.

Run: uv run --with playwright python tests/browser_ai_director.py
No audio or credentials leave the process.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import os
from unittest.mock import patch
import time

from playwright.sync_api import sync_playwright

from rigby.editor_server import route
from test_ai_director import DirectorTests, model_result


def main():
    fixture = DirectorTests()
    fixture.setUp()
    editor = fixture.editor
    calls = []

    def provider(audio, diagram, context, model, key):
        calls.append(context)
        time.sleep(0.35)
        return model_result(
            context["edit_range"][0],
            min(context["edit_range"][1], context["edit_range"][0] + 4),
        )

    editor.ai.provider = provider

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
            if not route(self, editor, "GET"):
                self._send(404, b"", "text/plain")

        def do_POST(self):
            if not route(self, editor, "POST"):
                self._send(404, b"", "text/plain")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1600, "height": 1000})
            errors = []
            http_errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on(
                "response",
                lambda response: (
                    http_errors.append(f"{response.status}: {response.url}")
                    if response.status >= 400
                    else None
                ),
            )
            base_url = f"http://127.0.0.1:{server.server_port}"
            tracks = editor.project["tracks"]
            editor.project["tracks"] = []
            preferences = editor.ai.preferences
            with patch.dict(os.environ, {"GEMINI_API_KEY": ""}):
                page.goto(base_url + "/rig-layout")
                page.wait_for_function(
                    "document.querySelector('#layoutDevice').options.length > 0"
                )
                assert page.locator("audio").count() == 0
                assert page.locator("#aiDialog").count() == 0
                page.locator("#saveLayout").click()
                page.wait_for_function(
                    "document.querySelector('#layoutNotice').textContent==='Physical layout saved'"
                )
                assert editor.ai.preferences == preferences
                assert editor.revision == 0
                response = page.request.get(base_url + "/api/rig-layout").json()
                assert set(response) == {"layout", "revision", "can_identify", "error"}
            editor.project["tracks"] = tracks
            page.goto(base_url + "/orchestrator")
            page.wait_for_selector(".track.active")
            with page.expect_popup() as popup:
                page.locator("#openLayout").click()
            layout_page = popup.value
            layout_page.on("pageerror", lambda error: errors.append(str(error)))
            layout_page.on(
                "response",
                lambda response: (
                    http_errors.append(str(response.status))
                    if response.status >= 400
                    else None
                ),
            )
            layout_page.wait_for_function(
                "document.querySelector('#layoutDevice').options.length > 0"
            )
            layout_page.locator("#layoutLabel").fill("Bottom fan")
            layout_page.locator("#layoutGroup").fill("Case fans")
            layout_page.locator("#layoutX").fill(".25")
            layout_page.locator("#layoutY").fill(".75")
            layout_page.locator("#layoutRotation").fill("90")
            layout_page.locator("#layoutReverse").check()
            box = layout_page.locator("#layoutCanvas").bounding_box()
            layout_page.mouse.move(
                box["x"] + box["width"] * 0.25, box["y"] + box["height"] * 0.75
            )
            layout_page.mouse.down()
            layout_page.mouse.move(
                box["x"] + box["width"] * 0.3, box["y"] + box["height"] * 0.65
            )
            layout_page.mouse.up()
            assert (
                abs(float(layout_page.locator("#layoutX").input_value()) - 0.3) < 0.02
            )
            layout_page.locator("#saveLayout").click()
            layout_page.wait_for_function(
                "document.querySelector('#layoutNotice').textContent==='Physical layout saved'"
            )
            layout_page.screenshot(path="/tmp/rigby-ai-layout.png")
            layout_page.reload()
            layout_page.wait_for_function(
                "document.querySelector('#layoutDevice').options.length > 0"
            )
            assert layout_page.locator("#layoutLabel").input_value() == "Bottom fan"
            assert layout_page.locator("#layoutReverse").is_checked()
            for width, height in [(3840, 2160), (1366, 768), (600, 700)]:
                layout_page.set_viewport_size({"width": width, "height": height})
                assert layout_page.evaluate(
                    "document.documentElement.scrollWidth <= innerWidth && document.documentElement.scrollHeight <= innerHeight"
                )
            layout_page.close()
            # Select arbitrary timestamps directly on the waveform.
            page.locator("#fitTimeline").click()
            timeline = page.locator("#timeline").bounding_box()
            page.mouse.move(
                timeline["x"] + timeline["width"] * 2.2 / 12, timeline["y"] + 40
            )
            page.mouse.down()
            page.mouse.move(
                timeline["x"] + timeline["width"] * 7.3 / 12,
                timeline["y"] + 40,
                steps=5,
            )
            page.mouse.up()
            selected = page.evaluate("timeSelection")
            assert abs(selected[0] - 2.2) < 0.03 and abs(selected[1] - 7.3) < 0.03
            page.locator("#changeHere").click()
            page.wait_for_selector("#aiPanel:not([hidden])")
            assert page.locator("dialog[open]").count() == 0
            page.locator("#feedbackQuiet").click()
            assert "less busy" in page.locator("#aiInstruction").input_value()
            assert len(calls) == 0
            # Restore the deliberately arbitrary selection for the actual request.
            page.evaluate("setTimeSelection([2.2,7.3])")
            page.locator("#suggestSelection").click()
            page.wait_for_selector("#aiPanel:not([hidden])")
            assert page.locator("dialog[open]").count() == 0
            assert page.locator("#timeline").is_visible()
            assert page.locator("#aiScope").input_value() == "selection"
            page.evaluate("document.querySelector('#audio').muted=true")
            page.locator("#loopSelection").click()
            page.locator("#output").check()
            page.locator("#aiInstruction").fill(
                "Blue, quiet, and slowly rising through the fans"
            )
            page.locator("#generateAI").click()
            page.wait_for_selector("#aiStage.busy")
            page.wait_for_function("!document.querySelector('#applyAI').disabled")
            assert editor.revision == 0 and not editor.project["tracks"][0]["clips"]
            assert calls[0]["edit_range"] == selected
            assert calls[0]["physical_layout"]["fan_1"]["label"] == "Bottom fan"
            assert calls[0]["physical_layout"]["fan_1"]["reverse"]
            assert len(page.locator("#aiClips button").all()) == 1
            page.locator("#aiClips button").first.click()
            page.evaluate("document.querySelector('#audio').play()")
            page.wait_for_timeout(300)
            assert page.evaluate("document.querySelector('#audio').currentTime") > 2
            page.wait_for_function(
                "aiColours.fan_1 && aiColours.fan_1[0][2] > 200 && aiColours.fan_1[0][0] === 0"
            )
            page.evaluate("document.querySelector('#audio').pause()")
            page.wait_for_function("state.transport.proposal === aiJob.id")
            assert page.locator("#output").is_enabled()
            assert page.locator("#output").is_checked()
            assert editor.active is True
            assert editor.live_frame()["fan_1"][0][2] > 0.9
            assert editor.live_frame()["fan_1"][0][0] == 0
            assert editor.revision == 0
            assert page.locator("#outputStatus").inner_text() == "● Sending to LEDs"
            assert page.locator("#addLED").is_disabled()
            assert page.evaluate("visibleTrack().clips.length") == 1
            page.locator("#aiCompare").click()
            page.wait_for_function("aiColours.fan_1[0][0] > 0")
            assert page.evaluate("visibleTrack().clips.length") == 0
            page.wait_for_function("state.transport.proposal === null")
            assert editor.live_frame()["fan_1"][0][0] > 0.3
            page.locator("#aiCompare").click()
            page.wait_for_function("aiColours.fan_1[0][0] === 0")
            page.wait_for_function("state.transport.proposal === aiJob.id")
            assert editor.live_frame()["fan_1"][0][0] == 0
            assert page.evaluate("visibleTrack().clips.length") == 1
            page.evaluate("document.querySelector('#audio').pause()")
            page.screenshot(path="/tmp/rigby-ai-proposal.png")
            for width, height in [(3840, 2160), (1366, 768)]:
                page.set_viewport_size({"width": width, "height": height})
                assert page.evaluate(
                    "document.documentElement.scrollHeight <= innerHeight"
                )
                assert (
                    page.locator("#aiCanvas").bounding_box()["y"]
                    + page.locator("#aiCanvas").bounding_box()["height"]
                    <= height
                )
            page.set_viewport_size({"width": 1600, "height": 1000})
            page.locator("#aiStart").fill("3")
            page.locator("#aiStart").press("Tab")
            page.locator("#aiEnd").fill("5")
            page.locator("#aiEnd").press("Tab")
            page.locator("#aiInstruction").fill(
                "Keep the colour, slow down the movement"
            )
            page.locator("#reviseAI").click()
            page.wait_for_selector("#aiStage.busy")
            page.wait_for_function("!document.querySelector('#applyAI').disabled")
            assert len(calls) == 2 and calls[1]["previous_proposal"]
            assert calls[1]["edit_range"] == [3, 5]
            assert page.evaluate("visibleTrack().clips.length") == 2
            assert calls[1]["conversation"][0]["instruction"].startswith("Blue")
            # Reloading the browser recovers the reviewable server-side draft.
            page.locator("#clipTab").click()
            page.reload()
            page.wait_for_selector(".track.active")
            page.locator("#openAI").click()
            page.wait_for_function("!document.querySelector('#applyAI').disabled")
            assert "slow down" in page.locator("#aiConversation").text_content()
            page.locator("#applyAI").click()
            page.wait_for_function(
                "document.querySelector('#aiStage').textContent.startsWith('Applied')"
            )
            assert editor.revision == 1
            assert editor.project["tracks"][0]["clips"][0]["origin"] == "ai"
            page.locator("#clipTab").click()
            page.locator("#undo").click()
            page.wait_for_function("state.revision===2")
            assert not editor.project["tracks"][0]["clips"]
            page.locator("#openAI").click()
            page.locator("#generateAI").click()
            page.wait_for_function("!document.querySelector('#applyAI').disabled")
            page.locator("#cancelAI").click()
            page.wait_for_function(
                "document.querySelector('#aiStage').textContent==='Draft discarded'"
            )
            assert not editor.project["tracks"][0]["clips"]
            page.locator("#clipTab").click()
            page.reload()
            page.wait_for_selector(".track.active")
            with page.expect_popup() as popup:
                page.locator("#openLayout").click()
            layout_page = popup.value
            layout_page.wait_for_function(
                "document.querySelector('#layoutDevice').options.length > 0"
            )
            assert layout_page.locator("#layoutLabel").input_value() == "Bottom fan"
            layout_page.close()
            assert not errors, errors
            assert not http_errors, http_errors
            browser.close()
            print(
                "PASS: physical layout drag/save/reload, audio-aware proposal, isolated audition, conversational revision, apply/undo/discard; no browser errors"
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        fixture.tearDown()


if __name__ == "__main__":
    main()
