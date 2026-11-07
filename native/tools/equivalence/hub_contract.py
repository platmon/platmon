"""The fleet server's contract: the released platmon-hub, as installed from PyPI, consumes this server's answers.

  python3 -I hub_contract.py PATH/TO/platmon-server [--seconds 20]

Run it with the Python of an environment that has the pinned Hub (hub-requirements.txt):

  python3 -m venv /tmp/hub && /tmp/hub/bin/pip install --require-hashes -r hub-requirements.txt
  /tmp/hub/bin/python -I hub_contract.py PATH/TO/platmon-server

Starts `platmon-server serve` on a free loopback port, with every slow group on and the probe aimed at the
server itself, then:
- Hub's own validators (hub.protocol) accept /api/status, /api/stats and /api/observations, and every group
  the server publishes is one Hub stores (hub.history.raw.STREAMS)
- a Hub (hub.runtime.Fleet with a raw-capture HistoryWriter on a new database, polling every 1 s / 2 s) runs
  for --seconds, the server is restarted (SIGTERM, then the same port) and the Hub runs as long again
- the node ends live and current, with no core, optional or alert-input error; the writer is healthy and
  dropped, rejected or failed nothing
- per instance and stream: raw publications stored from id 1 without a hole; history rows for core and every
  group; the only replay losses are one agent_restart_unknown_extent per captured stream of the first
  instance; the database passes integrity_check
Only the server processes it started are stopped, by their own handles. Exit 1 if a check fails.

A failure means a released Hub would mis-read this server: change the server, or release a Hub that reads
the new answers and pin it here (docs/development/architecture.md, "The fleet server's contract")."""
import argparse
import importlib.metadata
import json
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path

try:
    HUB_VERSION = importlib.metadata.version("platmon-hub")
except importlib.metadata.PackageNotFoundError:  # reported by main()
    HUB_VERSION = None

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
RESTART_LOSS = "agent_restart_unknown_extent"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def config(directory, port):
    path = Path(directory) / "platmon.ini"
    # the slow groups at their shortest intervals, so that each publishes several times in a short run
    path.write_text(f"[http]\nbind = 127.0.0.1\nport = {port}\n[storage]\ninterval = 5\n[wifi]\ninterval = 1\n"
                    f"[battery]\ninterval = 10\n[probe]\ntargets = 127.0.0.1:{port}\ninterval = 2\ntimeout = 1\n")
    return path


def start(binary, ini, log):
    return subprocess.Popen([binary, "serve", str(ini)], stdout=log, stderr=subprocess.STDOUT)


def stop(p):
    p.terminate()
    try:
        return p.wait(5)
    except subprocess.TimeoutExpired:
        p.kill()
        return p.wait()


def ready(port, timeout=20):
    deadline = time.monotonic() + timeout
    while True:
        try:
            with OPENER.open(f"http://127.0.0.1:{port}/api/status", timeout=2) as response:
                status = json.load(response)
            if status.get("ready") is True:
                return status
        except (OSError, ValueError):
            pass
        if time.monotonic() > deadline:
            raise RuntimeError(f"no ready /api/status on port {port} within {timeout} s")
        time.sleep(0.2)


def validators(endpoint, failures):
    """Hub's validators on one answer of each endpoint; the groups the server publishes that Hub stores."""
    from hub import protocol
    from hub.history.raw import STREAMS
    client = protocol.HttpAgentClient()

    def valid(path, validate, *args):
        try:
            return validate(client.get(endpoint, path, 3), *args)
        except protocol.AgentFailure as e:
            failures.append(f"hub.protocol refused {path}: {type(e).__name__}: {e}")
            return None

    status = valid("/api/status", protocol.status)
    if status is None:
        return []
    valid("/api/stats", protocol.stats, status["instance_id"])  # each endpoint on its own
    observations = valid("/api/observations", protocol.observations, status["instance_id"])
    if observations is None:
        return []
    groups = sorted(observations["groups"])
    unknown = [g for g in groups if g not in STREAMS]
    if unknown:
        failures.append(f"groups the Hub does not store: {unknown} (it stores {list(STREAMS)})")
    return [g for g in groups if g in STREAMS]  # the rest are reported once, here


def node_checks(node, failures):
    expected = dict(headline="live", ready=True, data_current=True, core_error=None, optional_error=None,
                    optional_alert_error=False, consecutive_transport_failures=0)
    for key, value in expected.items():
        if node.get(key) != value:
            failures.append(f"node {key} is {node.get(key)!r}, not {value!r}")


