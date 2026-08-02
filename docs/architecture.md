# onair — Architecture

An iPad control panel that acts as a master switch for the Mac's microphone and camera,
plus meeting and system controls. The iPad sits beside the Mac's display as an
always-on physical-feeling control surface.

Target machine: macOS 26.5.2, Apple Silicon (arm64), Python 3.9.6 (system).

---

## 1. The constraint that shapes everything

macOS exposes **no supported API to disable the camera**, and on Apple Silicon with SIP
enabled the driver cannot be unloaded. There is no clean switch to flip. The microphone
is different — system input volume is both readable and settable, so a true system-wide
mic cut is available.

This asymmetry forces a two-tier design:

| Tier | Mechanism | Reversible | Use |
|---|---|---|---|
| **App tier** | Drive the conferencing app's own controls | Yes | 99% of presses |
| **Hard cut** | Input volume → 0, kill the camera assistant | Messy | Panic only |

The app tier is what a Stream Deck does, and it is what actually matches intent
("turn my camera off in this call"). The hard cut genuinely severs capture but kills
video mid-call and cannot *prevent* an app from reacquiring the device.

### Why the browser tier is the main one

Zoom and Teams are **not installed on this machine** — all conferencing is web-based in
Chrome (the default browser). That collapses three native integrations into one
mechanism: AppleScript's `execute javascript` against the Chrome tab.

This is strictly better than sending keystrokes, because it can **read the real state
back out of the DOM**. A master switch that displays the wrong state is worse than no
switch at all, so state truth is a primary design goal, not a nicety.

Prerequisite: Chrome → View → Developer → **Allow JavaScript from Apple Events**
(currently off). Nothing in the browser tier works without it.

---

## 2. System diagram

```
   iPad (Safari PWA, added to Home Screen)
   ┌──────────────────────────────────┐
   │  button grid rendered from       │
   │  /api/layout                     │
   │  state polled from /api/state    │
   └───────────────┬──────────────────┘
                   │  HTTP over LAN, bearer token
                   │  (GET state ~1s · POST action)
   ────────────────┼─────────────────────────────────────────────
                   │                            Mac (macOS 26)
   ┌───────────────▼──────────────────────────────────────────┐
   │  onair agent — Python 3.9 stdlib, zero dependencies      │
   │                                                          │
   │   ThreadingHTTPServer                                    │
   │     ├─ auth: bearer token, LAN-bound                     │
   │     ├─ GET  /              → panel (static)              │
   │     ├─ GET  /api/layout    → buttons from config.json    │
   │     ├─ GET  /api/state     → cached truth                │
   │     └─ POST /api/action/ID → dispatch                    │
   │                                                          │
   │   State poller (background thread, ~1s)                  │
   │     probes every control, caches result                  │
   │                                                          │
   │   Action registry  ── ids only, never client commands    │
   │     │                                                    │
   │     ├─ BrowserAdapter ─ osascript → Chrome → JS in tab   │
   │     │    Google Meet · Teams web · Zoom web              │
   │     │    click real button · read real state             │
   │     ├─ AppAdapter ───── System Events keystroke (Slack)  │
   │     ├─ SystemAdapter ── input/output volume, Focus,      │
   │     │                   lock screen, display sleep       │
   │     └─ HardCutAdapter ─ input volume 0 + kill VDCAssistant│
   └──────────────────────────────────────────────────────────┘
```

---

## 3. Components

### 3.0 Optimistic reconciliation

Taps and drags paint immediately so the panel feels instant, but the poller runs
on its own 1s cadence. An in-flight poll carries a snapshot taken *before* the
change, and letting it land produced a visible **new → old → new** flicker: the
control appeared to snap back before settling.

Fixed with a **state sequence**, not value matching:

- Every mutation on the agent bumps a monotonic `seq`, exposed in each snapshot.
- An action returns the snapshot it produced. The client applies that directly —
  it is post-action truth, including whether the write actually took.
- The client then drops any poll whose `seq` is lower than the last it applied.

That makes stale data impossible to render: it is an ordering fact, not a guess.
Matching the *values* within a tolerance was tried first and is too fragile — a
small adjustment (50 → 51) falls inside any sane tolerance, so the stale reading
is mistaken for confirmation and the flicker returns.

