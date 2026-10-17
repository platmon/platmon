import json
import socket
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from runtime.sampler import METADATA_KEYS, Sampler
from frontends.cli import VERSION, render
from frontends.server import BoundedHTTPServer, REQUEST_TIMEOUT, WRITE_TIMEOUT, make_handler


class NoWait(Sampler):
    """The real sampler without the first-snapshot wait of /api/stats and /text, to keep tests fast."""

    def read(self, timeout=0.0):
        return super().read(0.0)

    def _stats_json(self, timeout=0.0):  # /api/stats
        return super()._stats_json(0.0)


def serve(stats, web=True, sampler=None, timeout=None, wrap=None, server_class=BoundedHTTPServer):
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
    httpd = server_class(("127.0.0.1", 0), wrap(cls) if wrap else cls)
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
    recorded = threading.Event()

    def watch(cls):
        class Watched(cls):
            def do_GET(self):
                super().do_GET()
                seen.append(self.connection.gettimeout())
                recorded.set()
        return Watched

    httpd, base = serve({"platform": "PC"}, timeout=0.3, wrap=watch)
    try:
        assert get(base + "/api/status")[0] == 200
        # the client has the whole response before the handler records, so wait for the handler
        assert recorded.wait(5)
        assert seen == [WRITE_TIMEOUT]  # response progress has its own budget
    finally:
        httpd.shutdown()


@pytest.mark.parametrize("reading", [False, True])
def test_response_reclaims_stalled_reader_but_allows_continued_progress(reading):
    finished = threading.Event()
    body = b"x" * (256 * 1024)

    def wrap(cls):
        class Response(cls):
            write_timeout = 0.3

            def setup(self):
                super().setup()
                self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)

            def handle(self):
                try:
                    super().handle()
                finally:
                    finished.set()
        return Response

    class Payload(NoWait):
        def _stats_json(self, timeout=0.0):
            return body, {}

    httpd, _ = serve(None, sampler=Payload(lambda: None), wrap=wrap)
    client = socket.socket()
    client.settimeout(5)
    client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    try:
        client.connect(httpd.server_address)
        client.sendall(b"GET /api/stats HTTP/1.0\r\n\r\n")
        if reading:
            response = bytearray()
            start = time.monotonic()
            while True:
                chunk = client.recv(2048)
                if not chunk:
                    break
                response.extend(chunk)
                time.sleep(0.01)
            assert time.monotonic() - start > 0.3
            assert bytes(response).split(b"\r\n\r\n", 1)[1] == body
        assert finished.wait(3), "stopped reader must release the request thread before client closes"
    finally:
        client.close()
        httpd.shutdown()
        httpd.server_close()


def test_connection_limit_rejects_excess_and_reuses_released_slot():
    entered, finished, release = threading.Event(), threading.Event(), threading.Event()

    class OneConnection(BoundedHTTPServer):
        max_connections = 1

    def wrap(cls):
        class Waiting(cls):
            def do_GET(self):
                entered.set()
                assert release.wait(5)
                super().do_GET()

            def finish(self):
                try:
                    super().finish()
                finally:
                    finished.set()
        return Waiting

    httpd, base = serve({}, wrap=wrap, server_class=OneConnection)
    client = socket.create_connection(httpd.server_address, timeout=5)
    try:
        client.sendall(b"GET /api/status HTTP/1.0\r\n\r\n")
        assert entered.wait(2)
        with socket.create_connection(httpd.server_address, timeout=5) as excess:
            assert excess.recv(1) == b""  # rejected before another handler is created
        release.set()
        assert finished.wait(2)
        # Acquiring the permit waits for process_request_thread's final cleanup as well as finish().
        assert httpd._slots.acquire(timeout=2)
        httpd._slots.release()
        assert get(base + "/api/status")[0] == 200
    finally:
        release.set()
        client.close()
        httpd.shutdown()
        httpd.server_close()


def test_connection_slot_is_released_when_thread_creation_fails(monkeypatch):
    httpd, _ = serve(None)
    request = socket.socket()
    available = httpd.max_connections
    def fail(*args):
        raise RuntimeError("cannot start thread")
    monkeypatch.setattr(ThreadingHTTPServer, "process_request", fail)
    try:
        with pytest.raises(RuntimeError, match="cannot start thread"):
            httpd.process_request(request, ("127.0.0.1", 0))
        for _ in range(available):
            assert httpd._slots.acquire(blocking=False)
        assert not httpd._slots.acquire(blocking=False)
    finally:
        request.close()
        httpd.shutdown()
        httpd.server_close()


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
    (False, "/app.js", 404), (False, "/styles.css", 404),
    (False, "/assets/brand/platmon-logo-light.svg", 404),
    (False, "/assets/brand/platmon-logo-dark.svg", 404),
    (False, "/assets/brand/favicon.svg", 404),
    (False, "/assets/brand/platmon-icon-light.svg", 404),
])
def test_routes(web, path, expected):
    from tests.frontends.test_cli import FULL
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
    asset = Path(__file__).resolve().parents[2] / "frontends" / "web" / "assets" / "brand" / filename
    httpd, base = serve(None)
    try:
        with urllib.request.urlopen(base + "/assets/brand/" + filename, timeout=5) as r:
            assert r.status == 200
            assert r.headers["Content-Type"] == "image/svg+xml"
            assert r.read() == asset.read_bytes()
    finally:
        httpd.shutdown()


