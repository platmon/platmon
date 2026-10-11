import json
import socket
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from collector.sampler import METADATA_KEYS, Sampler
from frontends.cli import VERSION, render
from frontends.server import REQUEST_TIMEOUT, make_handler


class NoWait(Sampler):
    """The real sampler without the first-snapshot wait of /api/stats and /text, to keep tests fast."""

    def read(self, timeout=0.0):
        return super().read(0.0)

    def _stats_json(self, timeout=0.0):  # /api/stats
        return super()._stats_json(0.0)


def serve(stats, web=True, sampler=None, timeout=None, wrap=None):
    """Serves one collected snapshot of stats (None: nothing collected yet), or the given sampler.
    timeout: the handler's request deadline, shortened from the product default to keep tests fast.
    wrap: called with the handler class, returns the class to serve with."""
    if sampler is None:
        sampler = NoWait(lambda: stats)
        if stats is not None:
            sampler._attempt()
    cls = make_handler(sampler, web)
    if timeout is not None:
        cls = type("Impatient", (cls,), {"timeout": timeout})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), wrap(cls) if wrap else cls)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def test_a_silent_client_is_dropped_instead_of_parking_its_thread():
    """A client that connects and then sends no request is closed when the request deadline passes.
    Without one it would hold its thread, and the memory behind it, for as long as it stayed connected,
    so a dropped client or a port scan leaves threads parked for good."""
    assert make_handler(NoWait(lambda: None)).timeout == REQUEST_TIMEOUT > 0  # the shipped deadline
    httpd, base = serve({"platform": "PC"}, timeout=0.3)  # the same mechanism, shortened for the test
    try:
        s = socket.create_connection(("127.0.0.1", httpd.server_address[1]), timeout=5)
        s.settimeout(5)  # the recv below would block until this, were the server still waiting
        assert s.recv(1) == b""  # closed by the server
        s.close()
        assert get(base + "/api/status")[0] == 200  # and it goes on serving
    finally:
        httpd.shutdown()


def test_answering_is_not_bounded_by_the_request_deadline():
    """The deadline covers reading the request only. A socket timeout is a deadline for a whole write,
    not an idle one, so leaving it in force would cut a large response (/api/history can be hundreds of
    kB) off a client that reads slowly but steadily."""
    seen = []

    def watch(cls):
        class Watched(cls):
            def do_GET(self):
                super().do_GET()
                seen.append(self.connection.gettimeout())
        return Watched

    httpd, base = serve({"platform": "PC"}, timeout=0.3, wrap=watch)
    try:
        assert get(base + "/api/status")[0] == 200
        assert seen == [None]  # no deadline left on the connection the response went out on
    finally:
        httpd.shutdown()


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def plain(stats):
    return {k: v for k, v in stats.items() if k not in METADATA_KEYS}


@pytest.mark.parametrize("web, path, expected", [
    (True, "/api/stats", 200), (True, "/api/status", 200), (True, "/text", 200), (True, "/", 200),
    (True, "/index.html", 200), (True, "/nope", 404),
    (False, "/api/stats", 200), (False, "/api/status", 200), (False, "/text", 200),
    (False, "/", 404),  # web = no: API and text only
    (False, "/index.html", 404),
    (False, "/assets/brand/platmon-logo-light.svg", 404),
    (False, "/assets/brand/platmon-logo-dark.svg", 404),
    (False, "/assets/brand/favicon.svg", 404),
    (False, "/assets/brand/platmon-icon-light.svg", 404),
])
def test_routes(web, path, expected):
    from test_cli import FULL
    httpd, base = serve(FULL, web)
    try:
        code, headers, body = get(base + path)
        assert code == expected
        if path == "/api/stats":  # the old fields as they were, plus the sample metadata
            stats = json.loads(body)
            assert plain(stats) == FULL and stats["schema_version"] == 1 and stats["sample"]["sequence"] == 1
        if path == "/api/status":
            assert json.loads(body)["state"] == "ready" and "levels" not in json.loads(body)  # none configured
        if expected == 200 and path in ("/", "/index.html"):  # the footer carries the running version
            assert f"platmon v{VERSION}".encode() in body and b"@VERSION@" not in body
            assert b'<script type="application/json" id="levels">{}</script>' in body  # no levels: page defaults
        if path == "/text":  # the terminal view, unchanged by the metadata, no screen control codes
            assert body.decode() == render(FULL) + "\n" and b"\033[" not in body
        if path in ("/api/stats", "/api/status", "/text"):
            ctype = "text/plain; charset=utf-8" if path == "/text" else "application/json"
            assert headers["Content-Type"] == ctype and headers["Cache-Control"] == "no-store"
    finally:
        httpd.shutdown()


@pytest.mark.parametrize("filename", ["platmon-logo-light.svg", "platmon-logo-dark.svg", "favicon.svg",
                                      "platmon-icon-light.svg", "platmon-icon-dark.svg"])
def test_brand_assets(filename):
    asset = Path(__file__).resolve().parents[1] / "frontends" / "web" / "assets" / "brand" / filename
    httpd, base = serve(None)
    try:
        with urllib.request.urlopen(base + "/assets/brand/" + filename, timeout=5) as r:
            assert r.status == 200
            assert r.headers["Content-Type"] == "image/svg+xml"
            assert r.read() == asset.read_bytes()
    finally:
        httpd.shutdown()


