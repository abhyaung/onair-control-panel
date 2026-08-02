# onair

A tablet or phone becomes a physical control panel for your computer's
microphone and camera. It sits beside your display, always on, and answers one
question at a glance: **am I live right now?**

Mic and camera drive the meeting app's own controls, so other participants see
the change — Google Meet, Microsoft Teams and Zoom. Display brightness, output
volume and mute come along too.

There is no cloud service. The agent runs on your own machine and serves the
panel over your LAN. Nothing leaves your network, and there is no account.

```
  ┌──────────────────────┐            ┌───────────────────────────┐
  │  iPad / Android /    │   LAN,     │  agent on your Mac or PC  │
  │  any browser         │◄──────────►│  menu-bar / tray icon     │
  │  (added to home      │  bearer    │  drives mic, camera,      │
  │   screen as an app)  │  token     │  brightness, volume       │
  └──────────────────────┘            └───────────────────────────┘
```

## Install

**1 — get the agent for your computer**

| | |
|---|---|
| **macOS** | [`agent-macos/`](agent-macos/) — build from source, see below |
| **Windows** | [`agent-windows/`](agent-windows/) — not built yet |

**2 — pair your tablet**

Open the agent's icon → **Pair device** → scan the QR code → **Add to Home
Screen**. That is the whole setup; the token travels in the link.

## macOS

```sh
cd agent-macos
make cert     # once: creates a self-signed signing identity
make app      # builds and signs build/OnAir.app
```

Drag `build/OnAir.app` to `/Applications`, launch it, then grant it
**Accessibility** in System Settings → Privacy & Security.

**The app must be signed to work at all.** macOS will not let an unsigned app
hold Accessibility for the processes it launches, so an unsigned build fails
*silently* — every control appears fine and changes nothing. `make cert` creates
a stable self-signed identity so the grant also survives rebuilds. Distributing
to other people needs a Developer ID; this is why there is no prebuilt download.

Also required, once: **Chrome → View → Developer → Allow JavaScript from Apple
Events** (must be clicked by hand). Without it, meeting controls cannot work.

Run `make doctor` any time to check every permission and dependency.

Optional: [MonitorControl](https://github.com/MonitorControl/MonitorControl) for
external-display brightness and volume. macOS exposes no API for these itself.

## Design notes

**Red means live.** Broadcast on-air convention: red is "you are being heard or
seen", dark is safe. From across a desk the panel answers *am I hot right now?*,
so hot is the loud state.

**State comes from the OS, not the page.** Mic state enumerates every input
device — muting only the default one silences a microphone nobody is using while
the app records from another. Camera state comes from the OS capture API, so it
stays correct regardless of what changed it.

**Every control has three states, never two.** On, off, and *unknown*. A control
that is readable but not settable renders read-only with the reason. A panel that
displays the wrong state is worse than no panel.

**The client sends action ids, never commands.** No client input reaches a shell.

## Layout

```
panel/           the web app — shared verbatim by every agent
protocol/        API.md, the contract; default config
agent-macos/     Python agent + Swift menu-bar app
agent-windows/   not built yet
docs/            architecture, UI mockup
```

[`protocol/API.md`](protocol/API.md) is the contract. Implement it and the panel
works unchanged — it also records the mistakes worth not repeating.

## Licence

MIT — see [LICENSE](LICENSE).
