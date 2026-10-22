"""The Python side of tools/runtime_fixture.py: the service's runtime.sampler.Sampler and runtime.slow.Slow,
driven by the runtime script's operations with their times and collections (`platmon-native
runtime-script` documents them). One JSON line out for each line in.

A collection is Sampler._attempt() with a collect function that answers "begin" and then reads further
operations until its "finish"; a slow group's observation is Slow.observe_once() the same way. So a view
can be taken while one runs, and they nest (an observation inside a collection) as the script orders them.

  PYTHONPATH=<platmon tree> python3 runtime_py.py"""
import json
import socket
import sys
import threading

from collector.record import Collected
from runtime.server import BoundedHTTPServer, make_handler
from runtime.history import History
from runtime.replay import Replay
from runtime.sampler import Sampler
from runtime.slow import Observations, Slow

now = {"ns": 0, "wall": 0.0}
ops = (json.loads(line) for line in sys.stdin)
state = {}


def elapsed():
    return now["ns"]


def other_clock():  # read records on another clock than the Sampler's
    return 0


def wall():
    return now["wall"]


def answer(value):
    print(json.dumps(value, separators=(",", ":"), ensure_ascii=False), flush=True)


def at(op):
    if "now" in op:
        now["ns"] = op["now"]
    if "wall" in op:
        now["wall"] = op["wall"]


def spans(value):
    """Lists as tuples, as a recording collect() gives its spans; anything else as it is."""
    return tuple(value) if isinstance(value, list) else value


def collected(op):
    r = op["result"]
    if "error" in r:
        raise RuntimeError(r["error"])
    sensors = r.get("sensors")
    if isinstance(sensors, dict):
        sensors = {p: ({**i, "span": spans(i["span"])} if isinstance(i, dict) and "span" in i else i)
                   for p, i in sensors.items()}
    required = r.get("required")
    if isinstance(required, dict):
        required = {k: spans(v) for k, v in required.items()}
    passive = {k: spans(v) for k, v in (r.get("passive") or {}).items()}
    return Collected(r["stats"], sensors, required, other_clock if op.get("clock") == "other" else elapsed,
                     passive.get("network"), passive.get("disk_io"), passive.get("pressure"))


def until(end, name=None):
    """Runs operations until the one that ends the running collection or observation; returns it."""
    for op in ops:
        if op["op"] == end and op.get("group") == name:
            at(op)
            return op
        run(op)
    sys.exit("runtime_py: the script ended inside a collection or an observation")


def collect():
    answer({})  # the "begin": the Sampler has recorded that a collection runs
    return collected(until("finish"))


def observer(name):
    def observe(group):
        answer({})
        op = until("slow_finish", name)
        for target, reason in op.get("notes", []):
            group.note(target, reason)
        for _ in range(op.get("values", 0)):
            group.got(None)
        if op.get("raise"):
            raise RuntimeError(op["raise"])
        return op.get("data")
    return observe


def run(op):
    kind = op["op"]
    if kind == "setup":
        clock = (elapsed, {"source": op["clock"], "suspend_aware": op["clock"] == "boottime"})
        sampler = Sampler(collect, op["interval"], clock=clock, wall=wall)
        sampler.instance_id = op["instance_id"]
        groups = {name: Slow(name, observer(name), interval, elapsed, wall) for name, interval in op.get("slow", [])}
        observations = Observations(list(groups.values()), op["instance_id"], clock[1])
        # wired as platmon.py does
        history = None
        if op.get("replay"):
            replay = Replay(sampler.instance_id, elapsed, clock[1], list(groups), op["replay"]["retention"],
                            op["replay"]["max_bytes"])
            sampler.replay = replay
            for g in groups.values():
                g.replay = replay
        if op.get("history"):
            history = History(op["history"]["retention"], op["interval"])
            sampler.on_publish = history.append
            for g in groups.values():
                g.on_publish = history.append_observation
        httpd = BoundedHTTPServer(("127.0.0.1", 0), make_handler(sampler, False, observations, history, op.get("levels")))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        state.update(sampler=sampler, groups=groups, observations=observations, port=httpd.server_address[1])
        answer({})
    elif kind == "begin":
        at(op)
        state["sampler"]._attempt()
        answer({})  # the "finish"
    elif kind == "view":
        at(op)
        stats, status = state["sampler"]._stats_json()  # as frontends/server.py serves /api/stats
        answer({"stats": None if stats is None else stats.decode(), "status": json.dumps(status)})
    elif kind == "slow_begin":
        at(op)
        state["groups"][op["group"]].observe_once()
        answer({})
    elif kind == "observations":
        at(op)
        answer({"observations": json.dumps(state["observations"].view())})
    elif kind == "http":
        at(op)
        with socket.create_connection(("127.0.0.1", state["port"])) as c:
            c.sendall(op["request"].encode("latin-1"))
            data = b""
            while chunk := c.recv(65536):
                data += chunk
        answer({"response": data.decode("utf-8", "replace")})
    else:
        sys.exit(f"runtime_py: {kind} outside a collection or an observation")


if __name__ == "__main__":
    for op in ops:
        run(op)
