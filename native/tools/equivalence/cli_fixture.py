"""Equivalence of the platmon command (native/src/bin/platmon) with frontends/cli.py, its reference until
cli.py is removed (docs/development/remaining-work.md, "The platmon command in Rust"). Both are run on the
same snapshots and must agree, byte for byte, on:

- --once: stdout, stderr and the exit code, for every /api/stats and /api/observations answer of the
  recorded text scenario (golden/runtime/text.jsonl) and the snapshots below, served by a local stub;
  where cli.py's render() raises (a traceback), only the exit code (1);
- a pipe: the first two snapshots with a blank line between them;
- failures: a closed port on localhost and 127.0.0.1, a name that does not resolve, a 503 and a 404
  answer (explain()), and the command line's errors, --help and --version;
- the live screen: screen() frames through the binary's hidden `--screen-script` against cli.screen(),
  for every view at several widths and heights, with and without color, ASCII, scrolled, live, partial,
  late, remote and offline, with the server's levels; and the keys (next_view, next_offset).

  cli_fixture.py BINARY"""
import itertools
import json
import os
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
CLI = os.path.join(REPO, "frontends", "cli.py")
sys.path.insert(0, os.path.join(REPO, "frontends"))
import cli  # noqa: E402  the reference, one standalone file

ENV = {"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"}
G = 2**30


def recorded():
    """(stats, observations) of each 200 /api/stats answer in the text scenario, with the observations
    answered at the same moment."""
    out, stats = [], None
    for line in open(os.path.join(HERE, "golden", "runtime", "text.jsonl"), encoding="utf-8"):
        entry = json.loads(line)
        request = json.loads(entry["request"])
        if request.get("op") != "http":
            continue
        head, _, body = entry["answer"]["response"].partition("\r\n\r\n")
        path, ok = request["request"].split()[1], " 200 " in head.split("\r\n")[0]
        if path == "/api/stats":
            stats = json.loads(body) if ok else None
        elif path == "/api/observations" and stats is not None:
            out.append((stats, json.loads(body) if ok else None))
            stats = None
    return out


