"""platmon.ini read by `platmon-server config-check`, compared exactly with the recorded answers
(golden/config.json, first recorded from the Python service's platmon.load_config before its removal in
1.6.0; the temporary directory as ${dir}): every setting with its type, the levels (with a platform's temperature default) and the probe
targets, or the same refusal message; also with a --frontends value, as platmon.py passed it on. Then
refusals as the server gives them (`platmon-server serve`: exit 2, the message platmon.py gave), only for
configurations it must refuse, so nothing starts listening. Last, the shipped platmon.ini must read as no
file does: every value in it is the default. Runs on the host: the files are written to a temporary
directory. --bless records the server's answers instead and rewrites golden/config.json when every refusal
still exits with 2 and the shipped platmon.ini still reads as the defaults.

  config_fixture.py BINARY [--bless]"""
import json
import os
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
GOLDEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "golden", "config.json")

CASES = [  # name, file text (None: no file), platform
    ("no file", None, "Jetson Orin"),
    ("empty file", "", None),
    ("the defaults written out", "[core]\ninterval = 1.0\n[network]\nenabled = yes\n[disk_io]\nenabled = yes\n[storage]\n"
     "enabled = yes\ninterval = 30\n[wifi]\nenabled = yes\ninterval = 5\n[battery]\nenabled = yes\ninterval = 60\n[probe]\n"
     "enabled = yes\ninterval = 10\ntimeout = 2\ntargets =\n[history]\nenabled = yes\nretention = 600\n[replay]\nenabled = yes\n"
     "retention = 600\nmax_bytes = 16777216\n[pressure]\nenabled = yes\n[http]\nenabled = yes\nbind = 0.0.0.0\nport = 9797\n"
     "web = yes\n[thresholds]\ncpu = 80, 90\ngpu = 80, 90\nmemory = 80, 90\nfilesystem = 80, 90\ntemperature =\n"
     "battery = 20, 10\nwifi = -70, -80\n", "Raspberry Pi"),  # the shipped platmon.ini's settings in 1.6.0
    ("everything set", "[core]\ninterval = 2\n[network]\nenabled = no\n[disk_io]\nenabled = Off\n[storage]\nenabled = 0\ninterval = 60\n"
     "[wifi]\nenabled = TRUE\ninterval = 1\n[battery]\ninterval = 3600\n[probe]\ninterval = 30\ntimeout = 0.5\n"
     "targets = 192.0.2.1:443, [2001:DB8::1]:22, 10.0.0.1:1\n[history]\nretention = 60\n[replay]\nretention = 1\nmax_bytes = 1_048_576\n"
     "[pressure]\nenabled = on\n[http]\nbind = 127.0.0.1\nport = 80\nweb = no\n"
     "[thresholds]\ncpu = 70,95\ngpu = 1, 100\nmemory = 50.5 , 60\nfilesystem = 90, 99\ntemperature = 60, 150\nbattery = 50, 5\nwifi = -60, -90\n", "PC"),
    ("comments, colons, case, continuation", "# top\n; also\n[core]\n  # indented comment\nINTERVAL: 0.5\n[thresholds]\ncpu = 80,\n  90\n", None),
    ("a value continued after a blank line", "[thresholds]\ncpu = 80,\n\n  90\n", None),
    ("DEFAULT keys reach every section", "[DEFAULT]\nenabled = no\n", None),
    ("header with trailing text", "[core] trailing\ninterval = 3\n", None),
    ("no section header", "interval = 1\n", None),
    ("duplicate section", "[core]\n[core]\n", None),
    ("duplicate option", "[core]\ninterval = 1\nInterval = 2\n", None),
    ("parsing errors", "[core]\ngarbage\n= 3\n[http]\nmore garbage\n", None),
    ("unknown section", "[cores]\n[zzz]\n", None),
    ("removed section", "[terminal]\nx = 1\n", None),
    ("unknown key", "[core]\nintervall = 1\nz = 2\n", None),
    ("not a bool", "[network]\nenabled = maybe\n", None),
    ("not an int", "[http]\nport = 80.0\n", None),
    ("not a float", "[core]\ninterval = fast\n", None),
    ("float with underscores", "[core]\ninterval = 1_0.5\n", None),
    ("float bad underscore", "[core]\ninterval = 1__0\n", None),
    ("interval too small", "[core]\ninterval = 0.05\n", None),
    ("interval nan", "[core]\ninterval = nan\n", None),
    ("interval inf", "[core]\ninterval = inf\n", None),
    ("storage interval too small", "[storage]\ninterval = 4.9\n", None),
    ("port too large", "[http]\nport = 65536\n", None),
    ("port zero", "[http]\nport = 0\n", None),
    ("max_bytes too small", "[replay]\nmax_bytes = 1048575\n", None),
    ("probe timeout too large", "[probe]\ntimeout = 10.5\n", None),
    ("history retention too short", "[history]\nretention = 59\n", None),
    ("thresholds one number", "[thresholds]\ncpu = 80\n", None),
    ("thresholds three numbers", "[thresholds]\ngpu = 1, 2, 3\n", None),
    ("thresholds reversed", "[thresholds]\nmemory = 90, 80\n", None),
    ("thresholds above 100", "[thresholds]\ncpu = 90, 101\n", None),
    ("temperature up to 150", "[thresholds]\ntemperature = 100, 151\n", None),
    ("battery high is good", "[thresholds]\nbattery = 10, 20\n", None),
    ("wifi above 0", "[thresholds]\nwifi = 1, -80\n", None),
    ("thresholds not numbers", "[thresholds]\nfilesystem = a, b\n", None),
    ("thresholds nan", "[thresholds]\ncpu = nan, 90\n", None),
    ("probe target without port", "[probe]\ntargets = 192.0.2.1\n", None),
    ("probe target port 0", "[probe]\ntargets = 192.0.2.1:0\n", None),
    ("probe IPv6 unbracketed", "[probe]\ntargets = 2001:db8::1:22\n", None),
    ("probe name, not an address", "[probe]\ntargets = example.com:80\n", None),
    ("probe IPv4 leading zero", "[probe]\ntargets = 192.0.02.1:80\n", None),
    ("probe five targets", "[probe]\ninterval = 60\ntargets = 192.0.2.1:1, 192.0.2.1:2, 192.0.2.1:3, 192.0.2.1:4, 192.0.2.1:5\n", None),
    ("probe twice", "[probe]\ntargets = 192.0.2.1:1, 192.0.2.1:1\n", None),
    ("probe same after normalizing", "[probe]\ntargets = [2001:db8::1]:1, [2001:DB8:0::1]:1\n", None),
    ("probe slower than its interval", "[probe]\ninterval = 4\ntimeout = 2\ntargets = 192.0.2.1:1, 192.0.2.2:1\n", None),
    ("probe empty items", "[probe]\ntargets = , 192.0.2.1:1 ,,\n", None),
    ("nothing to run", "[http]\nenabled = no\n", None),
]
FRONTENDS = [  # name, file text (None: no file), the --frontends value as given
    ("--frontends http over enabled = no", "[http]\nenabled = no\n", "http"),
    ("--frontends http, already enabled", None, "http"),
    ("--frontends http twice", None, "http,http"),
    ("an empty --frontends keeps the file", "[http]\nenabled = no\n", ""),
    ("an unknown frontend", None, "web"),
    ("unknown frontends sorted, once each", None, "zz,http,aa,zz"),
    ("a trailing comma: an empty name", None, "http,"),
    ("names are case-sensitive", None, "HTTP"),
    ("a space is part of the name", None, " http"),
    ("a config error comes first", "[core]\ninterval = fast\n", "web"),
    ("a probe error comes first", "[probe]\ntargets = 192.0.2.1\n", "web"),
    ("--frontends http keeps the file's other settings", "[http]\nenabled = no\nport = 8080\nweb = no\n", "http"),
]
REFUSED = [  # name, file text, the arguments (FILE: where the file goes, else first); both servers refuse
    ("serve: unknown frontend", None, ["--frontends", "web"]),
    ("serve: --frontends=VALUE", None, ["--frontends=web,zz"]),
    ("serve: --frontends= keeps enabled = no", "[http]\nenabled = no\n", ["--frontends="]),
    ("serve: the last --frontends counts", None, ["--frontends", "http", "--frontends", "web"]),
    ("serve: --frontends before the file", "[http]\nenabled = no\n", ["--frontends", "", "FILE"]),
    ("serve: a config error before the frontends", "[core]\ninterval = 0\n", ["--frontends", "web"]),
]


