# rigby

Audio-reactive lighting desk for OpenRGB. Uses OpenRGB as a *driver*, not an
effects engine.

## Run it

OpenRGB's SDK server must be up (this is separate from the GUI):

```sh
openrgb --server            # headless, listens on 6742
```

Then:

```sh
uv run rigby --look auto                       # picks its own behaviour (start here)
uv run rigby --look prism                      # spectrum response with moving colour
uv run rigby --look duotone                    # two-tone rotating rings, beat-driven
uv run rigby --look rain                       # sparse drops that ramp on swells
uv run rigby                                   # spectrum look, taps your default sink
uv run rigby --look chase --no-audio           # rig check, no audio needed
uv run rigby --palette cyanmag --master 0.6
uv run rigby --source file:track.mp3           # program a show against a known track
```

Ctrl-C blacks out and exits.

## Tuning it live

```sh
uv run rigby --look auto --control        # then open http://localhost:8721
```

The page lives in `ui.py`, apart from the server: a control surface is a design
artefact, not request-handling code, and the two change for different reasons.

Every knob is applied to the running show between frames -- no restart, so you
don't lose the passage you were listening to. The page also shows what the
analyser thinks is happening (bands, dynamics, swell, pulse, output level, beat
flashes), because most tuning questions are really "is the analysis right or is
the mapping wrong", and reading that off a number beats inferring it from the
lights.

### Devices

The **devices** tab is where the rig gets calibrated, and it writes to
`~/.config/rigby/config.json` so it survives restarts -- calibration is
something you settle once by eye, not a flag you retype.

- **zones** -- set a header to its real chain length. Resizes the OpenRGB zone
  live and re-resolves the patch, since a zone that grew shifts every offset
  after it.
- **chain** -- fan mode, fan count, LEDs per fan, swap headers. Applied by
  rebuilding the patch between frames.
- **fixtures** -- per ring: spin direction, and a rotate offset that moves where
  LED 0 sits. Fans mounted mirrored run backwards and each fan's first LED lands
  at whatever clock position its own wiring puts it; neither is knowable in
  advance, so both are dialled in by eye.
- **identify** -- dims the whole rig and lights one fixture, so you can tell
  which physical fan is `fan_c`.

CLI flags still win over the saved file, so a one-off run can override
calibration without editing it.

### Typefaces

The UI is set in Fira Sans with Google Sans Code for figures, served by the app
from wherever fontconfig says they live. Nothing is copied into this repo -- no
redistribution, no megabytes of base64 in a source file -- and a phone on the
same network gets the same design as the machine running the show. TTFs are
gzipped on the way out and marked immutable. If a face isn't installed the CSS
falls through to a stack that degrades sensibly.

`system-ui` is a different typeface on every machine, so a layout tuned on one
is wrong everywhere else.

The chrome is deliberately monochrome with a single signal red used only for
state. Every other hue on screen belongs to the rig, which is the thing you're
actually meant to be looking at.

Append `?static` to render one frame and stop. An open event stream is a
request that never finishes, so headless capture never sees the page go idle --
the UI has to be able to sit still to be screenshotted.

### Live state

The page is pushed, not polled: `/events` is a server-sent event stream that
blocks until the telemetry version actually changes, so nothing crosses the
wire that isn't news. SSE rather than websockets because this channel is
one-way -- controls stay ordinary POSTs -- and `EventSource` reconnects by
itself. Polling remains as a fallback.

Three things had to change together, and the transport was the least of them:

| | before | after |
|---|---|---|
| telemetry published | 5/sec | **30/sec** |
| payload | 4,500 B per poll | 2,156 B per event |
| repaint | `querySelector` per LED per frame | cached refs, changed LEDs only |

The render loop had been publishing every 200ms, so the UI could never be
smoother than 5fps however fast the browser asked. The frame is now one hex run
per fixture rather than 197 `"#rrggbb"` strings, and paints are coalesced onto
`requestAnimationFrame` because pushes can outrun the display.

### Playground

The **playground** tab draws the rig as it physically is -- rings as rings, the
keyboard as its real 6x23 matrix (read from the device's own `matrix_map`, gaps
included), strips as strips -- and lets you paint individual LEDs by clicking or
dragging. Pick a colour, set a brightness, or switch to erase; `fill` works per
fixture or across everything, and `capture from show` loads whatever the show is
currently displaying so you can start from it.

