"""platmon.ini read by platmon.load_config and by `platmon-native config-check`, compared exactly: every
setting with its type, the levels (with a platform's temperature default) and the probe targets, or the
same refusal message. Runs on the host: the files are written to a temporary directory.

  PYTHONPATH=<platmon tree> config_fixture.py BINARY"""
import json
import os
import subprocess
import sys
import tempfile

from platmon import DEFAULTS, levels, load_config
from collector.probe import parse_targets

CASES = [  # name, file text (None: no file), platform
    ("no file", None, "Jetson Orin"),
    ("empty file", "", None),
    ("the shipped platmon.ini", open(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "platmon.ini")).read(), "Raspberry Pi"),
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


def python_side(path, platform):
    try:
        cfg = load_config(path)
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


def main():
    binary, failed = os.path.abspath(sys.argv[1]), 0
    with tempfile.TemporaryDirectory() as d:
        for i, (name, text, platform) in enumerate(CASES):
            path = ""
            if text is not None:
                path = os.path.join(d, f"case{i}.ini")
                with open(path, "w") as f:
                    f.write(text)
            py = python_side(path or None, platform)
            rs = json.loads(subprocess.run([binary, "config-check", path] + ([platform] if platform else []),
                                           capture_output=True, text=True, check=True).stdout)
            same = py == rs and json.dumps(py) == json.dumps(rs)  # also key order and int/float
            failed += not same
            shown = py["error"].replace(d, "DIR").split("\n")[0][:90] if "error" in py else "settings"
            print(f"  {name}: {'OK' if same else 'DIFFERENT'} ({shown})")
            if not same:
                print(f"       python: {json.dumps(py)[:300]}\n       rust:   {json.dumps(rs)[:300]}")
    print(f"{len(CASES) - failed}/{len(CASES)} configurations the same")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