# Snapshots of tests/frontends (test_cli.py FULL, test_cli_screen.py RICH and OBSERVED), and a few more.
FULL = {
    "platform": "Jetson Orin", "model": "Test Board", "uptime": 90061, "power_mode": "MAXN_SUPER",
    "system": {"os": "Ubuntu 22.04.5 LTS", "kernel": "5.15.199-tegra", "arch": "aarch64", "hostname": "orin", "l4t": "R36.5.2"},
    "cpu": [{"id": 0, "usage": 12.5, "freq": 1728000000}, {"id": 7, "usage": 91.4, "freq": 1728000000}],
    "gpu": {"usage": 73.0, "freq": 918000000, "max_freq": 1300000000}, "npu": {"usage": 11.0, "freq": 614400000},
    "memory": {"total": 8 * G, "used": 2 * G, "swap_total": 4 * G, "swap_used": 0},
    "disk": {"total": 100 * G, "used": 25 * G},
    "temperature": {"cpu": 51.2, "tj": 86.2},
    "power": {"VDD_IN": 4.82, "VDD_CPU_GPU_CV": 1.25},
    "fans": [{"name": "pwmfan", "rpm": None, "percent": 31}, {"name": "pwm_tach", "rpm": 1741, "percent": None}],
    "network": {"interfaces": [
        {"name": "eth0", "rates": {"rx_bytes_per_s": 2048.0, "tx_bytes_per_s": 1024.0, "rx_packets_per_s": 8.5, "tx_packets_per_s": 4.0},
         "rx": {"errors": 0, "dropped": 0}, "tx": {"errors": 1, "dropped": 2}},
        {"name": "enx80691aa55882", "rates": {"rx_bytes_per_s": 0.0, "tx_bytes_per_s": 0.0, "rx_packets_per_s": 0.0, "tx_packets_per_s": 0.0},
         "rx": {"errors": 0, "dropped": 0}, "tx": {"errors": 0, "dropped": 0}},
        {"name": "lo", "rates": {"rx_bytes_per_s": 9e6, "tx_bytes_per_s": 9e6, "rx_packets_per_s": 1.0, "tx_packets_per_s": 1.0},
         "rx": {"errors": 0, "dropped": 0}, "tx": {"errors": 0, "dropped": 0}},
        {"name": "wlan1", "rates": None, "reason": "warmup"}]},
    "disk_io": {"disks": [{"name": "nvme0n1", "in_flight": 0, "rates": {"read_bytes_per_s": 4096.0, "write_bytes_per_s": 512.0,
                                                                       "reads_per_s": 3.0, "writes_per_s": 0.5, "io_time_ratio": 0.25}},
                          {"name": "sda", "in_flight": None, "rates": None, "reason": "gap"}]},
    "pressure": {"resources": {"cpu": {"some": {"avg10": 12.4}, "full": None}, "io": {"some": {"avg10": 3.1}, "full": {"avg10": 0.8}}}},
    "sample": {"data_age_ms": 300},
}
OBSERVED = {"groups": {
    "storage": {"observation": {"data_age_ms": 4200, "stale": False}, "data": {
        "filesystems": [{"mount_points": [{"path": "/", "read_only": False}, {"path": "/srv", "read_only": True}], "device": "/dev/nvme0n1p1",
                         "fstype": "ext4", "major": 259, "minor": 1, "used_bytes": 40 * G, "total_bytes": 100 * G, "available_bytes": 55 * G},
                        {"mount_points": [{"path": "/data", "read_only": True}], "device": None, "fstype": "btrfs", "major": 0, "minor": 40,
                         "used_bytes": 95 * G, "total_bytes": 100 * G, "available_bytes": 5 * G},
                        {"mount_points": [{"path": "/boot/efi", "read_only": False}], "device": "sda1", "fstype": "vfat", "major": 8, "minor": 1,
                         "used_bytes": None, "total_bytes": None, "available_bytes": None}],
        "partitions": [{"disk": "nvme0n1", "mount_points": ["/"]}, {"disk": "nvme0n1", "mount_points": []}, {"disk": "sda", "mount_points": []}]}},
    "wifi": {"observation": {"data_age_ms": 4200, "stale": False}, "data": {"interfaces": [
        {"name": "wlP1p1s0", "connected": True, "signal_dbm": -75, "signal_raw": None, "link_quality": 62, "noise_dbm": -92},
        {"name": "wlx00c0ca123456", "connected": True, "signal_dbm": None, "signal_raw": 40, "link_quality": None, "noise_dbm": None},
        {"name": "wlan0", "connected": False}, {"name": "wlan9", "connected": None, "connection_state": "permission_denied"}]}},
    "probe": {"observation": {"data_age_ms": 4200, "stale": True}, "data": {"targets": [
        {"address": "192.168.1.1", "port": 53, "connect_ms": 1.8, "failures": 0, "attempts": 10, "reason": None},
        {"address": "2001:db8::1", "port": 443, "connect_ms": None, "failures": 3, "attempts": 3, "reason": "timeout"}]}},
    "battery": {"observation": {"data_age_ms": 4200, "stale": False}, "data": {
        "batteries": [{"name": "BAT1", "present": True, "percent": 9, "status": "Discharging", "capacity_level": "Low",
                       "charge_full_uah": 3400000, "charge_full_design_uah": 4000000, "current_ua": -1200000, "power_uw": None,
                       "voltage_uv": 16436000, "cycle_count": 237, "temp_c": 31.2},
                      {"name": "BAT2", "present": False}, {"name": "BAT3", "percent": None, "capacity_level": None}],
        "external": [{"name": "ADP1", "type": "Mains", "online": True},
                     {"name": "ucsi-source-psy-USBC000:001", "type": "USB", "online": False}, {"name": "W", "type": "Wireless", "online": None}]}}}}
BARE = dict(FULL, platform="Linux", power_mode=None, gpu=None, npu=None, temperature={}, power={}, fans=[], network=None, disk_io=None,
            pressure=None, system={"hostname": "wsl", "firmware": "1.2"}, cpu=[{"id": 0, "usage": 3.0, "freq": None}],
            memory={"total": 8 * G, "used": 2 * G, "swap_total": 0, "swap_used": 0}, collectors={"core": {"state": "ok"}, "gpu": {"state": "error"}})