def test_levels_reach_the_page():
    """[thresholds] as the page's JSON, filled in when the page is served, and in /api/status."""
    levels = {"cpu": [70.0, 85.0], "temperature": None}
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(NoWait(lambda: None), True, levels=levels))
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    try:
        body = get(f"http://127.0.0.1:{httpd.server_address[1]}/")[2]
        assert f'id="levels">{json.dumps(levels)}</script>'.encode() in body
        status = json.loads(get(f"http://127.0.0.1:{httpd.server_address[1]}/api/status")[2])
        assert status["levels"] == levels  # the same levels for the platmon command
    finally:
        httpd.shutdown()


def test_web_asset_paths_are_allowlisted(tmp_path, monkeypatch):
    brand_dir = tmp_path / "assets" / "brand"
    brand_dir.mkdir(parents=True)
    (brand_dir / "private.svg").write_text("private asset")
    (tmp_path / "index.html").write_text("private page")
    monkeypatch.setattr("frontends.server.WEB_DIR", str(tmp_path))
    httpd, base = serve(None)
    try:
        for path in (
            "/assets/brand/",
            "/assets/brand/private.svg",
            "/assets/brand/../../index.html",
            "/assets/brand/%2e%2e/%2e%2e/index.html",
        ):
            assert get(base + path)[0] == 404, path
    finally:
        httpd.shutdown()


def test_no_snapshot_yet_is_503():
    """JSON for the API and text for /text, so clients need not parse an HTML error page."""
    httpd, base = serve(None)
    try:
        for path, ctype in (("/api/stats", "application/json"), ("/text", "text/plain; charset=utf-8")):
            with pytest.raises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(base + path, timeout=5)
            assert e.value.code == 503 and e.value.headers["Content-Type"] == ctype
            assert e.value.headers["Cache-Control"] == "no-store"
            body = e.value.read().decode()
            assert (json.loads(body)["error"] if path == "/api/stats" else body).startswith("no current data")
    finally:
        httpd.shutdown()


@pytest.mark.parametrize("web", [True, False])
def test_503_metadata(web):
    """The last good sample's identity and age, never its values; sample is null when there was none."""
    clock = [0]
    s = NoWait(lambda: {"cpu": [{"id": 0, "usage": 5.0}]}, clock=(lambda: clock[0], {"source": "fake"}))
    httpd, base = serve(None, web, sampler=s)
    try:
        code, _, body = get(base + "/api/stats")
        assert code == 503 and json.loads(body) == {
            "error": "no current data (not collected yet, or collection keeps failing)", "schema_version": 1,
            "code": "no_current_data", "instance_id": s.instance_id, "sample": None}
        s._attempt()
        clock[0] += 6 * 10**9  # past stale_after (5 s)
        code, _, body = get(base + "/api/stats")
        assert code == 503 and json.loads(body)["sample"] == {"sequence": 1, "age_ms": 6000.0, "cycle_age_ms": 6000.0,
                                                              "data_age_ms": 6000.0, "data_age_basis": "cycle_start_upper_bound"}
        assert "cpu" not in json.loads(body) and "sensor_meta" not in json.loads(body)
        code, _, body = get(base + "/text")
        assert code == 503 and body.decode().startswith("no current data")
        code, _, body = get(base + "/api/status")  # 200 even when not ready: read "ready", not the code
        assert code == 200 and json.loads(body)["ready"] is False and json.loads(body)["state"] == "stale"
    finally:
        httpd.shutdown()


def test_status_does_not_wait_for_the_first_snapshot():
    release = threading.Event()
    s = Sampler(lambda: release.wait(10) and {}).start()  # the real wait: /api/stats would hold up to 5 s
    httpd, base = serve(None, sampler=s)
    try:
        t0 = time.monotonic()
        code, _, body = get(base + "/api/status")
        assert time.monotonic() - t0 < 2
        assert code == 200 and json.loads(body)["state"] == "starting"
    finally:
        httpd.shutdown()
        s.stop()
        release.set()


def test_polling_does_not_collect():
    """Requests read the published snapshot; only the sampler's own rounds read /proc/stat."""
    from test_cli import FULL
    calls = []
    s = NoWait(lambda: calls.append(1) or FULL)
    s._attempt()
    httpd, base = serve(None, sampler=s)
    try:
        polls = [json.loads(get(base + "/api/stats")[2]) for _ in range(3)]
        get(base + "/text"), get(base + "/api/status")
        assert len(calls) == 1
        assert [p["sample"]["sequence"] for p in polls] == [1, 1, 1] and all(plain(p) == FULL for p in polls)
    finally:
        httpd.shutdown()


def test_partial_snapshot_is_served_and_degraded():
    """A snapshot with an unreadable optional group is current: 200 on /api/stats and /text, and /api/status
    says degraded while ready."""
    from test_cli import FULL
    partial = dict(FULL, collectors={"temperature": {"state": "partial", "reason": "some_unreadable",
                                                     "issues": [{"target": "hwmon0.temp2", "reason": "invalid_data"}],
                                                     "issues_truncated": 0}})
    httpd, base = serve(partial)
    try:
        code, _, body = get(base + "/api/stats")
        assert code == 200 and json.loads(body)["collectors"]["temperature"]["state"] == "partial"
        code, _, body = get(base + "/text")
        assert code == 200 and "Collection: temperature partial" in body.decode()
        status = json.loads(get(base + "/api/status")[2])
        assert (status["state"], status["ready"]) == ("degraded", True)
    finally:
        httpd.shutdown()
