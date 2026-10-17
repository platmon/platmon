"""HTTP frontend: JSON at /api/stats, the sampler's runtime status at /api/status, the terminal view as plain
text at /text (for curl and watch) and, when web is enabled, the web viewer at /. Started by platmon.py.
Contract: docs/reference/api.md."""
import json
import os
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .cli import VERSION, render
from runtime.replay import CursorError, MAX_LIMIT

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
WEB_ASSETS = {
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
}
BRAND_ASSETS = {
    "/assets/brand/platmon-logo-light.svg": "platmon-logo-light.svg",
    "/assets/brand/platmon-logo-dark.svg": "platmon-logo-dark.svg",
    "/assets/brand/platmon-icon-light.svg": "platmon-icon-light.svg",
    "/assets/brand/platmon-icon-dark.svg": "platmon-icon-dark.svg",
    "/assets/brand/favicon.svg": "favicon.svg",
}
LIVE = ("/api/stats", "/api/status", "/api/observations", "/api/history", "/api/samples", "/api/observation-samples", "/text")
MAX_POINTS, MAX_PREFIX = 600, 128
REQUEST_TIMEOUT = 10  # seconds to send a request line and headers in; generous for any real client
WRITE_TIMEOUT = 10  # seconds without progress on a response write
WRITE_CHUNK = 64 * 1024


class BoundedHTTPServer(ThreadingHTTPServer):
    """Reject excess connections before allocating another request thread."""
    max_connections = 32

    def __init__(self, *args, **kwargs):
        self._slots = threading.BoundedSemaphore(self.max_connections)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def history_query(history, query):
    """(prefix, seconds, points) from /api/history?prefix=&seconds=&points=, bounded; ValueError if bad."""
    q = urllib.parse.parse_qs(query, max_num_fields=3, keep_blank_values=True)
    if set(q) - {"prefix", "seconds", "points"} or any(len(v) != 1 for v in q.values()):
        raise ValueError(query)
    prefix = q.get("prefix", [""])[0]
    seconds = float(q.get("seconds", [history.retention])[0])
    points = int(q.get("points", [300])[0])
    if len(prefix) > MAX_PREFIX or not 0 < seconds <= history.retention or not 0 < points <= MAX_POINTS:
        raise ValueError(query)
    return prefix, seconds, points


def replay_query(query, observation=False):
    """Strict, bounded cursor query. Resuming requires the original process identity."""
    q = urllib.parse.parse_qs(query, max_num_fields=4, keep_blank_values=True)
    allowed = {"instance_id", "after", "limit"} | ({"group"} if observation else set())
    if set(q) - allowed or any(len(v) != 1 for v in q.values()):
        raise ValueError("unknown or repeated query field")
    instance_id = q.get("instance_id", [None])[0]
    after_text, limit_text = q.get("after", ["0"])[0], q.get("limit", ["100"])[0]
    if not after_text.isascii() or not after_text.isdecimal() or len(after_text) > 19:
        raise ValueError("after must be a nonnegative integer")
    if not limit_text.isascii() or not limit_text.isdecimal() or len(limit_text) > 3:
        raise ValueError("invalid limit")
    after, limit = int(after_text), int(limit_text)
    if after > 2**63 - 1 or not 1 <= limit <= MAX_LIMIT:
        raise ValueError("cursor or limit out of range")
    if instance_id is not None and not 1 <= len(instance_id) <= 128:
        raise ValueError("invalid instance_id")
    if after and instance_id is None:
        raise ValueError("instance_id is required when after > 0")
    stream = q.get("group", [""])[0] if observation else "core"
    if observation and stream not in ("storage", "wifi", "battery", "probe"):
        raise ValueError("group must be storage, wifi, battery or probe")
    return stream, instance_id, after, limit


