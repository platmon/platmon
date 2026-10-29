"""The Python service's answers, frozen: what fixture.py and runtime_fixture.py compare platmon-native with,
recorded before the Python service was removed (1.6.0). Each scenario's file
(golden/<harness>/<scenario>.jsonl) holds one line per request: {"request": the line the harness wrote,
"answer": the Python side's answer}.

An answer was kept as the comparison sees it, with what differs by design or by host taken out: an HTTP
response unstamped (runtime_fixture.unstamped: no Date, Server or error-page markup), and, on the fake
trees, the values the container's kernel chose as tokens: the kernel's release and machine
(${kernel}, ${arch}), an interface's index (${ifindex:NAME}), the network namespace's id (${netns}) and the
device numbers of the two tmpfs mounts (${major:MOUNT}, ${minor:MOUNT}); a probe's connect time, which
fixture.py compares only as a number >= 0, as 0.
The harnesses run this file as the Python side: it answers each request with the recorded answer, the
tokens resolved as it answers, from the same places the collectors read them. A request other
than the recorded one, or one past the end, is an error on stderr and exit 1: a broken side, never a match.

  python3 golden.py FILE        # the replaying side: requests on stdin, answers on stdout
Read-only."""
import hashlib
import json
import os
import re
import socket
import sys

NETNS = "/proc/self/ns/net"  # the fake tree's link, as the collectors read it
MOUNTS = ("/fx", "/fxhost")  # the fake trees' tmpfs mounts (fixture.py FX, HOST)
TOKEN = re.compile(r"\$\{(ifindex|netns|major|minor|kernel|arch)(?::([^}]*))?\}")


def namespace_id(st):
    """The collectors' id of a network namespace (collector/network.py namespace_id, the same text)."""
    text = json.dumps({"v": 1, "dev": st.st_dev, "ino": st.st_ino}, sort_keys=True, separators=(",", ":"))
    return "netns1:" + hashlib.sha256(text.encode()).hexdigest()


def environment():
    """The values a fake-tree answer has from the container rather than from its tree."""
    env = {"ifindex": {name: i for i, name in socket.if_nameindex()}, "netns": None, "dev": {},
           "kernel": os.uname().release, "arch": os.uname().machine}
    try:
        env["netns"] = namespace_id(os.stat(NETNS))
    except OSError:
        pass
    for m in MOUNTS:
        try:
            st = os.stat(m)
        except OSError:
            continue
        env["dev"][(os.major(st.st_dev), os.minor(st.st_dev))] = m
    return env


def resolve(value, env):
    """value with its tokens resolved from the environment now. KeyError for a token it has no value for."""
    if isinstance(value, list):
        return [resolve(v, env) for v in value]
    if isinstance(value, dict):
        return {k: resolve(v, env) for k, v in value.items()}
    if not isinstance(value, str):
        return value
    m = TOKEN.fullmatch(value)
    if not m:
        return value
    kind, arg = m.groups()
    if kind == "ifindex":
        return env["ifindex"][arg]
    if kind in ("netns", "kernel", "arch"):
        if env[kind] is None:
            raise KeyError(value)
        return env[kind]
    dev = {mount: d for d, mount in env["dev"].items()}[arg]
    return dev[0] if kind == "major" else dev[1]


def replay(path):
    with open(path, encoding="utf-8") as f:
        recorded = [json.loads(line) for line in f]
    for n, line in enumerate(sys.stdin):
        request = line.rstrip("\n")
        if n >= len(recorded):
            sys.exit(f"golden.py: request {n + 1} past the {len(recorded)} recorded: {request[:80]!r}")
        if request != recorded[n]["request"]:
            sys.exit(f"golden.py: request {n + 1} is {request[:80]!r}, recorded {recorded[n]['request'][:80]!r}")
        try:
            answer = resolve(recorded[n]["answer"], environment())
        except KeyError as e:
            sys.exit(f"golden.py: request {n + 1}: no value for {e}")
        print(json.dumps(answer, separators=(",", ":"), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    replay(sys.argv[1])
