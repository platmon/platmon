"""HTTP frontend: JSON at /api/stats, the sampler's runtime status at /api/status, the terminal view as plain
text at /text (for curl and watch) and, when web is enabled, the web viewer at /. Started by platmon.py.
Contract: docs/reference/api.md."""
import json
import os
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .cli import VERSION, render

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
LIVE = ("/api/stats", "/api/status", "/api/observations", "/api/history", "/text")
MAX_POINTS, MAX_PREFIX = 600, 128
REQUEST_TIMEOUT = 10  # seconds to send a request line and headers in; generous for any real client


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

        def do_GET(self):
            # Answering is not bounded: a socket timeout is a deadline for a whole write, not an idle
            # one, so keeping it would cut a large response (/api/history can be hundreds of kB) off a
            # client that reads slowly but steadily. HTTP/1.0 closes after this, so no read follows.
            self.connection.settimeout(None)
            path, _, query = self.path.partition("?")
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
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    return Handler


def start(sampler, cfg, observations=None, history=None, levels=None):
    """cfg: the [http] config section (bind, port, web); levels: see make_handler."""
    httpd = ThreadingHTTPServer((cfg.get("bind"), cfg.getint("port")),
                                make_handler(sampler, cfg.getboolean("web"), observations, history, levels))
    print(f"platmon http on {cfg.get('bind')}:{cfg.getint('port')}", flush=True)
    t = threading.Thread(target=httpd.serve_forever, name="http", daemon=True)
    t.start()
    return t
