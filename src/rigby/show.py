"""Looks: the layered thing an operator actually builds.

A look is base wash + movement + hits, merged HTP, exactly like stacking
playbacks on a desk. Add new ones by writing a function and registering it in
LOOKS -- they all get the same (fixtures, features, phase) contract.
"""

from __future__ import annotations

import collections

import numpy as np

from . import fx
from .analyze import Features, Onset
from .music import Director, alpha
from .patch import Fixture


class Look:
    """Base class. Subclasses fill in render()."""

    name = "look"

    # A full turn of the wheel in ~5 minutes: slow enough that you never catch
    # it moving, fast enough that a track doesn't end on the colour it started.
    HUE_DRIFT = 1.0 / 300.0
    SEP_DRIFT = 1.0 / 170.0

    def __init__(self, fixtures: dict[str, Fixture], palette: str = "sunset",
                 gain: float = 1.6, curve: float = 0.45, duo: str = "ember",
                 hue_drift: float = 1.0, hit_style: str = "swing",
                 swing_min_beats: int = 6, saturation: float = 0.88,
                 hot: float = 0.5):
        self.fixtures = fixtures
        self.palette = palette
        self.gain = gain
        self.curve = curve
        self.duo = duo
        self._h_base, self._sep, self._acc = fx.DUOS.get(duo, fx.DUOS["ember"])
        self._hue_t = 0.0
        self._sep_t = 0.0
        self._drift = hue_drift
        self.hit_style = hit_style
        # A colour swing only has impact if it's rare. Ordinary beats get the
        # ordinary flash; the swing is saved for the hits that earn it.
        self.swing_min_beats = max(1, int(swing_min_beats))
        self.swing_max_beats = self.swing_min_beats * 4
        self._impacts: collections.deque[float] = collections.deque(maxlen=32)
        self._since_swing = 999
        self._swing_now = False
        self.saturation = saturation
        self.hot = hot
        self.t = 0.0          # global time in turns
        self.chase = 0.0      # chase phase in turns
        self._motion = 0.0
        self._energy = 0.0    # rhythmic activity, independent of relative swell
        self._hit = 0.0       # onset flash envelope
        self._lvl = 0.0
        self.director = Director()
        self._music_beats = 0.0
        self._events = ()
        self._event = None
        self._low_hit = self._mid_hit = self._high_hit = 0.0

    def tones(self) -> tuple[float, float, float]:
        """Current (primary, secondary, accent) hues.

        The separation breathes as well as the base drifting, so the pair keeps
        changing character -- near-complementary at one moment, a tighter
        analogous pair a minute later -- without ever collapsing together.
        """
        base = (self._h_base + self._hue_t) % 1.0
        wob = 0.5 + 0.5 * np.sin(2 * np.pi * self._sep_t)
        sep = self._sep * (0.78 + 0.34 * wob)
        return base, (base + sep) % 1.0, (base + self._acc) % 1.0

    def step(self, dt: float, f: Features) -> None:
        self._lvl += (f.level - self._lvl) * alpha(dt, .4)
        # Density measures accepted attacks, not tempo confidence: syncopated
        # drums should still have weight when the beat clock cannot lock.
        activity = np.clip((f.density - .12) / .30, 0., 1.)
        activity *= np.clip((f.dynamics - .35) / .45, 0., 1.) * f.presence
        self._energy += (activity - self._energy) * alpha(dt, .65 if activity > self._energy else 1.2)
        self.director.step(dt, f, energy=self._energy)
        self._hue_t += dt * self.HUE_DRIFT * self._drift
        self._sep_t += dt * self.SEP_DRIFT * self._drift
        self.t += dt * .015
        tempo = f.bpm / 60 if f.bpm and f.pulse > .2 else .4
        self._music_beats += dt * tempo
        if f.pulse > .4:
            error = (f.beat_position - self._music_beats + 2) % 4 - 2
            self._music_beats += float(np.clip(error * alpha(dt, .5), -dt * .4, dt * .4))
        self.chase = self._music_beats / 4
        self._motion += dt * tempo / 4 * (1. + 3. * self._energy)
        self._since_swing += dt * tempo
        events = f.events
        if not events and f.onset:   # supports hand-built feature streams
            events = (Onset(f.timestamp, f.onset_strength, .7, 'low'),)
        self._events = tuple(e for e in events if e.confidence >= .45 and f.presence > .3
                             and f.timestamp - e.time <= .15)
        self._event = max(self._events, key=lambda e: e.strength * e.confidence
                          * (0.4 if e.kind == 'high' else 1.), default=None)
        for kind, tau in (('low', .22), ('mid', .12), ('high', .07)):
            name = '_' + kind + '_hit'
            hit = max((self._impact(e, f, tau)
                       for e in self._events if e.kind == kind), default=0.)
            setattr(self, name, max(getattr(self, name) * np.exp(-dt / tau), hit))
        self._hit = max(self._low_hit, self._mid_hit * .75, self._high_hit * .25)

    def _impact(self, event, f, tau):
        # Preserve the detector's first 40 ms of latency without replaying old
        # hits. Confidence already gates events; avoid squaring its attenuation.
        age = max(0., f.timestamp - event.time - .04)
        return min(1., event.strength * event.confidence
                   * (1. + 1.6 * self._energy)) * np.exp(-age / tau)

    @staticmethod
    def _hit_scale(f: Features) -> float:
        """Flashes still land in quiet passages, just not at full power."""
        return 0.35 + 0.65 * f.dynamics

    def note_onset(self, f: Features) -> None:
        """Decide whether this beat is one of the ones that swings colour.

        A fixed threshold doesn't work across material: on a track with varied
        hits the top decile of impact is ~0.95, on a uniform four-to-the-floor
        it's ~0.55, so any constant either swings on everything or on nothing.
        The test is relative -- beat this well against its own recent
        neighbours -- with a floor on spacing so it stays an event, and a
        ceiling so it doesn't vanish entirely on very even material.
        """
        impact = f.onset_strength * (0.3 + 0.7 * f.dynamics)
        self._impacts.append(impact)

        if self._since_swing < self.swing_min_beats:
            self._swing_now = False
            return
        if self._since_swing >= self.swing_max_beats:
            self._swing_now = True
            self._since_swing = 0
            return

        thresh = (float(np.percentile(self._impacts, 70))
                  if len(self._impacts) >= 8 else 0.6)
        self._swing_now = impact >= thresh
        if self._swing_now:
            self._since_swing = 0

    def hit_hue(self, h1: float, h2: float, hacc: float, beat: int) -> float:
        """Hue a beat swings to: opposite the pair's midpoint.

        Taking the complement of the primary looks right on paper but lands on
        top of the secondary, because the two tones are already about half a
        turn apart -- the "swing" then just swaps the two colours over.
        Opposite the midpoint is the furthest point from *both*, which is a
        genuine third colour. With the pair spanning ~0.5 turns that is ~0.25
        from each, and no hue can do better than that.
        """
        if self.hit_style == "accent":
            return hacc
        sep = ((h2 - h1) + 1.0) % 1.0
        mid = (h1 + sep / 2.0) % 1.0
        # Alternate a little either side so consecutive beats aren't identical.
        return (mid + 0.5 + (0.09 if beat % 2 else -0.09)) % 1.0

    def apply_hit(self, rgb, mask, h1, h2, hacc, beat, lum_scale=1.0):
        """Swing the masked region to the opposite of the wheel.

        This *replaces* rather than adds. Adding a complementary hue on top of
        an existing one just sums to white, which is the thing we're trying to
        get away from -- the old accent-on-top flash trended white no matter
        which accent hue it used.
        """
        # Ordinary beats keep the plain flash; only the ones that earned it
        # swing the colour over.
        if self.hit_style == "white" or not self._swing_now:
            sat = 0.0 if self.hit_style == "white" else .8
            return fx.mix_layers(rgb, self.col(hacc, mask * lum_scale, sat=sat), ceiling=1.0)

        # Colour weight and brightness are separate: the hue swings all the way
        # over even in a quiet passage, and `lum_scale` decides how hard it
        # lands. Folding the two together only ever half-blends the hue, which
        # lands somewhere between the two tones and reads as neither.
        w = np.clip(mask, 0.0, 1.0)[:, None]
        lum = np.maximum(rgb.max(axis=1),
                         0.75 * np.clip(mask, 0, 1) * lum_scale)
        swung = self.col(self.hit_hue(h1, h2, hacc, beat), lum)
        return rgb * (1.0 - w) + swung * w

    HOT_KNEE = 0.5   # above this intensity the colour starts running hot

    def drive(self, x):
        """Every look pushes its brightness through here before col()."""
        return fx.drive(x, self.gain, self.curve)

    def col(self, h, v, sat: float = 1.0):
        """Colour with a hot core: saturation falls as intensity rises.

        Flat full saturation is why animation is hard to read on a 6-LED fan.
        A saturated hue carries very little luminance at the dim end, so an
        arc's falloff disappears and all you see is one lit LED jumping from
        place to place instead of a light sweeping round. Letting the bright
        core desaturate towards white gives the eye a highlight to track and
        keeps the coloured tail visible behind it -- which is how a real
        fixture behaves.
        """
        v = np.atleast_1d(np.asarray(v, dtype=np.float32))
        heat = np.clip((v - self.HOT_KNEE) / max(1.0 - self.HOT_KNEE, 1e-6),
                       0.0, 1.0)
        s = sat * self.saturation * (1.0 - self.hot * heat)
        return fx.hsv(h, s, v)

    def render(self, f: Features) -> dict[str, np.ndarray]:
        raise NotImplementedError


