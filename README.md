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

Both tabs mirror the live rig, so the show view doubles as a monitor.

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
| `--gain` | pre-curve multiplier, default `1.6` |
| `--master` | grand master, scales everything at the output |
| `--dynamics-db` | dB below the running reference that reads as dark, default `22`. Higher = flatter, lower = more dramatic. |
| `--onset-k` | onset threshold in std devs, default `1.7`. Raise if beats trigger too eagerly. |

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

Capture runs on its own thread into a ring buffer; the render loop samples the
newest window on a wall clock. This is not incidental -- PulseAudio delivers
audio in ~340ms bursts by default, so reading one hop per rendered frame
produces 20 instant frames then a 340ms freeze, and any backlog becomes
permanent latency that keeps animating after the music stops. `parecord` is also
asked for `--latency-msec=20`.

Measured: 60.2 fps, frame interval p50 16.66ms / max 20.9ms, zero bursts, and
+9ms of lag beyond the raw tap. A stalled source (suspended sink, paused stream)
fades to dark in ~800ms rather than looping on stale audio.

## Benchmarks

`bench.py` scores onset accuracy, dynamics and smoothness against synthetic
tracks with known beat times. The tracks aren't in git -- regenerate them:

```sh
uv run python tracks.py /tmp/rigby-tracks
uv run python bench.py /tmp/rigby-tracks/dyn.wav /tmp/rigby-tracks/dyn.json
```

Every seed is fixed, so the numbers reproduce exactly.

## Layout

| module | role |
|---|---|
| `patch.py` | fixture definitions, resolved against live devices by name |
| `analyze.py` | audio tap -> log-spaced bands, envelopes, spectral-flux onsets |
| `fx.py` | the desk FX primitive (waveform x rate x spread), HSV, palettes |
| `sink.py` | OpenRGB output, per-device tick rates, dirty checks |
| `show.py` | looks: layered wash + movement + hits |

### Fixtures

Effects address named fixtures, never device or LED indices, so replugging
hardware or OpenRGB reordering its device list doesn't touch effect code.
Anything not currently plugged in is skipped with a note at startup.

Current patch on this machine (57 LEDs live, 183 with the USB keyboard in):

```
fans      6 led ring x3 hid 60fps   fan hub, Aura Addressable 1 (mirrored)
aio      18 led ring    hid 60fps   AIO pump head, Aura Addressable 2
mobo      4 led line   hid 60fps    Aura Mainboard
ram_a     8 led line   i2c 12fps    ENE DRAM
ram_b     8 led line   i2c 12fps    ENE DRAM
gpu       1 led line   i2c 12fps    Palit RTX 3080
kbd     126 led line   hid 60fps    EVision / Redragon Mitra (USB only)
```

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

## The duotone look

`--look duotone` is the one that tries to stop looking like a visualiser. Three
things do that work, none of which are about amplitude:

- **Intrinsic movement.** The rings rotate whether or not anything is playing;
  music modulates the rotation rather than driving brightness directly.
  Measured: +0.302 / -0.302 / +0.302 / -0.302 turns/sec across fan_a, fan_b,
  fan_c and the AIO.
- **Phase relationships.** A third of a turn of spread per fan, plus
  counter-rotation on fan_b and the AIO. Three identical rings spinning in
  unison read as one object; opposed, they read as three. Measured: all three
  fans peak on the same LED in only 3.6% of frames.
- **Beats as colour events, not white flashes.** Each onset swings *one half*
  of *one* ring to the opposite of the colour wheel while everything else keeps
  running, cycling round the rings and alternating halves. Measured over 46 beats: hits distributed
  11/12/12/11 across the four rings, halves alternating exactly 23/23. Every
  fourth beat reverses the whole rig's spin direction, so a four-bar loop
  doesn't look like one bar.

### Gesture vocabulary

A single behaviour plus a direction flip is still a single behaviour: it reads
as swirling back and forth and stops being interesting after about a minute.
Rings pick from seven gestures -- `spin`, `lobes`, `pingpong`, `breathe`,
`wipe`, `sparkle`, `converge` -- and change on a phrase boundary
(`PHRASE_BEATS` onsets), or on a timer when there's no beat to count. Each
instance re-rolls its own rate, width, lobe count, direction and per-ring
spread, and gestures crossfade over `XFADE_S` so nothing snaps.

They are measurably different motions, not reskins:

| gesture | rotation (turns/s) | spatial variance | duty |
|---|---|---|---|
| spin | +0.35 | 0.21 | 0.57 |
| lobes | -0.90 | 0.04 | 1.00 |
| pingpong | 0.00 | 0.24 | 0.57 |
| breathe | +0.05 | 0.16 | 0.87 |
| wipe | +0.03 | 0.11 | 1.00 |
| sparkle | +0.70 | 0.16 | 1.00 |
| converge | 0.00 | 0.28 | 0.57 |

