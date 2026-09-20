"""MonitorPad agent -- a small LAN HTTP server that exposes this PC's
displays to the phone app.

Run it with:  python server.py

Standard library only. It binds to every interface on the LAN so the phone
can reach it, and every API call must carry the access token printed at
startup.
"""

import argparse
import hmac
import json
import logging
import logging.handlers
import mimetypes
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import display

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_ROOT = os.path.join(HERE, "web")

CONFIG_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "MonitorPad")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
LOG_PATH = os.path.join(CONFIG_DIR, "monitorpad.log")

DEFAULT_PORT = 8777
# How long the phone has to confirm a layout change before it is rolled back.
CONFIRM_SECONDS = 15


# --------------------------------------------------------------------- state


class Config:
    def __init__(self):
        # showDisconnected puts remembered-but-absent displays back in the
        # list; off by default, flip it in config.json if you want them.
        self.data = {"token": "", "profiles": {}, "known": {},
                     "showDisconnected": False}
        self._lock = threading.Lock()
        self.load()

    def load(self):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
                self.data.update(json.load(handle))
        except (OSError, ValueError):
            pass
        if not self.data.get("token"):
            self.data["token"] = secrets.token_urlsafe(24)
            self.save()

    def save(self):
        with self._lock:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            tmp = CONFIG_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(self.data, handle, indent=2)
            os.replace(tmp, CONFIG_PATH)


class PendingChange:
    """A change that rolls itself back unless the phone confirms in time."""

    def __init__(self, snapshot, seconds):
        self.id = secrets.token_urlsafe(8)
        self.snapshot = snapshot
        self.deadline = time.time() + seconds
        self.timer = None
        self.reverted = False
        self.errors = []


class Pending:
    def __init__(self):
        self._lock = threading.Lock()
        self.current = None
        self.last_revert = None

    def arm(self, snapshot, seconds=CONFIRM_SECONDS):
        with self._lock:
            self._cancel_timer()
            change = PendingChange(snapshot, seconds)
            self.current = change
            change.timer = threading.Timer(seconds, self._expire, [change.id])
            change.timer.daemon = True
            change.timer.start()
            return change

    def _cancel_timer(self):
        if self.current and self.current.timer:
            self.current.timer.cancel()

    def _expire(self, change_id):
        with self._lock:
            change = self.current
            if change is None or change.id != change_id:
                return
            self.current = None
        log("No confirmation for {} -- reverting.".format(change_id))
        errors = display.restore(change.snapshot)
        change.reverted = True
        change.errors = errors
        self.last_revert = {"id": change_id, "at": time.time(),
                            "errors": errors}
        if errors:
            log("Revert finished with problems: " + "; ".join(errors))

    def confirm(self, change_id):
        with self._lock:
            change = self.current
            if change is None or change.id != change_id:
                return False
            self._cancel_timer()
            self.current = None
            return True

    def revert_now(self, change_id):
        with self._lock:
            change = self.current
            if change is None or change.id != change_id:
                return None
            self._cancel_timer()
            self.current = None
        errors = display.restore(change.snapshot)
        self.last_revert = {"id": change_id, "at": time.time(),
                            "errors": errors}
        return errors

    def info(self):
        change = self.current
        if change is None:
            return None
        remaining = max(0, round(change.deadline - time.time()))
        return {"id": change.id, "secondsLeft": remaining}


