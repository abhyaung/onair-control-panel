"""Drive conferencing controls through Chrome's DOM.

All conferencing on this machine is web-based (no Zoom or Teams app installed),
so one mechanism covers every platform: AppleScript's `execute javascript`
against the matching Chrome tab.

This beats sending keystrokes because it reads the *real* state back out of the
page rather than tracking what we believe we set, and it does not steal focus.

Requires: Chrome > View > Developer > Allow JavaScript from Apple Events, plus a
one-time Automation grant. Without either, calls fail or hang — hence timeouts
on every call.

Selector note: these are matched by `aria-label` text rather than class names,
because Meet's generated class names change constantly while its accessibility
labels are comparatively stable. They still need validating against a live call.
"""

from ..shell import osascript, as_string, TIMEOUT, NOTAB

# One self-contained call returns every control's state, because each osascript
# invocation costs 100-300ms and the poller runs on a 1s budget. Probing mic,
# camera separately would overrun the interval on its own.
#
# Returns "mic:<s>|cam:<s>" where <s> is live | off | none.
# One self-contained call returns every control's state, because each osascript
# invocation costs 100-300ms and the poller runs on a 1s budget.
#
# State is read from the button's **action verb**, which is what every platform
# actually exposes: a control labelled "Turn off camera" means the camera is on.
# Meet also has `data-is-muted`, but Teams has no equivalent and Zoom differs
# again — the verb is the one thing common to all three, so it is the primary
# signal and the attribute is only a tiebreaker.
#
# Returns "mic:<s>|cam:<s>" where <s> is live | off | none.
PROBE_ALL_JS = """
(function(){
  var L = %(labels)s;
  // Zoom renders its entire meeting UI inside a same-origin iframe, so querying
  // only the top document finds the app shell (Home, Meetings, Settings) and
  // concludes there is no meeting. Walk into every frame we are allowed to read;
  // cross-origin frames throw and are skipped.
  function collect(doc, out){
    try {
      var found = doc.querySelectorAll('button,[role=button],[data-is-muted]');
      for (var i = 0; i < found.length; i++) out.push(found[i]);
      var frames = doc.querySelectorAll('iframe');
      for (var f = 0; f < frames.length; f++){
        try { var inner = frames[f].contentDocument; if (inner) collect(inner, out); }
        catch (e) {}
      }
    } catch (e) {}
    return out;
  }
  var nodes = collect(document, []);
  // Word-boundary match, NOT indexOf. Teams labels a muted mic "Unmute mic",
  // and "unmute mic" *contains* "mute mic" — so a plain substring test matched
  // the live pattern and reported a muted mic as ON AIR. Requiring a non-letter
  // (or start of string) before the pattern makes "mute" and "unmute" distinct.
  function hit(label, pattern){
    var i = label.indexOf(pattern);
    while (i >= 0){
      var before = i === 0 ? '' : label.charAt(i - 1);
      if (!/[a-z]/.test(before)) return true;
      i = label.indexOf(pattern, i + 1);
    }
    return false;
  }
  function scan(kind){
    var spec = L[kind]; if (!spec) return 'none';
    for (var i = 0; i < nodes.length; i++){
      var el = nodes[i];
      var label = (el.getAttribute('aria-label') || el.getAttribute('title') || '')
                    .toLowerCase();
      if (!label) continue;
      for (var a = 0; a < spec.on.length; a++)
        if (hit(label, spec.on[a])) return 'live';
      for (var b = 0; b < spec.off.length; b++)
        if (hit(label, spec.off[b])) return 'off';
    }
    return 'none';
  }
  return 'mic:' + scan('mic') + '|cam:' + scan('cam');
})()
"""

CLICK_JS = """
(function(){
  var needles = %(needles)s;
  function hit(label, pattern){
    var i = label.indexOf(pattern);
    while (i >= 0){
      var before = i === 0 ? '' : label.charAt(i - 1);
      if (!/[a-z]/.test(before)) return true;
      i = label.indexOf(pattern, i + 1);
    }
    return false;
  }
  // Zoom renders its entire meeting UI inside a same-origin iframe, so querying
  // only the top document finds the app shell (Home, Meetings, Settings) and
  // concludes there is no meeting. Walk into every frame we are allowed to read;
  // cross-origin frames throw and are skipped.
  function collect(doc, out){
    try {
      var found = doc.querySelectorAll('button,[role=button],[data-is-muted]');
      for (var i = 0; i < found.length; i++) out.push(found[i]);
      var frames = doc.querySelectorAll('iframe');
      for (var f = 0; f < frames.length; f++){
        try { var inner = frames[f].contentDocument; if (inner) collect(inner, out); }
        catch (e) {}
      }
    } catch (e) {}
    return out;
  }
  var nodes = collect(document, []);
  for (var i = 0; i < nodes.length; i++){
    var el = nodes[i];
    var label = (el.getAttribute('aria-label') || el.getAttribute('title') || '')
                  .toLowerCase();
    if (!label) continue;
    for (var n = 0; n < needles.length; n++){
      if (hit(label, needles[n])){ el.click(); return 'ok'; }
    }
  }
  return 'none';
})()
"""

VALID = ("live", "off", "none")


