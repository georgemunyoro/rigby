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
uv run rigby                                   # spectrum look, taps your default sink
uv run rigby --look chase --no-audio           # rig check, no audio needed
uv run rigby --palette cyanmag --master 0.6
uv run rigby --source file:track.mp3           # program a show against a known track
```

Ctrl-C blacks out and exits.

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
| `--curve` | brightness curve, default `0.55`. Below 1 lifts the low end. `0.4` is very punchy, `1.0` is linear and looks dead. |
| `--gain` | plain pre-curve multiplier on control signals |
| `--master` | grand master, scales everything at the output |

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
mobo      4 led   hid 60fps    Aura Mainboard
truss_l  18 led   hid 60fps    Aura Addressable 1
truss_r  18 led   hid 60fps    Aura Addressable 2
ram_a     8 led   i2c 12fps    ENE DRAM
ram_b     8 led   i2c 12fps    ENE DRAM
gpu       1 led   i2c 12fps    Palit RTX 3080
kbd     126 led   hid 60fps    EVision / Redragon Mitra (USB only)
```

**Per-device tick rates are load-bearing.** DRAM and the GPU sit on SMBus/i2c at
~100kHz. Driving them at 60fps makes the bus the bottleneck and stutters the
whole rig, so they're marked `slow` in `PATCH_SPEC` and get ~12fps plus a dirty
check. Keep them as wash fixtures; put detail on the HID devices.

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