class PairingPin:
    """A short code that trades itself for the real token, once.

    Typing a 32-character token on a phone keyboard is miserable, so the
    agent prints a six-digit PIN instead. It is deliberately short-lived and
    gives up after a handful of wrong guesses, which is what keeps six digits
    from being a weak secret.
    """

    LIFETIME = 600
    MAX_ATTEMPTS = 5

    def __init__(self):
        self._lock = threading.Lock()
        self.value = None
        self.expires = 0.0
        self.attempts = 0
        self.spent_reason = None

    def issue(self):
        with self._lock:
            self.value = "{:06d}".format(secrets.randbelow(1_000_000))
            self.expires = time.time() + self.LIFETIME
            self.attempts = 0
            self.spent_reason = None
            return self.value

    def status(self):
        with self._lock:
            if not self.value or time.time() > self.expires:
                return None
            return {"pin": self.value,
                    "secondsLeft": round(self.expires - time.time())}

    RETRY = " Restart the agent on your PC for a fresh one."

    def redeem(self, candidate):
        with self._lock:
            if self.spent_reason:
                raise display.DisplayError(self.spent_reason + self.RETRY)
            if not self.value or time.time() > self.expires:
                self.spent_reason = "That PIN has expired."
                raise display.DisplayError(self.spent_reason + self.RETRY)
            self.attempts += 1
            if self.attempts > self.MAX_ATTEMPTS:
                self.value = None
                self.spent_reason = "Too many wrong PIN attempts."
                raise display.DisplayError(self.spent_reason + self.RETRY)
            if not hmac.compare_digest(str(candidate).strip(), self.value):
                left = self.MAX_ATTEMPTS - self.attempts
                raise display.DisplayError(
                    "Wrong PIN. {} attempt{} left.".format(
                        left, "" if left == 1 else "s"))
            self.value = None  # single use
            self.spent_reason = "That PIN has already been used."
            return CONFIG.data["token"]


CONFIG = Config()
PENDING = Pending()
PIN = PairingPin()


def _build_logger():
    """Log to a rotating file beside the config, and to the console if there
    is one. The tray app has no console, so the file is the only record of
    what happened -- which is the whole point of having it."""
    logger = logging.getLogger("monitorpad")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    if logger.handlers:
        return logger

    fmt = logging.Formatter("%(asctime)s.%(msecs)03d  %(levelname)-7s "
                            "%(message)s", "%Y-%m-%d %H:%M:%S")
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        rotating = logging.handlers.RotatingFileHandler(
            LOG_PATH, maxBytes=512 * 1024, backupCount=3, encoding="utf-8")
        rotating.setFormatter(fmt)
        rotating.setLevel(logging.DEBUG)
        logger.addHandler(rotating)
    except OSError:
        pass  # A read-only disk must not stop the agent from running.

    # Some Python builds give a windowless process no stdout at all.
    if getattr(sys, "stdout", None) is not None:
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(fmt)
        console.setLevel(logging.INFO)
        logger.addHandler(console)
    return logger


LOGGER = _build_logger()


def log(message):
    LOGGER.info(message)


def log_debug(message):
    LOGGER.debug(message)


def log_warning(message):
    LOGGER.warning(message)


def log_exception(message):
    LOGGER.error(message, exc_info=True)


# ----------------------------------------------------------------- operations


# A monitor that has not been seen for this long is dropped from the list of
# remembered displays, so unplugging something for good does not leave a
# ghost in the app forever.
FORGET_AFTER_DAYS = 60


def remember(monitors):
    """Record the displays we can currently see.

    Windows stops reporting a monitor entirely once its output goes quiet --
    a TV on standby or switched to another input has no EDID to read, so it
    vanishes from QueryDisplayConfig rather than showing up as "off". Keeping
    our own list means the app can still show it and say why it is unusable,
    instead of the display silently disappearing.
    """
    known = CONFIG.data.setdefault("known", {})
    now = time.time()
    changed = False

    for mon in monitors:
        entry = known.get(mon["key"])
        fresh = {
            "name": mon["name"],
            "port": mon["port"],
            "devicePath": mon["devicePath"],
            "native": mon["native"],
            "adapterKey": mon.get("adapterKey", ""),
            "lastSeen": now,
        }
        if entry is None or any(entry.get(k) != v for k, v in fresh.items()
                                if k != "lastSeen"):
            changed = True
        else:
            # An entry can carry no timestamp at all -- one recovered from
            # elsewhere, or written by an older version. Note that a plain
            # .get(key, 0) would not help: the default only applies when the
            # key is absent, not when it is present and null.
            previous = entry.get("lastSeen")
            if not isinstance(previous, (int, float)):
                changed = True
            # Otherwise only persist the clock every few minutes, so routine
            # polling does not rewrite the config file constantly.
            elif now - previous > 300:
                changed = True
        if entry is None:
            known[mon["key"]] = fresh
        else:
            entry.update(fresh)

    # Entries with no usable timestamp are kept rather than swept or compared
    # against (a None here would raise), and get stamped the next time the
    # monitor turns up.
    cutoff = now - FORGET_AFTER_DAYS * 86400
    stale = [k for k, v in known.items()
             if isinstance(v.get("lastSeen"), (int, float))
             and v["lastSeen"] < cutoff]
    for key in stale:
        del known[key]
        changed = True

    if changed:
        CONFIG.save()
    return known