**Polls are also gated on in-flight writes**, which `seq` alone cannot cover.
`seq` orders events *server-side*; it has no way to know about a change made
locally that the agent has not processed yet. A poll issued between the tap and
the action landing therefore carries a legitimately-current *old* value and
renders as a snap-back. So a control with a write outstanding is not rendered
from polls at all — measured hold is ~220ms. A 4s deadline releases it, because
a request that never returns must not freeze the control.

Browser-backed actions re-probe the tab before returning, for the same reason:
otherwise their response carries a stale camera/share state and reintroduces the
bounce they were meant to remove.

**A consistency check is not a correctness check.** `direct_reliable()` first
accepted m1ddc if three reads agreed — and this display reliably answers `0`, so
three identical zeros passed. The agent then routed brightness writes through
m1ddc, which silently does nothing here while reporting success. The probe now
also requires the reading to *agree with a value known to be true* (MonitorControl's
cache); a 49-point disagreement rejects m1ddc as a liar rather than trusting it for
being consistently wrong.

**Writes verify that they changed something.** A sent keystroke is not a changed
setting: volume key codes are accepted by System Events and then go nowhere on
the LG, so reporting the osascript exit status as success claimed a change that
never happened. Each control is probed once on first write — did the value
actually move? — and a control proven unresponsive is reported unwritable, so the
panel renders it read-only rather than accepting drags that do nothing.

### 3.1 Web client (PWA)

Static HTML/CSS/JS. No framework, no build step — the agent serves the directory as-is.

- `manifest.json` + `apple-mobile-web-app-capable` so Add to Home Screen gives a
  full-screen app with no browser chrome.
- Renders the button grid from `/api/layout` rather than hardcoding it, so adding a
  button later is a config edit.
- Polls `/api/state` on a ~1s interval; refetches immediately on `visibilitychange`
  because iPadOS suspends background tabs.
- Optimistic UI on tap, reconciled by the next poll. If the poll disagrees, the poll wins.
- Committed to a **permanently dark theme** — it lives next to a monitor, often at night.
  No light mode by design.
- Holds a **Screen Wake Lock** so the iPad does not blank. A panel you have to wake
  before using is worse than a menu-bar icon. iPadOS releases the lock whenever the
  page is hidden and does not restore it, so it is re-acquired on every
  `visibilitychange` and on touch — the first request can be refused without a
  user gesture. Requires iPadOS 16.4+; older versions degrade rather than break.

### 3.2 Agent HTTP server

`http.server.ThreadingHTTPServer` from the stdlib. Python 3.9.6 has no `tomllib`, so
config is **JSON** — keeping the zero-dependency promise.

| Endpoint | Purpose |
|---|---|
| `GET /` | Panel HTML + static assets |
| `GET /api/layout` | Button definitions (id, label, icon, group) |
| `GET /api/state` | Cached state for every control |
| `POST /api/action/<id>` | Execute action, return new state |
| `GET /api/events` | SSE stream (stage 3, replaces polling) |

### 3.3 State poller

A background thread probes each control on an interval and caches the result.

This exists because `osascript` calls cost roughly 50–200 ms each. Probing on-demand
inside the request handler would make every state fetch slow and would hammer
AppleScript at 1 Hz per client. Polling into a cache keeps HTTP responses instant and
decouples poll rate from client count.

### 3.4 Adapters

**BrowserAdapter** — `osascript` → `tell application "Google Chrome"` →
`execute javascript` in the matching tab. Searches all windows for a tab whose URL
matches the site pattern. Each site contributes two JS snippets: a `probe()` returning
the current state and a `toggle()` that clicks the real control. Does not steal focus.

**AppAdapter** — for native apps (Slack huddles). Activates the app, sends the keystroke
via System Events, restores the previously frontmost app. Causes a brief visual flicker;
unavoidable, and the reason browser control is preferred wherever possible.

**SystemAdapter** — input/output volume via `osascript` (`set volume output volume N`,
readable via `get volume settings`). Brightness via the standard brightness key codes
(144 up / 145 down) through System Events, read back from `ioreg`.

Each control offers two input modes: a **fader** for big jumps and **± pads** for
precision.

The fader follows the Control Center pattern — a chunky pill where the entire surface is
the control and the fill level *is* the readout, rather than a thin track with a small
thumb.