class Spectrum(Look):
    """A spectral wash with moving space and local percussive highlights."""

    name = "spectrum"

    def _spectral_motion(self):
        return self._motion

    def _spectral_energy(self):
        return self._energy

    def render(self, f: Features) -> dict[str, np.ndarray]:
        out: dict[str, np.ndarray] = {}
        nb = len(f.bands)

        for key, fix in self.fixtures.items():
            if key in ("mobo", "ram_a", "ram_b", "gpu"):
                continue

            # The wash comes off the SLOW envelope and is smeared across
            # neighbouring LEDs; the fast envelope only adds a little accent on
            # top. Driving the wash from the fast envelope is what made the
            # between-beat picture look haywire.
            # The slow envelope lags transients and so reads lower than the
            # fast one; lift it back up rather than losing overall brightness.
            b = np.interp(fix.pos, np.linspace(0, 1, nb), f.bands_slow)
            b = fx.blur(b, 2)
            accent = (b if fix.slow else np.interp(fix.pos, np.linspace(0, 1, nb), f.bands))
            accent = fx.blur(accent, 1)

            # Movement layer: a travelling bump so it never sits still.
            mv = fx.wave("bump", self._spectral_motion(), fix.pos, spread=1.0, size=1.0)

            energy = 0. if fix.slow else self._spectral_energy()
            # Carve moving space into the wash, then let actual band attacks
            # fill it. More level alone does not flatten the whole fixture.
            b = b * (1. - .48 * energy * (1. - mv))
            transient = np.maximum(accent - b, 0.)
            v = fx.htp(b, accent * 0.45, mv * 0.30 * (0.3 + self._lvl))
            v += transient * energy * 1.2
            v = self.drive(v) * f.dynamics
            local_hit = (self._low_hit * (1-fix.pos) ** 2 + self._mid_hit
                         * np.exp(-((fix.pos-.5)/.18)**2) + self._high_hit * fix.pos**4 * .25)
            if not fix.slow:
                v = v + (1. - v) * np.clip(local_hit * (1. + energy), 0., 1.) * self._hit_scale(f) * f.presence

            hue = (fx.palette_hue(self.palette, self.t) + fix.pos * 0.22) % 1.0
            sat = np.clip(1.0 - self._hit * 0.25, 0.0, 1.0)
            out[key] = fx.hsv(hue, sat, v)

        if (fix := self.fixtures.get("mobo")) is not None:
            v = self.drive(np.full(fix.n, 0.18 + f.level * 0.82)) * f.dynamics
            v = np.maximum(v, self._low_hit * .45 * f.presence)
            hue = fx.palette_hue(self.palette, self.t + 0.25)
            out["mobo"] = fx.hsv(hue, 1.0 - self._hit * 0.8, v)

        # RAM sits on SMBus at 12fps -- give it slow bass wash, not detail.
        for key in ("ram_a", "ram_b"):
            if (fix := self.fixtures.get(key)) is None:
                continue
            sweep = fx.wave("sine", self.chase * 0.5, fix.pos, spread=0.5, size=1.0)
            v = self.drive(np.clip(0.10 + f.bass * 0.90 * (0.55 + sweep * 0.45),
                                   0, 1)) * f.dynamics
            hue = fx.palette_hue(self.palette, self.t + (0.0 if key == "ram_a" else 0.08))
            out[key] = fx.hsv(hue, 0.95, v)

        if (fix := self.fixtures.get("gpu")) is not None:
            hue = fx.palette_hue(self.palette, self.t + 0.5)
            out["gpu"] = fx.hsv(hue, 0.9,
                                self.drive(np.full(fix.n, 0.12 + f.level * 0.88))
                                * f.dynamics)

        return out


