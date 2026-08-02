"""HTTP server for the onair panel.

Security model, in order of importance:

1. **The client can only send an action id.** Every id must already exist in
   controls.ACTIONS. There is no code path where client input reaches a shell.
   This is the load-bearing control; the token is defence in depth behind it.
2. **Bearer token**, generated on first run, stored at ~/.onair/token mode 600,
   compared with hmac.compare_digest.
3. **Bound to the LAN interface**, never 0.0.0.0.

Zero dependencies — stdlib only, so this runs on the system Python 3.9 with no
pip, no venv. That also means no tomllib, which is why config is JSON.
"""

import hmac
import json
import os
import posixpath
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

from . import controls

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _first_existing(*candidates):
    for path in candidates:
        if path and os.path.isdir(path):
            return path
    return candidates[-1]


# The panel is shared by every platform's agent, so it lives at the repo root
# rather than inside this one. Bundled builds copy it into Resources/panel.
WEB = _first_existing(
    os.path.join(ROOT, "panel"),                       # inside an .app bundle
    os.path.join(os.path.dirname(ROOT), "panel"),      # repo checkout
)
TOKEN_DIR = os.path.expanduser("~/.onair")
TOKEN_PATH = os.path.join(TOKEN_DIR, "token")

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
}


def load_token():
    """Read the shared token, generating it on first run."""
    if os.path.exists(TOKEN_PATH):
        with open(TOKEN_PATH) as handle:
            existing = handle.read().strip()
        if existing:
            return existing
    os.makedirs(TOKEN_DIR, mode=0o700, exist_ok=True)
    token = secrets.token_urlsafe(24)
    # Create 0600 before writing, so the secret is never briefly world-readable.
    fd = os.open(TOKEN_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(token + "\n")
    return token


def lan_address():
    """Primary LAN IP. Never binds 0.0.0.0."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # No traffic is sent; this just selects the default route's interface.
        sock.connect(("192.0.2.1", 9))
        return sock.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        sock.close()


class Handler(BaseHTTPRequestHandler):
    server_version = "onair"
    protocol_version = "HTTP/1.1"

    # ── plumbing ─────────────────────────────────────────────────────────────

    def log_message(self, fmt, *args):
        if self.server.verbose:
            BaseHTTPRequestHandler.log_message(self, fmt, *args)

    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code, payload):
        self._send(code, json.dumps(payload).encode(), CONTENT_TYPES[".json"])

    def _authorised(self, query):
        """Accept the token from a header, the query string, or a cookie.

        Three channels because each fails somewhere: the header needs JS to have
        found the token already, the query string is lost when a browser
        bookmarks a bare URL, and the cookie is absent inside an iOS Home Screen
        app (separate storage container). Together they cover every install path.
        """
        header = self.headers.get("Authorization", "")
        supplied = header[7:] if header.startswith("Bearer ") else ""
        if not supplied:
            supplied = (query.get("t") or [""])[0]
        if not supplied:
            for part in (self.headers.get("Cookie") or "").split(";"):
                name, _, value = part.strip().partition("=")
                if name == "onair_token":
                    supplied = unquote(value)
                    break
        return bool(supplied) and hmac.compare_digest(supplied, self.server.token)

    # ── routes ───────────────────────────────────────────────────────────────

    def do_GET(self):
        parsed = urlparse(self.path)
        route = parsed.path
        query = parse_qs(parsed.query)

        if route.startswith("/api/"):
            if not self._authorised(query):
                return self._json(401, {"error": "unauthorised"})
            if route == "/api/state":
                return self._json(200, self.server.state.snapshot())
            if route == "/api/layout":
                return self._json(200, {
                    "sites": self.server.config.get("sites", {}),
                    "actions": sorted(controls.ACTIONS),
                })
            return self._json(404, {"error": "unknown endpoint"})

        if route == "/manifest.json":
            return self._manifest(query)

        return self._static(route)

    def do_POST(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if not self._authorised(query):
            return self._json(401, {"error": "unauthorised"})

        prefix = "/api/action/"
        if not parsed.path.startswith(prefix):
            return self._json(404, {"error": "unknown endpoint"})

        action_id = parsed.path[len(prefix):]
        action = controls.ACTIONS.get(action_id)
        if action is None:
            # Unknown ids die here — the client cannot invent an action.
            return self._json(404, {"error": "unknown action"})

        payload = {}
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            try:
                payload = json.loads(self.rfile.read(length).decode() or "{}")
            except ValueError:
                return self._json(400, {"error": "bad json"})
        if not isinstance(payload, dict):
            return self._json(400, {"error": "payload must be an object"})

        result = action(self.server.config, self.server.state, payload)
        # Return the post-action snapshot. The client applies it directly and
        # uses its seq to discard any poll still in flight from before the call.
        return self._json(200, {
            "action": action_id,
            "result": result,
            "state": self.server.state.snapshot(),
        })

    def _manifest(self, query):
        """Served dynamically so `start_url` carries the token.

        iOS gives a Home Screen web app its own storage container, separate
        from the browser it was added from — so a token saved in Safari is
        simply absent once the app launches, and the panel greets you with
        "no token". Baking it into start_url means the icon launches already
        paired.

        Requires the token, because the manifest would otherwise hand it to
        anyone on the LAN who asked.
        """
        if not self._authorised(query):
            return self._json(401, {"error": "unauthorised"})
        return self._json(200, {
            "name": "onair",
            "short_name": "onair",
            "display": "standalone",
            "orientation": "landscape",
            "background_color": "#0a0b0d",
            "theme_color": "#0a0b0d",
            "start_url": "/?t=%s" % self.server.token,
        })

    # ── static files ─────────────────────────────────────────────────────────

    def _static(self, route):
        if route in ("/", ""):
            route = "/index.html"
        # Normalise then confine to WEB — no traversal out of the directory.
        clean = posixpath.normpath(route).lstrip("/")
        target = os.path.realpath(os.path.join(WEB, clean))
        if not target.startswith(os.path.realpath(WEB) + os.sep):
            return self._send(403, b"forbidden")
        if not os.path.isfile(target):
            return self._send(404, b"not found")
        ctype = CONTENT_TYPES.get(os.path.splitext(target)[1], "application/octet-stream")
        with open(target, "rb") as handle:
            self._send(200, handle.read(), ctype)


def _exit_if_orphaned(httpd, poller):
    """Exit if our parent dies, instead of lingering on the port.

    Learned the hard way. Killing the menu-bar app orphans this process, which
    keeps holding port 8770 — so the next agent cannot bind and every request
    is still answered by the stale one. That stale process also carries the old
    permission context, so controls silently fail while the code and the grants
    are both fine. Hours went into chasing that.
    """
    original_parent = os.getppid()
    if original_parent <= 1:
        return   # already parentless (launchd/manual), nothing to watch

    def watch():
        while True:
            time.sleep(2.0)
            if os.getppid() != original_parent:
                print("parent exited; shutting down", flush=True)
                poller.stop()
                threading.Thread(target=httpd.shutdown, daemon=True).start()
                time.sleep(1.0)
                os._exit(0)

    threading.Thread(target=watch, name="orphan-watch", daemon=True).start()


def serve(config, verbose=False):
    state = controls.State()
    poller = controls.Poller(config, state)
    poller.start()

    host = config.get("host", "auto")
    if host == "auto":
        host = lan_address()
    port = int(config.get("port", 8770))

    try:
        httpd = ThreadingHTTPServer((host, port), Handler)
    except OSError as exc:
        # Failing loudly matters: a silent bind failure leaves an older agent
        # answering requests, which looks like the new code being broken.
        print("cannot bind %s:%d — %s\n"
              "Another agent is probably still running: pkill -f agent.server"
              % (host, port, exc), flush=True)
        raise SystemExit(1)
    httpd.daemon_threads = True
    httpd.token = load_token()
    httpd.config = config
    httpd.state = state
    httpd.verbose = verbose

    url = "http://%s:%d/?t=%s" % (host, port, httpd.token)
    # flush explicitly — stdout is block-buffered when not a tty, which
    # otherwise swallows this until the process exits.
    print("onair listening on http://%s:%d" % (host, port), flush=True)
    print("\nOpen this on the iPad once, then Add to Home Screen:\n\n  %s\n" % url,
          flush=True)
    print("The token is stored after first load; later visits need no query string.",
          flush=True)
    _exit_if_orphaned(httpd, poller)
    return httpd, poller


def config_path():
    """User config wins; the shared default ships with the repo."""
    user = os.path.expanduser("~/.onair/config.json")
    if os.path.isfile(user):
        return user
    return _first_existing_file(
        os.path.join(ROOT, "config.json"),                                  # bundled
        os.path.join(os.path.dirname(ROOT), "protocol", "config.default.json"),
    )


def _first_existing_file(*candidates):
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return candidates[-1]


def main():
    with open(config_path()) as handle:
        config = json.load(handle)
    httpd, poller = serve(config, verbose=bool(os.environ.get("ONAIR_VERBOSE")))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        poller.stop()
        httpd.shutdown()


if __name__ == "__main__":
    main()
