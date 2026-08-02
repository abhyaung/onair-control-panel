"""Action registry and background state poller.

Two design rules enforced here:

1. **The client sends action IDs, never commands.** Every action must exist in
   ACTIONS below. There is no path from client input to a shell.
2. **State is polled into a cache, never probed on demand.** Each osascript call
   costs 100-300ms; probing inside a request handler would make every fetch slow
   and would hammer AppleScript once per second per connected client.

Everything is polled every tick. Brightness once needed a ~500ms `ioreg` walk and
ran on a slower cadence; reading MonitorControl's DDC cache instead costs ~5ms,
so the split is no longer worth its complexity.

Actions additionally refresh the state they changed *before* returning. Without
that the cache still holds the pre-action value for up to a full poll interval,
and the panel's optimistic render visibly snaps back before settling.
"""

import threading
import time

from .adapters import audio, browser, camera, ddc, system

UNKNOWN = "unknown"


# ── level sources ─────────────────────────────────────────────────────────────
# Each level reports whether it is *writable*, not just its value. A control
# showing a real number with dead buttons is its own kind of lie, so the panel
# needs to know the difference between "unknown" and "known but read-only".


def volume_state():
    """Output volume, from whichever source actually governs it.

    AppleScript wins when it works. It returns `missing value` whenever audio is
    routed to a device macOS cannot attenuate — here, the LG over DisplayPort —
    and in that case the display's own DDC audio register is the real control.
    """
    level = system.get_output_volume()
    if level is not None:
        return {"value": level, "writable": True, "source": "system",
                "reason": None}

    # Output is on a device macOS cannot attenuate. Five paths were measured on
    # the LG UltraFine and all fail: AppleScript (`missing value`), CoreAudio
    # (the device exposes no volume property at all, master or per-channel),
    # the volume media keys (macOS swallows them before MonitorControl sees
    # them), m1ddc (display does not speak standard DDC), and automating
    # MonitorControl's own slider (its SwiftUI menu exposes no AX slider).
    #
    # MonitorControl succeeds because it sends VCP 0x62 over the display's
    # native protocol — telling the monitor's amplifier to change volume, not
    # touching the audio device at all. That protocol is not reachable from
    # here, so the control is read-only and points at the thing that works.
    level = ddc.read(ddc.VCP_VOLUME)
    if level is not None:
        writable = ddc.writable(ddc.VCP_VOLUME)
        return {
            "value": level,
            "writable": writable,
            "source": "ddc",
            "reason": ddc.unwritable_reason(ddc.VCP_VOLUME),
        }

    return {"value": None, "writable": False, "source": None,
            "reason": "no output device"}


def brightness_state():
    """Display brightness over DDC.

    macOS itself cannot reach this display's brightness at all. DDC can, which
    is how MonitorControl does it — reading costs ~5ms against ~500ms for the
    `ioreg` walk that turned out to be reporting the closed lid's panel anyway.
    """
    level = ddc.read(ddc.VCP_BRIGHTNESS)
    if level is not None:
        writable = ddc.writable(ddc.VCP_BRIGHTNESS)
        return {"value": level, "writable": writable, "source": "ddc",
                "reason": ddc.unwritable_reason(ddc.VCP_BRIGHTNESS)}

    # Built-in panel only — irrelevant in clamshell, correct on the laptop alone.
    level = system.get_brightness()
    return {
        "value": level,
        "writable": level is not None,
        "source": "internal" if level is not None else None,
    }


def mute_state():
    """Output mute, from whichever source governs the active device.

    Mirrors volume_state(): AppleScript where macOS controls the device, the
    display's own DDC mute register (VCP 0x8D) where it does not.
    """
    if system.get_output_volume() is not None:
        muted = system.get_output_muted()
        return {"muted": muted, "writable": muted is not None, "source": "system"}

    muted = ddc.get_muted()
    if muted is not None:
        # Same gate as volume/brightness. Checking only that the helper binary
        # exists reported mute as writable while every press silently failed —
        # the media key still needs the Accessibility grant to be delivered.
        return {"muted": muted,
                "writable": ddc.mediakey_available() and ddc.writable(ddc.VCP_MUTE),
                "source": "ddc",
                "reason": ddc.unwritable_reason(ddc.VCP_MUTE)}

    return {"muted": None, "writable": False, "source": None}


