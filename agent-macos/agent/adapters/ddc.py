"""DDC/CI control of the external display.

Background: macOS exposes no way to reach this LG UltraFine's brightness — see
the corrected finding in the architecture doc. But MonitorControl (installed and
running here) drives it over DDC/CI, which proves the display *is* controllable;
the gap was never the hardware, only Apple's API surface.

Two paths exploit that:

**Reading** — MonitorControl caches every DDC value it has set, per display, in
its preferences. That read costs ~7ms, versus ~500ms for a full `ioreg` walk.
The caveat is that it reflects the last value *MonitorControl* set: anything
that changes the display by another route leaves it stale.

**Writing** — `m1ddc` talks to the display directly. Optional; when it is absent
the controls stay read-only rather than pretending to work.

VCP codes are the DDC standard's register numbers, and MonitorControl names its
prefs after them: 16 = 0x10 brightness, 18 = 0x12 contrast, 98 = 0x62 audio
speaker volume.
"""

import os
import re
import time

from ..shell import run

# Compiled helper that posts real media-key events. See mediakey.m for why this
# exists: the volume keys are NSSystemDefined media keys, not regular key codes,
# so AppleScript's `key code 72` never reached MonitorControl. Brightness only
# appeared to work because 144/145 are legacy codes macOS still maps.
_AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MEDIAKEY = os.path.join(_AGENT_DIR, "bin", "mediakey")

# Inside a bundle the app's own executable is preferred, because TCC evaluates
# the Accessibility grant against the process that posts the event. The grant is
# on the app; a separate helper binary is a different process and was silently
# refused, so writes reported success while changing nothing.
# Layout: OnAir.app/Contents/Resources/agent/adapters/ddc.py
_BUNDLE_EXE = os.path.normpath(
    os.path.join(_AGENT_DIR, "..", "..", "MacOS", "OnAir"))


def bundled_app_posts():
    """True when the app's own executable will post the events."""
    return os.path.isfile(_BUNDLE_EXE) and os.access(_BUNDLE_EXE, os.X_OK)


def _mediakey_command(arg, count, fine):
    """argv for one media-key burst, preferring the granted app executable."""
    if bundled_app_posts():
        cmd = [_BUNDLE_EXE, "--mediakey", arg, str(count)]
    else:
        cmd = [MEDIAKEY, arg, str(count)]
    return cmd + (["fine"] if fine else [])


BUNDLE = "app.monitorcontrol.MonitorControl"

VCP_BRIGHTNESS = 16
VCP_VOLUME = 98
VCP_MUTE = 141          # 0x8D audio mute; DDC spec: 1 = muted, 2 = unmuted

# m1ddc's names for the same registers.
M1DDC_NAME = {VCP_BRIGHTNESS: "luminance", VCP_VOLUME: "volume"}

# mediakey helper argument per control, as (up, down).
MEDIAKEY_ARG = {
    VCP_BRIGHTNESS: ("brightup", "brightdown"),
    VCP_VOLUME: ("up", "down"),
}

_display_key = None
_m1ddc_path = None


def _discover_display_key():
    """Find MonitorControl's per-display key, e.g. 'LGHDR4K12345678@1'.

    Discovered rather than hardcoded so swapping monitors does not silently
    leave the panel reading a display that is no longer attached.
    """
    global _display_key
    if _display_key is not None:
        return _display_key or None
    ok, out = run(["defaults", "read", BUNDLE], timeout=3.0)
    if ok:
        match = re.search(r'"value%d\(([^)]+)\)"' % VCP_BRIGHTNESS, out)
        if match:
            _display_key = match.group(1)
            return _display_key
    _display_key = ""  # cache the miss; do not re-probe every poll
    return None


def available():
    """True when MonitorControl has cached state for an attached display."""
    return _discover_display_key() is not None


def m1ddc():
    """Path to m1ddc, or None. Writes are unavailable without it."""
    global _m1ddc_path
    if _m1ddc_path is None:
        ok, out = run(["/bin/sh", "-c", "command -v m1ddc"], timeout=3.0)
        _m1ddc_path = out.strip() if ok and out.strip() else ""
    return _m1ddc_path or None


