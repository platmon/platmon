"""platmon.ini read by platmon.load_config and by `platmon-native config-check`, compared exactly: every
setting with its type, the levels (with a platform's temperature default) and the probe targets, or the
same refusal message; also with a --frontends value, as platmon.py passes it on. Then refusals as the two
servers give them (`platmon.py` and `platmon-native serve`: exit 2, the same message), only for
configurations both must refuse, so nothing starts listening. Runs on the host: the files are written to
a temporary directory.

With --record FILE, the Python answers are also written to FILE (the temporary directory as ${dir}); with
--golden FILE, those recorded answers stand in for the Python side, which is then not imported.

  PYTHONPATH=<platmon tree> config_fixture.py BINARY [--record FILE | --golden FILE]"""
import json
import os
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CASES = [  # name, file text (None: no file), platform
    ("no file", None, "Jetson Orin"),
    ("empty file", "", None),
    ("the shipped platmon.ini", open(os.path.join(REPO, "platmon.ini")).read(), "Raspberry Pi"),
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


def python_side(path, platform, frontends=None):
    from platmon import DEFAULTS, levels, load_config
    from collector.probe import parse_targets
    try:
        cfg = load_config(path, frontends.split(",") if frontends else None)
    except ValueError as e:
        return {"error": str(e)}
    out = {}
    for section, options in DEFAULTS.items():
        get = lambda k, d: {bool: cfg[section].getboolean, int: cfg[section].getint, float: cfg[section].getfloat,
                            str: cfg[section].get}[type(d)](k)
        out[section] = {k: get(k, d) for k, d in options.items()}
    out["levels"] = levels(cfg, platform)
    out["targets"] = [list(t) for t in parse_targets(cfg["probe"].get("targets"))]
    return out


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


class Python:
    """The Python side's answers: computed (mode None), computed and recorded, or replayed from the file."""

    def __init__(self, mode, path, d):
        self.mode, self.path, self.d, self.out = mode, path, d, []
        if mode == "golden":
            with open(path, encoding="utf-8") as f:
                self.recorded = json.load(f)

    def answer(self, name, compute):
        if self.mode == "golden":
            got = self.recorded[len(self.out)] if len(self.out) < len(self.recorded) else {}
            if got.get("name") != name:
                sys.exit(f"{self.path}: answer {len(self.out) + 1} is for {got.get('name')!r}, not {name!r}")
            self.out.append(got)
            return dir_token(got["python"], self.d, back=True)
        value = compute()
        self.out.append({"name": name, "python": dir_token(value, self.d)})
        return value

    def close(self):
        if self.mode == "golden" and len(self.out) != len(self.recorded):
            sys.exit(f"{self.path}: {len(self.recorded)} answers recorded, {len(self.out)} asked for")
        if self.mode == "record":
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.out, f, indent=1, ensure_ascii=False)
                f.write("\n")


def main():
    binary, failed = os.path.abspath(sys.argv[1]), 0
    mode, path = (sys.argv[2][2:], os.path.abspath(sys.argv[3])) if sys.argv[2:3] in (["--record"], ["--golden"]) else (None, None)
    total = len(CASES) + len(FRONTENDS) + len(REFUSED)
    with tempfile.TemporaryDirectory() as d:
        python = Python(mode, path, d)
        cases = [(name, text, platform, None) for name, text, platform in CASES] + \
                [(name, text, None, frontends) for name, text, frontends in FRONTENDS]
        for i, (name, text, platform, frontends) in enumerate(cases):
            path = write(d, i, text)
            py = python.answer(name, lambda: python_side(path or None, platform, frontends))
            args = [path] + ([platform or "", frontends] if frontends is not None else [platform] if platform else [])
            rs = json.loads(subprocess.run([binary, "config-check"] + args,
                                           capture_output=True, text=True, check=True).stdout)
            same = py == rs and json.dumps(py) == json.dumps(rs)  # also key order and int/float
            failed += not same
            shown = py["error"].replace(d, "DIR").split("\n")[0][:90] if "error" in py else "settings"
            print(f"  {name}: {'OK' if same else 'DIFFERENT'} ({shown})")
            if not same:
                print(f"       python: {json.dumps(py)[:300]}\n       rust:   {json.dumps(rs)[:300]}")
        server = os.path.join(REPO, "platmon.py")
        for i, (name, text, args) in enumerate(REFUSED):
            path = write(d, len(cases) + i, text)
            if "FILE" in args:
                args = [path if a == "FILE" else a for a in args]
            elif path:
                args = [path] + args
            py = tuple(python.answer(name, lambda: list(refusal([sys.executable, server] + args))))
            rs = refusal([binary, "serve"] + args)
            same = py == rs and py[0] == 2
            failed += not same
            print(f"  {name}: {'OK' if same else 'DIFFERENT'} ({py[1].replace(d, 'DIR')[:90]})")
            if not same:
                print(f"       python: {py}\n       rust:   {rs}")
        python.close()
    print(f"{total - failed}/{total} configurations the same")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
