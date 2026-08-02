# onair agent API

The contract between the panel and any agent. The panel is platform-agnostic;
implement this and it works unchanged.

Transport: HTTP over the LAN. Bearer token. Bound to a specific interface,
never `0.0.0.0`.

---

## Authentication

Generated on first run, stored `0600` at `~/.onair/token` (or the platform
equivalent). Supplied as `Authorization: Bearer <token>`, or `?t=<token>` for
the initial pairing link and the manifest.

**The client sends action ids, never commands.** Every id must already exist
server-side. There must be no path from client input to a shell — this is the
load-bearing security property, and the token is defence in depth behind it.

---

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | the panel (static) |
| GET | `/panel.js`, `/panel.css` | panel assets |
| GET | `/manifest.json?t=…` | PWA manifest; `start_url` **must** carry the token |
| GET | `/api/state` | full state snapshot |
| GET | `/api/layout` | configured sites and available action ids |
| POST | `/api/action/<id>` | perform an action; returns the resulting state |

`/manifest.json` requires the token and embeds it in `start_url`. A Home Screen
web app gets its own storage container, so without this the installed app opens
unpaired.

---

## State

```json
{
  "seq": 412,
  "boot": "1785689427.862559",
  "mic":        { "state": "live|off|unknown", "source": "meeting|devices" },
  "camera":     { "state": "live|off|unknown" },
  "volume":     { "value": 0-100|null, "writable": true, "source": "system|ddc",
                  "reason": null },
  "brightness": { "value": 0-100|null, "writable": true, "reason": null },
  "mute":       { "muted": true|false|null, "writable": true },
  "context":    { "site": "meet|teams|zoom|null", "label": "Google Meet" },
  "notes":      ["human-readable problems worth surfacing"]
}
```

**`seq`** increments on every mutation. **`boot`** identifies the process.
Clients drop any snapshot with a lower `seq` than the last applied — that is
what stops an in-flight poll from undoing an optimistic render. `boot` changes
on restart so a client can reset its floor instead of rejecting everything
forever.

**Three states, never two.** `unknown` must be distinguishable from `off`.
A control that is readable but not settable reports `writable: false` with a
`reason`; the panel renders it read-only rather than accepting dead input.

---

## Actions

| id | payload | notes |
|---|---|---|
| `mic.toggle` | — | drives the meeting's own mute when in a call |
| `camera.toggle` | — | drives the meeting's own camera control |
| `volume.set` | `{"value": 0-100}` | |
| `brightness.set` | `{"value": 0-100}` | |
| `mute.toggle` | — | output mute |

Response: `{"action": id, "result": {...}, "state": <full state>}`

The response carries the post-action state so the client can apply truth
immediately rather than waiting for the next poll.

---

## Rules an implementation must follow

These were each learned by getting them wrong.

1. **Report state from the OS, not from the page.** Mic state comes from
   enumerating *every* input device — muting only the default one silences a
   microphone nobody is using while the app records from another. Camera state
   comes from the OS capture API, so it stays correct with no browser involved.

2. **A sent keystroke is not a changed setting.** Verify that a write actually
   moved the value before reporting success. Reporting a process exit code as
   success hides controls that silently do nothing.

3. **Never let a poll overwrite an unacknowledged local change.** Gate on `seq`
   *and* on whether a write for that control is still in flight.

4. **Say why a control is dead.** "Not responding" pointed at the display when
   the real cause was a missing OS permission — wrong diagnosis, wrong fix.

5. **Match labels on word boundaries.** Meeting apps label buttons by action
   ("Turn off microphone" means the mic is *on*). Teams uses "Unmute mic",
   which *contains* "mute mic" — plain substring matching reports a muted mic
   as live.

6. **Search inside same-origin frames.** Zoom renders its entire meeting UI in
   an iframe; a top-document query finds only the app shell.