def write(d, i, text):
    if text is None:
        return ""
    path = os.path.join(d, f"case{i}.ini")
    with open(path, "w") as f:
        f.write(text)
    return path


def refusal(cmd):
    """(exit code, the message after "error: " on stderr's last line); a server still running after 10 s is
    killed and reported as such."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=10, cwd=tempfile.gettempdir())
    except subprocess.TimeoutExpired:
        return "still running after 10 s", ""
    last = (p.stderr.strip().splitlines() or [""])[-1]
    return p.returncode, last.split("error: ", 1)[-1]


def dir_token(value, d, back=False):
    """value with the temporary directory d as ${dir} (back: ${dir} as d), in every string in it."""
    if isinstance(value, dict):
        return {k: dir_token(v, d, back) for k, v in value.items()}
    if isinstance(value, list):
        return [dir_token(v, d, back) for v in value]
    if isinstance(value, str):
        return value.replace("${dir}", d) if back else value.replace(d, "${dir}")
    return value


class Golden:
    """The recorded answers, in the order they are asked for; bless: the server's, kept to be written."""

    def __init__(self, d, bless=False):
        self.d, self.n, self.bless, self.out = d, 0, bless, []
        if not bless:
            with open(GOLDEN, encoding="utf-8") as f:
                self.recorded = json.load(f)

    def answer(self, name, server):
        if self.bless:
            self.out.append({"name": name, "python": dir_token(server, self.d)})
            return server
        got = self.recorded[self.n] if self.n < len(self.recorded) else {}
        if got.get("name") != name:
            sys.exit(f"{GOLDEN}: answer {self.n + 1} is for {got.get('name')!r}, not {name!r}")
        self.n += 1
        return dir_token(got["python"], self.d, back=True)

    def close(self):
        if self.bless:
            return
        if self.n != len(self.recorded):
            sys.exit(f"{GOLDEN}: {len(self.recorded)} answers recorded, {self.n} asked for")