Painted colours are written **exactly**: playground mode bypasses gamma and the
`MIN_LIT` floor, because those shape an effect's brightness and only get in the
way when you're hand-setting a colour. Picking `#803C30` puts `(128, 60, 48)` on
the LED. The show keeps stepping while paused, so switching back resumes
mid-gesture.

Both tabs mirror the live rig, so the show view doubles as a monitor. The
devices tab freezes the show while you're on it, so a rotation offset is judged
against a still picture rather than a moving one; the playground hands the rig
over to the canvas entirely.

Anything a rebuild replaces -- the canvas, the geometry -- lives behind a
mutable holder that handlers dereference per request. HTTP/1.1 keep-alive means
one handler instance serves a browser for its whole session, so closing over
those objects leaves an open connection writing into an orphaned canvas after
any rebuild, and the playground goes quietly dead while still returning 200.

Changing `look` or `duo` rebuilds the look; everything else is written straight
onto the live objects. `--control-host 0.0.0.0` lets a phone on the same network
reach it -- there is no authentication, so anyone who can reach the port can
drive your lights. Localhost is the default for that reason.

### Too dim?

```sh
uv run rigby --meter          # see the signal, drive no lights
```

If it prints `no signal ... after 3s`, nothing is reaching the tap at all --
that's a routing or volume problem, not an effects problem.

The live tap uses `parecord`, deliberately. `pw-record --target=<sink>.monitor`
does **not** resolve PulseAudio-style monitor names -- it silently attaches to
some unrelated source and records digital silence indefinitely, which is
indistinguishable from broken effects. If you swap the capture command, verify
with `--meter` that you still see signal.

The meter prints input dBFS, the auto-gained level, per-band bars, and the
resulting output brightness. If `in` sits below about -50 dBFS, the problem is
upstream: **PipeWire sink monitors are post-volume**, so a sink turned down to
11% hands the tap a signal ~750x smaller than you'd expect -- even though your
Bluetooth headphones sound loud, because their volume is handled on the headset.
Raise the sink, lower the headset.

If `in` looks healthy but `out` stays low, reach for the curve:

| flag | does |
|---|---|
| `--curve` | brightness curve, default `0.45`. Below 1 lifts the low end. |
| `--gain` | soft brightness drive, default `1.6` |
| `--master` | grand master, scales everything at the output |
| `--dynamics-db` | dB below the running reference that reads as dark, default `15`. Higher = flatter, lower = more dramatic. |
| `--onset-k` | attack threshold in std devs, default `1.7`. Raise if accents trigger too eagerly. |
| `--noise-floor-db` | silence gate, default `-72` dBFS |
| `--min-lit` | smooth output visibility toe, default `3`; `0` disables it |

Why this knob has to exist: band envelopes spend most of their time around
0.3-0.5, and output gamma then squares that away to nearly nothing. A chase
looks bright by comparison only because its bump peaks at exactly 1.0.

### Bluetooth sinks

A monitor tap grabs audio *before* BT encode + transmit, so the lights run
~150-250ms **ahead** of what you hear. Compensate:

```sh
uv run rigby --offset-ms 200
```

Tune by eye. Wired sinks want `0`.

## Offline programming

`--source file:PATH` decodes through ffmpeg, so mp3/flac/opus/m4a/wav all work.
It **plays the file out loud by default** while rendering the show against it --
one ffmpeg process with two outputs, so sound and lights cannot drift apart.
Use `--no-play` to render silently when you're programming rather than watching.

Note that the tap here is the file itself, upstream of the sink, so `--offset-ms`
still applies if you're listening on Bluetooth.

### Capture timing

Audio is analyzed on a fixed 100 Hz sample clock, independently of `--fps`.
The capture worker processes every 480-sample hop. Renderers interpolate a
bounded history of continuous features and consume timestamped attacks once.
Capture bursts do not insert repeated windows into the onset detector; a slow
renderer discards attacks older than 150 ms instead of replaying a backlog.

`--offset-ms` delays features by up to 2000 ms and drains the delay at file EOF.
Changing the offset cannot replay an event already delivered. Capture stalls
fade the last picture and reduce beat confidence. A long stall discards the
old rhythm history. Envelope coefficients are expressed in seconds.

