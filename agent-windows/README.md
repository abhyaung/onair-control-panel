# onair — Windows agent

> **Status: written, never run.** Developed on a Mac with no Windows machine
> available, so nothing here has been compiled or executed. Treat it as a
> documented first draft, not working software. The parts most likely to need
> correcting are listed below.

Implements the same contract as the macOS agent — see
[`../protocol/API.md`](../protocol/API.md) — so the panel in
[`../panel/`](../panel/) works against it unchanged.

## Build

Needs the [.NET 8 SDK](https://dotnet.microsoft.com/download).

```powershell
cd OnAir.Agent
dotnet publish -c Release
```

Produces a single self-contained `OnAir.exe` — no runtime for users to install.
Run it and a tray icon appears; right-click for the pairing link.

If binding the LAN address fails, either run once as administrator or reserve
the prefix:

```powershell
netsh http add urlacl url=http://+:8770/ user=%USERNAME%
```

## What works, and what does not

| | Status |
|---|---|
| Mute **every** input device | implemented (WASAPI) |
| Mic state | implemented |
| Camera in-use state | implemented (CapabilityAccessManager registry) |
| Display brightness | implemented (Dxva2 DDC) |
| Display / system volume + mute | implemented |
| **Meeting mic + camera control** | **not implemented** |

Meeting control is deliberately absent rather than stubbed. Windows has no
AppleScript equivalent, and driving Chrome through the DevTools protocol needs
it relaunched with a debug flag — too invasive. The right answer is a **browser
extension**, which would be shared with the macOS agent and would remove that
platform's AppleScript dependency too. Until it exists this agent does hardware
only, and says so in the panel rather than offering controls that silently do
nothing.

## Windows is easier than macOS here

Almost everything painful on macOS is simply absent:

| | macOS | Windows |
|---|---|---|
| Permission to synthesise input | Accessibility grant, per app | none needed |
| Signing required to *function* | yes — unsigned cannot hold the grant | no |
| Monitor brightness | no API; needs MonitorControl + media keys | native `SetMonitorBrightness` |
| Monitor volume | unreachable without a third-party app | `SetVCPFeature`, VCP 0x62 |

Signing only matters for avoiding SmartScreen warnings on download, not for the
agent to function.

## Most likely to be wrong

Written blind, in rough order of suspicion:

1. **COM vtable order** in `Adapters/CoreAudioInterop.cs`. Method order *is* the
   binary layout — a wrong slot silently calls the wrong function rather than
   failing to compile. Check here first if audio misbehaves.
2. **`PropVariant` field offsets** when reading device friendly names.
3. **`HttpListener` prefix registration** — non-loopback prefixes need a URL ACL
   or elevation. Loopback is also bound as a fallback.
4. **DDC on laptop panels** — internal displays often do not answer DDC at all,
   so brightness may be unavailable there while working on an external monitor.

## Rules worth keeping when finishing this

Each was learned by getting it wrong on macOS. The code already follows them.

- **Report state from the OS, not the page.** Mic state enumerates every input
  device. Muting only the default one silences a microphone nobody is using
  while the app records from another — the panel then says "off" while you are
  fully audible. This happened.
- **A write is not confirmed by its return code.** Verify the value actually
  moved, and fail *this* attempt if it did not. Reporting success while nothing
  changed hides a dead control, which is the worst failure mode.
- **Three states, never two.** `unknown` must be distinguishable from `off`, and
  a control that is readable but not settable reports `writable: false` with a
  reason the user can act on.
- **Keep `seq` and `boot` semantics exactly.** They stop a poll already in
  flight from undoing an optimistic render, and stop a client freezing
  permanently after the agent restarts. See the comments in `State.cs`.
- **Probe DDC, do not assume it.** A display can accept DDC reads and return
  noise. Consistency alone is not proof — three identical zeros passed that test
  on macOS.
