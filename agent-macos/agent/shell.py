"""Subprocess helpers.

Every call here is timeout-bounded and passes an argument list — never a shell
string. Two hard-won rules behind that:

1. `shell=True` would be the one place client input could reach a shell. It is
   never used, anywhere.
2. Timeouts are not optional. A macOS automation call with an ungranted
   permission does not return an error — it *blocks indefinitely* waiting on a
   TCC prompt that may never be answered. One such call without a timeout
   freezes the state poller forever.
"""

import subprocess

TIMEOUT = "__timeout__"
NOTAB = "__notab__"


def run(args, timeout=4.0):
    """Run argv. Returns (ok, output). Never raises.

    Captures bytes and decodes with errors="replace" rather than using
    text=True. `ioreg -l` embeds raw binary property data, which makes strict
    UTF-8 decoding raise UnicodeDecodeError partway through an otherwise fine
    2MB of output — silently turning a working probe into a permanent failure.
    """
    try:
        p = subprocess.run(args, capture_output=True, timeout=timeout)
        raw = p.stdout or p.stderr or b""
        return p.returncode == 0, raw.decode("utf-8", "replace").strip()
    except subprocess.TimeoutExpired:
        return False, TIMEOUT
    except Exception as exc:  # missing binary, OS refusal, etc.
        return False, "%s: %s" % (type(exc).__name__, exc)


def osascript(script, timeout=4.0):
    """Run an AppleScript snippet. Returns (ok, output)."""
    return run(["osascript", "-e", script], timeout=timeout)


def as_string(text):
    """Escape a Python string for embedding inside an AppleScript literal."""
    return text.replace("\\", "\\\\").replace('"', '\\"')