The control UI reports audio arrival age and estimated tempo. Arrival age is
**not** physical speaker-to-LED latency: it excludes upstream audio buffering,
controller buffering, and the LED's response. Start with offset 0 on wired
output; use a recorded click/light comparison to calibrate a device. Adding
an offset only delays lights. It cannot compensate for an already late attack.
Predictable movement uses the beat clock while unexpected attacks remain causal.

### Checking the UI

```sh
uv run python check_ui.py
```

The page is a Python string, so a stray escape becomes a real newline at import
and kills the entire script. The page still renders and the tabs still exist --
they just do nothing, with no error anywhere. This parses the served JS with
`node --check` and refuses unbalanced quotes. `PAGE` is a raw string for the
same reason: every escape in it belongs to JavaScript, not Python.

The tabs are hash-routed (`#playground`, `#devices`), so they deep-link and can
be driven headlessly for testing.

## Benchmarks

```sh
uv run python -m unittest discover -s tests -v
uv run python check_ui.py
uv run python tracks.py /tmp/rigby-tracks
uv run python bench.py --corpus /tmp/rigby-tracks/corpus.json --look auto \
  --trace /tmp/rigby-trace.json
uv run python bench.py /tmp/rigby-tracks/timing.wav /tmp/rigby-tracks/timing.json --fps 30
```

The fixtures cover rhythmic dynamics, dense percussion, a ballad, sustained
vibrato, silence, tempo changes, fills, and a short breakdown. Unit tests also
exercise pure tones, noise gating, capture bursts, stale frames, offset changes,
EOF draining, and rendering at 30/60/120 FPS.

Scores use all fixture pixels after the same output conversion as the sink,
with simulated 60 Hz fast / 12 Hz slow device cadence. JSON reports include
one-to-one attack precision/recall, signed timestamp bias, 95th percentile
absolute timestamp error, event delivery delay, beat phase error, tempo recovery,
section brightness, silence brightness, clipping, and motion between accents.
`motion_delta` describes movement; a lower number alone is not a quality score.
Device simulation does not measure physical hardware latency.

Annotations use seconds:

```json
{
  "hits": [2.0, 2.5, 3.0],
  "texture_hits": [2.25, 2.75],
  "beats": [2.0, 2.5, 3.0, 3.5],
  "quiet": [[2.0, 4.0]],
  "loud": [[8.0, 12.0]],
  "silence": [[0.0, 1.8]],
  "sustained": [[4.2, 7.8]],
  "sections": [{"start": 2.0, "end": 12.0, "bpm": 120}]
}
```

`hits` are primary attacks; optional `texture_hits` include hats or other fine
percussion. Overall onset scores use their union, while `primary_recall` keeps
weak/extra textures from hiding missed main hits. Annotations without texture
hits count any detected textures as false positives. `beats` describes the
underlying grid, including beats through rests; constant-tempo files may use
`bpm` and `beat_offset` instead. Brightness windows come from annotations, never
hardcoded positions. Allow time for intended fades when annotating silence.

A real-music corpus is a JSON list of `{"audio": "song.flac", "annotations":
"song.json"}` entries, with paths relative to the manifest. Audio stays outside
the repository. Use `--corpus` and `--trace` to inspect your own recordings.
For a listening comparison, replay the same excerpts at the same input volume,
master, gamma, palette, and offset. Check whether movement stays in time, fills
preserve the motif, verses retain space, and a chorus expands coherently. Include
acoustic music, vocals, sparse percussion, and changing tempos; synthetic scores
cannot establish that a show feels natural on real hardware.

## Layout

| module | role |
|---|---|
| `patch.py` | fixture definitions, resolved against live devices by name |
| `analyze.py` | fixed-rate audio analysis, gated bands, timestamped attacks |
| `music.py` | tempo/phase tracking and conservative arrangement decisions |
| `fx.py` | the desk FX primitive (waveform x rate x spread), HSV, palettes |
| `sink.py` | OpenRGB output, per-device tick rates, dirty checks |
| `show.py` | looks: layered wash + movement + hits |

### Fixtures

Effects address named fixtures, never device or LED indices, so replugging
hardware or OpenRGB reordering its device list doesn't touch effect code.
Anything not currently plugged in is skipped with a note at startup.

Current patch on this machine (57 LEDs live, 183 with the USB keyboard in):

