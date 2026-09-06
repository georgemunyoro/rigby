# Orchestrator

The orchestrator edits local-track lighting arrangements and plays them back as
an ordered set. It runs entirely on your computer, without an account, model,
cloud service, or OpenRGB connection for preview-only editing. The optional AI
director sends audio and rig context to Gemini only when you request generation.

## Open the editor

For standalone editing with a virtual three-fan and RAM patch:

```sh
uv run rigby-editor
```

Open <http://127.0.0.1:8722/orchestrator>. For your actual fixtures, start the live
desk instead:

```sh
uv run rigby --no-audio --control
```

Open **Orchestrator** in its header (normally
<http://127.0.0.1:8721/orchestrator>). The editor uses the current OpenRGB fixture
mapping. Enable **Send to LEDs** when you want the editor to own live output.
The live desk's master, gamma, minimum-output setting, blackout, and identify
controls still apply; playground mode takes precedence over arrangements.
Use `--no-audio` for arrangement playback: audio is played by the editor's browser,
and the prepared analysis supplies musical features. This also avoids an unrelated
file source ending the render loop while you edit.

FFmpeg and ffprobe must be on PATH. Imports accept audio files up to 200 MB and
30 minutes. Audio plays on the computer running the browser; use a browser on the
lighting computer for local speakers. A phone browser would play sound on the phone.

## Create a track arrangement

1. Import a local audio file. The first import decodes a seekable WAV, analyses
   the whole track, and prepares deterministic automatic lighting frames. Wait
   for the track to say **ready**. Additional look choices are prepared on demand.
2. Play or click the waveform to seek. The orange playhead follows browser audio.
   Beat and section markers are estimates; they can be wrong on ambiguous music.
3. Add a guided clip or LED animation. Clips are layered in document order, with
   later clips taking precedence only over their selected LEDs and properties.
4. Drag a clip to move it, or drag its edges to resize it. Beat snapping is
   optional. Resizing stretches its keyframe times. Inspector times are seconds.
5. Main inspector settings show **Unapplied changes** until you use the sticky
   **Apply clip** button. **Discard** restores saved settings. Unapplied drafts
   stay with their clip during selection changes and live keyframe edits; they
   are not saved across page reloads. Keyframe edits save immediately.
6. Use **Loop selected clip** to audition a passage. Undo/redo includes clip edits,
   regeneration, set imports, presets, and track ordering within the current tab.
7. Applied changes save locally after each successful edit. **Export set** downloads a
   portable JSON document; **Open set** validates and previews it before applying.

A set is an ordered list of tracks. Use the arrows in the sidebar to reorder it,
and enable **Continue through set** for sequential playback. This version uses
track cuts, not audio crossfades. Playback requires the browser to remain open.
A paused playhead holds its lighting frame; stopping returns to the first frame.
The end of a track blacks out arrangement output. Disabling **Send to LEDs**
returns ownership to the live look. Lost transport updates while playing cause
blackout rather than continuing a lighting show after its audio disappeared.

## Navigating the workspace

The editor fills the window. Drag the dividers beside the library and inspector
to resize them; widths are remembered in this browser. Focus a divider and use
Left/Right for keyboard resizing. Narrow windows show Library and Inspector
buttons to open those panes.

Over the timeline, use the mouse wheel to zoom around the pointer. Shift-wheel
or horizontal trackpad scrolling pans; middle-button or Alt-drag also pans.
**Fit track** resets the view. Click the waveform to seek, and drag clips or their
edges to move or resize them.

The LED canvas gives each fixture its own tile so devices cannot overlap.
Rings have spaced targets; rings with more than 24 LEDs use a labelled grid.
Large patches scroll within the canvas. This display arrangement does not change
physical fixture mapping, LED indices, or animation targeting.

## AI lighting director

The **AI director** is a docked tab beside the clip inspector. Open it from the
right sidebar, the library, or **Suggest selection** below the timeline. Configure a
Gemini API key in the environment of the Rigby server, then restart it:

```sh
export GEMINI_API_KEY="your-api-key"
uv run rigby --no-audio --control
# Or, for virtual fixtures: uv run rigby-editor
```

The default model is `gemini-3.8-flash`. Set `RIGBY_AI_MODEL` before starting Rigby
to choose another Gemini model that supports audio, image input, and structured
JSON output. The key stays on the server: it is never returned to the browser,
written to the project, or placed in a provider URL. Missing credentials, rejected
keys, quota limits, and unavailable models are reported in the director.

The provider uses Gemini's documented [audio input](https://ai.google.dev/gemini-api/docs/generate-content/audio)
and [structured generation API](https://ai.google.dev/api/generate-content).
A hosted request incurs your provider's normal API usage.

### Set up the physical rig once

Open **Rig layout** from the live desk, or **Physical rig layout** from the
orchestrator (opens a separate tab). It lives at `/rig-layout`, typically
<http://127.0.0.1:8721/rig-layout> on the live desk or
<http://127.0.0.1:8722/rig-layout> on the standalone server. No imported track or
Gemini API key is needed to arrange devices.

Select a device from the list, use **Identify device** to locate it on the actual
hardware, then drag its box into place on the canvas. Arrange devices from your
listening position, give
them useful names/groups, and save. Drag devices or enter X/Y, width/height,
clockwise rotation, and reversed LED direction. Coordinates are normalised:
X increases rightward and Y downward. Matrix fixtures preserve their matrix map;
rings preserve their mapped LED angles. LED 0 is highlighted in the layout.

In the live desk, **Identify device** and **Identify LED 0** briefly highlight the
actual hardware so you can check names and orientation. These buttons are disabled
in the standalone virtual editor. The physical layout guides the AI's targeting;
it does not rewrite the hardware patch or change existing effect geometry.
The normal nonoverlapping LED tiles remain the manual selection interface in
the clip inspector. The director uses the physical map for audition.

Layout and musical preferences are saved in `ai-settings.json` in the editor's
storage directory, separately from portable set exports. New fixtures receive
default placements and should be checked when the patch changes.

### Generate, audition, and refine

1. Import a track and wait for audio preparation to finish. Apply or discard any
   pending inspector edits.
2. **Drag across the waveform** to select any passage. Ctrl/Cmd-drag selects a
   range from any timeline lane. Select in either direction, at any zoom level.
   A single click still seeks; Middle/Alt/Shift-drag still pans. Choose **Suggest
   selection** to open the director on that range. You can also enter precise
   From/To times, use a selected clip or loop, or choose the whole track.
3. **Change here** selects a short passage around the playhead. **Less busy**,
   **Bigger impact**, and **Move upward** fill in a direction for the selected
   passage. They do not submit a provider request until you choose Suggest/Revise.
4. Choose **Suggest lighting**. Generation runs in the background while the
   timeline and playback controls remain available. The model receives actual
   audio with up to eight seconds of context, measured musical features,
   beat/section estimates, existing clips, per-LED positions, and a rig diagram.
5. Review the summary and clips in the director. The **main timeline** shows the
   proposed clips; the LED canvas shows their physical lighting preview. Use the
   **main Play button**, waveform seeking, and **Loop selection** to audition.
   The A/B control above the timeline switches between saved and proposed lighting
   at the same playback position. Enable **Send to LEDs** to audition either
   version on the real rig, including an unapplied draft. A/B changes take effect
   while paused too. Apply remains the separate step that saves the draft.
   Discarding, replacing, or invalidating a draft returns output to the saved
   arrangement; the existing disconnect blackout still applies.
6. Select a smaller range and request a change such as “keep these colours but
   slow the movement.” **Revise draft** includes recent conversation and the
   previous proposal. Suggestions made while a valid draft is displayed also
   refine that draft. Changes outside the new range stay in the proposal. You do
   not need to apply every draft before refining one passage.
7. **Apply draft** saves regular editable clips. Switch to **Clip inspector** to
   edit them; Undo restores the previous arrangement. **Discard draft** leaves
   saved lighting unchanged. Inspector and director tabs share the resizable pane.

Locked clips are preserved exactly, and generated cues cannot overlap their time
intervals. Only unlocked clips fully contained in the selected range are replaced;
clips crossing its boundaries remain underneath. A selected locked clip therefore
needs to be unlocked before AI replacement. This deliberately protects entire
locked passages, including their existing underlying lighting.

The server rejects unknown devices, out-of-range LED indices, invalid keyframes,
and cues outside the requested passage. It snaps cue boundaries to estimated beats
only within 120 ms, rescales local keys accordingly, and checks locked boundaries.
There is at most one automatic correction request for invalid cues. Invalid or
truncated results never become a saved project. Applying a stale proposal after
another edit or a layout change is rejected rather than overwriting newer work.

Proposals and recent conversation survive a browser refresh while the server
remains running; only applied clips and saved preferences/layout survive a server
restart. One generation runs at a time. Discarding suppresses its result, although
an already submitted provider request may finish before another can start.

The first version supports up to 160 generated cues per request. Audio is sent
inline with a bounded request size; if a large arrangement exceeds that limit,
work on a shorter region. Audio interpretation and beat estimates can be wrong:
listen to the preview before applying. Actual musical quality still needs audition
with your chosen model and tracks; automated tests use a simulated provider.

## Spatial effects

The model and manual clip inspector can use five additional patterns:

- **Sweep across rig:** a soft band crossing physical LED coordinates. Animate
  `position` from 0 to 1 for a pass and `direction` for its angle (0° right,
  90° down, −90° up).
- **Expanding ripple:** a ring growing from `origin_x` / `origin_y`. Position
  controls radius as a fraction of the rig's normalised diagonal (√2).
- **Mirrored motion:** bands expanding symmetrically from the rig centre along
  the chosen direction.
- **Spatial colour gradient:** a travelling two-colour blend across physical
  coordinates, spanning devices rather than restarting on each fixture.
- **Path across selected LEDs:** motion along the global ordered target list,
  following fixture order and then selected LED order within each fixture.

`width` controls band thickness (default 0.12). Keyframe `position` for deliberate
movement; without keys it advances one cycle every four seconds. Existing
brightness, fades, colour ownership, and bass/level/attack modulation still apply.
These effects render locally and deterministically, with no model calls in playback.

Generated spatial clips snapshot the physical coordinates they were authored for,
so moving devices in the layout editor does not silently change a saved show.
Manually created spatial clips without a snapshot use the current physical map.
The existing arc/chase patterns continue to animate independently on each device.

## Layering and LED ownership

- **Automatic look** clips substitute a prepared look in their interval, optionally
  with selected fixtures/LEDs, opacity, and entry/exit fades.
- **Guided properties** clips animate brightness, source colour movement speed,
  coverage, accents, hue, saturation, and opacity over the underlying effect.
- **Authored LEDs** add solid holds, gradients, rotating arcs, and ordered chases.
  Any mode other than Automatic can combine these patterns and curves.

Click individual LEDs in the rig preview, then **Use selected LEDs** on a clip.
Selection order is retained for chases. Select an entire fixture by clicking its
LEDs, or use **All LEDs** for the patch. **Target entire rig** removes a clip's
selection mask. Missing fixtures or out-of-range selections are reported rather
than silently remapped to different hardware. Reusable clips retain their target
names, so check selections when moving to a different rig.

LED layers can own full RGB, colour only (retaining underlying brightness), or
brightness only (retaining underlying colour). A solid black RGB layer is an
explicit blackout. A transparent pattern or zero opacity lets the underlying
lighting through. Audio modulation is optional: a precisely authored colour can
still follow bass, overall level, or attack strength when desired.

## Keyframes and reusable animations

The LED animation inspector includes **Hold**, **One turn**, and **Reverse**
shortcuts. These create position keys across the selected clip and save
immediately; apply or discard any pending inspector settings first. A solid or
transparent clip becomes an arc when using these animation shortcuts.

Select a property and click its graph or choose **Key at playhead**. The keyframe
table edits exact times, values, and outgoing interpolation: linear, smooth, or
hold. Values before/after the curve use its first/last key. Keyframe times are
relative to the clip, not the track.

**Key colour** records the primary colour picker as red, green, and blue keys at
the playhead. This supports arbitrary colour transitions, in addition to hue and
saturation curves. Pattern position is measured in turns through the selected
LED order. A constant position key holds a pattern still. Without position keys,
a gradient, arc, or chase takes four seconds per turn.

Movement keys affect the sampled colour motion of a prepared look while retaining
its current spectral brightness. They are not a general time warp of the original
audio or its beat clock. For exact motion, use authored pattern-position keys.
Coverage operates over each fixture's selected LED order; it is not yet a spatial
path across fixtures. Use targeted clips for coordinated multi-fixture gestures.

**Save clip** adds the selected animation to the set's reusable clips. Insert it
at the playhead and edit the instance independently. If an instance reaches past
the track end, it is shortened and its keys are proportionally rescaled.

## Whole-track preparation and regeneration

Prepared analysis runs the existing fixed-rate audio analyser, then sets dynamics
against a fixed full-song reference. This prevents a quiet intro from initially
becoming its own loudness maximum. Coarse, held section suggestions describe
breakdown, groove, build, and full passages. Similar section spectra can reuse a
colour theme. A sufficiently long quieter passage before a major full section
gets a four-second build suggestion, using future context unavailable to live
capture. This is a heuristic draft, not semantic verse/chorus or drop recognition.

**Regenerate suggestions** replaces only unlocked generated clips. Manual clips
and locked regions survive, and overlapping suggested regions are skipped.
Editing a generated clip marks it manual. Locking protects a clip from accidental
editing or deletion as well as regeneration. Unlock it in the inspector to edit.

Base look frames are prepared at 50 Hz and cached on disk, which makes seeking,
looping, and scrubbing repeatable. Slow fixtures preview at 12 Hz. Actual hardware
still follows its configured output cadence. Browser/network/audio-output latency
can cause an offset; physical synchronisation has not been calibrated automatically.
This version does not use neural models or alter live-capture arrangement analysis.

## Saved files and external editing / LLMs

Files default to `~/.local/share/rigby/orchestrator/`. The standalone editor accepts
`--directory PATH` for an isolated workspace. The directory contains the current
`session.json`, decoded audio, compressed feature data, and prepared frame caches.
Frame caches can be large on long tracks and large rigs; keep disk space available.
All tracks share this store between the standalone and live editor, but the store is locked so two processes cannot overwrite the same set concurrently.
Use a separate
`--directory` when you need independent sessions.

A set document has `version`, `name`, ordered `tracks`, and reusable `presets`.
Each track references the SHA-256 fingerprint of its original audio bytes, exact
duration, base look, and ordered clips. Audio and prepared frame caches are not
embedded in the exported JSON. Reimport the exact original audio after moving a
set to another machine; a different edit or encoding intentionally has a different
fingerprint.

The **Review JSON** dialog is a model-independent editing interface. Copy a set
into your preferred tool, paste the proposed document, validate it, inspect the
changed/new/deleted clip summary and full document, then apply it. Applying an
entire document can intentionally replace locked clips; the dialog states this,
and Undo remains available. No LLM is configured or called by Rigby.

Relevant local API routes:

- `GET /api/orchestrator/state`: current document, revision, readiness and transport.
- `POST /api/orchestrator/proposal`: validate `{ "project": ... }` without mutation.
- `POST /api/orchestrator/project`: apply `{ "project": ..., "revision": N }`.
  A stale revision receives HTTP 409 instead of overwriting another tab's edit.
- `POST /api/orchestrator/generate`: regenerate a track using its current revision.
- `GET /api/orchestrator/analysis?track=ID`: waveform, estimated beats and sections.
- `GET /api/orchestrator/preview?track=ID&time=SECONDS`: converted per-LED preview.

The server is a local control surface, like the existing live desk. Keep its
localhost default unless you intend to expose file uploads and lighting control.

## Validation

The full suite includes AI director tests alongside the existing tests, covering exact RGB and property ownership,
black versus transparency, deterministic seeking, whole-song dynamics,
regeneration protection, revision conflicts, transport ordering and disconnects,
HTTP routes, and the existing audio/effect regressions. Run it with:

```sh
uv run python -m unittest discover -s tests -q
uv run python check_ui.py
```

A separate browser regression imports real audio into an isolated temporary
store and exercises play/pause, looping, LED targeting, RGB keys, reusable clips,
protected regeneration, JSON review, undo/redo, and persisted reload:

```sh
uv run --with playwright python -m playwright install chromium
uv run --with playwright python tests/browser_orchestrator.py
```

That workflow passes with no browser errors. Physical lighting/audio latency has
not been measured; verify timing on your own output devices before relying on
precisely authored cues.

The AI browser regression exercises real audio preparation and HTTP routes with
an isolated store and a simulated model response. It covers physical layout
editing, draft audition and live A/B transport, conversational revision, refresh recovery,
apply/undo, and discard without contacting Google:

```sh
uv run --with playwright python tests/browser_ai_director.py
```