def run_js(url_match, js, timeout=5.0):
    """Execute JS in the first Chrome tab whose URL contains url_match.

    Returns the JS result, or None on any failure. None is deliberately
    indistinguishable from 'the call broke' — callers must treat it as unknown,
    never as off.
    """
    script = (
        'tell application "Google Chrome"\n'
        "  repeat with w in windows\n"
        "    repeat with t in tabs of w\n"
        '      if URL of t contains "%s" then\n'
        '        return execute t javascript "%s"\n'
        "      end if\n"
        "    end repeat\n"
        "  end repeat\n"
        "end tell\n"
        'return "%s"' % (as_string(url_match), as_string(js), NOTAB)
    )
    ok, out = osascript(script, timeout=timeout)
    if not ok or out == NOTAB:
        return None
    return out


def open_tabs():
    """URLs of every Chrome tab, in one call.

    With several conferencing sites configured, probing each one separately
    costs an osascript round-trip per site per poll — four sites at ~200ms
    overruns the 1s budget on its own. One enumeration lets the poller probe
    only the sites that actually have a tab open.
    """
    script = (
        'set out to ""\n'
        'tell application "Google Chrome"\n'
        "  repeat with w in windows\n"
        "    repeat with t in tabs of w\n"
        '      set out to out & (URL of t) & linefeed\n'
        "    end repeat\n"
        "  end repeat\n"
        "end tell\n"
        "return out"
    )
    ok, out = osascript(script, timeout=5.0)
    return [line for line in (out or "").splitlines() if line.strip()] if ok else []


def matching_sites(sites, urls=None):
    """Subset of `sites` (url_match strings) that currently have a tab open."""
    urls = open_tabs() if urls is None else urls
    return [s for s in sites if any(s in u for u in urls)]


def has_tab(url_match):
    """Whether a tab matching url_match exists — needs no JS bridge."""
    script = (
        'tell application "Google Chrome"\n'
        "  repeat with w in windows\n"
        "    repeat with t in tabs of w\n"
        '      if URL of t contains "%s" then return "yes"\n'
        "    end repeat\n"
        "  end repeat\n"
        "end tell\n"
        'return "no"' % as_string(url_match)
    )
    ok, out = osascript(script, timeout=5.0)
    return ok and out == "yes"


def _js_literal(value):
    return repr(value).replace("'", '"')


def probe_all(url_match, labels=None):
    """One call, both controls. Returns dict of mic/cam -> live|off|None.

    Only mic and camera are driven. Raise-hand, chat, leave and screen-share
    were dropped deliberately: they are the most platform-specific controls and
    the least useful on a panel, so supporting them made the tool less generic
    for no real gain.
    """
    blank = {"mic": None, "cam": None}
    spec = {k: labels.get(k) for k in ("mic", "cam")} if labels else {}
    spec = {k: v for k, v in spec.items() if v}
    if not spec:
        return blank
    out = run_js(url_match, PROBE_ALL_JS % {"labels": _js_literal(spec)})
    if not out:
        return blank
    parsed = dict(blank)
    for part in out.split("|"):
        if ":" not in part:
            continue
        key, _, value = part.partition(":")
        if key in parsed and value in VALID:
            parsed[key] = None if value == "none" else value
    return parsed


def click_labeled(url_match, needles):
    """Click the first control whose label contains any of `needles`."""
    if not needles:
        return False
    js = CLICK_JS % {"needles": _js_literal(list(needles))}
    return run_js(url_match, js) == "ok"


def toggle(url_match, kind, labels=None):
    """Toggle mic or cam by clicking whichever of its labels is present."""
    spec = (labels or {}).get(kind) or {}
    return click_labeled(url_match, list(spec.get("on", [])) + list(spec.get("off", [])))


# ── keystroke fallback ────────────────────────────────────────────────────────
# Used when the JS bridge is unavailable — Chrome's "Allow JavaScript from Apple
# Events" is off by default, and a panel that stops working because of a browser
# preference is not much of a panel.
#
# Costs a brief focus steal: Chrome comes forward, the tab is selected, the
# shortcut is sent, and the previous app is restored. That flicker is exactly why
# the DOM path is preferred whenever it is available.

MEET_KEYS = {"mic": "d", "cam": "e"}   # Google Meet: cmd-D mic, cmd-E camera


def keystroke(url_match, kind, keys=None):
    """Send the meeting app's own shortcut to the matching tab.

    Returns True if the tab was found and the keystroke sent. Cannot confirm the
    control actually changed — the caller should re-read real state (CoreAudio /
    CoreMediaIO) rather than trust this.
    """
    keys = keys or MEET_KEYS
    key = keys.get(kind)
    if not key:
        return False

    script = '''
    set previousApp to ""
    try
      tell application "System Events"
        set previousApp to name of first application process whose frontmost is true
      end tell
    end try

    set found to false
    tell application "Google Chrome"
      repeat with w in windows
        set i to 0
        repeat with t in tabs of w
          set i to i + 1
          if URL of t contains "%s" then
            set active tab index of w to i
            set index of w to 1
            activate
            set found to true
            exit repeat
          end if
        end repeat
        if found then exit repeat
      end repeat
    end tell

    if found then
      delay 0.25
      tell application "System Events" to keystroke "%s" using {command down}
      delay 0.15
      if previousApp is not "" and previousApp is not "Google Chrome" then
        try
          tell application previousApp to activate
        end try
      end if
      return "ok"
    end if
    return "%s"
    ''' % (as_string(url_match), key, NOTAB)

    ok, out = osascript(script, timeout=10.0)
    return ok and out == "ok"