def config_check(binary, args):
    """The settings, or {"error"}; exit 1 for a refusal and 0 otherwise, else an exception."""
    p = subprocess.run([binary, "config-check"] + args, capture_output=True, text=True)
    out = json.loads(p.stdout)
    if p.returncode != (1 if "error" in out else 0):
        raise SystemExit(f"config-check {args}: exit {p.returncode} with {p.stdout[:120]!r}")
    return out


def main():
    bless = sys.argv[2:] == ["--bless"]
    binary, failed = os.path.abspath(sys.argv[1]), 0
    total = len(CASES) + len(FRONTENDS) + len(REFUSED) + 1
    with tempfile.TemporaryDirectory() as d:
        python = Golden(d, bless)
        cases = [(name, text, platform, None) for name, text, platform in CASES] + \
                [(name, text, None, frontends) for name, text, frontends in FRONTENDS]
        for i, (name, text, platform, frontends) in enumerate(cases):
            path = write(d, i, text)
            args = [path] + ([platform or "", frontends] if frontends is not None else [platform] if platform else [])
            rs = config_check(binary, args)
            py = python.answer(name, rs)
            same = py == rs and json.dumps(py) == json.dumps(rs)  # also key order and int/float
            failed += not same
            shown = py["error"].replace(d, "DIR").split("\n")[0][:90] if "error" in py else "settings"
            print(f"  {name}: {'OK' if same else 'DIFFERENT'} ({shown})")
            if not same:
                print(f"       python: {json.dumps(py)[:300]}\n       rust:   {json.dumps(rs)[:300]}")
        for i, (name, text, args) in enumerate(REFUSED):
            path = write(d, len(cases) + i, text)
            if "FILE" in args:
                args = [path if a == "FILE" else a for a in args]
            elif path:
                args = [path] + args
            rs = refusal([binary, "serve"] + args)
            py = tuple(python.answer(name, list(rs)))
            same = py == rs and py[0] == 2
            failed += not same
            print(f"  {name}: {'OK' if same else 'DIFFERENT'} ({py[1].replace(d, 'DIR')[:90]})")
            if not same:
                print(f"       python: {py}\n       rust:   {rs}")
        python.close()
    shipped, defaults = (config_check(binary, [path, "Raspberry Pi"]) for path in (os.path.join(REPO, "platmon.ini"), ""))
    same = shipped == defaults
    failed += not same
    print(f"  the shipped platmon.ini: {'OK' if same else 'DIFFERENT'} (the defaults)")
    if not same:
        print(f"       shipped:  {json.dumps(shipped)[:300]}\n       defaults: {json.dumps(defaults)[:300]}")
    if bless and not failed:
        with open(GOLDEN + ".new", "w", encoding="utf-8") as f:
            json.dump(python.out, f, indent=1, ensure_ascii=False)
            f.write("\n")
        os.replace(GOLDEN + ".new", GOLDEN)
    print(f"{total - failed}/{total} configurations {'blessed' if bless and not failed else 'the same'}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
