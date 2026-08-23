# barehands: Stark background + easier tapping

One file changed: `stage.html`. Everything below is real, tested-for-syntax
code (validated with `node --check`, and diffed against the original to
confirm the change is scoped to exactly what's described here) — not a mockup.

## Apply it

```bash
cd ~/barehands              # wherever /barehands-setup put it
cp stage.html stage.html.backup     # so you can always get the original back
```

Then copy this zip's `stage.html` over the one in that folder. Reload
`http://127.0.0.1:8794/stage.html` in Chrome (or fully quit/reopen the tab if
it seems stuck — service workers and cached modules occasionally need that).

## What changed, and why

### 1. Stark background — `?bg=stark` on the URL

```
http://127.0.0.1:8794/stage.html?bg=stark
```

This hides the mirrored face-cam and replaces it with a procedural HUD
backdrop: a soft radial glow, a faint receding grid, a few concentric
"radar" rings with one slowly rotating dashed ring, a radar-style sweep,
and some drifting particles — canvas-drawn, no images or fonts to load,
costs almost nothing to render.

**Important: this is purely cosmetic.** The hand tracker (MediaPipe) reads
frames straight from the camera's `<video>` element regardless of whether
that element is visible on screen — hiding it with CSS (the same trick the
existing `overlay` and `key` modes already use, for OBS) has **zero** effect
on tracking. Your hands are tracked exactly as before; you just don't see
your own face behind the glass anymore.

It composes with everything else — add it alongside your existing flags,
e.g. `?bg=stark&mode=overlay` for streaming with the HUD instead of a
transparent/keyed background.

**Not included:** an actual 3D engine room / Iron Man lab render. That's a
real 3D scene (three.js, lighting, a model) rather than a 2D canvas doodle —
very doable as a next step if you want it, just a bigger lift than this pass.
This gets you most of the way to "doesn't look like my face on a projector"
for free.

### 2. Tapping — loosened, and now tunable

I read the actual gesture code rather than guessing. Every "tap" in
barehands (tap the ring, tap a card to open it, tap it again to close it,
tap an orb) is the same gate: **pinch-release within a time window, having
moved less than a pixel radius**. The original defaults were 300ms/26px for
most taps, 350ms/24px for a couple of others.

That's tight. Webcam hand-tracking has real jitter — a hand that's
genuinely holding still can still read as having moved 20-30px between
frames, especially at typical webcam resolution/framerate. That's almost
certainly why tapping felt unreliable even in good lighting: it's not (only)
detection confidence, it's the release-gate being narrower than the
tracker's natural noise floor.

**Changed:** every one of those gates (ring, orb, card-open, card-close,
image/model-close, browser-row, grip-bar) now shares two tunable constants,
defaulted looser than before:

| | old | new default |
|---|---|---|
| time window | 300-350ms | 380ms |
| travel tolerance | 24-26px | 34px |

Tune them per-session without touching the file again:

```
http://127.0.0.1:8794/stage.html?tapms=450&tappx=40
```

Go higher if it's still missing taps; go back toward the original (`tapms=300&tappx=24`)
if it ever starts firing on stuff you didn't mean to tap. I did not touch
`minHandDetectionConfidence` (0.7) or `minHandPresenceConfidence` (0.5) — the
original author's comments show those were deliberately tuned against
false-positive "ghost hands" from busy backgrounds, and loosening them
trades one problem for a worse one. The tap gate was the actual bug for
"tapping just won't work"; those confidence values almost certainly aren't.

### 3. A shorter default hint, full list on demand

The always-on-screen hint used to be one dense line listing every gesture
including the advanced ones (the claw-snap force-pull, the explode scrub) —
genuinely not beginner-friendly to stare at while you're just trying to tap
a ring. It now defaults to:

> tap the RING to start · pinch to move things · tap again to open or close · press H for every gesture

Press **H** to flip to the full original list, and again to flip back. Same
technique as the existing R/C/D single-key shortcuts.

## What I couldn't fix in code

Hand-tracking reliability past this point is genuinely sensor- and
environment-bound — no amount of JS tuning fully substitutes for the camera
getting a clean view of your hand. For what it's worth, the things that
actually move the needle with MediaPipe-style tracking:

- **Light the hand, not just the room.** Front-ish light (a desk lamp
  pointed roughly where your hands work, not just overhead) matters more
  than overall room brightness — backlighting (a bright window behind you)
  is the single most common killer, since it silhouettes your hand into a
  dark blob.
- **Plain background where your hands move.** A busy, hand-shaped, or
  skin-toned background behind your hands is exactly what the 0.7 detection
  threshold in the code above is fighting — a clear wall or desk surface
  behind your hand's working area gives it a much easier job.
- **Distance and framing.** Too close and fingers clip out of frame during
  a spread gesture; too far and the model has fewer pixels of actual hand to
  work with. Somewhere around comfortable typing distance from the camera is
  usually the sweet spot — you're looking for your whole hand, fingers
  spread, comfortably inside the frame with a little margin.
- **Camera quality genuinely matters.** A built-in laptop webcam at low
  light will always track worse than a decent external webcam with real
  autofocus and low-light performance — if this is going to be a permanent
  desk rig, that's the single highest-leverage hardware upgrade available.

None of that is a HELENA or barehands limitation specifically — it's the
same set of constraints any webcam hand-tracking system (this, or
commercial ones) runs into.

## If `update.sh` / `update.bat` ever complains after this

barehands' updater does a `git pull --ff-only`. With this file modified
locally, if upstream ever touches the same lines, the pull will refuse to
fast-forward and say so plainly rather than silently overwriting your
changes — your customization is safe either way. If you want to be extra
sure, `git add stage.html && git commit -m "local UX tweaks"` inside the
barehands folder once, so it's a tracked commit rather than just an
uncommitted edit.