def writer_checks(running, snapshot, failures):
    """running: the writer's snapshot while the Hub ran; snapshot: after it closed (every record handled)."""
    if running.get("state") != "healthy":
        failures.append(f"history writer {running.get('state')!r}: {running.get('last_error')!r}")
    for key, value in snapshot.items():
        if key.startswith("dropped_") and value:
            failures.append(f"history writer {key} = {value}")
    raw = snapshot.get("raw", {})
    for key in ("rejected", "failed_pages", "cursor_read_failures", "compression_fallback"):
        if raw.get(key):
            failures.append(f"raw capture {key} = {raw[key]}")


def database_checks(path, instances, streams, failures):
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            failures.append("the Hub's database fails integrity_check")
        for instance in instances:
            for stream in streams:
                ids = [r[0] for r in db.execute("SELECT publication_id FROM raw_publications WHERE instance_id=? AND "
                                                "stream=? ORDER BY publication_id", (instance, stream))]
                if not ids:
                    failures.append(f"{instance[:8]} {stream}: no raw publication stored")
                elif ids != list(range(1, len(ids) + 1)):
                    failures.append(f"{instance[:8]} {stream}: raw publications not 1..{len(ids)} without a hole")
            if not db.execute("SELECT count(*) FROM core_samples WHERE instance_id=?", (instance,)).fetchone()[0]:
                failures.append(f"{instance[:8]}: no core history row")
            for group in streams[1:]:
                if not db.execute("SELECT count(*) FROM observations WHERE instance_id=? AND group_name=?",
                                  (instance, group)).fetchone()[0]:
                    failures.append(f"{instance[:8]} {group}: no observation history row")
        losses = sorted(db.execute("SELECT instance_id, stream, reason FROM replay_losses").fetchall())
        expected = sorted((instances[0], s, RESTART_LOSS) for s in streams)
        if losses != expected:
            failures.append(f"replay losses {losses}, expected one {RESTART_LOSS} per stream of the first instance")
    finally:
        db.close()


def check(binary, seconds):
    from hub.history.writer import HistoryWriter
    from hub.registry import Node
    from hub.runtime import Fleet
    failures, instances = [], []
    with tempfile.TemporaryDirectory(prefix="platmon-hub-contract-") as directory:
        port = free_port()
        ini, endpoint, db = config(directory, port), f"http://127.0.0.1:{port}", Path(directory) / "history.db"
        log = open(Path(directory) / "server.log", "w")
        server = start(binary, ini, log)
        fleet = None
        try:
            instances.append(ready(port)["instance_id"])
            groups = validators(endpoint, failures)
            writer = HistoryWriter(db, raw_retention_days=1, raw_quota_bytes=64 * 1024 ** 2)
            fleet = Fleet([Node(str(uuid.uuid4()), "contract", endpoint)], history=writer, core_interval=1.0,
                          observations_interval=2.0)
            fleet.start()
            time.sleep(seconds)
            code = stop(server)
            if code != 0:
                failures.append(f"the server exited with {code} on SIGTERM")
            server = start(binary, ini, log)
            instances.append(ready(port)["instance_id"])
            time.sleep(seconds)
            node, running = fleet.list_nodes()[0], writer.snapshot()
            fleet.close()
            fleet = None
            if node.get("instance_id") != instances[-1]:
                failures.append(f"the Hub's instance {node.get('instance_id')} is not the server's {instances[-1]}")
            node_checks(node, failures)
            writer_checks(running, writer.snapshot(), failures)
            database_checks(db, instances, ["core", *groups], failures)
        finally:
            if fleet is not None:
                fleet.close()
            stop(server)
            log.close()
            if failures:
                print((Path(directory) / "server.log").read_text()[-4000:], file=sys.stderr)
    return failures, instances


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("server", help="the platmon-server binary")
    parser.add_argument("--seconds", type=float, default=20.0, help="how long the Hub runs per instance (10 to 300)")
    args = parser.parse_args()
    if not 10 <= args.seconds <= 300:
        parser.error("--seconds must be 10 to 300")
    if HUB_VERSION is None:
        parser.exit(1, "hub contract: platmon-hub is not installed in this Python (hub-requirements.txt)\n")
    try:
        failures, instances = check(args.server, args.seconds)
    except (OSError, RuntimeError) as e:
        parser.exit(1, f"hub contract failed: {e}\n")
    for f in failures:
        print("FAIL", f)
    if failures:
        parser.exit(1, f"hub contract: {len(failures)} check(s) failed against platmon-hub {HUB_VERSION}\n")
    print(f"hub contract passed: platmon-hub {HUB_VERSION} validated and captured {len(instances)} instances "
          "(core and every group, from id 1, no loss but the restart's), its history and database healthy")


if __name__ == "__main__":
    main()
