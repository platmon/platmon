"""The released Hub against the deployed candidate service: the same events on every host.

  hub_check.py --hub PATH/TO/platmon-hub [--hosts pc,orin=user@host,...] --out DIR

Starts the candidate unit (platmon-native-candidate.service, 127.0.0.1:19797) on every host, a
temporary Hub (the given platmon-hub executable, raw capture on, a new database, its own port; boards
through an SSH tunnel), then, at fixed times: a restart of every agent (180 s), a SIGKILL of every agent
(systemd restarts it, 300 s), the Hub stopped for 60 s (420 s) and started again on the same database,
every agent stopped for 30 s (600 s), the end (780 s). Only what this run started is stopped, by its own
handle; the units by the candidate sudoers rules. The production services and Hub are not touched.

Checked, per host, from the Hub's database and APIs:
- the agent instances the Hub stored are the ones the agents had (4: start, restart, kill, stop/start)
- replay, per instance and stream: publication ids rising without a hole that no loss explains; every
  instance that began during the run stored from id 1; no buffer_unavailable loss; one
  agent_restart_unknown_extent loss per stream that was captured, per instance change (3)
- the Hub's 60 s down: the core publications reported inside it (one a second) were stored after it came back
- at the end: the active core cursor is the agent's instance and within 10 ids of its latest; the node is
  current; no history writer drops; the database passes integrity_check; no traceback in the Hub log
Exit 1 if a check fails."""
import argparse
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
import uuid

AGENT_PORT = 19797
EVENTS = ((180, "restart"), (300, "kill"), (420, "hub down"), (480, "hub up"), (600, "agents down"),
          (630, "agents up"), (780, "end"))
INSTANCES = 4  # the first, then after the restart, the kill and the stop
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def get(port, path, timeout=3):
    with OPENER.open(f"http://127.0.0.1:{port}{path}", timeout=timeout) as response:
        return json.load(response)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Host:
    def __init__(self, name, ssh):
        self.name, self.ssh, self.unit = name, ssh, "platmon-native-candidate.service"
        self.node_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"platmon-hub-check/{name}"))
        self.port, self.tunnel, self.instances = AGENT_PORT, None, []

    def systemctl(self, *args):
        command = ["sudo", "-n", "/usr/bin/systemctl", *args]
        if self.ssh:
            command = ["ssh", "-o", "BatchMode=yes", self.ssh, " ".join(command)]
        subprocess.run(command, check=True, capture_output=True, timeout=60)

    def open_tunnel(self):
        if self.ssh:
            self.port = free_port()
            self.tunnel = subprocess.Popen(["ssh", "-N", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes",
                                            "-L", f"127.0.0.1:{self.port}:127.0.0.1:{AGENT_PORT}", self.ssh])

    def observe(self):
        """The agent's instance now (None when it does not answer), remembered in order."""
        try:
            instance = get(self.port, "/api/status", 2)["instance_id"]
        except (OSError, ValueError, KeyError):
            return None
        if not self.instances or self.instances[-1] != instance:
            self.instances.append(instance)
        return instance


def start_hub(hub, registry, port, db, log):
    return subprocess.Popen([hub, "--nodes", str(registry), "--port", str(port), "--history-db", str(db),
                             "--raw-capture", "--raw-retention-days", "1", "--raw-quota-mib", "1024",
                             "--no-registry-reload", "--log-file", str(log)],
                            cwd=db.parent, stdout=subprocess.DEVNULL, stderr=open(log.with_suffix(".err"), "w"))


def stop(process):
    if process is not None and process.poll() is None:
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    return None if process is None else process.returncode


def wait_hub(port, deadline=30):
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        try:
            if get(port, "/api/history/status")["history"].get("schema_version") is not None:
                return
        except (OSError, ValueError, KeyError):
            pass
        time.sleep(0.2)
    raise RuntimeError("the Hub did not become ready")


