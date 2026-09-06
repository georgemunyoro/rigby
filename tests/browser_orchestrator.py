"""End-to-end editor regression. Run with `uv run --with playwright python tests/browser_orchestrator.py`.

Uses an isolated store, virtual fixtures, and muted browser audio. Requires the
Playwright Chromium browser (`python -m playwright install chromium`).
"""

import signal
import subprocess
import sys
import tempfile
from pathlib import Path
import wave
import json

import numpy as np
from playwright.sync_api import sync_playwright


def check_layout(page):
    for width, height in [(3840, 2160), (1920, 1080), (1366, 768), (800, 700)]:
        page.set_viewport_size({"width": width, "height": height})
        page.wait_for_timeout(100)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth && document.documentElement.scrollHeight <= innerHeight")
        assert page.evaluate("document.querySelector('#rig').getBoundingClientRect().bottom <= innerHeight")
    page.set_viewport_size({"width": 1600, "height": 900})
    for handle, pane, delta in [("resizeLibrary", "library", 60), ("resizeInspector", "inspector", -70)]:
        before = page.locator('#' + pane).bounding_box()['width']
        box = page.locator('#' + handle).bounding_box()
        page.mouse.move(box['x'] + box['width']/2, box['y'] + 80)
        page.mouse.down()
        page.mouse.move(box['x'] + box['width']/2 + delta, box['y'] + 80)
        page.mouse.up()
        assert page.locator('#' + pane).bounding_box()['width'] > before + 40
    timeline = page.locator('#timeline').bounding_box()
    page.locator('#fitTimeline').click()
    page.mouse.move(timeline['x'] + timeline['width']/2, timeline['y'] + 40)
    initial = page.evaluate('bounds()')
    page.mouse.wheel(0, -300)
    page.wait_for_timeout(150)
    zoomed = page.evaluate('bounds()')
    assert zoomed['span'] < initial['span'] * .5
    assert abs(zoomed['start'] + zoomed['span']/2 - initial['duration']/2) < .02
    page.keyboard.down('Shift')
    page.mouse.wheel(0, 100)
    page.keyboard.up('Shift')
    page.wait_for_timeout(150)
    assert page.evaluate('bounds().start') > zoomed['start']
    start = page.evaluate('bounds().start')
    page.mouse.down(button='middle')
    page.mouse.move(timeline['x'] + timeline['width']/2 + 60, timeline['y'] + 40)
    page.mouse.up(button='middle')
    assert page.evaluate('bounds().start') < start
    page.locator('#fitTimeline').click()
    assert page.evaluate('timelineZoom === 1 && timelineStart === 0')
    # Coincident physical origins and dense rings must not overlap in the editor.
    page.evaluate("""window.savedGeometry=geom;geom=Object.fromEntries([6,18,24,60,120,9,18,24,60].map((n,i)=>['fixture_'+i,{kind:'ring',n,origin:[.5,.5],angle:Array(n).fill(0)}]));drawRig();""")
    assert page.evaluate("""() => {
        const fixtures=[...document.querySelectorAll('.fixture')];
        const overlap=(a,b)=>a.left<b.right-.5&&a.right>b.left+.5&&a.top<b.bottom-.5&&a.bottom>b.top+.5;
        const rectangles=fixtures.map(f=>f.getBoundingClientRect());
        if(rectangles.some((a,i)=>rectangles.slice(i+1).some(b=>overlap(a,b))))return false;
        return fixtures.every(f=>{
            const box=f.getBoundingClientRect(),leds=[...f.querySelectorAll('.led')].map(l=>l.getBoundingClientRect());
            return leds.every((a,i)=>a.left>=box.left&&a.right<=box.right&&a.top>=box.top&&a.bottom<=box.bottom&&!leds.slice(i+1).some(b=>overlap(a,b)));
        });
    }""")
    page.locator('.fixture').last.locator('.led').last.click()
    page.evaluate('geom=window.savedGeometry;selection={};drawRig()')
    page.screenshot(path='/tmp/rigby-fullbleed-desktop.png')
    print('PASS: viewport fit, resizing, cursor-anchored zoom, wheel/drag pan, dense fixture layout')