def absent_monitors(present, known):
    """Remembered displays that are not plugged in (or awake) right now."""
    seen = {mon["key"] for mon in present}
    out = []
    for key, entry in known.items():
        if key in seen:
            continue
        out.append({
            "key": key,
            "name": entry.get("name") or "Display",
            "port": entry.get("port") or "Unknown",
            "devicePath": entry.get("devicePath", ""),
            "native": entry.get("native"),
            "lastSeen": entry.get("lastSeen"),
            "present": False,
            "active": False,
            "primary": False,
            "gdiName": "",
            "adapter": "",
            # Nothing is driving it, so it has no source and cannot be a
            # duplicate of anything.
            "sourceGroup": None,
            "adapterKey": entry.get("adapterKey", ""),
            "duplicateOf": None,
            "modes": [],
            "rect": None,
            "current": None,
            "rotation": 0,
            "sdrWhiteNits": None,
            "hdr": {"supported": False, "enabled": False,
                    "forceDisabled": False, "encoding": None,
                    "bitsPerChannel": None, "readable": False},
        })
    out.sort(key=lambda m: -(m["lastSeen"] or 0))
    return out


def assign_labels(monitors, known):
    """Name the displays, giving identical panels a number that sticks.

    Numbering them by their order in the list is not good enough: buying a
    third identical monitor, or plugging one into a different port, would
    shuffle the numbers on the ones already there and "#2" would stop meaning
    the screen the user learned it as. So a number is allocated once and
    remembered against the display.
    """
    by_name = {}
    for mon in monitors:
        mon["name"] = mon.get("name") or "Display"
        by_name.setdefault(mon["name"], []).append(mon)

    changed = False
    for name, group in by_name.items():
        if len(group) < 2:
            group[0]["label"] = name
            continue

        group.sort(key=lambda m: (m.get("devicePath") or "", m["key"]))
        taken = set()
        for mon in group:
            entry = known.get(mon["key"]) or {}
            number = entry.get("number")
            if isinstance(number, int) and number > 0 and number not in taken:
                mon["_number"] = number
                taken.add(number)

        following = 1
        for mon in group:
            if mon.get("_number"):
                continue
            while following in taken:
                following += 1
            mon["_number"] = following
            taken.add(following)
            known.setdefault(mon["key"], {})["number"] = following
            changed = True

        for mon in group:
            mon["label"] = "{} ({} #{})".format(
                name, mon.get("port") or "Unknown", mon.pop("_number"))
    return changed