```
cfan_a..g 11 led ring    hid 60fps   7 chassis fans, Adalight (Arduino)
afan_a..c 18 led ring    hid 60fps   3 AIO fans, Aura Addressable 1
aio       12 led ring    hid 60fps   AIO block, Aura Addressable 2
mobo      4 led line   hid 60fps    Aura Mainboard
ram_a     8 led line   i2c 12fps    ENE DRAM
ram_b     8 led line   i2c 12fps    ENE DRAM
gpu       1 led line   i2c 12fps    Palit RTX 3080
kbd     126 led line   hid 60fps    EVision / Redragon Mitra (USB only)
```

**Nothing is hardcoded to a device name.** Every zone OpenRGB reports becomes a
fixture, and the config says how to carve the interesting ones up. An Adalight
strip carrying seven fans is seven rings; no amount of guessing from a device
name would work out that it is anything but a strip. Set it in the devices tab,
which lists each zone's even divisions so a fan count can be picked rather than
worked out -- 77 LEDs offers `7x11`, 54 offers `3x18`.

**Wiring order is not mounting order.** The `order` field on a group is a
permutation mapping each *physical* slot to the electrical segment sitting
there, so `cfan_a..cfan_j` cross the case in a straight line whatever the
cabling does and sequential effects sweep properly. Edit it in the devices tab:
one box per physical slot, plus buttons that light a chosen segment so you can
see where it actually is. An order that isn't a clean permutation is rejected
outright rather than half-applied, since that would silently double-drive one
segment and leave another dark.

Virtual devices are skipped: they remap LEDs that are already driven, and
writing to both would fight over the same hardware.

**Fans on a hub are usually one ring, not several.** A passive ARGB splitter
feeds every port the same signal, so three fans on one header show the *same*
six LEDs -- you cannot light one without the others. `--fan-mode mirrored`
(default) models that honestly: a single `fans` ring whose data is repeated to
every port. Modelling it as three independent rings means two thirds of the
frame is computed and sent into a void, and every phase offset between fans is
invisible.

A daisy-chain hub, or fans wired in series, does give each fan its own slice --
that's `--fan-mode chained`.

### Finding out what's really on a header

OpenRGB cannot see past the header. It reports whatever LED count the zone is
configured for, and writes beyond the real chain vanish silently -- so a wrong
count looks exactly like working code. The only way to establish the truth is to
light things and look:

```sh
uv run rigby --probe 1 --probe-size 60      # motherboard zone 1
```