**Drag is relative, not absolute** (changed 1 Aug 2026). Touching the fader grabs it
without changing the value; sliding then moves the level by the distance travelled, so the
control adjusts from wherever it already was. On a panel sitting on a desk this matters
for more than preference — an absolute fader turns an accidental brush into a brightness
slam. The delta is measured from the previous move rather than the touch origin, so
running into 0 or 100 and sliding back responds immediately instead of crossing a dead
zone the size of the overshoot. A tap that never moves commits nothing. On a touch panel operated by finger at arm's length, a 60 px target beats a 19 px
one; there is no thumb to miss. It swells slightly on grab for tactile feedback, and drops
its fill transition while dragging so the level tracks the finger with no rubber-banding.

Styled flat, square, and minimal: 7 px corners, solid fills (`#8AB4F8` volume, `#FDD663`
brightness), no gradients, borders, or shadows. The faders carry no container card — they
are substantial enough to stand on the panel background unaided.

The guiding rule for the minimal pass was **remove decoration, not information**.
Gradients, glows, shadows, large radii, and the tiles' source-of-truth captions all went.
The red live state, the three-state distinction, and the large touch targets all stayed —
those are the panel's entire job.

That flatness forced two changes, both worth keeping:

- **The percentage moved out of the pill** into the label row above. White text on a flat
  bright fill is unreadable, and a value that sits partly over the fill and partly over the
  track can't be styled to work at every level.
- **The pill icon flips color with the fill.** It's dark by default because it rides on the
  bright fill, but below ~15% the fill no longer reaches it, so JS sets `data-low` and the
  icon goes light. Without this the icon vanishes at low volume — exactly when you're most
  likely to be looking for it.

The pads step in **6.25% increments** to match the macOS native 1/16 notch, so
the panel and the on-screen HUD stay in agreement, and support press-and-hold to ramp
(400 ms delay, then 110 ms repeat).

> **Slider writes must be throttled.** A drag emits pointer events at display rate, and
> each write is an `osascript` call costing 50–200 ms. Writing per-event would queue
> hundreds of AppleScript invocations behind a single swipe. Throttle outbound writes to
> ~100 ms during the drag and always commit the final value on release, so the last write
> wins and the control never settles on a stale position.

> **Corrected 1 Aug 2026 — brightness is NOT currently controllable.**
>
> An earlier reading of `ioreg` found `IODisplayParameters`
> (`brightness = 32768 / 65536`) and concluded the LG UltraFine was controllable.
> That was wrong. The service owning that value is **`AppleARMBacklight`** — the
> MacBook's *internal* panel, which stays registered in clamshell and keeps
> publishing a plausible brightness for a screen nobody can see. CoreGraphics
> reports exactly one online display: `id=4, builtin=False`.
>
> Three paths tested against the LG, none work:
> `IODisplayParameters` (belongs to the internal panel),
> `DisplayServicesGetBrightness` (returns rc=1000 for this display),
> and per-class `ioreg` queries (`AppleCLCD2`, `AppleDisplay`, `IODisplayConnect`,
> `AppleBacklightDisplay` — zero hits).
>
> **Resolved the same day — DDC/CI reaches it.** The gap was never the hardware,
> only Apple's API surface. MonitorControl (installed and running) drives this
> display over DDC/CI, which proves it is controllable.
>
> MonitorControl has no scripting interface — no `.sdef`, no URL scheme — so it
> cannot be driven directly. But it caches every DDC value it sets, per display,
> in its preferences under VCP register numbers:
>
> | Pref | VCP | Meaning | Observed |
> |---|---|---|---|
> | `value16(LGHDR4K12345678@1)` | 0x10 | Brightness | 51.6% |
> | `value18(...)` | 0x12 | Contrast | 75% |
> | `value98(...)` | 0x62 | Audio speaker volume | 13.5% |
>
> Reading that costs **~5ms**, against ~500ms for the `ioreg` walk that was
> reporting the wrong panel anyway — so brightness no longer needs a slow polling
> cadence. The display key is discovered, not hardcoded, so swapping monitors does
> not silently leave the panel reading a display that is gone.
>
> The same finding revives **output volume**: the LG's speaker level is VCP 0x62,
> which is why AppleScript reported `missing value` — macOS cannot attenuate that
> device, but the display itself can.
>
> **m1ddc does not work on this display** — installed and tested. The UltraFine is
> a Thunderbolt display that does not implement standard DDC/CI. Five consecutive
> reads returned `0, 3, 0, 0, 0` while it sat steady at 51.6%, and writes were
> no-ops. `direct_reliable()` probes this and also cross-checks the reading
> against MonitorControl's cache — consistency alone is not enough, since three
> identical zeros once passed the test and silently routed writes into a no-op.
>
> **The working mechanism is real media-key events**, posted by a small compiled
> helper (`agent/bin/mediakey.m`) and translated by MonitorControl.
>
> This was the crux of a long dead end. AppleScript `key code 144/145` drove
> brightness fine, so the same approach was assumed correct for volume — but
> `key code 72` is a 1980s Apple Extended Keyboard key, while the modern volume
> keys are **`NSSystemDefined` media keys** (`NX_KEYTYPE_SOUND_UP`). MonitorControl
> listens for the latter, so synthesised key codes never reached it. Brightness
> only worked because 144/145 are legacy codes macOS still maps.
>
> Both controls now use the helper, which removes the AppleScript keystroke path
> and its Accessibility hang risk. shift+option is the fine-adjust modifier:
> measured at exactly 1.000% per press versus 6.25% plain, on both controls.
>
> Verified end to end: volume 45/20/35 and brightness 45/58/52 all land within
> 0.5%.