def build_state():
    monitors = display.list_monitors()
    public = []
    for mon in monitors:
        entry = {k: v for k, v in mon.items() if not k.startswith("_")}
        entry["lastSeen"] = time.time()
        public.append(entry)

    known = remember(monitors)
    public += absent_monitors(monitors, known)
    # Label against the combined list so a name does not flip between
    # "LG TV SSCR2" and "LG TV SSCR2 (HDMI #1)" as its twin comes and goes --
    # worth doing even when the absent ones are then dropped below.
    if assign_labels(public, known):
        CONFIG.save()

    if not CONFIG.data.get("showDisconnected"):
        # Displays Windows cannot currently see are remembered (that is what
        # keeps labels and saved profiles stable) but not shown: for a setup
        # where a TV is usually off, a permanent card for it is just clutter.
        public = [mon for mon in public if mon.get("present")]

    return {
        "monitors": public,
        "pending": PENDING.info(),
        "lastRevert": PENDING.last_revert,
        "profiles": sorted(CONFIG.data.get("profiles", {}).keys()),
        "host": socket.gethostname(),
        "confirmSeconds": CONFIRM_SECONDS,
    }


def apply_batch(body):
    """Run an enable/layout/HDR batch, arming a rollback if it is risky.

    Order matters: outputs are lit or unlit first (that reshuffles GDI names
    and can move everything), then geometry, then HDR last because toggling
    advanced colour can itself force a mode renegotiation.
    """
    enable = body.get("enable") or {}
    layout = body.get("layout") or {}
    hdr = body.get("hdr") or {}
    mirror = body.get("mirror") or {}

    if not (enable or layout or hdr or mirror):
        raise display.DisplayError("Nothing to apply.")

    # Check the batch as a whole before touching anything. Monitors are
    # switched one at a time, so without this a request to turn everything
    # off would blank a screen and only then hit the "last display" guard.
    monitors = {m["key"]: m for m in display.list_monitors()}
    known = CONFIG.data.get("known", {})
    for key in list(enable) + list(layout) + list(hdr) + list(mirror):
        if key not in monitors:
            if key in known:
                raise display.DisplayError(
                    "{} is not connected right now -- check it is powered on "
                    "and showing the right input.".format(
                        known[key].get("name") or "That display"))
            raise display.DisplayError("Unknown display {}.".format(key))
    surviving = [key for key, mon in monitors.items()
                 if bool(enable.get(key, mon["active"]))]
    if enable and not surviving:
        raise display.DisplayError(
            "That would switch off every display, leaving you no way to see "
            "the desktop.")

    risky = bool(enable or layout or mirror)
    snapshot = display.snapshot() if risky else None

    applied = []
    try:
        for key, wanted in enable.items():
            display.set_enabled(key, bool(wanted))
            applied.append("enable" if wanted else "disable")
        # Before geometry: duplicating decides whether a display has a
        # rectangle of its own at all.
        for key, wanted in mirror.items():
            display.set_mirror(key, wanted or None)
            applied.append("mirror")
        if layout:
            display.apply_layout(layout)
            applied.append("layout")
        for key, wanted in hdr.items():
            display.set_hdr(key, bool(wanted))
            applied.append("hdr")
    except display.DisplayError as exc:
        if snapshot is not None and applied:
            problems = display.restore(snapshot)
            if problems:
                log("Rollback after a failed apply was incomplete: "
                    + "; ".join(problems))
                raise display.DisplayError(
                    "{} -- and putting things back did not fully work: {}"
                    .format(exc, "; ".join(problems)))
        raise

    result = build_state()
    if risky and body.get("confirm", True):
        change = PENDING.arm(snapshot)
        result = build_state()
        result["pending"] = {"id": change.id, "secondsLeft": CONFIRM_SECONDS}
    return result


def save_profile(name):
    name = (name or "").strip()
    if not name:
        raise display.DisplayError("A profile needs a name.")
    if len(name) > 40:
        raise display.DisplayError("Profile names are limited to 40 chars.")
    snap = display.snapshot()
    monitors = {m["key"]: m["label"] for m in display.list_monitors()}
    CONFIG.data.setdefault("profiles", {})[name] = {
        "snapshot": snap,
        "labels": monitors,
        "savedAt": time.time(),
    }
    CONFIG.save()