class Prism(Spectrum):
    """Spectrum's dynamics with travelling two-tone colour and larger swings."""

    name = "prism"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._colour_target = 0.0
        self._colour = 0.0
        self._slow_colour = 0.0
        self._since_colour = 999.0
        self._lift_armed = True
        self._engagement = 1.0
        self._prism_phase = 0.0

    def _spectral_motion(self):
        return self._prism_phase

    def _spectral_energy(self):
        return self._energy * self._engagement

    def step(self, dt, f):
        super().step(dt, f)
        # Density has a long tail after a busy section. Relative loudness
        # pulls back independently, before that history has time to decay.
        target = min(float(np.clip((f.dynamics - .4) / .4, 0., 1.)),
                     float(np.clip((f.swell - .28) / .24, 0., 1.))) * f.presence
        self._engagement += (target - self._engagement) * alpha(dt, .2 if target > self._engagement else .25)
        tempo = f.bpm / 60 if f.bpm and f.pulse > .2 else .4
        self._prism_phase += dt * tempo * (.005 + .075 * self._spectral_energy())
        self._since_colour += dt
        if f.energy_slope < .02:
            self._lift_armed = True
        lift = (self._lift_armed and f.energy_slope > .06
                and f.swell > .55 and f.presence > .3)
        accent = False
        if (self._event is not None and self._event.kind != 'high'
                and self._event.strength * self._event.confidence >= .6
                and f.dynamics > .65 and f.swell > .45):
            self.note_onset(f)
            accent = self._swing_now
        if (lift or accent) and self._since_colour >= 6.:
            # Move the entire colour family, then settle into it. A sustained
            # build earns one change, not a fresh palette every render frame.
            self._colour_target += .19
            self._since_colour = 0.0
            if lift:
                self._lift_armed = False
        self._colour += (self._colour_target - self._colour) * alpha(dt, .8)
        self._slow_colour += (self._colour_target - self._slow_colour) * alpha(dt, 1.1)

    def render(self, f):
        spectral = super().render(f)
        h1, h2, _ = self.tones()
        out = {}
        for key, fix in self.fixtures.items():
            # Retain headroom in breakdowns, without reducing the full-level
            # spectral response. A single slow colour field keeps the rig calm.
            v = spectral[key].max(axis=1) * (.65 + .35 * self._engagement)
            shift = self._slow_colour if fix.slow else self._colour
            pos = fix.angle if fix.is_ring else fix.pos
            phase = self._prism_phase * (.5 if fix.slow else fix.spin)
            ribbon = .5 + .5 * np.cos(2 * np.pi * (pos - phase))
            pair = (self.col((h1 + shift) % 1., v) * ribbon[:, None]
                    + self.col((h2 + shift) % 1., v) * (1. - ribbon[:, None]))
            hue = (fx.palette_hue(self.palette, self.t) + fix.pos * .10 + shift) % 1.
            base = self.col(hue, v)
            amount = (.10 if fix.slow else .08 + .20 * self._spectral_energy())
            rgb = base * (1. - amount) + pair * amount
            # Mixing complementary colours must not become a hidden dimmer.
            peak = rgb.max(axis=1)
            out[key] = rgb * (v / np.maximum(peak, 1e-9))[:, None]
        return out