### Saturation and legibility

Flat full saturation is why animation is hard to read on a 6-LED fan. A
saturated hue carries very little *luminance* -- pure red is about 21% of white
-- so an arc's falloff disappears and you see one lit LED jumping rather than a
light sweeping. Measured before: 77% of pixels above saturation 0.9.

Colour now runs a **hot core**: saturation falls as intensity rises, so the
bright head desaturates toward white and the coloured tail stays visible behind
it. That's how a real fixture behaves, and it gives the eye a highlight to
track.

| | old (flat, full sat) | default (`--saturation 0.88 --hot 0.5`) |
|---|---|---|
| pixels above sat 0.9 | 77% | **11%** |
| peak perceived luminance | 0.204 | **0.360** |
| luminance span across a fan | 0.181 | **0.325** |
| frame-to-frame motion signal | 0.0090 | **0.0139** |

Confirmed on hardware: luminance span 0.158 -> 0.209, saturation 0.74 -> 0.60.

Note the contrast *ratio* barely moves (0.803 -> 0.829) -- it's the absolute
span that nearly doubles, which is what matters at these light levels. Arcs are
also never allowed to span less than about 1.5 LEDs, since a 0.15-turn arc on a
six-LED ring is a single pixel. `--saturation 1.0 --hot 0` restores the old
flat look.

### What a beat does to colour

`--hit-style swing` (default) swings the hit region to the far side of the
wheel instead of flashing white. Two details make it work:

- It **replaces** rather than adds. Adding a complementary hue on top of an
  existing one sums to white, which is the thing being avoided -- the old
  accent-on-top flash trended white whatever accent hue it used.
- Colour weight and brightness are **separate**. Scaling the blend weight by
  hit strength only ever half-blends the hue, landing between the two tones and
  reading as neither; the hue now swings fully and `lum_scale` decides how hard
  it lands.

**Swings are rationed.** A colour change only has impact if it's rare, so
ordinary beats get the plain flash and the swing is saved for hits that earn
it. The test is relative -- beat your own recent neighbours on
`onset_strength * dynamics` -- with a floor on spacing so it stays an event and
a ceiling so it doesn't vanish on very even material. A fixed threshold cannot
work across tracks: the top decile of impact is 0.95 on material with varied
hits and 0.55 on a uniform four-to-the-floor, so any constant either swings on
everything or on nothing.

| track | beats | swings | swing impact vs all beats | mean gap |
|---|---|---|---|---|
| varied, with a quiet section | 46 | 5 (11%) | 0.72 vs 0.49 | 4.7s |
| dense, uniform kicks | 69 | 8 (12%) | 0.53 vs 0.43 | 3.1s |

None of the five swings on the first track land in its quiet section.
`--swing-min-beats` tunes the rate (1 -> 52%, 3 -> 20%, 6 -> 11%, 12 -> 7%).

It swings to the point opposite the pair's *midpoint*, not the complement of
the primary -- the complement of the primary sits on top of the secondary,
since the two tones are already about half a turn apart, so the "swing" would
just swap the colours over. Measured on the hit LED:

| style | saturation | dist. from primary | from secondary |
|---|---|---|---|
| `white` | 0.44 | 0.094 | 0.416 |
| `accent` | 1.00 | 0.070 | 0.481 |
| `swing` | **1.00** | **0.219** | **0.219** |

For a pair spanning ~0.5 turns, 0.25 is the furthest any hue can be from both,
so 0.219 is close to the ceiling. `--hit-style white` restores the old flash.

### Colour

`--duo ember|toxic|vapor|cobalt|mono` sets a two-tone *relationship* -- base
hue, separation, accent offset -- not two fixed colours. The base drifts right
around the wheel over ~5 minutes and the separation breathes between roughly
0.43 and 0.60 turns, so the pair keeps changing character without ever
collapsing into one muddy colour. Storing fixed hues is why a rig ends up
looking like the same red and blue forever. `--hue-drift 0` pins it; higher
values speed it up.

## Music without a usable beat

Slow melodic material defeats beat detection outright. Its onset envelope has
no periodicity to lock onto, so a beat-driven look either sits still or invents
a pulse that isn't there -- measured, the detector fired **84 times on a ballad
with 4 real ticks**.

`--look auto` runs both behaviours and crossfades on `pulse`, so a track that
drifts in and out of having a beat drifts between them and nothing snaps:

| track | pulse settles at | behaviour |
|---|---|---|
| dyn (128bpm, kick+snare) | 0.67 | beat-driven |
| hard (dense, syncopated) | 0.99 | beat-driven |
| ballad (sustained, 4 ticks) | 0.14 | rain |

**`pulse`** is the periodicity of the onset envelope -- autocorrelation over 6s
in the 40-180 BPM lag range. Two things had to be right for this to work at all:
the envelope must be detrended against a local moving average first (otherwise
slow drift dominates and *every* track scores ~0.70), and the test material must
not contain a periodic LFO, which will happily masquerade as a beat.

**`swell`** is sustained loudness in dB against the song's own average, weighted
by how much energy sits in the vocal range. It drives `rain`: drop density and
brightness rise with it, and above a knee the gaps fill in so the pattern
crossfades from discrete drops to a continuous glow with no mode change.

Drops have an attack, not just a decay -- spawning at full brightness in one
frame is a pop, not a raindrop -- and arrive on exponential inter-arrival times,
because a fractional carry spawns at exactly even spacing and reads as a
metronome. Blobs widen on the coarse 6-LED fan rings, where a narrow one is
just a single LED blinking. Measured on a ballad: mean frame delta 1.47 -> 0.69
on the AIO and 2.22 -> 0.64 on a fan, worst single-frame jump 241 -> 108.
Measured on hardware across a ballad's arc:

| section | AIO LEDs lit | peak |
|---|---|---|
| verse | 5.4 / 18 | 113 |
| build | 9.5 / 18 | 642 |
| belt | 13.0 / 18 | 628 |
| outro | 0 / 18 | 10 |

**The reference must be an average, not a decaying max.** Against a max, a slow
build tracks its own reference upward and reads 1.0 the whole way -- verse and
chorus come out identical, which is exactly the bug that made ballads look flat.
The same mistake in `vox_peak` made the swell run *backwards*.

## Dynamics and beats

Two things are deliberately separated:

- **Band auto-gain** normalises each band against its own recent peak. That's
  what makes the spectrum readable at any volume -- and on its own it destroys
  dynamics, since a quiet passage gets amplified straight back up.
- **`dynamics`** measures loudness in dB against a slow reference (~45s
  half-life) and is applied as a master intensity. Shape and loudness stay
  separate concerns.

Onsets are peak-picked from spectral flux over **40-400 Hz only**. Measuring
flux across half the spectrum meant sustained pads and hi-hats counted as
transients. Detection requires a genuine local maximum (one frame of lookahead),
a threshold of mean + `k`*std over recent flux, and a refractory gap.

Scored against synthetic tracks with known beat times (`bench.py`):

| | before | after |
|---|---|---|
| onsets fired (51 real) | 566 | 46 |
| precision | 0.088 | **1.00** |
| f1 | 0.162 | **0.948** |
| loud vs quiet brightness | 1.1x | **8.2x** |
| frame-to-frame jitter | 5.8% | **4.3%** |

On a deliberately hard mix -- dense pad, 16th hats, noise floor, syncopated
kicks -- precision 1.00 / recall 0.99.

**Measure output post-gamma.** An early version of the benchmark scored
brightness before gamma and 8-bit quantisation, which hid that quiet passages
were going *fully black* on hardware: with gamma 2.2, any perceptual value below
~0.11 quantises to zero. `sink.MIN_LIT` now guarantees a non-zero intent never
lands on zero.

## Writing a look

Subclass `Look`, implement `render(features) -> {fixture_name: (n,3) float RGB}`,
register it in `LOOKS`. The FX primitive is the thing to reach for:

```python
v = fx.wave("sine", self.chase, fix.pos, spread=2.0, size=1.0)
```

waveform x phase x spread across the fixture. Sine on intensity with spread is
the classic truss wave; `step` on hue with zero spread is a colour chase. Merge
layers with `fx.htp()` — highest takes precedence, same as a real desk.

## Not built yet

- **Tap tempo / beat tracking.** Effect rate currently rides on broadband level.
  Real beat tracking (`aubio`) would let effects run on musical time — 1/4, 1/8,
  bar — and hold phase through breakdowns.
- **Cue stack.** Ordered looks with fade/wait times and a GO trigger.
- **MIDI.** A nanoKONTROL2 or APC Mini turns this into an actual desk: faders to
  layer intensities, pads to cue GO and flash.
- **Timecode.** For shows programmed to a specific track, drive cues from MPRIS
  playback position (`playerctl position`) rather than live analysis — far more
  reliable for hitting a drop on cue than hoping the onset detector agrees.
- **Keyboard as a matrix.** The 126 keys have a `matrix_map`; right now they're
  treated as a 1D strip.