def apply_profile(name):
    profile = CONFIG.data.get("profiles", {}).get(name)
    if not profile:
        raise display.DisplayError("No profile called {}.".format(name))
    snapshot = display.snapshot()
    errors = display.restore(profile["snapshot"])
    if errors:
        raise display.DisplayError("; ".join(errors))
    change = PENDING.arm(snapshot)
    result = build_state()
    result["pending"] = {"id": change.id, "secondsLeft": CONFIRM_SECONDS}
    return result


def forget_monitor(key):
    """Drop a remembered display that is gone for good."""
    known = CONFIG.data.setdefault("known", {})
    if key not in known:
        raise display.DisplayError("That display is not in the list.")
    if any(m["key"] == key for m in display.list_monitors()):
        raise display.DisplayError(
            "That display is connected right now, so it would come straight "
            "back. Unplug it first.")
    del known[key]
    CONFIG.save()


def delete_profile(name):
    if CONFIG.data.get("profiles", {}).pop(name, None) is None:
        raise display.DisplayError("No profile called {}.".format(name))
    CONFIG.save()


def identify():
    """Flash a big number on each monitor so you can tell them apart."""
    monitors = [m for m in display.list_monitors() if m["active"] and m["rect"]]
    if not monitors:
        raise display.DisplayError("No active monitors to identify.")
    spec = [{"label": m["label"], "rect": m["rect"],
             "index": i + 1, "primary": m["primary"]}
            for i, m in enumerate(monitors)]
    script = os.path.join(HERE, "identify.py")
    creation = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
    try:
        subprocess.Popen([sys.executable, script, json.dumps(spec)],
                         creationflags=creation, cwd=HERE)
    except OSError as exc:
        raise display.DisplayError("Could not show overlays: {}".format(exc))
    return {"shown": len(spec)}


# -------------------------------------------------------------------- server