class State(object):
    """Thread-safe snapshot of every control's true state."""

    def __init__(self):
        self._lock = threading.Lock()
        self._seq = 0
        # Identifies this process. `seq` restarts at 0 whenever the agent does,
        # so a client holding the previous run's high-water mark would reject
        # every future poll and freeze — looking perfectly healthy while showing
        # state that never updates again. The boot id lets it notice and reset.
        self._boot = "%.6f" % time.time()
        self._data = {
            "seq": 0,
            "boot": "%.6f" % time.time(),
            "mic": {"state": UNKNOWN, "level": None},
            "camera": {"state": UNKNOWN},
            "volume": {"value": None},
            "mute": {"muted": None, "writable": False},
            "brightness": {"value": None},
            "context": {"site": None, "label": None},
            "notes": [],
            "updated": 0,
        }

    def snapshot(self):
        with self._lock:
            return dict(self._data, updated=self._data["updated"])

    def seq(self):
        with self._lock:
            return self._seq

    def update_if(self, expected_seq, **fields):
        """Write only if nothing else has written since `expected_seq`.

        Closes a race the sequence number exposed rather than caused. The poller
        reads, then writes ~200ms later; if an action lands in that gap, the
        poller's *older* data would be committed with a *newer* seq and sail
        straight past the client's staleness gate — the flicker, but now
        undetectable. Losing a poll cycle here costs nothing: the action already
        published correct state, and the next tick re-reads in 1s.
        """
        with self._lock:
            if self._seq != expected_seq:
                return False
            self._seq += 1
            self._data.update(fields)
            self._data["updated"] = time.time()
            self._data["seq"] = self._seq
            return True

    def update(self, **fields):
        """Mutate state and bump the sequence.

        The sequence is what lets the client discard a poll that was already
        in flight when it acted: any snapshot built before the action carries a
        lower seq and is dropped, so an optimistic render can never be undone by
        stale data. Tolerance-matching the values was tried first and is too
        fragile — a small adjustment falls inside the tolerance and the stale
        value gets mistaken for confirmation.
        """
        with self._lock:
            self._seq += 1
            self._data.update(fields)
            self._data["updated"] = time.time()
            self._data["seq"] = self._seq


class Poller(object):
    def __init__(self, config, state):
        self.config = config
        self.state = state
        self.sites = [
            (key, site)
            for key, site in config.get("sites", {}).items()
            if site.get("enabled")
        ]
        self._stop = threading.Event()
        self._tick = 0
        self._meeting_mic = None   # last known meeting mute, for a stable read
        self._notes = []

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self):
        thread = threading.Thread(target=self._loop, name="poller", daemon=True)
        thread.start()
        return thread

    def stop(self):
        self._stop.set()

    def _loop(self):
        fast = float(self.config.get("poll", {}).get("fast_seconds", 1.0))
        while not self._stop.is_set():
            started = time.time()
            try:
                self._poll_once()
            except Exception as exc:  # a poller that dies takes the panel with it
                self._notes = ["poller error: %s" % exc]
            # Hold the cadence even when a probe ran long, rather than stacking.
            elapsed = time.time() - started
            self._stop.wait(max(0.1, fast - elapsed))

    # ── polling ──────────────────────────────────────────────────────────────

    def _poll_once(self):
        self._tick += 1
        notes = []

        base = self.state.seq()
        mic = mic_state(self._meeting_mic)
        cam = camera_state()
        volume = volume_state()

        # Publish the cheap truths immediately. The browser probe below can
        # block for its full timeout when Chrome is wedged or a permission
        # prompt is pending, and the mic state — the panel's most important
        # readout — must not be held hostage behind it.
        if not self.state.update_if(
            base,
            mic=mic,
            camera=cam,
            volume=volume,
            mute=mute_state(),
            brightness=brightness_state(),
        ):
            return  # an action landed mid-read; its data is newer than ours

        base = self.state.seq()
        site_label, site_key = None, None
        meeting_mic = None
        # One tab enumeration, then probe only the sites actually open. With
        # Meet + Teams + Zoom configured, probing every site each tick would
        # cost four osascript round-trips a second.
        urls = browser.open_tabs()
        candidates = [(k, s) for k, s in self.sites
                      if any(s["url_match"] in u for u in urls)]
        for key, site in candidates:
            probed = browser.probe_all(site["url_match"], _labels(self.config))
            if probed["mic"] or probed["cam"]:
                site_key, site_label = key, site.get("label", key)
                # In a call the meeting's mic state is the one that matters —
                # it is what participants actually see.
                meeting_mic = probed["mic"]
                break

        if not ddc.accessibility_ok():
            notes.append(
                "Accessibility not granted to this app — volume, brightness and "
                "mute are read-only. System Settings > Privacy & Security > "
                "Accessibility, add OnAir, then restart the agent."
            )

        if candidates and site_key is None:
            # Distinguish "no meeting" from "meeting present but unreadable".
            # The second is a fixable setup problem and should say so rather
            # than looking like an empty calendar.
            if candidates:
                notes.append(
                    "meeting tab found but Chrome's JS bridge is off — "
                    "enable View > Developer > Allow JavaScript from Apple Events"
                )
        elif self.sites and site_key is None:
            notes.append("no active meeting tab")

        # Remember it either way: leaving a call must clear the override, or the
        # tile would keep reporting a meeting mute that no longer exists.
        self._meeting_mic = meeting_mic
        # Always republish with the freshly probed meeting value. The previous
        # condition compared a state ("off"/"live") against a source string
        # ("meeting"/"devices"), so it was never meaningfully true or false —
        # the fast update's stale meeting value could survive a whole cycle.
        fields = {"mic": mic_state(meeting_mic)}
        self.state.update_if(
            base,
            context={"site": site_key, "label": site_label},
            notes=notes,
            **fields
        )