def make_handler(sampler, web=True, observations=None, history=None, levels=None):
    """observations: the service's runtime.slow.Observations (low-frequency groups), or None.
    history: its runtime.history.History, or None (then /api/history is 404).
    levels: the warning and critical levels from [thresholds], for the web page and /api/status, or None for
    the clients' defaults."""
    page_values = {b"@VERSION@": VERSION.encode(), b"@LEVELS@": json.dumps(levels or {}).encode()}
    def observed():
        return observations.view() if observations else {"schema_version": 2, "instance_id": sampler.instance_id,
                                                          "clock": dict(sampler.clock), "groups": {}}

    class Handler(BaseHTTPRequestHandler):
        # Without a deadline a client that connects and then sends nothing holds its thread, and the
        # memory behind it, for as long as it stays connected: a dropped client or a port scan leaves
        # the thread parked in the request read forever. This bounds reading the request only.
        timeout = REQUEST_TIMEOUT
        write_timeout = WRITE_TIMEOUT

        def handle(self):
            try:
                super().handle()
            except (TimeoutError, ConnectionError):
                pass  # stalled or disconnected peer: finish closes the socket and releases its slot

        def do_GET(self):
            self.connection.settimeout(self.write_timeout)
            path, _, query = self.path.partition("?")
            if path in ("/api/samples", "/api/observation-samples"):
                self.path = path
                replay = getattr(sampler, "replay", None)
                code = 200
                try:
                    if replay is None:
                        raise KeyError("replay_disabled")
                    args = replay_query(query, path == "/api/observation-samples")
                    body = replay.view(*args)
                except CursorError as e:
                    code, body = 409, {"code": str(e), "instance_id": sampler.instance_id}
                except ValueError as e:
                    code, body = 400, {"code": "invalid_query", "error": str(e)}
                except KeyError:
                    code, body = 404, {"code": "replay_unavailable"}
                self.reply(code, json.dumps(body, separators=(",", ":")).encode(), "application/json")
                return
            if history is not None and path == "/api/history":  # read-only, bounded; never collects
                try:
                    prefix, seconds, points = history_query(history, query)
                except ValueError:
                    self.send_error(400, "prefix (at most 128 characters), seconds (up to the retention) and points "
                                         f"(1 to {MAX_POINTS}) only")
                    return
                series, dropped = history.view(sampler._elapsed(), prefix, seconds, points)
                body = {"schema_version": 1, "instance_id": sampler.instance_id, "retention_s": history.retention,
                        "interval_ms": round(sampler.interval * 1000, 3), "dropped_series": dropped, "series": series,
                        "series_intervals_ms": {sid: ms for sid, ms in history.intervals().items() if sid in series}}
                self.path = path  # for the no-store header
                self.reply(200, json.dumps(body).encode(), "application/json")
                return
            if self.path == "/api/status":  # never waits for the first snapshot; 200 even when not ready
                status = sampler.read()[1]
                if levels is not None:  # [thresholds], for clients that color readings (the platmon command)
                    status["levels"] = levels
                body, ctype = json.dumps(status).encode(), "application/json"
            elif self.path == "/api/observations":  # low-frequency groups with their own ages; always 200
                body, ctype = json.dumps(observed()).encode(), "application/json"
            elif self.path in ("/api/stats", "/text"):
                # code and body from the same capture; /api/stats serializes the snapshot without copying it
                if self.path == "/api/stats":
                    stats, status = sampler._stats_json(timeout=5.0)
                else:
                    stats, status = sampler.read(timeout=5.0)
                if stats is None:  # JSON for the API, text for /text, so clients need not parse an HTML page
                    why = "no current data (not collected yet, or collection keeps failing)"
                    if self.path == "/api/stats":  # the last good sample's identity and age, never its values
                        body = {"error": why, "schema_version": status["schema_version"], "code": "no_current_data",
                                "instance_id": status["instance_id"], "sample": status["sample"]}
                        self.reply(503, json.dumps(body).encode(), "application/json")
                    else:
                        self.reply(503, (why + "\n").encode(), "text/plain; charset=utf-8")
                    return
                if self.path == "/api/stats":
                    body, ctype = stats, "application/json"  # already the JSON bytes
                else:  # same screen as the platmon command: watch -n1 curl -s host:9797/text
                    body, ctype = (render(stats, observed()) + "\n").encode(), "text/plain; charset=utf-8"
            elif web and self.path in ("/", "/index.html"):
                with open(os.path.join(WEB_DIR, "index.html"), "rb") as f:  # the version and levels it shows
                    body, ctype = f.read(), "text/html; charset=utf-8"
                for mark, value in page_values.items():
                    body = body.replace(mark, value)
            elif web and self.path in WEB_ASSETS:
                filename, ctype = WEB_ASSETS[self.path]
                with open(os.path.join(WEB_DIR, filename), "rb") as f:
                    body = f.read()
            elif web and self.path in BRAND_ASSETS:
                with open(os.path.join(WEB_DIR, "assets", "brand", BRAND_ASSETS[self.path]), "rb") as f:
                    body, ctype = f.read(), "image/svg+xml"
            else:
                self.send_error(404)
                return
            self.reply(200, body, ctype)

        def reply(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            if self.path in LIVE:  # a cached answer would show old data as current
                self.send_header("Cache-Control", "no-store")
            self.end_headers()
            # send(), unlike sendall(), returns on partial progress. Each call gets its own timeout,
            # so a large response can take longer overall while a stopped reader is still reclaimed.
            pending = memoryview(body)
            while pending:
                sent = self.connection.send(pending[:WRITE_CHUNK])
                if not sent:
                    raise ConnectionError("response socket closed")
                pending = pending[sent:]

        def log_message(self, *args):
            pass

    return Handler


def start(sampler, cfg, observations=None, history=None, levels=None):
    """cfg: the [http] config section (bind, port, web); levels: see make_handler."""
    httpd = BoundedHTTPServer((cfg.get("bind"), cfg.getint("port")),
                                make_handler(sampler, cfg.getboolean("web"), observations, history, levels))
    print(f"platmon http on {cfg.get('bind')}:{cfg.getint('port')}", flush=True)
    t = threading.Thread(target=httpd.serve_forever, name="http", daemon=True)
    t.start()
    return t