class Handler(BaseHTTPRequestHandler):
    server_version = "MonitorPad"
    protocol_version = "HTTP/1.1"

    # HTTP/1.1 keeps connections alive, and without a timeout the handler
    # blocks in readline() waiting for a request that may never come -- one
    # thread pinned per connection, forever. A phone that sleeps, changes
    # network or is swiped away leaves its connections half-open, so they
    # accumulate all day and the client is left holding sockets the server
    # will never answer on. Closing idle ones costs nothing: the client
    # simply opens a fresh connection for its next request.
    timeout = 30

    def log_request(self, *args):
        pass  # _serve logs each request with its timing instead.

    def log_message(self, fmt, *args):
        # Catch the stdlib's own notes -- idle timeouts, malformed requests
        # -- which would otherwise vanish with no console attached.
        log_debug("http: " + (fmt % args))

    def log_error(self, fmt, *args):
        log_debug("http: " + (fmt % args))

    # ---------------------------------------------------------- helpers

    _status = 0

    def _send(self, status, body=b"", content_type="application/json",
              extra=None):
        self._status = status
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status, payload):
        self._send(status, json.dumps(payload).encode("utf-8"))

    def _error(self, status, message):
        self._json(status, {"error": message})

    def _authorised(self):
        token = CONFIG.data["token"]
        header = self.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            return hmac.compare_digest(header[7:], token)
        return False

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0 or length > 1_000_000:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise display.DisplayError("Malformed request body.")

    # ------------------------------------------------------------ routes

    def _serve(self, method):
        """Dispatch one request and record how it went.

        Every request is logged with its outcome and duration. That trail is
        what makes an intermittent fault diagnosable after the fact: a gap in
        the poll, a burst of retries, or a slow apply all show up plainly.
        """
        path = urlparse(self.path).path
        started = time.perf_counter()
        self._status = 0
        try:
            if path.startswith("/api/"):
                self._api(method, path)
            elif method == "POST":
                self._error(404, "Not found")
            else:
                self._static(path)
        except (BrokenPipeError, ConnectionResetError) as exc:
            # The client vanished mid-reply: a phone locking, or Wi-Fi
            # dropping. Nothing is wrong on this side, but it is worth
            # seeing when tracking down flaky requests.
            log_debug("{} {} aborted by client ({})".format(
                method, path, type(exc).__name__))
            self.close_connection = True
            return
        finally:
            elapsed = (time.perf_counter() - started) * 1000
            if self._status and self._status >= 400:
                log_warning("{} {} -> {} in {:.0f}ms".format(
                    method, path, self._status, elapsed))
            elif path.startswith("/api/"):
                log_debug("{} {} -> {} in {:.0f}ms  [{}]".format(
                    method, path, self._status, elapsed,
                    self.address_string()))

    def do_GET(self):
        return self._serve("GET")

    def do_HEAD(self):
        return self._serve("GET")

    def do_POST(self):
        return self._serve("POST")

    def _api(self, method, path):
        if path == "/api/ping":
            return self._json(200, {"app": "MonitorPad",
                                    "host": socket.gethostname(),
                                    "pinOffered": PIN.status() is not None})
        if method == "POST" and path == "/api/pair":
            try:
                token = PIN.redeem(self._read_body().get("pin", ""))
            except display.DisplayError as exc:
                log("Pairing refused: {}".format(exc))
                return self._error(403, str(exc))
            log("A device paired using the PIN.")
            return self._json(200, {"token": token,
                                    "host": socket.gethostname()})
        if not self._authorised():
            return self._error(401, "Bad or missing access token.")
        try:
            body = self._read_body() if method == "POST" else {}
            if method == "GET" and path == "/api/state":
                return self._json(200, build_state())
            if method == "POST" and path == "/api/apply":
                return self._json(200, apply_batch(body))
            if method == "POST" and path == "/api/confirm":
                ok = PENDING.confirm(body.get("id", ""))
                if not ok:
                    return self._error(409, "That change already expired.")
                log("Change {} confirmed.".format(body.get("id")))
                return self._json(200, build_state())
            if method == "POST" and path == "/api/revert":
                errors = PENDING.revert_now(body.get("id", ""))
                if errors is None:
                    return self._error(409, "Nothing pending to revert.")
                return self._json(200, build_state())
            if method == "POST" and path == "/api/forget":
                forget_monitor(body.get("key", ""))
                return self._json(200, build_state())
            if method == "POST" and path == "/api/identify":
                return self._json(200, identify())
            if method == "POST" and path == "/api/client-log":
                # The phone reporting a request that failed at the network
                # layer. The agent never sees those itself -- they never
                # arrive -- so without this they are invisible here.
                note = str(body.get("message", ""))[:300]
                log_warning("client [{}]: {}".format(
                    self.address_string(), note))
                return self._json(200, {"logged": True})
            if method == "POST" and path == "/api/profile/save":
                save_profile(body.get("name"))
                return self._json(200, build_state())
            if method == "POST" and path == "/api/profile/apply":
                return self._json(200, apply_profile(body.get("name")))
            if method == "POST" and path == "/api/profile/delete":
                delete_profile(body.get("name"))
                return self._json(200, build_state())
            return self._error(404, "No such endpoint.")
        except display.DisplayError as exc:
            log("Rejected {}: {}".format(path, exc))
            return self._error(400, str(exc))
        except Exception as exc:  # noqa: BLE001 - surface anything unexpected
            log_exception("Unhandled error serving {} {}".format(method, path))
            return self._error(500, "Agent error: {}".format(exc))

    def _static(self, path):
        if path in ("/", ""):
            path = "/index.html"
        # Resolve inside WEB_ROOT and refuse anything that escapes it.
        target = os.path.normpath(
            os.path.join(WEB_ROOT, path.lstrip("/").replace("/", os.sep)))
        if not target.startswith(WEB_ROOT + os.sep) and target != WEB_ROOT:
            return self._error(403, "Forbidden")
        if not os.path.isfile(target):
            return self._error(404, "Not found")
        ctype, _ = mimetypes.guess_type(target)
        if target.endswith(".webmanifest"):
            ctype = "application/manifest+json"
        with open(target, "rb") as handle:
            data = handle.read()
        return self._send(200, data, ctype or "application/octet-stream")