def main():
    with tempfile.TemporaryDirectory() as directory:
        audio_path = Path(directory) / "test.wav"
        t = np.arange(48000 * 6) / 48000
        signal_audio = (
            12000 * np.sin(2 * np.pi * 90 * t) * np.exp(-(t % 0.5) / 0.08)
        ).astype("<i2")
        with wave.open(str(audio_path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(48000)
            wav.writeframes(signal_audio.tobytes())
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "rigby.editor_server",
                "--port",
                "0",
                "--directory",
                directory,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            line = process.stdout.readline()
            if not line.startswith("Orchestrator: "):
                raise RuntimeError("Editor failed to start: " + line)
            url = line.removeprefix("Orchestrator: ").strip()
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(viewport={"width": 1600, "height": 1100})
                # Reproduce LAN HTTP / older browser capability restrictions.
                page.add_init_script(
                    "Object.defineProperty(globalThis.crypto, 'randomUUID', {value: undefined});"
                )
                errors = []
                http_errors = []
                page.on(
                    "response",
                    lambda response: (
                        http_errors.append(f"{response.status} {response.url}")
                        if response.status >= 400
                        else None
                    ),
                )
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(url)
                assert page.evaluate("typeof crypto.randomUUID") == "undefined"
                assert (
                    page.evaluate("new Set(Array.from({length:1000},()=>uid())).size")
                    == 1000
                )
                with page.expect_file_chooser() as chooser:
                    page.locator("#import").click()
                chooser.value.set_files(str(audio_path))
                page.wait_for_selector("#loading:not([hidden])")
                assert page.locator("#import").is_disabled()
                assert page.locator("#loadingStage").inner_text()
                page.wait_for_function(
                    "document.querySelector('.track.active') && !document.querySelector('#play').disabled",
                    timeout=30000,
                )
                page.wait_for_selector("#loading", state="hidden")
                assert page.locator("#import").is_enabled()
                page.evaluate("document.querySelector('#audio').muted=true")
                page.locator("#play").click()
                page.wait_for_timeout(750)
                assert (
                    page.evaluate("document.querySelector('#audio').currentTime") > 0.3
                )
                page.locator("#play").click()
                stopped = page.evaluate("document.querySelector('#audio').currentTime")
                page.wait_for_timeout(200)
                assert (
                    abs(
                        page.evaluate("document.querySelector('#audio').currentTime")
                        - stopped
                    )
                    < 0.02
                )
                page.locator("#snap").uncheck()
                page.locator("#addLED").click()
                page.wait_for_selector("#form:not([hidden])")
                page.locator("#clipName").fill("Authored blue fan")
                page.locator("#colour").fill("#0000ff")
                assert "Unapplied changes" in page.locator("#saveStatus").inner_text()
                # A live keyframe edit must not wipe an unapplied inspector draft.
                page.locator("#addKey").click()
                page.wait_for_timeout(200)
                assert page.locator("#clipName").input_value() == "Authored blue fan"
                page.locator("#discardClip").click()
                assert page.locator("#clipName").input_value() == "LED animation"
                assert page.locator("#applyClip").is_disabled()
                page.locator("#clipName").fill("Authored blue fan")
                page.locator("#colour").fill("#0000ff")
                page.locator("#form button[type=submit]").click()
                page.wait_for_function(
                    "document.querySelector('#notice').textContent==='Saved locally'"
                )
                page.wait_for_function("document.querySelector('#applyClip').disabled")
                page.locator("#turnAnimation").click()
                page.wait_for_function(
                    "document.querySelector('#property').value==='position' && document.querySelectorAll('#keys tr:has(input)').length===2"
                )
                page.locator("#reverseAnimation").click()
                page.wait_for_timeout(200)
                assert (
                    page.locator("#keys input[data-field=value]").last.input_value()
                    == "-1"
                )
                page.locator(".led").first.click()
                page.locator("#targetSelection").click()
                page.wait_for_function(
                    "document.querySelector('#targetInfo').textContent==='fan_1: 0'"
                )
                page.locator("#keyColour").click()
                page.wait_for_timeout(300)
                page.locator("#property").select_option("blue")
                assert page.locator("#keys tr:has(input)").count() == 1
                page.locator("#savePreset").click()
                page.wait_for_function(
                    "document.querySelector('#presets').options.length===1"
                )
                page.locator("#locked").check()
                page.locator("#form button[type=submit]").click()
                page.wait_for_timeout(250)
                page.locator("#generate").click()
                page.wait_for_function(
                    "document.querySelector('#notice').textContent.includes('preserved')"
                )
                s = page.request.get(
                    url.replace("/orchestrator", "/api/orchestrator/state")
                ).json()
                authored = [
                    c
                    for c in s["project"]["tracks"][0]["clips"]
                    if c.get("origin") == "manual"
                ]
                assert len(authored) == 1 and authored[0]["locked"]
                page.locator("#loopClip").click()
                page.locator("#loopIn").fill("1")
                page.locator("#loopOut").fill("1.5")
                page.locator("#loopOut").press("Tab")
                page.evaluate("document.querySelector('#audio').currentTime=1")
                page.locator("#play").click()
                page.wait_for_timeout(1300)
                now = page.evaluate("document.querySelector('#audio').currentTime")
                assert 1 <= now < 1.6, now
                page.locator("#play").click()
                page.locator("#json").click()
                document = json.loads(page.locator("#jsonText").input_value())
                document["name"] = "Reviewed set"
                page.locator("#jsonText").fill(json.dumps(document))
                page.locator("#validateProposal").click()
                page.wait_for_function(
                    "!document.querySelector('#applyProposal').disabled"
                )
                page.locator("#applyProposal").click()
                page.wait_for_function(
                    "document.querySelector('#setName').value==='Reviewed set'"
                )
                page.locator("#undo").click()
                page.wait_for_function(
                    "document.querySelector('#setName').value==='Untitled set'"
                )
                page.locator("#redo").click()
                page.wait_for_function(
                    "document.querySelector('#setName').value==='Reviewed set'"
                )
                page.reload()
                page.wait_for_function(
                    "document.querySelector('#setName').value==='Reviewed set'"
                )
                check_layout(page)
                assert not errors, errors

                print(
                    "PASS: upload, whole-track preparation, play/pause, targeted LED clip, RGB keys, preset, protected regeneration, looping, JSON review, undo/redo, persisted reload; no browser errors"
                )
                fallback = browser.new_page()
                fallback.add_init_script(
                    "Object.defineProperty(globalThis, 'crypto', {value: undefined});"
                )
                fallback.on("pageerror", lambda error: errors.append(str(error)))
                fallback.goto(url)
                fallback.wait_for_selector(".track.active")
                assert (
                    fallback.evaluate(
                        "new Set(Array.from({length:1000},()=>uid())).size"
                    )
                    == 1000
                )
                with fallback.expect_file_chooser() as chooser:
                    fallback.locator("#import").click()
                assert not errors, errors
                assert not http_errors, http_errors
                assert (
                    page.request.get(
                        url.replace("/orchestrator", "/favicon.ico")
                    ).status
                    == 204
                )
                browser.close()
        finally:
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            process.stdout.close()


if __name__ == "__main__":
    main()