class Chase(Look):
    """No spectrum -- pure movement. Useful for checking the rig with no audio."""

    name = "chase"

    def render(self, f: Features) -> dict[str, np.ndarray]:
        out: dict[str, np.ndarray] = {}
        for key, fix in self.fixtures.items():
            v = fx.wave("bump", self.chase, fix.pos, spread=1.0, size=1.0)
            v = np.maximum(v, 0.06)
            hue = (fx.palette_hue(self.palette, self.t) + fix.pos * 0.3) % 1.0
            out[key] = fx.hsv(hue, 1.0, v)
        return out


class _Gesture:
    """One way for a ring to move.

    A single behaviour plus a direction flip is still a single behaviour -- it
    reads as "swirling back and forth" and stops being interesting after about
    a minute. What keeps a rig alive is a vocabulary of gestures and something
    that changes between them on musical structure, the way an operator would
    change look at a phrase boundary rather than continuously.
    """

    NAMES = ("spin", "lobes", "pingpong", "breathe", "wipe", "sparkle",
             "converge")

    def __init__(self, name: str, rng: np.random.Generator, n_rings: int):
        self.name = name
        self.rate = float(rng.choice([.5, 1., 2.]))
        self.width = float(rng.uniform(0.13, 0.24))
        self.lobes = int(rng.integers(2, 4))
        self.dir = 1.0 if rng.random() < 0.5 else -1.0
        self.spread = float(rng.choice([0.0, 1.0 / max(n_rings, 1), 0.5]))
        self._seed = int(rng.integers(0, 1 << 30))

    def field(self, ang, phase, t, idx, lo, hi, expansion=.5, low_hit=0.):
        """Returns (tone0, tone1) intensity across the ring."""
        ph = phase * self.rate * self.dir + idx * self.spread
        # On a 6-LED fan a 0.15-turn arc is barely one LED, which reads as
        # blinking rather than sweeping. Never let an arc span less than about
        # one and a half LEDs.
        w = max(self.width * (.7 + expansion * .8 + low_hit * .4), 1.5 / max(ang.size, 1))

        if self.name == "spin":
            return (fx.arc(ang, ph, w, 1.3) * (0.35 + 0.65 * lo),
                    fx.arc(ang, ph + 0.5, w * 0.85, 1.3) * (0.25 + 0.6 * hi))

        if self.name == "lobes":
            a = np.zeros_like(ang)
            b = np.zeros_like(ang)
            for k in range(self.lobes):
                c = ph + k / self.lobes
                a = np.maximum(a, fx.arc(ang, c, w * 0.8, 1.4))
                b = np.maximum(b, fx.arc(ang, c + 0.5 / self.lobes, w * 0.55, 1.4))
            return a * (0.3 + 0.7 * lo), b * (0.2 + 0.6 * hi)

        if self.name == "pingpong":
            # Sweeps a limited arc instead of going all the way round.
            c = 0.25 * np.sin(2 * np.pi * ph)
            return (fx.arc(ang, c, w, 1.2) * (0.35 + 0.65 * lo),
                    fx.arc(ang, -c, w * 0.8, 1.2) * (0.25 + 0.6 * hi))

        if self.name == "breathe":
            # No rotation at all: the ring holds two static halves and pulses.
            g = 0.5 + 0.5 * np.sin(2 * np.pi * ph)
            return (fx.half(ang, 0.0, 0.16) * (0.25 + 0.75 * lo) * g,
                    fx.half(ang, 0.5, 0.16) * (0.2 + 0.7 * hi) * (1.0 - 0.6 * g))

        if self.name == "wipe":
            # A filled arc grows from a point, then snaps back and refills.
            grow = (ph % 1.0)
            d = np.abs(fx.wrap_delta(ang, 0.0))
            m = np.clip((grow * 0.5 - d) * 8.0, 0.0, 1.0)
            return (m * (0.3 + 0.7 * lo),
                    (1.0 - m) * (0.15 + 0.5 * hi))

        if self.name == "sparkle":
            # Deterministic per-LED twinkle: no particles, just phase offsets.
            off = ((np.arange(ang.size) * 2654435761 + self._seed) % 1000) / 1000.0
            v = 0.5 + 0.5 * np.sin(2 * np.pi * (ph * 2.0 + off))
            v = np.clip(v, 0, 1) ** 3.0
            return v * (0.3 + 0.7 * lo), (1.0 - v) * 0.5 * (0.2 + 0.6 * hi)

        # converge: two arcs walk toward each other and apart again
        c = 0.25 * (0.5 + 0.5 * np.sin(2 * np.pi * ph))
        return (fx.arc(ang, c, w, 1.3) * (0.35 + 0.65 * lo),
                fx.arc(ang, -c, w, 1.3) * (0.3 + 0.6 * hi))