> **Output volume is conditional.** `output volume of (get volume settings)` returns
> `missing value` whenever audio is routed to a device macOS cannot attenuate in
> software — observed here with output on the LG UltraFine over DisplayPort
> (`Manufacturer: GSM`). Switching to the MacBook speakers or headphones makes it
> readable again. Rendered as `unknown` with pads disabled, which is honest: the
> control genuinely does not work for that output device.

> **System Events hangs — it does not error.** A keystroke call without the
> Accessibility grant blocks indefinitely waiting on a TCC prompt rather than
> returning a failure. A two-minute hang was observed before this was understood.
> Every `osascript` call is therefore timeout-bounded; one ungranted permission
> would otherwise freeze the state poller permanently.

**HardCutAdapter** — sets input volume to 0, and releases the camera in every
conferencing tab. Guarded in the UI behind a 1-second press-and-hold so it cannot fire
by accident.

> **Corrected 1 Aug 2026 — the camera daemon cannot be killed.** `VDCAssistant` runs as
> `_cmiodalassistants`, not the logged-in user; `kill -0` returns *operation not
> permitted*. Killing it requires root. Running a LAN-reachable HTTP service as root, or
> granting it a passwordless sudo rule to kill processes, was rejected as a bad trade for
> a marginally harder cut.
>
> What replaces it is sufficient **on this machine specifically**: every camera client
> here is a Chrome tab, so releasing the camera in each conferencing tab genuinely
> releases the device and the green hardware light goes out. If a native camera client is
> ever installed this stops being a complete cut, and the UI must say so rather than
> quietly under-delivering — which is why `cut()` returns a `camera_complete` flag.

---

### 3.4b Mute

Sits with the volume pads (`− / mute / +`) rather than in its own row, since it
is the same control surface. Red when engaged.

Mirrors the volume split: AppleScript `output muted` where macOS governs the
device, the display's own **VCP 0x8D** (audio mute) where it does not. Toggled
with a real `NX_KEYTYPE_MUTE` media key through the same helper.

One trap: 0x8D is an *enum* (1 = muted, 2 = unmuted), not a fraction. The usual
x100 scaling applied to level registers would turn 2 into 200, clamp to 100, and
read as a level rather than a state — so mute reads raw.

A second trap, found by using it: **changing the volume unmutes the display in
hardware, but MonitorControl never notices** and leaves its cached flag on
"muted" indefinitely — so the button stayed lit over audible sound. The cache is
therefore trusted only when it *changes* (meaning MonitorControl itself acted);
otherwise an internally-tracked belief wins, which a volume write clears. Not
perfect — a mute made through MonitorControl's own UI while its cache is already
stale can still be missed — but it matches the hardware in every path the panel
drives.

### 3.5 Screen sharing — dropped (1 Aug 2026)

Removed along with raise-hand, chat and leave-call. These were the most
platform-specific controls and the least useful on a panel: supporting them
meant per-platform label sets and per-platform quirks, for buttons already
within reach in the meeting window. Dropping them makes the tool generic across
Meet, Teams and Zoom, and removes a probe from every poll.