def _read_raw(vcp):
    """MonitorControl's cached value for a register, unscaled, or None."""
    key = _discover_display_key()
    if not key:
        return None
    ok, out = run(
        ["defaults", "read", BUNDLE, "value%d(%s)" % (vcp, key)], timeout=3.0
    )
    if not ok:
        return None
    try:
        return float(out.strip())
    except ValueError:
        return None


def _read_cached(vcp):
    """MonitorControl's cached value for a register as 0-100, or None."""
    raw = _read_raw(vcp)
    return None if raw is None else max(0.0, min(100.0, raw * 100.0))


_mute_belief = None      # our tracked mute state for the display
_last_mute_raw = None    # last value141 observed, to spot external changes


def get_muted():
    """True/False for display audio mute, or None.

    Read raw, not scaled: VCP 0x8D is an enum (1 muted / 2 unmuted), not a
    fraction, so the usual x100 conversion would clamp 2 to 100 and read as a
    level rather than a state.

    MonitorControl's cache alone is not enough here. Changing the volume
    **unmutes the display in hardware**, but MonitorControl does not notice and
    leaves its cached flag at "muted" indefinitely — so the panel would keep
    showing a mute that is no longer in effect.

    So the cache is treated as authoritative only when it *changes* (meaning
    MonitorControl itself did something); otherwise our own tracked belief wins,
    which `note_volume_changed()` keeps current.
    """
    global _last_mute_raw, _mute_belief
    raw = _read_raw(VCP_MUTE)
    if raw is None:
        return _mute_belief

    if raw != _last_mute_raw:
        _last_mute_raw = raw
        _mute_belief = (raw == 1)

    return _mute_belief if _mute_belief is not None else (raw == 1)


def note_volume_changed():
    """Record that a volume change just unmuted the display.

    Hardware behaviour, not ours: adjusting volume on a muted display releases
    the mute. Nothing else observes that, so it has to be recorded here.
    """
    global _mute_belief
    _mute_belief = False


def toggle_mute():
    """Toggle display audio mute. Returns (ok, reason)."""
    if not mediakey_available():
        return False, "mediakey helper not built"
    ok, out = run([MEDIAKEY, "mute", "1"], timeout=5.0)
    return (True, None) if ok else (False, out)


def read(vcp):
    """Return 0-100 for a VCP register, or None.

    **MonitorControl's cache is preferred over asking the display.** That looks
    backwards — querying the hardware should be more authoritative than reading
    somebody's cache — but on this display it is not.

    The LG UltraFine is a Thunderbolt display that does not implement standard
    DDC/CI; it uses Apple's native protocol. Measured, five consecutive `m1ddc`
    reads returned `0, 3, 0, 0, 0` for a display sitting steady at 51.6%. That
    is noise, and trusting it would drive the panel to show 0%.

    MonitorControl speaks the native protocol correctly, and because it is also
    the only writer (see `write`), its cache and the display never diverge.

    m1ddc remains the fallback for a conventional DDC monitor, where querying
    the hardware genuinely is more authoritative.
    """
    cached = _read_cached(vcp)
    if cached is not None:
        return cached

    binary = m1ddc()
    if binary:
        ok, out = run([binary, "get", M1DDC_NAME[vcp]], timeout=3.0)
        if ok and out.strip().isdigit():
            return float(out.strip())

    return None


# Media keys MonitorControl intercepts and translates to the display's native
# protocol. (up, down)
KEY = {VCP_BRIGHTNESS: (144, 145), VCP_VOLUME: (72, 73)}
NOTCH = 100.0 / 16.0   # plain media key
FINE = 1.0             # shift+option media key, measured exactly 1.000%

_direct_ok = None
_accessibility_ok = None
# vcp -> {"fails": n, "checked": monotonic}. Not a latch: a control is only
# treated as dead after repeated failures, and gets re-probed after a cooldown.
# The verification compares before/after values, so it produces false negatives
# whenever the value legitimately cannot move — the user adjusting the same
# control in MonitorControl mid-check, or the level already at 0 or 100. A
# single bad sample must not disable the control until the agent restarts.
_responds = {}
RESPOND_FAIL_LIMIT = 2
RESPOND_COOLDOWN = 60.0


def responsive(vcp):
    rec = _responds.get(vcp)
    if not rec or rec["fails"] < RESPOND_FAIL_LIMIT:
        return True
    return (time.monotonic() - rec["checked"]) > RESPOND_COOLDOWN