class Duotone(Look):
    """Two rotating tones per ring, with beats as events rather than flashes.

    The point of this look is that the movement is *intrinsic* -- the rings
    rotate whether or not anything is playing, and the music modulates that
    rotation. Amplitude-drives-brightness is what makes a rig read as a
    visualiser; phase relationships between fixtures are what make it read as
    a lighting rig.

    Three things carry that here:
      * counter-rotation -- fan_b and the AIO spin against the others, so three
        identical rings stop reading as one object,
      * a phase spread of a third of a turn per fan, straight off a desk,
      * beats land on *one* ring's *half*, cycling round, while everything else
        keeps running. A global flash is the thing that gets boring fastest.
    """

    name = "duotone"

    def __init__(self, fixtures, palette="sunset", gain=1.6, curve=0.45,
                 duo="ember", hue_drift=1.0, hit_style="swing",
                 swing_min_beats=6, saturation=0.88, hot=0.5):
        super().__init__(fixtures, palette=palette, gain=gain, curve=curve,
                         duo=duo, hue_drift=hue_drift, hit_style=hit_style,
                         swing_min_beats=swing_min_beats,
                         saturation=saturation, hot=hot)
        self.spin = 0.0
        self.dir = 1.0
        self.beat = 0
        self._flash = 0.0
        self._flash_key = None
        self._flash_centre = 0.0
        self._flash_kind = "low"
        self._rings = [k for k, f in fixtures.items() if f.is_ring]
        self._rng = np.random.default_rng(20260821)
        self._g = _Gesture("spin", self._rng, len(self._rings))
        self._g_prev = self._g
        self._xf = 1.0          # crossfade into the current gesture
        self._since_change = 0.0
        self._phrase = -1

    XFADE_S = .8

    def _new_gesture(self):
        vocabulary = {
            'sparse': ('breathe', 'converge'),
            'groove': ('spin', 'pingpong', 'lobes'),
            'build': ('converge', 'wipe'),
            'full': ('lobes', 'spin', 'sparkle'),
        }
        choices = [name for name in vocabulary[self.director.scene] if name != self._g.name]
        name = str(self._rng.choice(choices or vocabulary[self.director.scene]))
        self._g_prev, self._g = self._g, _Gesture(name, self._rng, len(self._rings))
        if self._energy > .35:
            self._g.rate = float(self._rng.choice([2., 4.]))
            self._g.width *= .75
        self._xf = 0.0
        self._since_change = 0.0

    def step(self, dt, f):
        super().step(dt, f)
        self.spin = self.chase
        self.beat = int(np.floor(f.beat_position))
        self._since_change += dt
        fade = self.XFADE_S * (1. - .6 * self._energy)
        self._xf = min(1., self._xf + dt / fade)
        phrase = int(np.floor((f.beat_position - f.bar_offset) / 16))
        refresh = (self._energy > .35 and self._since_change >= 6.
                   and ((f.pulse > .4 and phrase != self._phrase)
                        or (f.pulse <= .4 and self._since_change >= 12.)))
        self._phrase = phrase
        if self.director.changed or refresh:
            self._new_gesture()
        self._flash *= np.exp(-dt / .15)
        if self._event:
            self.note_onset(f)
            event = self._event
            self._flash = max(self._flash, self._impact(event, f, .15))
            if self._rings:
                self._flash_key = self._rings[(int(np.floor(self._music_beats * 2))
                                                + (1 if event.kind == "mid" else 0)) % len(self._rings)]
                self._flash_centre = (0.0 if event.kind == 'low' else
                                      .5 if event.kind == 'mid' else .25)
            self._flash_kind = event.kind

    def _ring_rgb(self, key, fix, f) -> np.ndarray:
        ang = fix.angle
        idx = self._rings.index(key) if key in self._rings else 0
        phase = self.spin * fix.spin + idx / max(len(self._rings), 1)

        nb = len(f.bands_slow)
        # Each ring listens to a different part of the spectrum, so they move
        # independently instead of pumping in unison.
        lo = float(f.bands_slow[min(idx, nb - 1)])
        hi = float(f.bands_slow[min(nb - 1 - idx, nb - 1)])

        a1, a2 = self._g.field(ang, phase, self._since_change, idx, lo, hi, self.director.expansion, self._low_hit)
        if self._xf < 1.0:
            p1, p2 = self._g_prev.field(ang, phase, self._since_change, idx,
                                        lo, hi, self.director.expansion, self._low_hit)
            a1 = a1 * self._xf + p1 * (1.0 - self._xf)
            a2 = a2 * self._xf + p2 * (1.0 - self._xf)

        h1, h2, hacc = self.tones()
        # Expansion recruits fixtures gradually; a sparse scene keeps dark space.
        coverage = np.clip(max(self.director.expansion, self._energy) * len(self._rings) - idx + 1, .15, 1.)
        a1 *= coverage
        a2 *= coverage
        rgb = fx.mix_layers(self.col(h1, self.drive(a1)),
                            self.col(h2, self.drive(a2), sat=.95))

        if self._flash > 0.01 and key == self._flash_key and not fix.slow:
            m = (fx.arc(ang, self._flash_centre, .12) * .3 if self._flash_kind == 'high'
                 else fx.half(ang, self._flash_centre)) * self._flash
            rgb = self.apply_hit(rgb, m, h1, h2, hacc, self.beat,
                                 lum_scale=self._hit_scale(f))

        return np.clip(rgb * f.dynamics, 0.0, 1.0)

    def _line_rgb(self, key, fix, f) -> np.ndarray:
        # Lines get the same two tones split along their length, with a slow
        # travelling seam so they aren't static blocks.
        seam = (self.spin * 0.5) % 1.0
        d = np.abs(fx.wrap_delta(fix.pos * 0.5, seam))
        mix = np.clip(d * 4.0, 0.0, 1.0)
        wash = np.interp(fix.pos, np.linspace(0, 1, len(f.bands_slow)),
                         f.bands_slow)
        wash = fx.blur(wash, 1) if fix.n > 3 else wash
        h1, h2, hacc = self.tones()
        v = self.drive(np.clip(0.14 + wash * 0.9, 0, 1))
        rgb = fx.mix_layers(self.col(h1, v * (1.0 - mix)),
                            self.col(h2, v * mix, sat=.95))
        if self._flash > .01 and not fix.slow and self._flash_kind == 'mid':
            mask = np.exp(-((fix.pos-.5)/.2)**2) * self._flash * .35
            rgb = fx.mix_layers(rgb, self.col(hacc, mask * self._hit_scale(f)), ceiling=1.)
        return np.clip(rgb * f.dynamics, 0.0, 1.0)

    def render(self, f: Features) -> dict[str, np.ndarray]:
        out = {}
        for key, fix in self.fixtures.items():
            out[key] = (self._ring_rgb(key, fix, f) if fix.is_ring
                        else self._line_rgb(key, fix, f))
        return out




