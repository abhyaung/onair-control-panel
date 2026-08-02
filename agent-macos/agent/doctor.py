"""Permission and dependency checks.

This exists because the permissions *are* the onboarding. Every capability in
this project sits behind a macOS grant or a non-default browser setting, and
each one fails in its own confusing way — a hang, a silent no-op, or a control
that reports the wrong state. A stranger who installs this and gets no
explanation will conclude it is broken.

Each check returns a status, what it means, and where to fix it.
"""

import os
import subprocess

from .adapters import audio, camera, ddc
from .shell import osascript, run

OK, WARN, FAIL = "ok", "warn", "fail"

# Deep links into the right System Settings pane, so a fix is one click away.
PANE_ACCESSIBILITY = ("x-apple.systempreferences:com.apple.preference.security"
                      "?Privacy_Accessibility")
PANE_AUTOMATION = ("x-apple.systempreferences:com.apple.preference.security"
                   "?Privacy_Automation")


def _accessibility():
    """The correct per-process check is AXIsProcessTrusted().

    Two plausible alternatives are traps, and both were tried first:
    `tell System Events to return name of it` needs only Automation, so it
    succeeds while keystrokes still fail; `UI elements enabled` reports the
    *global* accessibility state, not this process's grant.
    """
    try:
        import ctypes

        services = ctypes.CDLL(
            "/System/Library/Frameworks/ApplicationServices.framework"
            "/ApplicationServices"
        )
        services.AXIsProcessTrusted.restype = ctypes.c_bool
        trusted = bool(services.AXIsProcessTrusted())
    except Exception as exc:
        return WARN, "could not determine (%s)" % exc, None

    if trusted:
        return OK, "granted", None
    return (FAIL,
            "not granted — brightness, volume and mute cannot be changed",
            PANE_ACCESSIBILITY)


def _automation_chrome():
    ok, out = osascript('tell application "Google Chrome" to count windows',
                        timeout=6.0)
    if ok and out.strip().isdigit():
        return OK, "granted (%s window(s))" % out.strip(), None
    if "1743" in (out or ""):
        return FAIL, "not granted — cannot reach the meeting tab", PANE_AUTOMATION
    return WARN, "unclear: %s" % (out or "no response"), PANE_AUTOMATION


def _chrome_js():
    ok, out = osascript(
        'tell application "Google Chrome" to execute '
        '(active tab of front window) javascript "1+1"', timeout=6.0)
    if ok and out.strip() == "2":
        return OK, "enabled", None
    if "turned off" in (out or "").lower():
        return (FAIL,
                "off — meeting controls will not work. Chrome menu bar: "
                "View > Developer > Allow JavaScript from Apple Events "
                "(must be clicked by hand; it shows a confirmation dialog)",
                None)
    return WARN, "unclear: %s" % ((out or "no response")[:80]), None


def _mediakey():
    if ddc.mediakey_available():
        return OK, "built", None
    return (FAIL,
            "not built — display brightness/volume/mute unavailable. Run: make",
            None)


def _monitorcontrol():
    running = subprocess.run(["pgrep", "-x", "MonitorControl"],
                             capture_output=True).returncode == 0
    if running and ddc.available():
        return OK, "running, display state readable", None
    if running:
        return WARN, "running but no cached display state yet — adjust "\
                     "brightness once from its menu", None
    return (WARN,
            "not running — external display brightness/volume unavailable. "
            "Only needed for external displays.",
            None)


def _inputs():
    devices = audio.status()
    if not devices:
        return FAIL, "no input devices found", None
    unreadable = audio.unreadable()
    text = "%d device(s): %s" % (len(devices),
                                 ", ".join(d["name"] for d in devices))
    if unreadable:
        return WARN, text + " — unreadable: %s" % ", ".join(unreadable), None
    return OK, text, None


def _cameras():
    devices = camera.devices()
    if not devices:
        return WARN, "no cameras found", None
    return OK, "%d camera(s): %s" % (
        len(devices), ", ".join(d["name"] for d in devices)), None


CHECKS = [
    ("Accessibility", _accessibility),
    ("Automation (Chrome)", _automation_chrome),
    ("Chrome JS bridge", _chrome_js),
    ("Media-key helper", _mediakey),
    ("MonitorControl", _monitorcontrol),
    ("Audio inputs", _inputs),
    ("Cameras", _cameras),
]


def run_checks():
    """[{name, status, detail, pane}] for every check."""
    results = []
    for name, check in CHECKS:
        try:
            status, detail, pane = check()
        except Exception as exc:
            status, detail, pane = WARN, "check errored: %s" % exc, None
        results.append({"name": name, "status": status,
                        "detail": detail, "pane": pane})
    return results


def open_pane(url):
    """Open a System Settings pane."""
    return run(["open", url], timeout=5.0)[0]


def main():
    mark = {OK: "  ok  ", WARN: " warn ", FAIL: " FAIL "}
    results = run_checks()
    print()
    for r in results:
        print("[%s] %-22s %s" % (mark[r["status"]], r["name"], r["detail"]))
    print()
    failed = [r for r in results if r["status"] == FAIL]
    if failed:
        print("%d blocking issue(s). The panel will run, but those controls "
              "will not work." % len(failed))
    else:
        print("All required checks passed.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