@pytest.mark.parametrize("filename,ctype", [("app.js", "text/javascript; charset=utf-8"),
                                           ("styles.css", "text/css; charset=utf-8")])
def test_web_script_and_styles(filename, ctype):
    asset = Path(__file__).resolve().parents[2] / "frontends/web" / filename
    httpd, base = serve(None)
    try:
        with urllib.request.urlopen(base + "/" + filename, timeout=5) as response:
            assert response.headers["Content-Type"] == ctype
            assert response.read() == asset.read_bytes()
        page = get(base + "/")[2]
        assert f'"/{filename}"'.encode() in page
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
            "/private.js", "/../app.js", "/%2e%2e/app.js",
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
    from tests.frontends.test_cli import FULL
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
    from tests.frontends.test_cli import FULL
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


@pytest.mark.parametrize("web", [True, False])
def test_replay_http_pagination_staleness_and_restart(web):
    from runtime.replay import Replay
    from runtime.slow import Slow
    clock = [0]
    s = NoWait(lambda: {"cpu": [{"id": 0, "usage": 0}], "nested": [None]},
               clock=(lambda: clock[0], {"source": "fake"}))
    s.replay = Replay(s.instance_id, s._elapsed, s.clock, ("battery",), retention=10)
    for _ in range(3):
        s._attempt()
    slow = Slow("battery", lambda g: g.got({"batteries": [{"percent": 0}], "external": []}), 60, s._elapsed)
    slow.replay = s.replay
    slow.observe_once()
    httpd, base = serve(None, web, sampler=s)
    try:
        code, headers, body = get(base + "/api/samples?limit=2")
        page = json.loads(body)
        assert code == 200 and headers["Cache-Control"] == "no-store"
        assert [r["id"] for r in page["records"]] == [1, 2]
        assert page["has_more"] and page["next_after"] == 2
        code, _, body = get(base + f"/api/samples?instance_id={s.instance_id}&after=2")
        assert code == 200 and json.loads(body)["records"][0]["id"] == 3
        clock[0] = 6 * 10**9
        assert get(base + "/api/stats")[0] == 503
        code, _, body = get(base + "/api/samples")
        assert code == 200 and json.loads(body)["records"][0]["data"]["sample"]["stale"]
        code, _, body = get(base + "/api/observation-samples?group=battery")
        assert code == 200 and json.loads(body)["records"][0]["data"]["data"]["batteries"][0]["percent"] == 0
        code, _, body = get(base + "/api/samples?instance_id=previous-process&after=3")
        assert code == 409 and json.loads(body) == {"code": "instance_changed", "instance_id": s.instance_id}
        assert get(base + f"/api/samples?instance_id={s.instance_id}&after=4")[0] == 409
        assert get(base + "/api/observation-samples?group=wifi")[0] == 404
        clock[0] = 11 * 10**9
        code, _, body = get(base + "/api/samples")
        page = json.loads(body)
        assert code == 200 and page["records"] == [] and page["next_after"] == 3
        assert page["missing_ranges"] == [{"from": 1, "to": 3, "reason": "buffer_unavailable"}]
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.mark.parametrize("query", [
    "after=1", "after=-1", "after=1.0", "after=", "limit=0", "limit=101", "limit=", "limit=nan",
    "limit=1&limit=2", "extra=x", "group=battery", "instance_id=", "instance_id=" + "x" * 129,
    "after=" + "9" * 20, "limit=1&after=0&instance_id=x&extra=x&other=y",
])
def test_replay_http_rejects_invalid_query(query):
    from runtime.replay import Replay
    s = NoWait(lambda: {})
    s.replay = Replay(s.instance_id, s._elapsed, s.clock)
    httpd, base = serve(None, sampler=s)
    try:
        code, headers, body = get(base + "/api/samples?" + query)
        assert code == 400 and json.loads(body)["code"] == "invalid_query"
        assert headers["Cache-Control"] == "no-store"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_replay_disabled_and_observation_group_required():
    from runtime.replay import Replay
    s = NoWait(lambda: {})
    httpd, base = serve(None, sampler=s)
    try:
        assert get(base + "/api/samples")[0] == 404
        assert get(base + "/api/observation-samples?group=battery")[0] == 404
        s.replay = Replay(s.instance_id, s._elapsed, s.clock)
        for query in ("", "group=core", "group=unknown", "group=wifi&group=battery"):
            assert get(base + "/api/observation-samples?" + query)[0] == 400
    finally:
        httpd.shutdown()
        httpd.server_close()