class _Drops:
    """A small particle pool: sparse points that appear and fade in place.

    Rain is the one pattern that reads as *intentional* when the music has no
    beat to hang anything off, because its structure comes from sparseness
    rather than from timing. Density and brightness are what respond, so a
    verse is a few scattered points and a chorus fills in.
    """

    ATTACK_S = 0.13   # a drop that appears at full brightness in one frame
    TAU_S = 0.62      # is a pop, not a raindrop
    CULL_S = 2.6

    def __init__(self, n: int, seed: int, wrap: bool):
        self.n = n
        self.wrap = wrap
        self.rng = np.random.default_rng(seed)
        self.pos = np.zeros(0, dtype=np.float32)
        self.amp = np.zeros(0, dtype=np.float32)
        self.age = np.zeros(0, dtype=np.float32)
        self.hjit = np.zeros(0, dtype=np.float32)
        self.tone = np.zeros(0, dtype=np.int8)
        self.accent_hue = np.zeros(0, dtype=np.float32)
        self.accent_sat = np.zeros(0, dtype=np.float32)
        self.punch = np.zeros(0, dtype=np.float32)
        self._idx = np.arange(n, dtype=np.float32)
        self._next = float(self.rng.exponential(1.0))
        # Peak of the attack/decay envelope, so `amp` still means peak height.
        a = np.linspace(0.0, self.CULL_S, 512)
        self._norm = float(((1 - np.exp(-a / self.ATTACK_S))
                            * np.exp(-a / self.TAU_S)).max())

    def _env(self, age):
        return ((1.0 - np.exp(-age / self.ATTACK_S))
                * np.exp(-age / self.TAU_S)) / self._norm

    def spawn(self, amp: float, tone: int | None = None, hue=0., age=0., sat=.9, punch=0.) -> None:
        self.pos = np.append(self.pos, self.rng.random() * self.n)
        self.amp = np.append(self.amp, amp)
        self.age = np.append(self.age, age)
        # A little hue wander per drop, so a shower isn't one flat colour.
        self.hjit = np.append(self.hjit, self.rng.normal(0.0, 0.028))
        t = self.rng.integers(0, 2) if tone is None else tone
        self.tone = np.append(self.tone, np.int8(t))
        self.accent_hue = np.append(self.accent_hue, hue)
        self.accent_sat = np.append(self.accent_sat, sat)
        self.punch = np.append(self.punch, punch)

    def step(self, dt: float, rate: float, amp: float) -> None:
        self.age += dt
        if rate > 0:
            self._next -= dt * rate
            while self._next <= 0:
                age = -self._next / rate
                self.spawn(amp * float(self.rng.uniform(.55, 1.)), age=age)
                self._next += float(self.rng.exponential(1.))
        keep = self.age < self.CULL_S
        self.pos, self.amp, self.age, self.tone, self.hjit, self.accent_hue, self.accent_sat, self.punch = (
            a[keep] for a in (self.pos, self.amp, self.age, self.tone, self.hjit, self.accent_hue, self.accent_sat, self.punch))

    def render(self, width: float = 0.85, gain=1.6, curve=.45, saturation=.88, hot=.5):
        """Returns (tone0, tone1, tone2, hue_offset) fields."""
        out = [np.zeros(self.n, dtype=np.float32),
               np.zeros(self.n, dtype=np.float32),
               np.zeros(self.n, dtype=np.float32)]
        hue = np.zeros(self.n, dtype=np.float32)
        accent = np.zeros((self.n, 3), dtype=np.float32)
        if self.pos.size:
            d = np.abs(self._idx[None, :] - self.pos[:, None])
            if self.wrap:
                d = np.minimum(d, self.n - d)
            envelope = ((1. - self.punch) * self._env(self.age)
                        + self.punch * np.exp(-self.age / .18))
            g = (np.exp(-(d ** 2) / (2.0 * width ** 2))
                 * (self.amp * envelope)[:, None])
            for t in (0, 1, 2):
                m = self.tone == t
                if m.any():
                    out[t] = g[m].sum(axis=0).astype(np.float32)
            for j in np.flatnonzero(self.tone == 2):
                v = fx.drive(g[j], gain, curve)
                heat = np.clip((v - .5) / .5, 0, 1)
                sat = self.accent_sat[j] * saturation * (1-hot*heat)
                accent = fx.mix_layers(accent, fx.hsv(self.accent_hue[j], sat, v), ceiling=1.)
            tot = g.sum(axis=0)
            hue = np.where(tot > 1e-6,
                           (g * self.hjit[:, None]).sum(axis=0) / np.maximum(tot, 1e-6),
                           0.0).astype(np.float32)
        return out[0], out[1], accent, hue