It resizes the zone, lights every LED at once (count how far the lit run
reaches -- that's your real chain length), then walks one LED at a time. If the
same position lights on *every* fan, the hub is a splitter and `--fan-mode
mirrored` is right. If the lit LED moves from fan to fan, it's chained, and the
index where it jumps to the next fan is your LEDs-per-fan.

The original zone size is restored on exit, including on Ctrl-C. Back up
`~/.config/OpenRGB/sizes.ors` first if you want a belt-and-braces undo.

**A fan is a ring, not a strip.** Ring fixtures carry an `angle` per LED, a
`spin` direction and an origin in the case, which is what makes rotation,
half-ring splits and per-fan phase offsets expressible at all. Fan count and
LEDs-per-fan live in `patch.py` (`FANS_PER_HUB`, `LEDS_PER_FAN`); if the AIO and
hub are on the other header, pass `--swap-headers`. `--identify` walks one LED
at a time so you can read the physical order off the case.

**Per-device tick rates are load-bearing.** DRAM and the GPU sit on SMBus/i2c at
~100kHz. Driving them at 60fps makes the bus the bottleneck and stutters the
whole rig, so they're marked `slow` in `PATCH_SPEC` and get ~12fps plus a dirty
check. Keep them as wash fixtures; put detail on the HID devices.

## Orchestrator: editable track arrangements

Run `uv run rigby-editor` for a standalone waveform/timeline editor, or open
**Orchestrator** from the live desk to use your actual fixture map. Import local
audio, generate a first arrangement, then edit look clips, property and RGB
keyframes, and individual LED animations. Scrub, loop, save reusable clips, and
play tracks as a set. Authored or locked regions survive automatic regeneration.

The editor saves locally and exports portable JSON. Its proposal-review interface
also supports edits from external tools or an LLM without granting them direct
control of the lights. See [the orchestrator guide](docs/orchestrator.md) for
playback, LED ownership, prepared analysis, and current limits.

## Musical behavior

`auto` is the default look. It combines `duotone` movement with `rain` using both
rhythmic confidence and arrangement energy. The individual looks can be selected directly, and `chase` remains an intentionally audio-independent
fixture check. `--no-audio` supplies visible calibration features.

For a consistent look throughout a playlist, use `--look duotone`,
`--look spectrum`, or `--look rain`. These respond to sustained attack density
as well as relative loudness, so a steady drum-heavy track can stay energetic
without needing to get progressively louder or maintain a confident beat lock.

- **Duotone:** energetic grooves recruit more rings and use faster gestures.
  Even arrangements get a fresh motif every 16 tracked beats (with a minimum
  six-second hold); without reliable timing, changes stay slower. Accents move
  between fixtures using subdivisions and drum roles.
- **Prism:** spectrum's response with a restrained, slow two-tone colour
  pattern. Strong low/mid accents and sustained lifts shift the whole palette,
  with at least six seconds between shifts. Quiet sections reduce movement and
  brightness promptly, leaving room for the next drop. `--palette` sets the spectral
  colour base; `--duo` sets the moving pair. Slow fixtures use gentler movement
  and colour transitions.
- **Spectrum:** energetic passages open moving gaps in the wash, with fast band
  activity and local drum accents filling them. This preserves visible movement
  when the underlying spectrum stays loud and steady.
- **Rain:** soft passages retain gentle drops. Percussive passages add short,
  sharp splashes; kicks can reach two fast fixtures, while high accents stay
  small. Slow bus fixtures keep their smoother washes.

Rhythmic intensity rises over roughly a second and relaxes over a few seconds.
It is an activity estimate, not a genre classifier; gain and curve still control
brightness separately.

### Audio features and silence

Bands retain their spectral balance. Each band's adaptive gain is bounded by a
common broadband reference, and insignificant energy is suppressed. A 1 kHz
tone should light the midrange without generating bass from FFT leakage. Bass
has its own 40–120 Hz reference. `balance` exposes actual spectral energy shares
separately from the normalized activity envelopes.

`--noise-floor-db` (default -72 dBFS) sets the minimum signal gate. Hysteresis
and a short release distinguish brief rests from silence. Long silence clears
gain and beat history; quiet active music still has a small intensity floor.
Lower the gate if a deliberately quiet monitor tap is being suppressed.

`dynamics` follows loudness relative to an active-audio reference with a running
mean during warm-up and a 25-second long-term time constant. `swell` follows
sustained relative loudness. Energy slope and smoothed spectral change describe
builds and arrangement changes. `mid_share` measures energy from 200–4000 Hz;
it is not a vocal detector. Causal analysis cannot know at the beginning of a
song whether the opening will later prove quiet relative to its chorus.

### Attacks, beats, and bar hypotheses

Attack detection uses log-compressed, semitone-grouped spectral flux, a frequency
maximum filter to suppress pitch motion, energy-rise support, adaptive thresholds,
and independent refractory times for low/mid/high evidence. Broadband attacks
are assigned a dominant role. Events carry estimated audio time, strength, and
confidence. Roles are approximate frequency descriptions, not instrument labels.
The frequency-maximum approach is inspired by
[SuperFlux](https://www.dafx.de/paper-archive/details.php?id=0oee-99Z88WL7pSo749gcA).

The beat clock evaluates up to six seconds of history, starts estimating after
two seconds, and searches 55–180 BPM. Autocorrelation, attack/grid coherence,
and tempo continuity determine confidence. A phase loop corrects gradually;
short breaks retain musical time, while prolonged silence forgets the grid.
Tempo and phase estimates are heuristic: half/double-time ambiguity, rubato,
and very sparse rhythms can remain uncertain. No additional dependency or
model download is required.

An onset never increments a beat counter. Four-beat bar position has a separate
accent-based confidence; uniform beats provide no downbeat evidence. This is a
4/4 hypothesis, not general meter recognition or a verse/chorus classifier.
When bar evidence is weak, transitions use a beat boundary rather than claiming
a phrase boundary.

### Movement and structure

Movement runs at musical divisions: the base chase spans four beats and gesture
rates select half, normal, or double speed. Energy changes width, fixture
coverage, and intensity rather than accelerating a chase between hits.
With uncertain tempo the movement falls back to a slow, continuous drift.

The arrangement director chooses among stable behaviors:

| behavior | movement vocabulary | visual intent |
|---|---|---|
| sparse | breathe, converge | retain gaps and a small motif |
| groove | spin, pingpong, lobes | repeat movement aligned to rhythm |
| build | converge, wipe | widen and recruit fixtures |
| full | lobes, spin, sparkle | expand coverage while preserving accent headroom |

Changes require persistent evidence and a minimum hold time. With a confident
clock they land on a beat or credible bar boundary and crossfade. Smoothed
spectral novelty can refresh a motif after a longer hold. Randomness chooses
within a suitable vocabulary; drum fills do not advance a phrase counter or
reverse every ring.

Low attacks widen rings, mid attacks create local accents, and high attacks
produce smaller texture. Strength, confidence, and event age scale their
amplitude. Slow fixtures keep a wash instead of receiving short flashes.
Rain gates accents more strictly without a beat and puts each on one fast
fixture. Its particles keep their accent hue through their lifetime, and a
zero birth rate produces no particles. Density expands into a sustained wash
as the music grows.

### Color and output calibration

`--duo ember|toxic|vapor|cobalt|mono` selects the two-tone relationship.
`--hue-drift 0` pins it. `--saturation` and `--hot` set color intensity and
highlight desaturation; arcs remain wide enough to read on coarse fan rings.
`--hit-style swing|accent|white` sets accent treatment. Color swings are spaced
in musical time and selected by relative impact.

`drive()` has a soft shoulder instead of clipping every input above 0.625.
Headroom is reserved in the layer mixer, without an additional drive multiplier.
Mixed colors are normalized before intensity is applied, so overlapping hues do
not accidentally dim one another. Auto crossfades in approximate emitted-light
space to avoid dark dips between patterns. Scene coverage is applied inside the
look rather than also reducing its crossfade weight. `--gain` and `--curve` shape the picture, then
`--master`, `--gamma`, and `--min-lit` shape device output. The visibility toe
(default 3 raw output units) fades smoothly and preserves absent color channels;
set it to 0 to disable it. Master 0 always sends black.

The control preview uses the same conversion as the hardware and benchmark.
Calibrate against the complete chain on your LEDs: establish master/gamma/toe,
then adjust drive so normal passages leave visible room for accents. Preview
colors and simulated cadence cannot replace a physical brightness/latency check.

## Writing a look

Subclass `Look`, implement `render(features) -> {fixture_name: (n,3) float RGB}`,
register it in `LOOKS`. The FX primitive is the thing to reach for:

```python
v = fx.wave("sine", self.chase, fix.pos, spread=2.0, size=1.0)
```

waveform x phase x spread across the fixture. Sine on intensity with spread is
the classic truss wave; `step` on hue with zero spread is a colour chase. Merge
intensity layers with `fx.htp()`; combine colored layers with `fx.mix_layers()`
to retain headroom. Use `f.events` for attacks and `f.beat_position` for musical
position. Envelopes should use `music.alpha(dt, tau)` rather than frame constants.

## Not built yet

- **Tap tempo and meter override.** Manual correction for ambiguous beat grids
  and music outside the automatic tempo/meter assumptions.
- **Cue stack.** Ordered looks with fade/wait times and a GO trigger.
- **MIDI.** A nanoKONTROL2 or APC Mini turns this into an actual desk: faders to
  layer intensities, pads to cue GO and flash.
- **Timecode.** For shows programmed to a specific track, drive cues from MPRIS
  playback position (`playerctl position`) rather than live analysis — far more
  reliable for hitting a drop on cue than hoping the onset detector agrees.
- **Keyboard as a matrix.** The 126 keys have a `matrix_map`; right now they're
  treated as a 1D strip.

The orchestrator also includes an optional **AI director**. Set `GEMINI_API_KEY`
on the server, arrange your devices in **Physical rig layout**, then generate and
refine lighting from actual song audio. Proposals can be auditioned before applying;
locked passages stay protected. See the [AI setup and workflow](docs/orchestrator.md#ai-lighting-director).

**Rig layout** is also available independently at `/rig-layout`, linked from the live desk. Arrange and save your physical devices there without opening an audio track or configuring AI.

AI direction is docked beside the timeline: drag a waveform passage, choose **Suggest selection**, and compare saved/proposed lighting with A/B during playback. Spatial sweeps, ripples, mirrored motion, gradients, and ordered LED paths can span the physical rig.
