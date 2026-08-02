/* onair panel client.
 *
 * Two rules mirror the agent's design:
 *
 * 1. The poll is the source of truth. Taps update the UI optimistically so the
 *    panel feels instant, but when the next poll disagrees, the poll wins. This
 *    is what stops the panel from confidently displaying a stale state.
 * 2. Every control has three states, never two. `unknown` renders distinctly
 *    from `off` — conflating them is how a panel lies about a live mic.
 */
(function () {
  'use strict';

  var POLL_MS = 1000;
  var STEP = 1;                 // ± pads nudge by 1%; faders drag freely
  var WRITE_THROTTLE_MS = 100;  // a drag emits far faster than osascript can run

  // ── token ──────────────────────────────────────────────────────────────────
  // Delivered once via ?t=, then persisted so the Home Screen icon needs no
  // query string. Stripped from the URL immediately so it stays out of history.
  var token = new URLSearchParams(location.search).get('t');
  if (token) {
    try { localStorage.setItem('onair.token', token); } catch (e) {}
    history.replaceState(null, '', location.pathname);
  } else {
    try { token = localStorage.getItem('onair.token'); } catch (e) { token = null; }
  }

  var $ = function (sel) { return document.querySelector(sel); };

  // Point the manifest at a tokenised URL so "Add to Home Screen" captures a
  // start_url that is already paired. Without this the Home Screen app opens
  // in its own empty storage container and cannot authenticate.
  if (token) {
    var manifest = document.querySelector('link[rel="manifest"]');
    if (manifest) manifest.setAttribute('href', '/manifest.json?t=' + token);
  }
  var lamp = $('#lamp'), banner = $('#banner'), ctx = $('#ctx');
  var LABEL = { live: 'ON AIR', off: 'OFF', unknown: 'NO SIGNAL' };

  function api(path, options) {
    options = options || {};
    options.headers = { 'Authorization': 'Bearer ' + token };
    if (options.body) options.headers['Content-Type'] = 'application/json';
    return fetch(path, options).then(function (r) {
      if (!r.ok) throw new Error('http ' + r.status);
      return r.json();
    });
  }

  function send(action, payload, key) {
    markBusy(key);
    return api('/api/action/' + action, {
      method: 'POST',
      body: JSON.stringify(payload || {})
    }).then(function (res) {
      // Clear before applying, so this control renders from its own
      // post-action truth — which includes whether the write actually took.
      clearBusy(key);
      if (res && res.state) absorb(res.state);
      return res;
    }).catch(function () { clearBusy(key); });
  }

  // ── optimistic reconciliation ──────────────────────────────────────────────
  //
  // Taps and drags paint immediately, but the poller runs on its own 1s cadence
  // and a poll already in flight carries a snapshot from *before* the change.
  // Letting it land produced a visible new → old → new flicker.
  //
  // Every state mutation on the agent bumps a sequence number, and an action
  // returns the snapshot it produced. Applying that response and then dropping
  // any poll with a lower seq makes stale data impossible to render — it is an
  // ordering fact, not a guess.
  //
  // Matching the *values* within a tolerance was tried first and is too fragile:
  // a small adjustment (50 → 51) falls inside any sane tolerance, so the stale
  // reading is mistaken for confirmation and the flicker returns.

  // A control with a write in flight is not rendered from polls. `seq` orders
  // events *server-side*, but it cannot know about a change made locally that
  // the agent has not processed yet — so a poll issued between the tap and the
  // action landing carries a legitimately-current old value, and renders as a
  // snap-back. The deadline is a safety net: a request that never returns must
  // not freeze the control forever.
  var busy = {};
  var BUSY_MS = 4000;

  function markBusy(key) { if (key) busy[key] = Date.now() + BUSY_MS; }
  function clearBusy(key) { if (key) delete busy[key]; }
  function isBusy(key) { return busy[key] && Date.now() < busy[key]; }

  var minSeq = 0;
  var boot = null;
  var rejected = 0;
  var MAX_REJECTS = 5;

  function absorb(state) {
    if (!state) return;

    // The agent's seq restarts at 0 when the agent does. Without noticing that,
    // a client holding the previous run's high-water mark rejects every poll
    // forever — the panel freezes while still looking connected, which is far
    // worse than the flicker this gate exists to prevent.
    if (state.boot && state.boot !== boot) {
      boot = state.boot;
      minSeq = 0;
      rejected = 0;
    }

    if ((state.seq || 0) < minSeq) {
      // Safety valve: staleness rejection must never wedge the panel
      // permanently. Whatever the cause, give up after a few and trust the data.
      if (++rejected < MAX_REJECTS) return;
      minSeq = 0;
    }

    rejected = 0;
    minSeq = state.seq || 0;
    apply(state);
  }

  // ── rendering ──────────────────────────────────────────────────────────────

  function renderTile(el, state) {
    var s = state === 'live' || state === 'off' ? state : 'unknown';
    el.dataset.s = s;
    el.querySelector('.txt').textContent = LABEL[s];
  }

  function renderMute(info) {
    var el = $('#mute');
    if (!el) return;
    info = info || {};
    var known = typeof info.muted === 'boolean';
    el.dataset.on = (known && info.muted) ? '1' : '0';
    el.disabled = !info.writable;
  }

  function renderStepper(sp, info) {
    info = info || {};
    var value = info.value;
    var known = typeof value === 'number' && isFinite(value);
    // Readable but not settable is a real state: the value is true, the
    // controls are dead. Showing live buttons over it would be a lie.
    var writable = known && info.writable !== false;
    var v = known ? Math.max(0, Math.min(100, value)) : 0;

    sp.dataset.v = v;
    sp.dataset.known = known ? '1' : '0';
    sp.dataset.writable = writable ? '1' : '0';
    sp.classList.toggle('readonly', known && !writable);
    sp.querySelector('.pct').textContent = known ? Math.round(v) + '%' : '—';
    // Name the reason a control is dead. "Read only" with no explanation reads
    // as a bug; "use the monitor" tells you where the real control is.
    var why = sp.querySelector('.why');
    if (why) why.textContent = (!writable && info.reason) ? info.reason : '';
    sp.querySelector('.fill').style.height = v + '%';
    sp.querySelector('.vfader').dataset.low = v < 15 ? '1' : '0';
    sp.querySelectorAll('.pad').forEach(function (p) { p.disabled = !writable; });
  }

  function apply(state) {
    if (!isBusy('mic')) {
      renderTile($('[data-ctl="mic"]'), state.mic && state.mic.state);
    }
    if (!isBusy('camera')) {
      renderTile($('[data-ctl="camera"]'), state.camera && state.camera.state);
    }
    if (!dragging.volume && !isBusy('volume')) {
      renderStepper($('[data-step="volume"]'), state.volume);
    }
    if (!isBusy('mute')) renderMute(state.mute);
    if (!dragging.brightness && !isBusy('brightness')) {
      renderStepper($('[data-step="brightness"]'), state.brightness);
    }

    var label = (state.context && state.context.label) || 'No meeting';
    ctx.innerHTML = '<b></b>';
    ctx.firstChild.textContent = label;

    var notes = state.notes || [];
    banner.textContent = notes.join(' · ');
    banner.classList.toggle('show', notes.length > 0);
    lamp.className = 'lamp' + (notes.length ? ' warn' : '');
  }

  // ── polling ────────────────────────────────────────────────────────────────

  var timer = null;
  function poll() {
    return api('/api/state').then(function (state) {
      document.body.classList.remove('offline');
      absorb(state);
    }).catch(function () {
      document.body.classList.add('offline');
      lamp.className = 'lamp bad';
    });
  }

  function startPolling() {
    if (timer) clearInterval(timer);
    poll();
    timer = setInterval(poll, POLL_MS);
  }

  // ── keep the screen awake ──────────────────────────────────────────────────
  //
  // A panel that blanks after 30s is not a panel — you would wake it more often
  // than you use it. The lock is released by the OS whenever the page is
  // hidden and is NOT restored automatically, so it has to be re-acquired every
  // time the panel comes back. Requires iPadOS 16.4+; older versions simply do
  // without rather than breaking.

  var wakeLock = null;

  function acquireWake() {
    if (!navigator.wakeLock || wakeLock) return;
    navigator.wakeLock.request('screen').then(function (lock) {
      wakeLock = lock;
      lock.addEventListener('release', function () { wakeLock = null; });
    }).catch(function () {
      // Denied, or the document is not visible. Retried on the next
      // visibility change or touch — some browsers want a user gesture first.
    });
  }

  // iPadOS suspends background tabs; refetch on resume before rendering stale
  // state, or the panel shows whatever was true when you last looked at it.
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'visible') {
      startPolling();
      acquireWake();
    } else if (timer) {
      clearInterval(timer);
      timer = null;
    }
  });

  // Gesture fallback: the first request can be refused without user interaction.
  document.addEventListener('pointerdown', acquireWake, { once: false });

  // ── taps ───────────────────────────────────────────────────────────────────

  document.querySelectorAll('[data-action]').forEach(function (el) {
    if (el.classList.contains('vstepper')) return;
    el.addEventListener('click', function () {
      var tile = el.classList.contains('tile');
      if (tile && el.dataset.s !== 'unknown') {
        // Optimistic flip, held until a poll confirms it or the deadline passes.
        var next = el.dataset.s === 'live' ? 'off' : 'live';
        renderTile(el, next);
      }
      send(el.dataset.action, null, el.dataset.ctl);
    });
  });

  // ── faders ─────────────────────────────────────────────────────────────────

  var dragging = { volume: false, brightness: false };

  document.querySelectorAll('.vstepper').forEach(function (sp) {
    var key = sp.dataset.step, action = sp.dataset.action;
    var fader = sp.querySelector('.vfader');
    var lastWrite = 0, queued = null;

    function paint(v) {
      v = Math.max(0, Math.min(100, v));
      sp.dataset.v = v;
      sp.querySelector('.pct').textContent = Math.round(v) + '%';
      sp.querySelector('.fill').style.height = v + '%';
      fader.dataset.low = v < 15 ? '1' : '0';
      return v;
    }

    // Throttled during the drag, always committed on release, so the final
    // position is never lost to the throttle window.
    function write(v, force) {
      var now = Date.now();
      if (!force && now - lastWrite < WRITE_THROTTLE_MS) { queued = v; return; }
      lastWrite = now; queued = null;
      send(action, { value: Math.round(v) }, key);
    }

    // Relative drag, not absolute jump-to-position.
    //
    // Touching the fader does NOT set the value to wherever the finger landed —
    // it only grabs. Sliding then moves the level by the distance travelled, so
    // the control adjusts from whatever it already was. Brushing the panel can
    // no longer slam brightness to an arbitrary value.
    //
    // The delta is measured from the previous move rather than from the touch
    // origin, so hitting 0 or 100 and sliding back responds immediately instead
    // of dragging through a dead zone equal to the overshoot.
    var lastY = 0, moved = false;

    fader.addEventListener('pointerdown', function (e) {
      if (sp.dataset.writable !== '1') return;
      e.preventDefault();
      dragging[key] = true;
      moved = false;
      lastY = e.clientY;
      fader.classList.add('grab');
      fader.setPointerCapture(e.pointerId);
    });
    fader.addEventListener('pointermove', function (e) {
      if (!dragging[key]) return;
      // Inverted: dragging up raises the level. Height, not width, and the
      // full panel height of travel means ~12px per percent instead of ~4.
      var height = fader.getBoundingClientRect().height;
      var delta = ((lastY - e.clientY) / height) * 100;
      if (!delta) return;
      lastY = e.clientY;
      moved = true;
      write(paint(parseFloat(sp.dataset.v) + delta));
    });
    ['pointerup', 'pointercancel'].forEach(function (ev) {
      fader.addEventListener(ev, function () {
        if (!dragging[key]) return;
        dragging[key] = false;
        fader.classList.remove('grab');
        // A tap that never moved is not an adjustment — committing it would
        // cost a needless osascript round-trip to set the value it already has.
        if (moved) {
          var final = parseFloat(sp.dataset.v);
          write(final, true);
          setTimeout(poll, 250);
        }
      });
    });

    sp.querySelectorAll('.pad').forEach(function (pad) {
      var d = parseInt(pad.dataset.d, 10), delay = null, repeat = null;
      function step() {
        var next = paint(parseFloat(sp.dataset.v) + d * STEP);
        write(next, true);
      }
      function stop() {
        clearTimeout(delay); clearInterval(repeat);
        delay = repeat = null;
        setTimeout(poll, 250);
      }
      pad.addEventListener('pointerdown', function (e) {
        if (sp.dataset.writable !== '1') return;
        e.preventDefault();
        step();
        delay = setTimeout(function () { repeat = setInterval(step, 140); }, 400);
      });
      ['pointerup', 'pointerleave', 'pointercancel'].forEach(function (ev) {
        pad.addEventListener(ev, stop);
      });
    });
  });

  // ── mute ───────────────────────────────────────────────────────────────────

  (function () {
    var el = $('#mute');
    if (!el) return;
    el.addEventListener('click', function () {
      if (el.disabled) return;
      el.dataset.on = el.dataset.on === '1' ? '0' : '1';   // optimistic
      send('mute.toggle', null, 'mute');
    });
  })();

  // ── install helper ─────────────────────────────────────────────────────────
  //
  // iOS provides no way to install a home-screen web app programmatically — no
  // beforeinstallprompt, no URL scheme, nothing a QR code can trigger. Apple
  // requires Share > Add to Home Screen by hand. Android Chrome does fire
  // beforeinstallprompt, so there it really is one tap.
  //
  // What this removes is the guesswork: it says which step you are on, and it
  // catches the trap of opening the link in Chrome on iOS, which silently
  // cannot install at all and offers no hint that anything is wrong.

  (function () {
    var standalone = window.matchMedia('(display-mode: standalone)').matches
                     || window.navigator.standalone === true;
    if (standalone) return;               // already installed, nothing to say

    var ua = navigator.userAgent;
    var isIOS = /iPad|iPhone|iPod/.test(ua)
                || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
    var iosSafari = isIOS && !/CriOS|FxiOS|EdgiOS/.test(ua);
    var iosOther = isIOS && !iosSafari;

    var tip = document.createElement('div');
    tip.className = 'install';
    var deferred = null;

    function show(html, withButton) {
      tip.innerHTML = '';
      var text = document.createElement('span');
      text.innerHTML = html;
      tip.appendChild(text);
      if (withButton) {
        var btn = document.createElement('button');
        btn.textContent = 'Install';
        btn.addEventListener('click', function () {
          if (!deferred) return;
          deferred.prompt();
          deferred.userChoice.then(function () { tip.remove(); });
        });
        tip.appendChild(btn);
      }
      var close = document.createElement('button');
      close.className = 'x';
      close.textContent = '\u00d7';
      close.setAttribute('aria-label', 'Dismiss');
      close.addEventListener('click', function () { tip.remove(); });
      tip.appendChild(close);
      if (!tip.parentNode) document.body.appendChild(tip);
    }

    window.addEventListener('beforeinstallprompt', function (e) {
      e.preventDefault();
      deferred = e;
      show('Add onair to your home screen', true);
    });

    if (iosOther) {
      show('To install, open this link in <b>Safari</b> — '
           + 'other iOS browsers cannot add to the home screen.', false);
    } else if (iosSafari) {
      show('Install: tap <b>Share</b>, then <b>Add to Home Screen</b>.', false);
    }
  })();

  // ── clock ──────────────────────────────────────────────────────────────────

  function clock() {
    var d = new Date();
    $('#clock').textContent =
      String(d.getHours()).padStart(2, '0') + ':' +
      String(d.getMinutes()).padStart(2, '0');
  }
  clock(); setInterval(clock, 10000);

  if (!token) {
    banner.textContent = 'Not paired. This browser has no token — each browser '
      + 'and the Home Screen app each need pairing once. Get the link from the '
      + 'onair menu-bar icon (Pair iPad, or Copy pairing link).';
    banner.classList.add('show');
  } else {
    startPolling();
    acquireWake();
  }
})();