MANY = dict(FULL, temperature={f"coretemp Core {i}": 40.0 + i for i in range(38)},
            cpu=[{"id": n, "usage": 10.0 + 7 * n, "freq": None} for n in range(12)], cpu_sampling={"unavailable": [{"id": 12, "reason": "warmup"}]})
SNAPSHOTS = [(FULL, OBSERVED), (FULL, None), (BARE, {"groups": {}}), (MANY, OBSERVED)]


class Stub:
    """A server answering each path with (code, JSON body); 404 for any other path."""

    def __init__(self, answers):
        self.answers = answers
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                code, body = stub.answers.get(self.path, (404, {"error": "not found"}))
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.addr = f"127.0.0.1:{self.httpd.server_address[1]}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def run(cmd, timeout=20):
    r = subprocess.run(cmd, capture_output=True, timeout=timeout, env=ENV, cwd="/")
    return r.returncode, r.stdout, r.stderr


def both(binary, args):
    return run([sys.executable, CLI, *args]), run([binary, *args])


def compare(label, want, got, problems, streams=(0, 1, 2)):
    names = ("exit code", "stdout", "stderr")
    for k in streams:
        if want[k] != got[k]:
            problems.append(f"{label}: {names[k]} differs\n  cli.py: {want[k]!r:.600}\n  binary: {got[k]!r:.600}")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def check_once(binary, problems):
    for n, (stats, obs) in enumerate(recorded() + SNAPSHOTS):
        stub = Stub({"/api/stats": (200, stats), **({"/api/observations": (200, obs)} if obs is not None else {})})
        try:
            want, got = both(binary, ["--once", stub.addr])
        finally:
            stub.close()
        compare(f"--once, snapshot {n}", want, got, problems, (0, 1, 2) if want[0] == 0 else (0,))
    return len(recorded() + SNAPSHOTS)


def check_pipe(binary, problems):
    stub = Stub({"/api/stats": (200, FULL), "/api/observations": (200, OBSERVED)})
    outs = []
    try:
        for cmd in ([sys.executable, CLI], [binary]):
            p = subprocess.Popen([*cmd, stub.addr, "0.05"], stdout=subprocess.PIPE, env=ENV, cwd="/")
            lines = [p.stdout.readline() for _ in range(2 * len(cli.render(FULL, OBSERVED).split("\n")) + 1)]
            p.kill()
            p.wait()
            p.stdout.close()
            outs.append(b"".join(lines))
    finally:
        stub.close()
    if outs[0] != outs[1]:
        problems.append(f"pipe: differs\n  cli.py: {outs[0]!r:.600}\n  binary: {outs[1]!r:.600}")


def check_failures(binary, problems):
    port = free_port()
    cases = [[f"localhost:{port}"], ["--once", f"127.0.0.1:{port}"], ["--once", "nonexistent.invalid"], ["--once", f"[::1]:{port}"],
             ["localhost:abc"], ["--once", "--view", "network"], ["localhost", "0"], ["localhost", "-1"], ["localhost", "nan"],
             ["localhost", "x"], ["a", "b", "c"], ["--v"], ["--view"], ["--once=1"], ["-x"], ["127.0.0.1:1", "--once", "2"], ["--version"],
             ["--help"], ["--he", "--view", "x"], ["x", "y", "--version"], ["--view", "cpu"]]
    for args in cases:
        if args[1:] == ["--once", "2"] and sys.version_info < (3, 13):
            continue  # argparse before 3.13 left a positional after an option unrecognized; the binary reads it as 3.13 does
        want, got = both(binary, args)
        if b"Traceback" in want[2]:  # cli.py does not catch it: the traceback's last line
            want = (want[0], want[1], want[2].strip().split(b"\n")[-1] + b"\n")
        if args == ["--view", "cpu"]:  # how argparse lists the choices changed between Python versions
            want, got = [(r[0], r[1], r[2].split(b"(choose from")[0]) for r in (want, got)]
        compare(" ".join(args), want, got, problems)
    for code in (503, 404):
        stub = Stub({"/api/stats": (code, {"error": "no current data", "code": "no_current_data"})} if code == 503 else {})
        try:
            want, got = both(binary, ["--once", stub.addr])
        finally:
            stub.close()
        compare(f"answer {code}", want, got, problems)
    return len(cases) + 2


