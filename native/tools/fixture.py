"""Deterministic equivalence: the Python collector and platmon-native on the same fake /sys and /proc
trees with the same clocks, compared exactly (no tolerance: every value, JSON type, key order, span,
required read, collectors state and source id). Runs inside the container fixture.sh starts, where
/sys and /proc are bind mounts of the trees written here; never on a host.

Each scenario writes a tree, starts both collectors (as nobody, so file modes apply), and runs its steps:
before a step its change is applied to the tree, then each collector makes one collection on the step's
clocks (fixture_py.py, `platmon-native script`). Every step says whether it expects a collection or a
failed collection (only /proc/stat missing and a model that is not text fail); both sides must give it,
collections must be equal, and both sides must exit with 0 at the end. A failed collection is the
collector's {"error"} answer; exiting, not answering in time or writing something else is a broken side,
never a match. Error messages are not compared. Exit 1 on any difference.

Before the scenarios, the harness is run against fake sides that read no tree (CONTROLS): equal ones must
pass, and both sides failing where a collection is expected, a nonzero exit, a hang or garbage must fail.

  fixture.py BINARY [SCENARIO ...]
  fixture.py --self-test          # the controls only; needs no container"""
import json
import os
import select
import shutil
import subprocess
import sys
import tempfile

FX = "/fx"            # tmpfs; FX/sys is mounted at /sys and FX/proc at /proc
HOST = "/fxhost"      # PLATMON_HOST_ROOT: os-release, nv_tegra_release; statvfs reads this tmpfs
ETC_EXTRA = ("/var/lib/nvpmodel", "/etc/nvpmodel.conf")  # Jetson files read at their absolute paths
HERE = os.path.dirname(os.path.abspath(__file__))
NOBODY = 65534
S = 1_000_000_000
OK, FAILS = "a collection", "a failed collection"  # what a step expects from both sides
TIMEOUT = 30  # s for one answer


# --- trees ---------------------------------------------------------------------------------------------

def put(path, text, mode=None):
    """Writes text (str, or bytes as they are) at path, making parent directories."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(text if isinstance(text, bytes) else text.encode())
    if mode is not None:
        os.chmod(path, mode)


def link(path, target):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    os.symlink(target, path)


def sys_(p):
    return f"{FX}/sys/{p}"


def proc(p):
    return f"{FX}/proc/{p}"


def stat_text(cpus):
    """/proc/stat with per-cpu rows {id: [user, nice, system, idle, iowait, irq, softirq, steal]}."""
    total = [sum(c[i] for c in cpus.values()) for i in range(8)]
    rows = ["cpu  " + " ".join(map(str, total + [0, 0]))]
    rows += [f"cpu{n} " + " ".join(map(str, c + [0, 0])) for n, c in sorted(cpus.items())]
    return "\n".join(rows + ["intr 1 0", "ctxt 100", "btime 1700000000", "processes 10"]) + "\n"


def base_proc(cpus):
    put(proc("stat"), stat_text(cpus))
    put(proc("meminfo"), "MemTotal:        8000000 kB\nMemFree:         1000000 kB\nMemAvailable:    5000000 kB\n"
                         "Buffers:           10000 kB\nSwapTotal:       2000000 kB\nSwapFree:        1500000 kB\n")
    put(proc("uptime"), "1234.56 4000.00\n")
    link(proc("self"), "/realproc/self")


def hwmon(n, device, attrs):
    """/sys/class/hwmon/hwmonN -> the device's hwmon directory (device None: a directory in the class)."""
    if device is None:
        d = sys_(f"class/hwmon/hwmon{n}")
    else:
        d = sys_(f"{device}/hwmon/hwmon{n}")
        link(sys_(f"class/hwmon/hwmon{n}"), f"../../{device}/hwmon/hwmon{n}")
    for k, v in attrs.items():
        put(f"{d}/{k}", v)
    return d


def zone(n, ztype, temp):
    d = sys_(f"devices/virtual/thermal/thermal_zone{n}")
    put(f"{d}/type", ztype + "\n")
    put(f"{d}/temp", f"{temp}\n")
    link(sys_(f"class/thermal/thermal_zone{n}"), f"../../devices/virtual/thermal/thermal_zone{n}")
    return d


