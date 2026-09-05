# Musical lighting validation

Validated offline with the regenerated corpus, `auto`, 60 FPS, default output
settings, and simulated fast/slow device cadence. The full reproduction commands
and annotation format are in the [README](../README.md#benchmarks).

| Fixture | Primary attack recall | Mean beat confidence | Mean beat phase error (cycles) | Loud/quiet output ratio |
| --- | ---: | ---: | ---: | ---: |
| Dynamic kick/snare | 98.0% | 0.842 | 0.0037 | 8.82 |
| Dense percussion | 95.7% | 0.502 | 0.0083 | not annotated |
| Ballad | 50.0% | 0.005 | no confident grid | 18.76 |
| Tempo change / breakdown | 96.4% | 0.768 | 0.0626 | not annotated |
| Sustained vibrato / ticks | 100% | 0.000 | no confident grid | 21.66 |

Brightness follow-up: the first implementation applied several successive
brightness reductions. The drive multiplier has been removed, mixed chroma is
normalized before intensity is applied, and Auto blends approximate emitted
light without applying scene coverage a second time. Mean loud-section output
is now 2.26x higher on the dynamic fixture, 2.35x on the ballad, and 2.93x on
sustained material, compared with that initial implementation. These are
post-conversion channel averages, not physical photometric measurements.
All five default-setting corpus runs still have zero clipped output, and
annotated silence remains black. All 39 regression tests pass.

The dynamic fixture also annotates fine percussion: 101 of 102 distinct attacks
were matched, with no unmatched detections. The dense fixture deliberately
buries many hats in noise: only 67 of 228 total annotated attacks are detected.
Its primary recall is reported separately rather than concealing that limitation.

The ballad produces 16 detections, versus 66 in the original reviewed analyzer.
Only two of its four annotated ticks match; fourteen detections remain unmatched.
The arrangement director does not trust these as a beat, and Rain applies
stricter, local accent gating. This fixture remains a useful regression case,
not evidence of solved vocal or instrument recognition.

There was no clipped output in these default-setting runs. Both explicitly
annotated silence cases reached zero output after their release allowance.
The sustained-vibrato fixture had no unannotated detections. First beat lock
occurred after 3.6 seconds on the dynamic fixture and 6.2 on dense percussion.
The abrupt 120-to-100 BPM change recovered a stable tempo in approximately
5.6 seconds; phase recovery is also covered by a regression test.

Matched attack timestamp error at the 95th percentile ranged from about 1 to
16 ms. Scheduled render delivery at the 95th percentile ranged from about 33
to 57 ms after the annotated attack. These are different measurements: attack
time is retrospectively estimated by a causal detector; the event becomes
available later. Neither measurement includes physical capture/output latency.

The regression suite covers signal leakage, silence, weak input, vibrato,
non-finite samples, fixed-rate processing, event coalescing/deduplication,
capture bursts and stalls, offset/EOF draining, beat fills and tempo changes,
scene stability, local accents, particle scheduling, output headroom, zero
master, and annotation-aware benchmark scoring. UI JavaScript parsing, Python
compilation, and static undefined/unused-name checks also pass.

No real-music recordings were supplied during implementation, and no subjective
listening comparison or physical LED latency measurement was performed. The
corpus loader and feature traces support those checks without adding recordings
to the repository. Automatic tracking is limited to a 55–180 BPM search and a
confidence-gated four-beat bar hypothesis; half/double-time ambiguity and rubato
can still require manual calibration or a future tempo override.

### Direct-look rhythmic range (2026-09-05)

Duotone, spectrum, and rain now use a smoothed attack-density response, gated by
relative dynamics and presence, independently of beat confidence and swell.
This addresses steady percussion staying restrained after loudness adaptation.
Auto's mixing policy was not changed for this pass; its component looks inherit
the new response.

All 44 regression tests pass. New cases cover steady syncopated material reaching
full expansion, sustained loudness not creating rhythmic activity, motif renewal
on 16-beat boundaries, immediate local rain splashes, and spectrum accent contrast
above a bright wash. Static checks and `git diff --check` also pass.

Offline 60 FPS rendering of `hard.wav` and `sustained.wav` through all three looks
produced finite, bounded RGB, no encoded channels at 255, and black output in the
sustained fixture's final silence (after 14 seconds). Median rhythmic intensity
after seven seconds was 0.943 on the hard fixture and 0.000 on the sustained
fixture. These synthetic checks establish response separation and output bounds;
they do not establish how the effects feel on physical lights or real recordings.

### Prism and breakdown contrast

Prism adds coordinated two-tone movement and spaced palette changes to the
spectral response. Relative dynamics and swell reduce its activity promptly in
breakdowns, independently of lingering attack density. Full-energy spectral
brightness is retained at equivalent spatial phase; colour mixing does not add
an unintended dimmer.

The final suite has 49 passing tests, including colour movement, palette-change
qualification, silence, UI registration, and breakdown-to-drop recovery. The UI
JavaScript check passes. Focused static checks on the effects and tests pass;
a broader check also reports two pre-existing unused names in `__main__.py`
(`default_monitor` and `given`). Physical listening feedback remains necessary.