# ── actions ───────────────────────────────────────────────────────────────────
# Client sends only these ids. Anything not here is a 404 by construction.


def _sites(config):
    return [
        site["url_match"]
        for site in config.get("sites", {}).values()
        if site.get("enabled")
    ]


def _labels(config):
    return config.get("labels", {}) or {}


def _first_site(config):
    sites = _sites(config)
    return sites[0] if sites else None


def _refresh_browser(config, state):
    """Re-probe the conferencing tab after acting on it.

    Costs ~200ms, which is acceptable on an explicit press and is the difference
    between the panel confirming immediately and appearing to bounce.
    """
    for site in _sites(config):
        probed = browser.probe_all(site, _labels(config))
        if probed["mic"] or probed["cam"]:
            state.update(camera={"state": probed["cam"] or UNKNOWN})
            return


def camera_state():
    """Camera state from macOS, not from the page.

    CoreMediaIO reports whether any camera is capturing for any process, so this
    stays correct with Chrome's JS bridge off, in apps we know nothing about,
    and when no meeting tab exists at all. Reading Meet's DOM for this made the
    tile show `no signal` while the camera was plainly live.
    """
    live = camera.any_live()
    return {
        "state": UNKNOWN if live is None else ("live" if live else "off"),
        "devices": camera.devices(),
        "live_names": camera.live_names(),
    }


def mic_state(meeting=None):
    """Live if ANY input device is open — not just the default one.

    `meeting` overrides the verdict when a call is in progress, because the
    meeting's own mute is what participants actually see. Without a single
    source the field gets written twice per poll — device state, then meeting
    state — and reads oscillate between two different questions.

    `set volume input volume 0` mutes only the macOS default input device. An
    app may capture from a different one: Meet was using a Logitech Brio while
    the default was the MacBook mic, so the panel showed OFF while the user was
    fully audible. A master switch has to cover every device.
    """
    live = audio.any_live()
    device_state = UNKNOWN if live is None else ("live" if live else "off")
    return {
        "state": meeting if meeting is not None else device_state,
        "source": "meeting" if meeting is not None else "devices",
        "device_state": device_state,
        "devices": audio.status(),
        "unreadable": audio.unreadable(),
    }


def _refresh_mic(state):
    """Re-read the mic straight after changing it.

    Without this the cache still holds the pre-action value for up to a full
    poll interval, so the panel's confirmation arrives late and the control
    visibly hangs in its optimistic state.
    """
    state.update(mic=mic_state())