class Rain(Look):
    """Gentle swell-driven drops with sharp splashes on percussive material.

    Density supplies rhythmic intensity even when syncopation prevents a beat
    lock. Sustained material keeps the slower attack and decay of ordinary rain.
    """

    name = "rain"

    BASE_RATE = 0.45     # drops/sec/fixture at rest
    PEAK_RATE = 16.0     # ...and at full swell
    # `swell` sits near 0.5 at a song's average level, so the rate curve has to
    # be shifted and steep: at mid-swell this must be a handful of scattered
    # points, not a wash. Without the shift a quiet verse lights every LED.
    SWELL_KNEE = 0.38
    SWELL_EXP = 2.2

    def __init__(self, fixtures, palette="sunset", gain=1.6, curve=0.45,
                 duo="ember", hue_drift=1.0, hit_style="swing",
                 swing_min_beats=6, saturation=0.88, hot=0.5):
        super().__init__(fixtures, palette=palette, gain=gain, curve=curve,
                         duo=duo, hue_drift=hue_drift, hit_style=hit_style,
                         swing_min_beats=swing_min_beats,
                         saturation=saturation, hot=hot)
        self.drops = {k: _Drops(f.n, seed=i * 977 + 13, wrap=f.is_ring)
                      for i, (k, f) in enumerate(fixtures.items())}
        self._sw = 0.0
        self._beats = 0

    def step(self, dt: float, f: Features) -> None:
        super().step(dt, f)
        self._sw += (f.swell - self._sw) * alpha(dt, .25)
        self._beats = int(np.floor(f.beat_position))
        s = self._eff(self._sw)
        rate = (self.BASE_RATE + s * self.PEAK_RATE) * f.presence
        amp = .30 + .55 * s
        for key, d in self.drops.items():
            d.step(dt, rate * (.45 if self.fixtures[key].slow else 1.), amp)
        # One local accent, with strong evidence when there is no tracked rhythm.
        event = self._event
        if event and (self._energy > .3 or f.pulse > .4 or (event.confidence >= .9 and event.strength >= .6)):
            keys = [k for k, fix in self.fixtures.items() if not fix.slow]
            if keys:
                self.note_onset(f)
                h1, h2, hacc = self.tones()
                hue = self.hit_hue(h1, h2, hacc, self._beats) if self._swing_now else hacc
                index = int(np.floor(self._music_beats * 2)) + (1 if event.kind == 'mid' else 0)
                # A kick splashes across a pair on energetic material. Hats
                # stay small and local; slow bus fixtures retain their wash.
                count = 2 if self._energy > .6 and event.kind == 'low' else 1
                for offset in range(min(count, len(keys))):
                    key = keys[(index + offset) % len(keys)]
                    impact = self._impact(event, f, .18)
                    self.drops[key].spawn((amp + .65 * self._energy) * impact
                                          * (.5 if event.kind == 'high' else 1.),
                                          tone=2, hue=hue, punch=self._energy,
                                          sat=0. if self.hit_style == 'white' else .9)

    def _eff(self, sw: float) -> float:
        """Swell shaped for density: flat until the music actually lifts."""
        x = (sw - self.SWELL_KNEE) / max(1.0 - self.SWELL_KNEE, 1e-6)
        return float(np.clip(x, 0.0, 1.0)) ** self.SWELL_EXP

    def render(self, f: Features) -> dict[str, np.ndarray]:
        out: dict[str, np.ndarray] = {}
        s = self._eff(self._sw)
        # As the swell rises the gaps fill in, so it crossfades from discrete
        # drops to a continuous glow without a mode change.
        ambient = (s ** 1.8) * 0.34
        h1, h2, hacc = self.tones()
        for key, fixt in self.fixtures.items():
            # Wider blobs on the coarse 6-LED fan rings, or a drop is just one
            # LED blinking on and off.
            w = 0.7 if fixt.n >= 12 else 1.05
            t0, t1, accent, hj = self.drops[key].render(width=w, gain=self.gain, curve=self.curve,
                                                            saturation=self.saturation, hot=self.hot)
            v0 = self.drive(np.clip(t0 + ambient, 0, 1))
            v1 = self.drive(np.clip(t1 + ambient * 0.6, 0, 1))
            rgb = fx.mix_layers(self.col((h1 + hj) % 1.0, v0, sat=.95),
                                self.col((h2 + hj) % 1.0, v1, sat=.90))
            rgb = fx.mix_layers(rgb, accent, ceiling=1.)
            out[key] = np.clip(rgb * f.dynamics, 0.0, 1.0)
        return out