def lan_addresses():
    """Best-effort list of addresses the phone could reach us on."""
    found = []
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 53))
        found.append(probe.getsockname()[0])
        probe.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None,
                                       socket.AF_INET):
            address = info[4][0]
            if address not in found and not address.startswith("127."):
                found.append(address)
    except socket.gaierror:
        pass
    return found


def build_server(host="0.0.0.0", port=DEFAULT_PORT):
    """Create the HTTP server without starting it."""
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    # A clear marker for the start of each run, so a log covering several
    # sessions can be read back without guessing where one ends.
    log("=" * 60)
    log("MonitorPad starting on {}:{}  (python {}, pid {})".format(
        host, port, sys.version.split()[0], os.getpid()))
    log("Reachable at: {}".format(
        ", ".join(lan_addresses()) or "no LAN address found"))
    return httpd


def serve_in_background(httpd):
    """Run the server on its own thread, for the tray app to sit alongside."""
    thread = threading.Thread(target=httpd.serve_forever,
                              name="monitorpad-http", daemon=True)
    thread.start()
    return thread


def shut_down(httpd):
    """Stop serving, rolling back anything still waiting on a confirmation."""
    change = PENDING.current
    if change is not None:
        log("Shutting down with a change still unconfirmed -- reverting.")
        problems = display.restore(change.snapshot)
        if problems:
            log("Rollback on shutdown was incomplete: " + "; ".join(problems))
    try:
        httpd.shutdown()
    except Exception:  # noqa: BLE001 - already stopping
        pass
    httpd.server_close()
    log("MonitorPad stopped.")


def pairing_url(port):
    addresses = lan_addresses()
    return "http://{}:{}".format(addresses[0] if addresses
                                 else "<this-pc-ip>", port)


def pairing_link(port):
    return "{}/#t={}".format(pairing_url(port), CONFIG.data["token"])


def main():
    parser = argparse.ArgumentParser(description="MonitorPad display agent")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--new-token", action="store_true",
                        help="Issue a fresh token, invalidating paired phones")
    parser.add_argument("--show-link", action="store_true",
                        help="Print the pairing link for an already-paired "
                             "device and exit")
    args = parser.parse_args()

    if args.new_token:
        CONFIG.data["token"] = secrets.token_urlsafe(24)
        CONFIG.save()
        print("New token issued; re-pair your phone.")

    token = CONFIG.data["token"]
    addresses = lan_addresses()

    if args.show_link:
        for address in addresses or ["<this-pc-ip>"]:
            print("http://{}:{}/#t={}".format(address, args.port, token))
        return

    httpd = build_server(args.host, args.port)

    pin = PIN.issue()
    primary = addresses[0] if addresses else "<this-pc-ip>"

    print()
    print("  MonitorPad is running on {}".format(socket.gethostname()))
    print()
    print("  1. On your iPhone (same Wi-Fi), open Safari and go to:")
    print()
    print("       http://{}:{}".format(primary, args.port))
    print()
    print("  2. Enter this pairing PIN:        {}  {}  {}".format(
        pin[0:2], pin[2:4], pin[4:6]))
    print("     (valid for {} minutes, one device)".format(
        PairingPin.LIFETIME // 60))
    print()
    print("  3. Tap Share, then Add to Home Screen.")
    print()
    if len(addresses) > 1:
        print("  Other addresses this PC answers on: {}".format(
            ", ".join("http://{}:{}".format(a, args.port)
                      for a in addresses[1:])))
        print()
    print("  Already paired a device? Its link is:")
    print("    http://{}:{}/#t={}".format(primary, args.port, token))
    print()
    print("  Config: {}".format(CONFIG_PATH))
    print("  Ctrl+C to stop.")
    print()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        shut_down(httpd)


if __name__ == "__main__":
    main()