Kept for the record, since it cost real investigation: Meet's "Present now"
calls `getDisplayMedia()`, and Chrome draws that source picker in the *browser
process*, invisible to the JS bridge that drives everything else here — browsers
make it unscriptable on purpose, because silent screen capture is the attack it
exists to prevent. The workable shape was an asymmetric toggle (start opens the
picker on the Mac; stop is a clean DOM click), not a symmetric one.

Only **mic and camera** are driven now — the two controls every platform shares,
and the two the panel exists for.

## 4. Security model

The agent can mute the mic and kill processes. An unauthenticated LAN endpoint that does
that is genuinely dangerous, and avoiding it is ten minutes of work.

1. **Bearer token**, generated on first run, stored at `~/.onair/token` (mode 600).
   Delivered to the iPad once via a URL fragment, then persisted in `localStorage`.
2. **Bind to the LAN interface**, never `0.0.0.0`, never the public internet.
3. **The client can never send a command — only an action ID.** Every action must already
   exist in server-side config. There is no code path where client input reaches a shell.
   This is the load-bearing invariant; the token is defence in depth behind it.
4. No shell interpolation anywhere: `subprocess` with argument lists, never `shell=True`.

---

## 5. macOS permissions (one-time)

Every one of these is a silent-failure trap if missed:

- **Chrome** → View → Developer → Allow JavaScript from Apple Events
- **Privacy & Security → Accessibility** → the process running the agent
  (needed for System Events keystrokes)
- **Privacy & Security → Automation** → allow control of Chrome, Slack, System Events

The agent should self-check these on startup and surface a clear banner in the panel
rather than failing silently.

---

## 6. Failure modes

State has **three** values, never two. `unknown` must render distinctly from `off` —
conflating them is exactly how a panel lies to you.

| Failure | Detection | Behaviour |
|---|---|---|
| Non-UltraFine monitor attached | `ioreg` has no brightness | Brightness stepper shows `unknown`, pads disabled |
| DOM selector drift after a Meet/Teams UI change | `probe()` returns null | Control shows `unknown`; keystroke fallback available |
| Chrome not running / no meeting tab | No matching tab | Meeting controls render unavailable, not "off" |
| `osascript` hangs or times out | Poll timeout | Mark state stale, show disconnected |
| iPad Safari suspended the PWA | `visibilitychange` | Refetch on resume before rendering |
| Agent not running | Fetch fails | Full-panel disconnected banner |

Selector drift is the standing maintenance tax of this design. Mitigation: every selector
lives in `config.json`, so a break is a one-line fix rather than a debugging session.

---

## 7. Build stages

**Stage 1 — built and verified 1 Aug 2026.** Agent skeleton, token auth, JSON config,
PWA shell, two-cadence state poller, system mic cut, hard cut, and the Chrome/Meet
adapter. Verified end to end: LAN bind, 401 without token, 404 on unknown action ids,
path traversal blocked, and mic mute round-tripping `50 → 0 → 50`.

**Stage 1 is complete.** Validated against a live Google Meet call: camera and mic both
toggle Meet's own controls via the DOM (no focus steal), the context pill resolves, and the
selectors needed no changes — they had only ever been blocked by Chrome's JS bridge being off.

Mic and camera *state* no longer come from the page at all: CoreAudio reports every input
device, CoreMediaIO reports any capturing camera. Both stay correct with the bridge off, in
apps we know nothing about, and with no meeting running.

**Stage 2.** Teams web and Zoom web selectors. Slack huddle mute. Meeting extras — raise
hand, chat, leave call, and the asymmetric share toggle (§3.5).

**Stage 3.** System steppers — output volume and display brightness, both with live
readback. `launchd` autostart so it survives reboots. SSE instead of polling.
Config-driven layout editing.

---

## 8. Layout

```
onair/
├── README.md
├── config.json              # buttons, selectors, keystrokes
├── docs/
│   ├── architecture.md      # this file
│   └── ui-mockup.html       # visual mockup
├── agent/
│   ├── server.py            # HTTP + routing + auth
│   ├── registry.py          # action registry, state poller
│   └── adapters/
│       ├── browser.py
│       ├── app.py
│       ├── system.py
│       └── hardcut.py
└── web/
    ├── index.html
    ├── panel.css
    ├── panel.js
    └── manifest.json
```