def act_mic_toggle(config, state, _payload):
    """Mute the mic the way the current context expects.

    **In a call, drive the meeting app's own mute.** Cutting system input
    volume silences you, but the meeting app cannot see it — Meet keeps showing
    an unmuted icon while participants hear nothing. That is worse than either
    state on its own: you look live and are inaudible.

    Outside a call there is no app control to drive, so the system cut applies.
    HARD CUT remains the system-level guarantee regardless.
    """
    labels = _labels(config)
    for site in _sites(config):
        probed = browser.probe_all(site, labels)
        if probed["mic"] is not None:
            ok = browser.toggle(site, "mic", labels)
            _refresh_browser(config, state)
            state.update(mic=mic_state(browser.probe_all(site, labels)["mic"]))
            return {"ok": ok, "via": "meeting"}

    # A meeting tab exists but its control could not be read. Try the app's own
    # shortcut — but do NOT fall through to a device-level mute afterwards.
    #
    # Cutting the device silences you without the meeting knowing, so the app
    # reports "microphone muted by system" while its own button still shows you
    # live. That is a worse state than failing outright, and it is not what a
    # mic button in a meeting is expected to do. HARD CUT remains the deliberate
    # device-level option.
    open_meetings = browser.matching_sites(_sites(config))
    if open_meetings:
        for site in open_meetings:
            if browser.keystroke(site, "mic"):
                _refresh_mic(state)
                return {"ok": True, "via": "keystroke"}
        return {"ok": False, "via": "meeting",
                "reason": "meeting open but its mic control is unreachable — "
                          "enable Chrome's JS bridge (View > Developer)"}

    live = audio.any_live()
    if live is None:
        return {"ok": False, "reason": "no readable input device"}
    results = audio.mute_all() if live else audio.unmute_all()
    _refresh_mic(state)
    return {"ok": all(ok for _, ok in results), "via": "devices",
            "devices": results}


def act_camera_toggle(config, state, _payload):
    site = _active_site(config) or _first_site(config)
    if not site:
        return {"ok": False, "reason": "no site configured"}
    # DOM first; keystroke only if the JS bridge is unavailable.
    ok = browser.toggle(site, "cam", _labels(config))
    via = "dom"
    if not ok:
        ok = browser.keystroke(site, "cam")
        via = "keystroke"
    _refresh_browser(config, state)
    # Verify against CoreMediaIO rather than trusting either mechanism.
    state.update(camera=camera_state())
    return {"ok": ok, "via": via}


def _active_site(config):
    """The site with a meeting actually in progress.

    With Meet, Teams and Zoom all enabled, "first configured site" is the wrong
    target — it would send Teams controls to a Meet tab that happens to be open.
    """
    labels = _labels(config)
    for site in browser.matching_sites(_sites(config)):
        probed = browser.probe_all(site, labels)
        if probed["mic"] or probed["cam"]:
            return site
    return None






def act_mute_toggle(_config, state, _payload):
    """Toggle output mute on whichever device is active."""
    current = mute_state()
    if not current["writable"]:
        return {"ok": False, "reason": "mute not available"}

    if current["source"] == "system":
        ok, reason = system.set_output_muted(not current["muted"]), None
    else:
        ok, reason = ddc.toggle_mute()

    state.update(mute=mute_state())
    return {"ok": ok, "reason": reason}


def act_volume_set(_config, _state, payload):
    value = payload.get("value")
    if value is None:
        return {"ok": False, "reason": "value required"}
    current = volume_state()
    if current["source"] == "system":
        ok, reason = system.set_output_volume(value), None
    elif current["source"] == "ddc":
        ok, reason = ddc.write(ddc.VCP_VOLUME, value)
        if ok:
            # Changing volume releases the display's mute in hardware.
            ddc.note_volume_changed()
    else:
        return {"ok": False, "reason": "no writable output"}
    _state.update(volume=volume_state(), mute=mute_state())
    return {"ok": ok, "reason": reason}


def act_brightness_set(_config, _state, payload):
    """Absolute set over DDC — no stepping, no Accessibility, no key codes."""
    value = payload.get("value")
    if value is None:
        return {"ok": False, "reason": "value required"}
    if not ddc.writable(ddc.VCP_BRIGHTNESS):
        return {"ok": False, "reason": "no writable brightness path"}
    ok, reason = ddc.write(ddc.VCP_BRIGHTNESS, value)
    _state.update(brightness=brightness_state())
    return {"ok": ok, "reason": reason}



ACTIONS = {
    "mic.toggle": act_mic_toggle,
    "camera.toggle": act_camera_toggle,
    "volume.set": act_volume_set,
    "mute.toggle": act_mute_toggle,
    "brightness.set": act_brightness_set,
}