def run(args, hosts, out):
    out.mkdir(parents=True)
    registry, db, log = out / "nodes.json", out / "history.db", out / "hub.log"
    registry.write_text(json.dumps({"nodes": [{"node_id": h.node_id, "name": h.name,
                                               "endpoint": f"http://127.0.0.1:{h.port}"} for h in hosts]}))
    hub_port, hub, timeline, final = free_port(), None, [], {}
    try:
        for h in hosts:
            h.systemctl("start", h.unit)
        time.sleep(3)
        if any(h.observe() is None for h in hosts):
            raise RuntimeError("an agent did not answer after its start")
        hub = start_hub(args.hub, registry, hub_port, db, log)
        wait_hub(hub_port)
        began = time.monotonic()
        for at, event in EVENTS:
            while time.monotonic() < began + at * args.scale:
                for h in hosts:
                    h.observe()
                time.sleep(1)
            timeline.append({"event": event, "at_ms": int(time.time() * 1000)})
            if event in ("restart", "kill"):
                for h in hosts:
                    h.systemctl(*(("restart",) if event == "restart" else ("kill", "--signal=SIGKILL")), h.unit)
            elif event == "hub down":
                final["hub_exit_1"] = stop(hub)
            elif event == "hub up":
                hub = start_hub(args.hub, registry, hub_port, db, log)
                wait_hub(hub_port)
            elif event in ("agents down", "agents up"):
                for h in hosts:
                    h.systemctl("stop" if event == "agents down" else "start", h.unit)
        for h in hosts:
            instance = h.observe()
            latest = get(h.port, "/api/samples?limit=1")["latest_id"]
            final[h.name] = {"instance": instance, "latest_id": latest}
        final["nodes"] = get(hub_port, "/api/nodes")["nodes"]
        final["history"] = get(hub_port, "/api/history/status")["history"]
        final["replay"] = get(hub_port, "/api/replay/status")
    finally:
        final["hub_exit_2"] = stop(hub)
        for h in hosts:
            h.systemctl("stop", h.unit)
    return timeline, final


