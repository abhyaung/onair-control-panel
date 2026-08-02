"""System-level controls: microphone level, output volume, display brightness.

Permission notes, verified on this machine (macOS 26.5.2, M5 Pro, clamshell +
LG UltraFine):

- Volume get/set is plain AppleScript to the system. Needs no special grant.
- Brightness *reads* come from `ioreg`, which needs no grant but costs ~0.5s —
  far too slow for the 1s poll loop, so it is polled on a slower cadence.
- Brightness *writes* use the brightness key codes through System Events, which
  requires Accessibility. Without that grant the call hangs rather than failing,
  which is why every call is timeout-bounded.
"""

import re

from ..shell import osascript, run, TIMEOUT

# Observed shape: "brightness"={"min"=0,"max"=65536,"value"=32768}
# Safe against "rawBrightness" — that has no quote immediately before the word.
_BRIGHTNESS = re.compile(r'"brightness"=\{[^}]*?"max"=(\d+)[^}]*?"value"=(\d+)')

_KEY_BRIGHTNESS_UP = 144
_KEY_BRIGHTNESS_DOWN = 145

# macOS moves volume and brightness in sixteenths.
NOTCH = 100.0 / 16.0


# ── microphone ────────────────────────────────────────────────────────────────

def get_input_volume():
    """0-100, or None if unreadable."""
    ok, out = osascript("input volume of (get volume settings)")
    return int(out) if ok and out.isdigit() else None


def set_input_volume(level):
    level = max(0, min(100, int(level)))
    ok, _ = osascript("set volume input volume %d" % level)
    return ok


# ── output volume ─────────────────────────────────────────────────────────────

def get_output_volume():
    """0-100, or None when the active output device has no software volume.

    Returns 'missing value' — hence None — whenever audio is routed to a device
    macOS cannot attenuate itself. Observed here with output on the LG UltraFine
    over DisplayPort; switching to the MacBook speakers or headphones makes it
    readable again. The panel renders None as `unknown` with the pads disabled,
    which is correct: the control genuinely does not work for that device.
    """
    ok, out = osascript("output volume of (get volume settings)")
    return int(out) if ok and out.isdigit() else None


def set_output_volume(level):
    level = max(0, min(100, int(level)))
    ok, _ = osascript("set volume output volume %d" % level)
    return ok


def get_output_muted():
    """True/False, or None when the device exposes no mute state."""
    ok, out = osascript("output muted of (get volume settings)")
    if not ok:
        return None
    out = out.strip().lower()
    return True if out == "true" else (False if out == "false" else None)


def set_output_muted(muted):
    ok, _ = osascript("set volume output muted %s" % ("true" if muted else "false"))
    return ok


# ── brightness ────────────────────────────────────────────────────────────────

def main_display_is_builtin():
    """True when the primary display is the MacBook's own panel.

    Guards the brightness read. In clamshell the lid's `AppleARMBacklight`
    service stays registered and keeps publishing a plausible brightness value —
    but it belongs to a panel nobody can see. Reporting it would be worse than
    reporting nothing.
    """
    try:
        import ctypes
        import ctypes.util

        cg = ctypes.CDLL(ctypes.util.find_library("CoreGraphics"))
        cg.CGMainDisplayID.restype = ctypes.c_uint32
        return bool(cg.CGDisplayIsBuiltin(cg.CGMainDisplayID()))
    except Exception:
        return False


def get_brightness():
    """0-100 float, or None.

    None means genuinely unknown, and the panel must render that as `unknown`
    rather than pretending the value is 0.

    Verified 1 Aug 2026 on this machine (clamshell + LG UltraFine): the LG's
    brightness is not reachable. `IODisplayParameters` belongs to the closed
    lid's backlight, `DisplayServicesGetBrightness` returns rc=1000 for the
    external display, and no ioreg display class exposes it. The remaining
    untested path is the brightness key codes, which needs Accessibility.
    """
    if not main_display_is_builtin():
        return None
    ok, out = run(["ioreg", "-l", "-w", "0"], timeout=6.0)
    if not ok:
        return None
    match = _BRIGHTNESS.search(out)
    if not match:
        return None
    maximum, value = int(match.group(1)), int(match.group(2))
    return (value / maximum) * 100.0 if maximum else None


def step_brightness(notches):
    """Step brightness by N sixteenths. Negative steps down.

    Batched into a single AppleScript `repeat` so an eight-notch drag costs one
    process spawn (~100ms) instead of eight.

    Returns (ok, reason). reason == 'accessibility' means the call timed out,
    which on this platform almost always means the Accessibility grant is
    missing rather than anything being genuinely slow.
    """
    notches = int(notches)
    if notches == 0:
        return True, None
    code = _KEY_BRIGHTNESS_UP if notches > 0 else _KEY_BRIGHTNESS_DOWN
    script = (
        'tell application "System Events" to repeat %d times\n'
        "key code %d\n"
        "end repeat" % (abs(notches), code)
    )
    ok, out = osascript(script, timeout=5.0)
    if not ok and out == TIMEOUT:
        return False, "accessibility"
    return ok, None if ok else out