def screen_requests():
    error = cli.explain("localhost:9797", OSError(111, "Connection refused"))
    remote = cli.explain("my-device.local:9797", OSError("timed out"))
    statuses = [{"state": "live", "age": 0.4, "error": None, "addr": "localhost:9797", "interval": 1.0},
                {"state": "partial", "age": 3.4, "error": None, "addr": "192.168.55.53:9797", "interval": 0.5,
                 "traffic": {"eth0": 10.0, "enx80691aa55882": 99999.0}},
                {"state": "offline", "age": 47, "error": error, "addr": "localhost:9797", "interval": 2.0},
                {"state": "offline", "age": 9, "error": remote, "addr": "my-device.local:9797", "interval": 1.0}]
    levels = [None, {"temperature": None, "memory": [10, 20], "cpu": [90, 80], "battery": [30, 15], "wifi": [-80, -70], "gpu": "x"}]
    snapshots = recorded() + SNAPSHOTS
    for (stats, obs), view, width in itertools.product(snapshots, cli.VIEWS, (119, 99, 79, 59, 44, 30)):
        for n, status in enumerate(statuses):
            for color, utf8, height, offset in ((True, True, 40, 0), (False, True, 24, 3), (True, False, 14, cli.BOTTOM)):
                if n and not color:
                    continue
                yield {"stats": stats, "obs": obs, "width": width, "height": height, "view": view, "offset": offset,
                       "status": status, "color": color, "utf8": utf8, "levels": levels[n % 2]}
    for stats, obs in SNAPSHOTS:  # the terminal smaller than the frame
        for width, height in ((20, 6), (12, 3), (10, 1), (80, 5)):
            yield {"stats": stats, "obs": obs, "width": width, "height": height, "view": "overview", "offset": 0,
                   "status": statuses[0], "color": True, "utf8": True, "levels": None}


def python_screen(r):
    cli.LIMITS = {**{k: tuple(v) for k, v in cli.UI_SPEC["levels"]["defaults"].items()}, "temperature": None}
    cli.use_levels(r["levels"])
    status = dict(r["status"])
    lines = cli.screen(r["stats"], r["obs"], r["width"], r["height"], cli.Style(r["color"], r["utf8"]), status, r["view"], r["offset"])
    return {"lines": lines, "scroll": list(status["scroll"])}


def check_screens(binary, problems):
    requests = list(screen_requests())
    p = subprocess.run([binary, "--screen-script"], input="".join(json.dumps(r) + "\n" for r in requests), capture_output=True,
                       text=True, timeout=300, env=ENV)
    answers = p.stdout.splitlines()
    if p.returncode != 0 or len(answers) != len(requests):
        problems.append(f"--screen-script: exit {p.returncode}, {len(answers)} answers for {len(requests)}: {p.stderr[:300]}")
        return len(requests)
    raised = 0
    for r, answer in zip(requests, answers):
        try:
            want, got = python_screen(r), json.loads(answer)
        except (KeyError, TypeError, AttributeError, IndexError, ValueError):
            raised += 1  # an answer cli.py's screen() cannot draw (it ends with a traceback); the binary draws what it can
            continue
        if want != got:
            first = next((i for i, (a, b) in enumerate(zip(want["lines"], got["lines"])) if a != b), None)
            where = f"line {first}: {want['lines'][first]!r} / {got['lines'][first]!r}" if first is not None else \
                f"{len(want['lines'])}/{len(got['lines'])} lines, scroll {want['scroll']}/{got['scroll']}"
            problems.append(f"screen {r['view']} {r['width']}x{r['height']} color={r['color']} utf8={r['utf8']} "
                            f"{r['status']['state']} offset={r['offset']}: {where}")
    print(f"screens: {len(requests) - raised} compared, {raised} that cli.py cannot draw skipped")
    return len(requests)