def card(cls, name, pci, driver, attrs):
    """A DRM card or accel device: class/<cls>/<name> -> devices/<pci>/<cls>/<name>, device -> ../.."""
    d = sys_(f"devices/pci0000:00/{pci}/{cls}/{name}")
    link(sys_(f"class/{cls}/{name}"), f"../../devices/pci0000:00/{pci}/{cls}/{name}")
    link(f"{d}/device", "../..")
    put(sys_(f"devices/pci0000:00/{pci}/uevent"), f"DRIVER={driver}\nPCI_SLOT_NAME={pci}\n")
    for k, v in attrs.items():
        put(f"{d}/{k}", v)
    return d


CPUS = {0: [100, 0, 50, 800, 10, 0, 5, 0], 1: [200, 5, 60, 700, 0, 0, 0, 0]}


def advance(cpus, busy, idle):
    return {n: [c[0] + busy, c[1], c[2] + busy // 2, c[3] + idle, c[4], c[5], c[6], c[7]] for n, c in cpus.items()}


def pc_tree():
    base_proc(CPUS)
    put(sys_("class/dmi/id/product_name"), "Fixture PC\n")
    put(sys_("devices/system/cpu/cpu0/cpufreq/scaling_cur_freq"), "1800000\n")  # cpu1: none (not_exposed)
    put(f"{HOST}/etc/os-release", 'NAME="Fixture Linux"\nVERSION="1"\nPRETTY_NAME="Fixture Linux 1"\n')
    z0 = zone(0, "acpitz", 45000)
    zone(1, "x86_pkg_temp", 52000)
    zone(2, "acpitz", 47000)  # a second acpitz: "acpitz (thermal_zone2)"
    put(f"{z0}/hwmon0/name", "acpitz\n")  # the zone's hwmon twin, left out by its path
    put(f"{z0}/hwmon0/temp1_input", "45000\n")
    link(sys_("class/hwmon/hwmon0"), "../../devices/virtual/thermal/thermal_zone0/hwmon0")
    hwmon(1, "devices/platform/coretemp.0", {"name": "coretemp\n", "temp1_input": "55000\n", "temp1_label": "Package id 0\n",
                                             "temp2_input": "50000\n", "temp2_label": "Core 0\n", "temp3_input": "abc\n",
                                             "temp4_input": "+1_000\n"})
    hwmon(2, "devices/pci0000:00/0000:00:1d.0/nvme/nvme0", {"name": "nvme\n", "temp1_input": "40000\n", "temp1_label": "Composite\n"})
    hwmon(3, "devices/pci0000:00/0000:00:1e.0/nvme/nvme1", {"name": "nvme\n", "temp1_input": "41000\n", "temp1_label": "Composite\n"})
    hwmon(4, "devices/platform/PNP0C0B:00", {"name": "acpi_fan\n", "fan1_input": "1200\n", "pwm1": "128\n"})
    hwmon(5, "devices/platform/pwm-fan", {"name": "pwmfan\n", "pwm1": "255\n"})
    hwmon(6, "devices/platform/ina", {"name": "ina3221\n", "in1_label": "VDD_IN\n", "curr1_input": "500\n", "in1_input": "5000\n",
                                      "curr2_input": "100\n", "in2_input": "1000\n",  # no label: a sum channel
                                      "in3_label": "VDD_SOC\n", "curr3_input": "333\n", "in3_input": "1001\n",
                                      "power1_input": "2500000\n", "power1_label": "SYS\n"})
    hwmon(7, "devices/platform/multi", {"name": "multi\n", "temp1_input": "30000\n"})  # 7 and 8 share a device
    hwmon(8, "devices/platform/multi", {"name": "multi\n", "temp1_input": "31000\n"})
    hwmon(9, None, {"name": "virtualchip\n", "temp1_input": "-5000\n"})
    card("drm", "card0", "0000:00:02.0", "i915", {"gt/gt0/rc6_residency_ms": "1000\n", "gt_act_freq_mhz": "0\n",  # RC6: 0 MHz
                                                  "gt_max_freq_mhz": "2250\n"})
    link(sys_("class/drm/card0-DP-1"), "../../devices/pci0000:00/0000:00:02.0/drm/card0/card0-DP-1")  # a connector
    card("accel", "accel0", "0000:00:0b.0", "intel_vpu", {"device/npu_busy_time_us": "0\n",
                                                          "device/npu_current_frequency_mhz": "700\n",
                                                          "device/npu_max_frequency_mhz": "1400\n"})


def pc_steps():
    def busy():
        put(proc("stat"), stat_text(advance(CPUS, 30, 70)))
        put(sys_("devices/pci0000:00/0000:00:02.0/drm/card0/gt/gt0/rc6_residency_ms"), "1500\n")  # 500 of 1000 ms idle
        put(sys_("devices/pci0000:00/0000:00:02.0/drm/card0/gt_act_freq_mhz"), "1300\n")
        put(sys_("devices/pci0000:00/0000:00:0b.0/accel/accel0/device/npu_busy_time_us"), "250000\n")
        put(sys_("devices/platform/coretemp.0/hwmon/hwmon1/temp1_input"), "61000\n")

    def regress():
        cpus = advance(CPUS, 60, 140)
        cpus[1] = [c - 1 for c in CPUS[1]]  # cpu1's counters went backwards
        put(proc("stat"), stat_text(cpus))
        put(sys_("devices/pci0000:00/0000:00:02.0/drm/card0/gt/gt0/rc6_residency_ms"), "10\n")  # a reset

    def idle():  # no ticks at all on cpu0
        cpus = advance(CPUS, 60, 140)
        cpus[1] = [c - 1 for c in CPUS[1]]
        put(proc("stat"), stat_text(cpus))

    def hotplug():
        cpus = advance(CPUS, 90, 210)
        cpus[2] = [1, 0, 1, 10, 0, 0, 0, 0]  # a core that came online
        put(proc("stat"), stat_text(cpus))
        os.remove(sys_("class/hwmon/hwmon4"))  # the fan chip went away between collections
        put(sys_("devices/pci0000:00/0000:00:0b.0/accel/accel0/device/npu_busy_time_us"), "1250000\n")

    def after_gap():
        cpus = advance(CPUS, 120, 280)
        cpus[2] = [11, 0, 6, 50, 0, 0, 0, 0]
        put(proc("stat"), stat_text(cpus))
        # 1200 ms of RC6 in a 1 s window (the counters are not read at the same instant): 0 %, not -20 %
        put(sys_("devices/pci0000:00/0000:00:02.0/drm/card0/gt/gt0/rc6_residency_ms"), "1210\n")
        put(sys_("devices/pci0000:00/0000:00:0b.0/accel/accel0/device/npu_busy_time_us"), "1500000\n")

    return [("warmup", None, 10 * S, OK), ("busy", busy, 11 * S, OK), ("counter regressed, rc6 reset", regress, 12 * S, OK),
            ("no ticks", idle, 13 * S, OK), ("same clock: invalid interval", None, 13 * S, OK),
            ("hotplug, fan chip gone, gap", hotplug, 20 * S, OK), ("after the gap", after_gap, 21 * S, OK)]


def failure_tree():
    pc_tree()
    os.remove(f"{HOST}/etc/os-release")
    put(f"{HOST}/usr/lib/os-release", 'NAME="Fallback"\nVERSION="2"\n')  # the fallback, NAME VERSION
    os.chmod(sys_("devices/platform/coretemp.0/hwmon/hwmon1/temp2_input"), 0)  # permission_denied
    put(sys_("devices/pci0000:00/0000:00:1d.0/nvme/nvme0/hwmon/hwmon2/temp1_label"), b"Comp\xffsite\n")  # invalid_data
    put(sys_("devices/pci0000:00/0000:00:1e.0/nvme/nvme1/hwmon/hwmon3/temp1_input"), "\n")  # empty: invalid_data
    os.chmod(sys_("devices/platform/multi/hwmon/hwmon8"), 0o311)  # listable no more, name still readable
    link(sys_("class/hwmon/hwmon10"), "../../devices/platform/gone/hwmon/hwmon10")  # disappeared
    put(sys_("devices/platform/ina/hwmon/hwmon6/in3_label"), "", 0)  # a rail label that cannot be read
    os.chmod(sys_("devices/virtual/thermal/thermal_zone1/type"), 0)  # named after its directory
    put(sys_("devices/virtual/thermal/thermal_zone2/temp"), b"4\xff\n")
    # GPU: an unknown driver first, then amdgpu, which is read; no accel class at all
    for p in ("class/drm", "class/accel", "devices/pci0000:00/0000:00:02.0", "devices/pci0000:00/0000:00:0b.0"):
        shutil.rmtree(sys_(p))
    card("drm", "card0", "0000:00:02.0", "nouveau", {})
    card("drm", "card1", "0000:03:00.0", "amdgpu", {"device/gpu_busy_percent": "37\n"})
    put(sys_("devices/pci0000:00/0000:03:00.0/gpu_busy_percent"), "37\n")


def failure_steps():
    stat = proc("stat")

    def proc_stat_gone():
        os.rename(stat, stat + ".off")

    def proc_stat_back():
        os.rename(stat + ".off", stat)
        os.chmod(sys_("devices/platform/coretemp.0/hwmon/hwmon1/temp2_input"), 0o644)  # readable again

    def model_not_text():
        put(sys_("class/dmi/id/product_name"), b"\xfe\xff\n")
        put(stat, stat_text(advance(CPUS, 30, 70)))

    def model_fixed():
        put(sys_("class/dmi/id/product_name"), "Fixture PC\n")
        put(stat, stat_text(advance(CPUS, 60, 140)))
        os.chmod(sys_("class/thermal"), 0o311)  # the class cannot be listed
        os.chmod(sys_("class/hwmon"), 0o311)

    def second():
        put(stat, stat_text(advance(CPUS, 10, 90)))

    return [("warmup", None, 10 * S, OK), ("second", second, 11 * S, OK),
            ("/proc/stat gone: collection fails", proc_stat_gone, 12 * S, FAILS),
            ("/proc/stat back: warmup again", proc_stat_back, 13 * S, OK),
            ("model not UTF-8: collection fails", model_not_text, 14 * S, FAILS),
            ("classes unlistable", model_fixed, 15 * S, OK)]


TEGRA = "nvidia,p3768-0000+p3767-0005-super\0nvidia,p3767-0005\0nvidia,tegra234\0"


def jetson_tree():
    base_proc({n: c for n, c in enumerate([CPUS[0], CPUS[1], CPUS[0], CPUS[1], CPUS[0], CPUS[1]])})
    put(proc("device-tree/compatible"), TEGRA)
    put(proc("device-tree/model"), "NVIDIA Jetson Orin Nano Engineering Reference Developer Kit Super\0")
    for n in range(6):
        put(sys_(f"devices/system/cpu/cpu{n}/cpufreq/scaling_cur_freq"), "729600\n")
    put(f"{HOST}/etc/os-release", 'PRETTY_NAME="Ubuntu 22.04.5 LTS"\n')
    put(f"{HOST}/etc/nv_tegra_release", "# R36 (release), REVISION: 5.2, GCID: 1, BOARD: generic, EABI: aarch64, DATE: x\n")
    put("/var/lib/nvpmodel/status", "pmode:0002\n")
    put("/etc/nvpmodel.conf", "< POWER_MODEL ID=0 NAME=15W >\n< POWER_MODEL ID=2 NAME=MAXN_SUPER >\n")
    for n, t in enumerate(("cpu-thermal", "gpu-thermal", "soc0-thermal", "tj-thermal")):
        zone(n, t, 48000 + 500 * n)
    gpu = "devices/platform/bus@0/17000000.gpu"
    put(sys_(f"{gpu}/load"), "123\n")
    put(sys_(f"{gpu}/devfreq/17000000.gpu/cur_freq"), "306000000\n")
    put(sys_(f"{gpu}/devfreq/17000000.gpu/max_freq"), "1020000000\n")
    link(sys_(f"{gpu}/devfreq/17000000.gpu/device"), "../..")
    link(sys_("class/devfreq/17000000.gpu"), f"../../{gpu}/devfreq/17000000.gpu")
    link(sys_("class/devfreq/17000000.host1x"), f"../../{gpu}/devfreq/17000000.gpu")  # not the GPU's name
    hwmon(1, "devices/platform/bus@0/c240000.i2c/i2c-1/1-0040", {
        "name": "ina3221\n", "in1_label": "VDD_IN\n", "in1_input": "5056\n", "curr1_input": "1120\n",
        "in2_label": "VDD_CPU_GPU_CNN\n", "in2_input": "5048\n", "curr2_input": "320\n",
        "in3_label": "VDD_SOC\n", "in3_input": "5040\n", "curr3_input": "400\n",
        "curr4_input": "1840\n", "in4_input": "5050\n"})
    hwmon(2, "devices/platform/pwm-fan", {"name": "pwmfan\n", "pwm1": "102\n"})
    hwmon(3, "devices/platform/39c0000.tachometer", {"name": "pwm_tach\n", "rpm": "2400\n"})


def jetson_steps():
    def second():
        put(proc("stat"), stat_text({n: advance(CPUS, 10, 90)[n % 2] for n in range(6)}))

    def change():
        put(proc("stat"), stat_text({n: advance(CPUS, 20, 180)[n % 2] for n in range(6)}))
        put("/var/lib/nvpmodel/status", "pmode:x\n")  # invalid_data
        os.chmod(sys_("devices/platform/39c0000.tachometer/hwmon/hwmon3/rpm"), 0)
        os.remove(sys_("devices/platform/bus@0/17000000.gpu/load"))  # no load: no GPU entry
    return [("warmup", None, 5 * S, OK), ("second", second, 6 * S, OK),
            ("nvpmodel invalid, rpm unreadable, no GPU load", change, 7 * S, OK)]


def bare_tree():
    """A Linux host with nothing optional: no DMI, no device tree, no thermal, hwmon, DRM or accel."""
    base_proc({0: CPUS[0]})
    os.makedirs(sys_("class"))


def pi_tree():
    base_proc({n: CPUS[0] for n in range(4)})
    put(proc("device-tree/compatible"), "raspberrypi,4-model-b\0brcm,bcm2711\0")
    put(proc("device-tree/model"), "Raspberry Pi 4 Model B Rev 1.5\0")
    put(f"{HOST}/etc/os-release", 'PRETTY_NAME="Debian GNU/Linux 13 (trixie)"\n')
    zone(0, "cpu-thermal", 51500)
    put(sys_("devices/virtual/thermal/thermal_zone0/hwmon0/name"), "cpu_thermal\n")  # the kernel's "_" form
    put(sys_("devices/virtual/thermal/thermal_zone0/hwmon0/temp1_input"), "51500\n")
    link(sys_("class/hwmon/hwmon0"), "../../devices/virtual/thermal/thermal_zone0/hwmon0")
    card("drm", "card0", "0000:00:00.0", "v3d", {})  # named, not read


def two_steps():
    return [("warmup", None, 10 * S, OK), ("second", lambda: put(proc("stat"), stat_text(
        {n: c for n, c in enumerate([advance(CPUS, 30, 70)[0]] * 4)})), 11 * S, OK)]


SCENARIOS = {
    "pc": (pc_tree, pc_steps),
    "failures": (failure_tree, failure_steps),
    "jetson": (jetson_tree, jetson_steps),
    "bare": (bare_tree, lambda: [("warmup", None, 10 * S, OK), ("same", None, 11 * S, OK)]),
    "pi": (pi_tree, two_steps),
}


# --- running and comparing -----------------------------------------------------------------------------

def clear():
    for root in (f"{FX}/sys", f"{FX}/proc", HOST):
        os.chmod(root, 0o755)
        for n in os.listdir(root):
            p = os.path.join(root, n)
            if os.path.islink(p) or not os.path.isdir(p):
                os.remove(p)
                continue
            for d, dirs, _ in os.walk(p):  # modes set by a scenario would stop the removal
                for x in dirs:
                    if not os.path.islink(os.path.join(d, x)):
                        os.chmod(os.path.join(d, x), 0o755)
            shutil.rmtree(p)
    for p in ETC_EXTRA:
        shutil.rmtree(p, ignore_errors=True) if os.path.isdir(p) else (os.path.exists(p) and os.remove(p))


class Broken(Exception):
    """A side that did not answer: it exited, timed out, or wrote something that is neither a collection
    nor an error. Unlike a collection that failed, this is never equal on both sides."""


def start(cmd, log):
    env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": "/platmon", "PLATMON_HOST_ROOT": HOST, "PYTHONDONTWRITEBYTECODE": "1"}
    user = {"user": NOBODY, "group": NOBODY} if os.geteuid() == 0 else {}  # the self-test also runs unprivileged
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, text=True,
                            env=env, cwd="/", **user)


def exited(p):
    try:
        return f"exited with {p.wait(timeout=5)}"
    except subprocess.TimeoutExpired:
        return "closed its output"


def step(p, base, wall, timeout):
    """One collection: {"stats", "sensors", "required"} or {"error"}, else Broken."""
    try:
        p.stdin.write(f"{base} {wall}\n")
        p.stdin.flush()
    except BrokenPipeError:
        raise Broken(exited(p)) from None
    if not select.select([p.stdout], [], [], timeout)[0]:
        raise Broken(f"no answer in {timeout} s")
    line = p.stdout.readline()
    if not line:
        raise Broken(exited(p))
    try:
        out = json.loads(line)
    except ValueError:
        raise Broken(f"not JSON: {line[:80]!r}") from None
    if not isinstance(out, dict) or set(out) not in ({"stats", "sensors", "required"}, {"error"}):
        raise Broken(f"neither a collection nor an error: {line[:80]!r}")
    return out


def kind(v):
    return "null" if v is None else type(v).__name__


def diff(a, b, ptr, out):
    """Every difference, exactly: types (int is not float), keys and their order, values."""
    if kind(a) != kind(b):
        out.append(f"{ptr}: {kind(a)} {a!r} (python) != {kind(b)} {b!r} (rust)")
    elif isinstance(a, dict):
        if list(a) != list(b):
            out.append(f"{ptr}: keys {list(a)} (python) != {list(b)} (rust)")
        for k in a:
            if k in b:
                diff(a[k], b[k], f"{ptr}/{k}", out)
    elif isinstance(a, list):
        if len(a) != len(b):
            out.append(f"{ptr}: length {len(a)} (python) != {len(b)} (rust)")
        for i, (x, y) in enumerate(zip(a, b)):
            diff(x, y, f"{ptr}/{i}", out)
    elif a != b:
        out.append(f"{ptr}: {a!r} (python) != {b!r} (rust)")


def states(a):
    if "error" in a:
        return "failed"
    c = a["stats"]["collectors"]
    out = " ".join(f"{g}={s['state']}" + (f"({s['reason']})" if s["reason"] else "")
                   for g, s in c.items() if s["state"] != "ok") or "all ok"
    issues = sorted({f"{g}:{i['reason']}" for g, s in c.items() for i in s["issues"]})
    return out + (f" issues: {' '.join(issues)}" if issues else "")


def compare(steps, py_cmd, rs_cmd, timeout=TIMEOUT):
    """Runs the steps on both sides: (differences, report lines). A step passes only when both sides gave
    what it expects (a collection, or a failed collection) and, for collections, the same one; and the run
    only when both sides then exit with 0. A side that breaks ends the run."""
    logs = {n: tempfile.TemporaryFile("w+") for n in ("python", "rust")}
    procs = {"python": start(py_cmd, logs["python"]), "rust": start(rs_cmd, logs["rust"])}
    report, problems, got = [], 0, {OK: 0, FAILS: 0}
    for i, (label, change, base, expect) in enumerate(steps):
        if change:
            change()
        wall = 1_700_000_000_000_000 + base // 1000
        out, res = [], {}
        for n, p in procs.items():
            try:
                res[n] = step(p, base, wall, timeout)
            except Broken as e:
                out.append(f"{n}: {e}")
        for n, r in res.items():
            if ("error" in r) != (expect == FAILS):
                out.append(f"{n}: expected {expect}, got " + (f"an error: {r['error']!r}" if "error" in r else "a collection"))
        if not out and expect == OK:
            diff(res["python"], res["rust"], "", out)
        if not out:
            got[expect] += 1
        shown = states(res["python"]) if "python" in res else "no answer"
        report.append(f"  {i + 1}. {label}: {'OK' if not out else f'{len(out)} DIFFERENCES'} [{shown}]")
        report += [f"       {d}" for d in out[:40]]
        problems += len(out)
        if len(res) < 2:
            report.append("       stopped: a side did not answer")
            break
    for n, p in procs.items():
        p.stdin.close()
        try:
            code = p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
            code = f"{p.wait()} (killed: no exit 10 s after its input closed)"
        if code != 0:
            report.append(f"  {n} exited with {code}")
            problems += 1
        if code != 0 or problems:
            logs[n].seek(0)
            tail = logs[n].read().splitlines()[-5:]
            report += [f"       {n} stderr: {t}" for t in tail]
        logs[n].close()
    want = {e: sum(s[3] == e for s in steps) for e in got}
    report.append(f"  {got[OK]}/{want[OK]} collections equal, {got[FAILS]}/{want[FAILS]} expected failures on both sides")
    return problems, report


def run(name, binary):
    tree, steps = SCENARIOS[name]
    steps = steps()
    clear()
    tree()
    problems, report = compare(steps, ["python3", f"{HERE}/fixture_py.py"], [binary, "script"])
    print(f"{name}: {'OK' if not problems else 'FAILED'}")
    print("\n".join(report))
    return problems


# --- the harness against itself ------------------------------------------------------------------------

FAKE = """import json, sys, time
kind, n = sys.argv[1], 0
for line in sys.stdin:
    n += 1
    if kind == "hang":
        time.sleep(60)
    if kind == "exit_now" or kind == "exit_after_one" and n == 2:
        sys.exit(0)
    if kind == "garbage":
        print("not json", flush=True)
        continue
    error = kind.startswith("error")
    out = {"error": "control"} if error else {"stats": {"collectors": {}, "x": 2 if kind == "other" else 1},
                                               "sensors": {}, "required": {}}
    print(json.dumps(out), flush=True)
sys.exit(17 if kind.endswith("exit17") else 0)
"""

CONTROLS = [  # name, python side, rust side, what the steps expect, whether the harness must pass it
    ("equal collections", "ok", "ok", [OK, OK], True),
    ("expected failures on both sides", "error", "error", [FAILS, FAILS], True),
    ("both fail where a collection is expected", "error", "error", [OK, OK], False),
    ("both fail as expected, then exit 17", "error_exit17", "error_exit17", [FAILS, FAILS], False),
    ("both collect where a failure is expected", "ok", "ok", [OK, FAILS], False),
    ("python exits 17 after equal collections", "ok_exit17", "ok", [OK, OK], False),
    ("rust's collection differs", "ok", "other", [OK, OK], False),
    ("rust exits after one collection", "ok", "exit_after_one", [OK, OK], False),
    ("rust exits where a failure is expected", "error", "exit_now", [FAILS], False),
    ("rust does not answer", "ok", "hang", [OK, OK], False),
    ("rust writes something that is not JSON", "ok", "garbage", [OK, OK], False),
    ("rust writes something that is not JSON where a failure is expected", "error", "garbage", [FAILS], False),
]


def self_test():
    """Each control must pass or fail as listed; the fake sides read no tree. True when all do."""
    wrong, report_lines = 0, []
    for name, a, b, expects, passes in CONTROLS:
        steps = [(f"step {i + 1}", None, (i + 1) * S, e) for i, e in enumerate(expects)]
        problems, report = compare(steps, ["python3", "-c", FAKE, a], ["python3", "-c", FAKE, b], timeout=2)
        if (problems == 0) != passes:
            wrong += 1
            report_lines += [f"  {name}: {'failed' if passes else 'passed'}, must {'pass' if passes else 'fail'}"] + report
    print(f"harness self-test: {len(CONTROLS) - wrong}/{len(CONTROLS)} controls as expected")
    if wrong:
        print("\n".join(report_lines))
    return not wrong


def main():
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(0 if self_test() else 1)
    if not self_test():
        sys.exit(1)
    binary, names = sys.argv[1], sys.argv[2:] or list(SCENARIOS)
    failed = sum(run(n, binary) > 0 for n in names)
    print(f"{len(names) - failed}/{len(names)} scenarios equal")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