class Auto(Look):
    """Crossfade between beat-driven and swell-driven by beat confidence.

    Rather than classifying a track and switching, both looks run and `pulse`
    mixes them. A song that drifts in and out of having a beat drifts between
    the two, and nothing ever snaps.
    """

    name = "auto"

    def __init__(self, fixtures, palette="sunset", gain=1.6, curve=0.45,
                 duo="ember", hue_drift=1.0, hit_style="swing",
                 swing_min_beats=6, saturation=0.88, hot=0.5):
        super().__init__(fixtures, palette=palette, gain=gain, curve=curve,
                         duo=duo, hue_drift=hue_drift, hit_style=hit_style,
                         swing_min_beats=swing_min_beats,
                         saturation=saturation, hot=hot)
        self.beaty = Duotone(fixtures, palette, gain, curve, duo, hue_drift,
                             hit_style, swing_min_beats, saturation, hot)
        self.calm = Rain(fixtures, palette, gain, curve, duo, hue_drift,
                         hit_style, swing_min_beats, saturation, hot)
        self.mix = 0.0

    def step(self, dt: float, f: Features) -> None:
        self.beaty.step(dt, f)
        self.calm.step(dt, f)
        self.director = self.beaty.director
        rhythmic = float(np.clip((f.pulse - .25) / .5, 0, 1))
        # The director already controls coverage inside Duotone. Applying it
        # again here dims the entire show, even with a confidently tracked beat.
        target = rhythmic
        self.mix += (target - self.mix) * alpha(dt, .8)

    def render(self, f: Features) -> dict[str, np.ndarray]:
        w = float(np.clip(self.mix, 0.0, 1.0))
        a = self.beaty.render(f)
        b = self.calm.render(f)
        return {k: fx.crossfade(a[k], b[k], w) for k in a}


LOOKS = {c.name: c for c in (Spectrum, Prism, Chase, Duotone, Rain, Auto)}