def _note_response(vcp, moved):
    if moved:
        _responds[vcp] = {"fails": 0, "checked": time.monotonic()}
    else:
        rec = _responds.setdefault(vcp, {"fails": 0, "checked": 0.0})
        rec["fails"] += 1
        rec["checked"] = time.monotonic()


def _verified(vcp):
    rec = _responds.get(vcp)
    return bool(rec) and rec["fails"] == 0


def direct_reliable():
    """Whether this display answers standard DDC queries consistently.

    Probed rather than assumed. Thunderbolt displays like the UltraFine accept
    the commands and return noise — five reads here gave 0, 3, 0, 0, 0 while the
    display sat steady at 51.6%. Three identical reads is a cheap, decisive test,
    and it keeps the code correct for a conventional monitor too.
    """
    global _direct_ok
    if _direct_ok is not None:
        return _direct_ok
    binary = m1ddc()
    if not binary:
        _direct_ok = False
        return False

    seen = []
    for _ in range(3):
        ok, out = run([binary, "get", M1DDC_NAME[VCP_BRIGHTNESS]], timeout=3.0)
        seen.append(out.strip() if ok else None)
    consistent = len(set(seen)) == 1 and seen[0] is not None

    # Consistency alone is not enough, and assuming it was caused a real bug:
    # this display reliably answers `0`, three identical zeros passed the test,
    # and the agent then routed writes through m1ddc — which silently does
    # nothing here while reporting success.
    #
    # So the reading must also *agree with a value known to be true*.
    # MonitorControl's cache is that ground truth.
    plausible = True
    if consistent:
        truth = _read_cached(VCP_BRIGHTNESS)
        if truth is not None:
            try:
                plausible = abs(float(seen[0]) - truth) <= 10.0
            except ValueError:
                plausible = False

    _direct_ok = consistent and plausible
    return _direct_ok


def accessibility_ok():
    """Whether this process may send keystrokes.

    `AXIsProcessTrusted()` is the correct per-process check. Two plausible
    alternatives are traps and were both tried first: `tell System Events to
    return name of it` needs only Automation, so it succeeds while keystrokes
    still fail; `UI elements enabled` reports the *global* accessibility state
    rather than this process's grant.

    A denial learned from an actual keystroke (macOS error 1002) still wins, in
    case the grant is revoked while running.
    """
    if _accessibility_ok is False:
        return False

    # When the app's own executable posts the events, this process's trust is
    # irrelevant — and asking about it is actively misleading. TCC responsibility
    # does not flow from an *unsigned* parent app to its children, so Python is
    # judged as itself and reports untrusted, blocking writes that the granted
    # app would have performed happily. Attempt-and-verify is the honest test.
    if bundled_app_posts():
        return True

    try:
        import ctypes

        services = ctypes.CDLL(
            "/System/Library/Frameworks/ApplicationServices.framework"
            "/ApplicationServices"
        )
        services.AXIsProcessTrusted.restype = ctypes.c_bool
        return bool(services.AXIsProcessTrusted())
    except Exception:
        return True   # unknown: let the write attempt decide


def _note_keystroke_result(ok, message):
    """Record what an attempted keystroke taught us about the grant."""
    global _accessibility_ok
    if ok:
        _accessibility_ok = True
    elif message and "1002" in message:
        _accessibility_ok = False
    return _accessibility_ok


def mediakey_available():
    for path in (_BUNDLE_EXE, MEDIAKEY):
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return True
    return False


def _tap(vcp, steps, fine=False):
    """Step a control by N presses.

    Prefers the compiled media-key helper, which posts the same events the
    physical keys do — the only thing MonitorControl actually listens for on
    volume. Falls back to AppleScript key codes, which work for brightness only.

    shift+option is macOS's fine-adjust modifier: exactly 1.000% per press
    against 6.25% for a plain press, measured on both controls.
    """
    count = abs(int(steps))

    if mediakey_available() and vcp in MEDIAKEY_ARG:
        arg = MEDIAKEY_ARG[vcp][0 if steps > 0 else 1]
        ok, out = run(_mediakey_command(arg, count, fine), timeout=8.0)
        if not ok:
            return False, "helper failed: %s" % (out or "no output")
        return True, None

    up, down = KEY[vcp]
    code = up if steps > 0 else down
    modifier = " using {shift down, option down}" if fine else ""
    script = (
        'tell application "System Events" to repeat %d times\n'
        "key code %d%s\n"
        "end repeat" % (count, code, modifier)
    )
    ok, out = run(["osascript", "-e", script], timeout=8.0)
    _note_keystroke_result(ok, out)
    if not ok and out and "1002" in out:
        return False, "needs accessibility"
    return ok, None if ok else out