def check_keys(binary, problems):
    keys = ["1", "3", "7", "9", "x", "q", "\t", "\033[C", "\033[D", "\033[Z", "\033[1;2C", "\033[A", "\033[B", "\033[5~", "\033[6~",
            "\033[1~", "\033[4~", "\033[H", "\033[F", "\033OH", "\033OF", "\033[B\033[B3", "2\033[A", "\033[", "\033[6~\033[6~"]
    requests = [{"keys": k, "view": v, "offset": o, "page": 10} for k in keys for v in cli.VIEWS for o in (0, 5)]
    p = subprocess.run([binary, "--screen-script"], input="".join(json.dumps(r) + "\n" for r in requests), capture_output=True,
                       text=True, timeout=60, env=ENV)
    for r, answer in itertools.zip_longest(requests, p.stdout.splitlines()):
        want = {"view": cli.next_view(r["keys"], r["view"]), "offset": cli.next_offset(r["keys"], r["offset"], r["page"])}
        if answer is None or json.loads(answer) != want:
            problems.append(f"keys {r['keys']!r} on {r['view']} at {r['offset']}: cli.py {want}, binary {answer}")
    return len(requests)


def terminal_session(cmd, keys, cols=100, rows=30):
    """Everything cmd writes to a terminal of cols x rows (a pty) while keys are pressed one after another,
    and its exit code."""
    import fcntl
    import select
    import struct
    import termios
    import time
    master, slave = os.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    p = subprocess.Popen(cmd, stdin=slave, stdout=slave, stderr=slave, env=dict(ENV, TERM="xterm"), cwd="/", start_new_session=True,
                         preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0))  # its controlling terminal: Ctrl+C interrupts
    os.close(slave)
    out = b""

    def read(seconds):
        nonlocal out
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if select.select([master], [], [], 0.05)[0]:
                try:
                    data = os.read(master, 65536)
                except OSError:  # the other side closed
                    return
                if not data:
                    return
                out += data
    read(1.0)
    for key in keys:  # bytes typed, or (columns, rows) the terminal is resized to
        if isinstance(key, tuple):
            fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", key[1], key[0], 0, 0))
        else:
            os.write(master, key)
        read(0.5)
    code = p.wait(timeout=10)
    read(0.2)
    os.close(master)
    return code, out


def check_live(binary, problems):
    """The live view on a terminal: the alternate screen and the server's levels, the first frame, views
    switched by an arrow, a number and Tab, scrolling, a resize, then q, and another session ended by
    Ctrl+C; every byte written, and exit 0."""
    current = json.loads(json.dumps(OBSERVED))  # every observation current: no age that grows while the keys are pressed
    current["groups"]["probe"]["observation"]["stale"] = False
    stub = Stub({"/api/stats": (200, MANY), "/api/observations": (200, current), "/api/status": (200, {"levels": {"temperature": [70, 75]}})})
    try:
        for args, keys in (([stub.addr, "30", "--view", "storage"], [b"\033[C", b"5", b"\033[6~", b"\033[B", (70, 20), b"\t", b"q"]),
                           ([stub.addr, "30"], [b"3", b"\003"])):
            (want, a), (got, b) = [terminal_session([*cmd, *args], keys) for cmd in ([sys.executable, CLI], [binary])]
            label = f"live view {' '.join(args[1:])}, keys {keys}"
            if (want, a) != (got, b):
                at = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
                problems.append(f"{label}: exit {want}/{got}, {len(a)}/{len(b)} bytes, first difference at {at}: "
                                f"{a[max(0, at - 40):at + 80]!r} / {b[max(0, at - 40):at + 80]!r}")
            elif want != 0 or b"\033[?1049h" not in a or not a.endswith(b"\033[?1049l"):
                problems.append(f"{label}: exit {want}, the alternate screen not entered and left")
    finally:
        stub.close()


def main(argv):
    if len(argv) != 2:
        sys.exit(__doc__)
    binary = os.path.abspath(argv[1])
    problems = []
    counts = [("--once snapshots", check_once(binary, problems)), ("failures and command lines", check_failures(binary, problems)),
              ("screens", check_screens(binary, problems)), ("key presses", check_keys(binary, problems))]
    check_pipe(binary, problems)
    check_live(binary, problems)
    for line in problems[:40]:
        print(line)
    print(", ".join(f"{n} {what}" for what, n in counts) + ", a pipe, two live terminal sessions: "
          + ("the same as frontends/cli.py" if not problems else f"{len(problems)} DIFFERENT"))
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main(sys.argv)