def judge(hosts, timeline, final, db, log):
    problems, report = [], {}
    at = {e["event"]: e["at_ms"] for e in timeline}
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    if c.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        problems.append("database integrity_check failed")
    text = log.read_text(errors="replace") + log.with_suffix(".err").read_text(errors="replace")
    if "Traceback" in text:
        problems.append("a traceback in the Hub log")
    if final.get("hub_exit_1") != 0 or final.get("hub_exit_2") != 0:
        problems.append(f"Hub exit codes {final.get('hub_exit_1')}, {final.get('hub_exit_2')}")
    drops = {k: v for k, v in final["history"].items() if k.startswith("dropped_") and v}
    if drops:
        problems.append(f"history writer drops {drops}")
    nodes = {n["node_id"]: n for n in final["nodes"]}
    for h in hosts:
        p, r = [], {}
        stored = [row[0] for row in c.execute(
            "SELECT instance_id FROM raw_publications WHERE node_id=? AND stream='core' "
            "GROUP BY instance_id ORDER BY min(received_at_ms)", (h.node_id,))]
        r["instances"] = {"agent": len(h.instances), "stored": len(stored)}
        if len(h.instances) != INSTANCES or stored != h.instances:
            p.append(f"instances: agent {h.instances}, stored {stored}")
        losses = c.execute("SELECT instance_id, stream, from_id, to_id, reason FROM replay_losses WHERE node_id=?",
                           (h.node_id,)).fetchall()
        reasons = {}
        for _, stream, _, _, reason in losses:
            reasons.setdefault(reason, {}).setdefault(stream, 0)
            reasons[reason][stream] += 1
        r["losses"] = reasons
        if "buffer_unavailable" in reasons:
            p.append(f"buffer_unavailable losses {reasons['buffer_unavailable']}")
        streams = [row[0] for row in c.execute(
            "SELECT DISTINCT stream FROM replay_cursors WHERE node_id=?", (h.node_id,))]
        restarts = reasons.get("agent_restart_unknown_extent", {})
        if restarts != {s: INSTANCES - 1 for s in streams}:
            p.append(f"agent_restart_unknown_extent per stream {restarts}, captured streams {streams}")
        explained = {}
        for instance, stream, start, end, _ in losses:
            explained.setdefault((instance, stream), []).append((start, end))
        counts = {}
        for instance, stream in c.execute("SELECT DISTINCT instance_id, stream FROM raw_publications WHERE node_id=?",
                                          (h.node_id,)).fetchall():
            ids = [row[0] for row in c.execute(
                "SELECT publication_id FROM raw_publications WHERE node_id=? AND instance_id=? AND stream=? "
                "ORDER BY publication_id", (h.node_id, instance, stream))]
            counts[stream] = counts.get(stream, 0) + len(ids)
            holes = [(a + 1, b - 1) for a, b in zip(ids, ids[1:]) if b != a + 1]
            covered = explained.get((instance, stream), [])
            unexplained = [x for x in holes if not any(s <= x[0] and (e is None or x[1] <= e) for s, e in covered)]
            if unexplained:
                p.append(f"{stream} of {instance}: ids missing without a loss {unexplained[:3]}")
            if instance != h.instances[0] and ids[0] != 1:
                p.append(f"{stream} of {instance}: stored from id {ids[0]}, not 1")
        r["publications"] = counts
        backfill = c.execute("SELECT count(*) FROM raw_publications WHERE node_id=? AND stream='core' AND "
                             "reported_completed_at*1000 BETWEEN ? AND ? AND received_at_ms >= ?",
                             (h.node_id, at["hub down"] + 2000, at["hub up"] - 2000, at["hub up"])).fetchone()[0]
        r["hub_down_backfill"] = backfill
        down_s = (at["hub up"] - at["hub down"]) / 1000 - 4  # the window counted, at one collection a second
        if backfill < down_s - 2:
            p.append(f"only {backfill} core publications from the Hub's {down_s:.0f} s down were stored after it")
        cursor = c.execute("SELECT instance_id, after_id FROM replay_cursors WHERE node_id=? AND stream='core' "
                           "AND active=1", (h.node_id,)).fetchone()
        agent = final[h.name]
        r["cursor"] = {"hub": cursor, "agent": agent}
        if cursor is None or cursor[0] != agent["instance"] or not 0 <= agent["latest_id"] - cursor[1] <= 10:
            p.append(f"core cursor {cursor} vs the agent {agent}")
        node = nodes.get(h.node_id, {})
        if node.get("data_current") is not True:
            p.append(f"node not current at the end: {node.get('headline')}")
        report[h.name], problems = r, problems + [f"{h.name}: {x}" for x in p]
    c.close()
    return report, problems


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--hub", required=True, help="the platmon-hub executable")
    parser.add_argument("--hosts", default="pc", help="NAME (this host) or NAME=SSH-TARGET, comma separated")
    parser.add_argument("--out", required=True, help="a new directory for the database, logs and report")
    parser.add_argument("--scale", type=float, default=1.0, help=argparse.SUPPRESS)  # a shorter trial of the tool
    args = parser.parse_args()
    hosts = [Host(*(item.split("=", 1) if "=" in item else (item, None))) for item in args.hosts.split(",")]
    out = Path(args.out).resolve()
    try:
        for h in hosts:
            h.open_tunnel()
        time.sleep(2)
        if any(h.tunnel is not None and h.tunnel.poll() is not None for h in hosts):
            raise RuntimeError("an SSH tunnel did not start")
        timeline, final = run(args, hosts, out)
    finally:
        for h in hosts:
            stop(h.tunnel)
    report, problems = judge(hosts, timeline, final, out / "history.db", out / "hub.log")
    result = {"unit": hosts[0].unit, "timeline": timeline, "hosts": report, "problems": problems,
              "passed": not problems}
    (out / "report.json").write_text(json.dumps(result, indent=1) + "\n")
    print(json.dumps(result, indent=1))
    sys.exit(0 if not problems else 1)


if __name__ == "__main__":
    main()