def write(vcp, value):
    """Set a control to 0-100. Returns (ok, reason).

    Absolute via m1ddc where direct DDC is trustworthy; otherwise stepped via
    the media keys, letting MonitorControl do the protocol translation it
    already does correctly for this display.
    """
    value = max(0.0, min(100.0, float(value)))

    if direct_reliable():
        before = read(vcp)
        ok, out = run(
            [m1ddc(), "set", M1DDC_NAME[vcp], str(int(round(value)))], timeout=4.0
        )
        if not ok:
            return False, out
        # Same verification as the keystroke path: an exit code is not a change.
        if not _verified(vcp) and before is not None:
            time.sleep(0.4)
            after = read(vcp)
            _note_response(vcp, after is not None and abs(after - before) > 0.05)
        if not responsive(vcp):
            return False, "control does not respond"
        return True, None

    if not accessibility_ok():
        return False, "needs accessibility"

    # A sent keystroke is not a changed setting. Volume key codes are accepted
    # by System Events and then go nowhere on this display — MonitorControl does
    # not translate them the way it does brightness — so reporting the osascript
    # exit status as success would claim a change that never happened.
    if not responsive(vcp):
        return False, "control does not respond"

    current = read(vcp)
    if current is None:
        return False, "unknown current value"

    # Fine presses only — never coarse.
    #
    # Mixing them seemed obvious (coarse for the bulk, fine for the remainder)
    # but coarse presses do not move a fixed amount: the first one *snaps to the
    # notch grid*. From 59.25% a coarse press lands on 56.25%, a 3% move rather
    # than 6.25%, so any jump starting off-grid accumulates error. A measured
    # jump to 30% landed on 33.5%.
    #
    # Fine presses are a true fixed 1%, and they are cheap enough to use for
    # everything: measured 0.21s for 10 presses, 1.05s for 60, all inside one
    # osascript process. A fader drag sends small deltas anyway.
    steps = int(round((value - current) / FINE))
    if steps == 0:
        return True, None

    ok, why = _tap(vcp, steps, fine=True)
    if not ok:
        return ok, why

    # Only verify until the control has proven itself once — a settle-delay in
    # the middle of every fader drag would be worse than the check is worth.
    # Skip it entirely when the value is pinned at the end of its range, where
    # "did not move" says nothing about whether the control works.
    clamped = (steps > 0 and current >= 99.5) or (steps < 0 and current <= 0.5)
    if not _verified(vcp) and not clamped:
        time.sleep(0.4)  # let MonitorControl apply and persist
        after = read(vcp)
        moved = after is not None and abs(after - current) > 0.05
        _note_response(vcp, moved)
        if not moved:
            # Report THIS attempt as failed even on the first occurrence. The
            # earlier version only failed after the retry budget ran out, so a
            # write that demonstrably changed nothing still returned ok=True —
            # precisely the silent success this verification exists to prevent.
            return False, "no change (%s)" % (unwritable_reason(vcp) or "unknown cause")

    return True, None


def writable(vcp=None):
    """Whether writes can work — optionally for one specific control.

    A control proven unresponsive is reported unwritable so the panel renders it
    read-only instead of accepting drags that do nothing.
    """
    if vcp is not None and not responsive(vcp):
        return False
    return direct_reliable() or accessibility_ok()


def unwritable_reason(vcp):
    """Why a control cannot be written, or None if it can.

    Distinguishing these matters: both used to surface as "use MonitorControl"
    or "display not responding", which pointed the user at the display when the
    real problem was a missing macOS grant. Wrong diagnosis, wrong fix.
    """
    if not mediakey_available() and not direct_reliable():
        return "helper not built — run make"
    if not responsive(vcp):
        return "control not responding"
    if not (direct_reliable() or accessibility_ok()):
        return "needs Accessibility"
    return None


def write_path():
    """Which mechanism writes will use — surfaced so the UI can explain itself."""
    if direct_reliable():
        return "m1ddc"
    if accessibility_ok():
        return "mediakeys"
    return None
