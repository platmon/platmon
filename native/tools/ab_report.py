"""The A B B A results of service_ab.py, per host: each window's numbers for the first variant of the order
(the baseline: A, Python, in 3C; S, the system allocator, against M, mimalloc) and the other, the two runs of
each and their mean, the other against the baseline, and the rules README set for it (3C; System against
mimalloc).

Each run is first checked for completeness (complete()): a run that did not do its load cannot pass, it
is invalid. The self-test runs first: synthetic runs the judgement must pass, refuse as invalid, or find
in violation.

  python3 ab_report.py OUT.jsonl [OUT.jsonl ...]     # no file: the self-test only"""
import copy
import json
import sys

import worker


def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def change(a, b):
    return None if not a or b is None else round(100 * (b - a) / a, 1)


def f(x, n=3):
    return "-" if x is None else f"{x:.{n}f}" if isinstance(x, float) else str(x)


ROWS = [  # name, how to read it from a run
    ("w1 CPU % (buffers filling)", lambda r: r["w1"]["cpu_pct"]),
    ("w2 CPU % (buffers full)", lambda r: r["w2"]["cpu_pct"]),
    ("w3 CPU % (Hub polling)", lambda r: r["w3"]["cpu_pct"]),
    ("idle CPU % (no client)", lambda r: r["idle"]["cpu_pct"]),
    ("reconnect s (per round)", lambda r: r["reconnect"]["seconds"]["p50"]),
    ("reconnect CPU s (per round)", lambda r: r["reconnect"]["cpu_s_each"]),
    ("reconnect round 1 s", lambda r: r["reconnect"]["rounds"][0]["seconds"]),
    ("reconnect round 1 CPU s", lambda r: r["reconnect"]["rounds"][0]["cpu_s"]),
    ("reconnect rounds 2+ s (mean)", lambda r: mean([x["seconds"] for x in r["reconnect"]["rounds"][1:]])),
    ("reconnect rounds 2+ CPU s (mean)", lambda r: mean([x["cpu_s"] for x in r["reconnect"]["rounds"][1:]])),
    ("reconnect peak VmRSS MiB", lambda r: r["reconnect"]["peak_rss_kib"] / 1024),
    ("full buffers RSS MiB", lambda r: r["points"]["filled"]["rollup_kib"]["Rss"] / 1024),
    ("full buffers PSS MiB", lambda r: r["points"]["filled"]["rollup_kib"]["Pss"] / 1024),
    ("end PSS MiB", lambda r: r["points"]["after"]["rollup_kib"]["Pss"] / 1024),
    ("w2 /api/stats p95 ms", lambda r: r["w2"]["response_ms"]["p95"]),
    ("w3 /api/stats p95 ms", lambda r: r["w3"]["response_ms"]["stats"]["p95"]),
    ("w3 replay page p95 ms", lambda r: r["w3"]["response_ms"]["replay"]["p95"]),
    ("w2 collection p95 ms", lambda r: r["w2"]["collection_ms"]["p95"]),
    ("concurrent s (per round)", lambda r: r["concurrent"]["seconds"]["p50"]),
    ("concurrent CPU s (per round)", lambda r: r["concurrent"]["cpu_s_each"]),
    ("concurrent page p95 ms", lambda r: r["concurrent"]["response_ms"]["replay"]["p95"]),
    ("concurrent peak VmRSS MiB", lambda r: r["concurrent"]["peak_rss_kib"] / 1024),
    ("concurrent peak PSS MiB", lambda r: r["concurrent"]["peak_pss_kib"] / 1024),
    ("settled PSS MiB", lambda r: r["points"]["settled"]["rollup_kib"]["Pss"] / 1024),
    ("settled threads", lambda r: r["points"]["settled"]["threads"]),
    ("settled open fds", lambda r: r["points"]["settled"]["fds"]),
]


def read(get, r):
    """A row's value, or None when the run has no such phase (3C runs have no concurrent phase)."""
    try:
        return get(r)
    except (KeyError, IndexError, TypeError):
        return None


POINTS = ("w1_end", "filled", "hub_end", "disconnected", "reconnected", "after")
LATER = ("concurrent", "settled")  # with a concurrent phase


def complete(r):
    """(invalid, unverified): whether the run did the load it was meant to. A thread that died, a window with
    too few answers or collections, a missing point: the run cannot be judged, whatever its numbers say.
    The counts follow from the phases: the budget client asks once a second; the poller polls every 5 s,
    observations every 10 s, core replay at least every 5 s; collection every 1 s; a window's last few
    collections are not replayed yet when it ends."""
    ph, invalid, unverified = r["phases"], [], []
    ww = ph["window"]
    if "failed" in r:
        return [f"the run failed: {r['failed']}"], []
    if "workers" not in r:
        unverified.append("thread states not recorded (before worker.py); the counts below stand for them")
    for name, state in r.get("workers", {}).items():
        invalid += worker.problems(name, state)
    for name in ("budget", "rss", "hub w3", "hub after"):
        if "workers" in r and name not in r["workers"]:
            invalid.append(f"{name}: no thread state")
    if r.get("rss_gone"):
        invalid.append(f"the memory sampler stopped at {r['rss_gone']}")
    for w in ("w1", "w2"):
        x = r[w]
        for what, n, least in (("requests", x["requests"], ww - 2), ("answers", x["response_ms"]["n"], ww - 2),
                               ("collections", x["collection_ms"]["n"], ww - 2),
                               ("publications", x["publications"], ww - 3), ("intervals", x["interval_s"]["n"], ww - 3)):
            if n < least:
                invalid.append(f"{w}: {n} {what}, at least {least}")
    x, k = r["w3"], r["w3"]["by_kind"]
    for what, n, least in (("stats requests", k["stats"], ww // 5 - 1), ("observations requests", k["observations"], ww // 10 - 1),
                           ("replay requests", k["replay"], ww // 5 - 1), ("stats answers", x["response_ms"]["stats"]["n"], ww // 5 - 1),
                           ("replay answers", x["response_ms"]["replay"]["n"], ww // 5 - 1),
                           ("replayed collections", x["cadence"]["collections"], ww - 7),
                           ("lag samples", x["core_lag_ms"]["n"], ww - 7)):
        if n < least:
            invalid.append(f"w3: {n} {what}, at least {least}")
    sup = r.get("hub", {}).get("supported", {})
    if sup.get("core") is not True or None in sup.values():
        invalid.append(f"hub: streams {sup}")
    rc = r["reconnect"]
    rounds = rc["rounds"]
    if len(rounds) != ph["repeats"] or not all(x["done"] for x in rounds):
        invalid.append(f"reconnect: {sum(x['done'] for x in rounds)} of {ph['repeats']} rounds done")
    backlog = sum(rc["backlog"].values())
    if rc["records"] < len(rounds) * backlog:
        invalid.append(f"reconnect: {rc['records']} records for {len(rounds)} rounds of a {backlog} record backlog")
    span = rc.get("span_s", sum(x["seconds"] for x in rounds) + 2 * len(rounds))
    if rc["cadence"]["collections"] < span - 4:
        invalid.append(f"reconnect: {rc['cadence']['collections']} replayed collections over {span:.1f} s")
    if r["after"]["cadence"]["collections"] < ph["after"] - 7:
        invalid.append(f"after: {r['after']['cadence']['collections']} replayed collections, at least {ph['after'] - 7}")
    if ph.get("concurrent_rounds"):
        cc = r.get("concurrent")
        if cc is None:
            invalid.append("no concurrent phase")
        else:
            crounds = cc["rounds"]
            if len(crounds) != ph["concurrent_rounds"] or not all(x["done"] for x in crounds):
                invalid.append(f"concurrent: {sum(x['done'] for x in crounds)} of {ph['concurrent_rounds']} rounds done")
            for i, x in enumerate(crounds):
                for j, state in enumerate(x["workers"]):
                    invalid += worker.problems(f"concurrent round {i + 1} poller {j + 1}", state)
            need = len(crounds) * ph["concurrent_pollers"] * sum(cc["backlog"].values())
            if cc["records"] < need:
                invalid.append(f"concurrent: {cc['records']} records, at least {need}")
            if cc["cadence"]["collections"] < cc["span_s"] - 4:
                invalid.append(f"concurrent: {cc['cadence']['collections']} replayed collections over {cc['span_s']} s")
            if not cc.get("samples"):
                invalid.append("concurrent: no memory sample during the rounds")
    for pt in POINTS + (LATER if ph.get("concurrent_rounds") else ()):
        if "Pss" not in r.get("points", {}).get(pt, {}).get("rollup_kib", {}):
            invalid.append(f"point {pt}: no smaps_rollup")
    trace = r.get("rss_trace", [])
    end = ph["w2_at"] + 2 * ww + ph["hub_warmup"] + ph["disconnect"] + ph["after"]
    step = max((b[0] - a[0] for a, b in zip(trace, trace[1:])), default=None)
    if not trace or step > 6 or trace[-1][0] < end - 10:
        invalid.append(f"rss trace: {len(trace)} samples, largest step {step} s, last at "
                       f"{trace[-1][0] if trace else None} s of {end}")
    return invalid, unverified


def rules(r, a_runs):
    """README's 3C rules for B; also applied to A, so a broken run of either shows. A rule can only pass on a
    complete run (complete())."""
    out = []
    for w in ("w1", "w2", "w3"):
        if r[w]["errors"]:
            out.append(f"{w}: {r[w]['errors']} errors")
    for w in ("w1", "w2"):
        if (r[w]["interval_s"]["max"] or 0) > 1.5:
            out.append(f"{w}: a cadence step of {r[w]['interval_s']['max']} s")
    for w in ("w3", "reconnect", "after"):
        c = r[w]["cadence"]
        if c["missing_ids"] or c["gap_before"] or c["max_step_s"] > 1.5:
            out.append(f"{w}: cadence {c}")
    rc = r["reconnect"]
    if rc["errors"] or rc["losses"] or rc["problems"] or not rc["done"]:
        out.append(f"reconnect: errors {rc['errors']} losses {rc['losses']} problems {rc['problems']} done {rc['done']}")
    if r["hub"]["losses"] or r["hub"]["problems"]:
        out.append(f"hub: losses {r['hub']['losses'][:3]} problems {r['hub']['problems'][:3]}")
    if r["exited_by_itself"] is not None:
        out.append(f"the service exited by itself: {r['exited_by_itself']}")
    cc = r.get("concurrent")
    if cc is not None:
        c = cc["cadence"]
        if cc["errors"] or cc["losses"] or cc["problems"] or c["missing_ids"] or c["gap_before"] or c["max_step_s"] > 1.5:
            out.append(f"concurrent: errors {cc['errors']} losses {cc['losses'][:3]} problems {cc['problems'][:3]} cadence {c}")
        before, settled = r["points"]["disconnected"], r["points"]["settled"]
        for k in ("threads", "fds"):
            if k in before and settled[k] > before[k]:
                out.append(f"settled: {settled[k]} {k}, {before[k]} before the reconnect")
    base = a_runs[0]["variant"] if a_runs else None
    if a_runs and r["variant"] != base:  # the same window's p95, at most 1 ms above the baseline's mean
        for name, get in P95:
            a = mean([read(get, x) for x in a_runs])
            if a is not None and (read(get, r) is None or read(get, r) > a + 1):
                out.append(f"{name} {read(get, r)} ms, {base} {a:.3f} ms")
        if cc is not None:  # memory: at most 25 % above the baseline's
            for name, get in MEMORY:
                a = mean([read(get, x) for x in a_runs])
                if a is not None and (read(get, r) is None or read(get, r) > 1.25 * a):
                    out.append(f"{name} {read(get, r)} KiB, {base} {a:.0f} KiB (more than 25 % above)")
    return out


P95 = [("w1 /api/stats p95", lambda r: r["w1"]["response_ms"]["p95"]),
       ("w2 /api/stats p95", lambda r: r["w2"]["response_ms"]["p95"]),
       ("w3 /api/stats p95", lambda r: r["w3"]["response_ms"]["stats"]["p95"]),
       ("w3 replay p95", lambda r: r["w3"]["response_ms"]["replay"]["p95"]),
       ("concurrent page p95", lambda r: r["concurrent"]["response_ms"]["replay"]["p95"])]
MEMORY = [("full buffers PSS", lambda r: r["points"]["filled"]["rollup_kib"]["Pss"]),
          ("concurrent peak PSS", lambda r: r["concurrent"]["peak_pss_kib"]),
          ("settled PSS", lambda r: r["points"]["settled"]["rollup_kib"]["Pss"])]


def judge(r, a_runs):
    """"ok", or what makes the run invalid (it cannot be judged), or the rules it broke; and what is unverified."""
    invalid, unverified = complete(r)
    if invalid:
        return "invalid", invalid, unverified
    broken = rules(r, a_runs)
    return ("violations", broken, unverified) if broken else ("ok", [], unverified)


def report(path):
    lines = [json.loads(line) for line in open(path)]
    setup = next(x["setup"] for x in lines if "setup" in x)
    runs = [x for x in lines if "variant" in x]
    print(f"## {setup['host']} ({setup['machine']}, {setup['cpus']} CPUs, python {setup['python']}, "
          f"governor {setup['governor']}) {path}")
    every = runs
    runs = [r for r in runs if "failed" not in r]
    order = list(dict.fromkeys(setup.get("order") or ["A", "B"]))
    base, other = order[0], order[1]
    a, b = [r for r in runs if r["variant"] == base], [r for r in runs if r["variant"] == other]
    print(f"  {'':32} {base + ' runs':>17} {base:>8} {other + ' runs':>17} {other:>8} {other + ' vs ' + base:>8}")
    for name, get in ROWS:
        av, bv = [read(get, r) for r in a], [read(get, r) for r in b]
        if not any(x is not None for x in av + bv):
            continue
        ma, mb = mean(av), mean(bv)
        print(f"  {name:32} {' / '.join(f(x) for x in av):>17} {f(ma):>8} {' / '.join(f(x) for x in bv):>17} "
              f"{f(mb):>8} {f(change(ma, mb), 1) + ' %':>8}")
    r0 = (a or b)[0]
    rc = r0["reconnect"]
    print(f"  backlog {rc['backlog']}, {rc['records']} records and {rc['bytes']} bytes over {len(rc['rounds'])} rounds")
    if "concurrent" in r0:
        cc = r0["concurrent"]
        print(f"  concurrent backlog {cc['backlog']}, {cc['records']} records and {cc['bytes']} bytes over "
              f"{len(cc['rounds'])} rounds of {r0['phases']['concurrent_pollers']} pollers")
    verdicts = []
    for r in every:
        verdict, why, unverified = judge(r, a)
        verdicts.append(verdict)
        print(f"  {r['variant']} {verdict}{': ' + '; '.join(why) if why else ''}")
        for u in unverified:
            print(f"    unverified: {u}")
    return runs, verdicts


def synthetic(variant, concurrent=False):
    """A complete, rule-abiding run of the full phases, built from the counts the phases imply; with the
    concurrent and settle phases if asked."""
    ph = {"window": 120, "w2_at": 1290, "hub_warmup": 30, "disconnect": 480, "after": 120, "repeats": 5}
    sp = lambda n, p95=2.0, mx=1.0: {"n": n, "p50": 1.0, "p95": p95, "max": mx}
    budget = lambda: {"cpu_pct": 1.0, "requests": 120, "errors": 0, "response_ms": sp(120), "publications": 119,
                      "interval_s": sp(119, 1.0, 1.01), "collection_ms": sp(120)}
    cadence = lambda n: {"collections": n, "missing_ids": 0, "gap_before": 0, "max_step_s": 1.01}
    pt = {"rollup_kib": {"Rss": 10000, "Pss": 8000}}
    return {"variant": variant, "phases": ph, "w1": budget(), "w2": budget(), "idle": {"cpu_pct": 0.5},
            "w3": {"cpu_pct": 1.0, "errors": 0, "by_kind": {"status": 86, "stats": 24, "observations": 12, "replay": 62},
                   "response_ms": {"stats": sp(24), "replay": sp(62, 4.0)}, "cadence": cadence(116), "core_lag_ms": sp(116)},
            "reconnect": {"errors": 0, "losses": [], "problems": [], "done": True, "backlog": {"core": 600},
                          "rounds": [{"seconds": 0.3, "cpu_s": 0.2, "done": True}] * 5, "records": 3010, "span_s": 11.5,
                          "cadence": cadence(10)},
            "after": {"cadence": cadence(115)}, "hub": {"losses": [], "problems": [], "supported": {"core": True, "probe": False}},
            "exited_by_itself": None, "rss_gone": None, "points": {p: pt for p in POINTS},
            "workers": {n: {"error": None, "ended": True, "alive": False} for n in ("budget", "rss", "hub w3", "hub after")},
            "rss_trace": [[t, 10000] for t in range(5, 2175, 5)]} if not concurrent else with_concurrent(synthetic(variant))


def with_concurrent(r):
    r["phases"].update(concurrent_rounds=3, concurrent_pollers=4, settle=120)
    sp = {"n": 300, "p50": 3.0, "p95": 6.0, "max": 9.0}
    state = {"error": None, "ended": True, "alive": False}
    r["concurrent"] = {"backlog": {"core": 600}, "rounds": [{"seconds": 1.0, "cpu_s": 0.5, "done": True,
                                                             "workers": [state] * 4}] * 3,
                       "done": True, "span_s": 9.0, "records": 7300, "errors": 0, "losses": [], "problems": [],
                       "response_ms": {"stats": sp, "replay": sp}, "peak_rss_kib": 12000, "peak_pss_kib": 11000, "samples": 180,
                       "cadence": {"collections": 9, "missing_ids": 0, "gap_before": 0, "max_step_s": 1.01}}
    r["points"]["disconnected"] = {"rollup_kib": {"Rss": 10000, "Pss": 8000}, "threads": 5, "fds": 7}
    r["points"]["concurrent"] = {"rollup_kib": {"Rss": 11000, "Pss": 9000}, "threads": 5, "fds": 7}
    r["points"]["settled"] = {"rollup_kib": {"Rss": 10000, "Pss": 8200}, "threads": 5, "fds": 7}
    return r


def case(variant, *changes, concurrent=False):
    r = synthetic(variant, concurrent)
    for path, value in changes:
        *head, last = path.split(".")
        node = r
        for k in head:
            node = node[k]
        node[last] = value
    return r


SELF_TEST = [  # name, run, verdict it must get
    ("a complete run", case("B"), "ok"),
    ("a poller that died after one page", case("B", ("workers.hub w3.error", "JSONDecodeError: ... (turn:195)"),
                                               ("w3.by_kind", {"status": 2, "stats": 1, "observations": 1, "replay": 1}),
                                               ("w3.response_ms.replay.n", 1), ("w3.cadence.collections", 1)), "invalid"),
    ("the same poller, before worker.py", {k: v for k, v in case("B", ("w3.by_kind", {"status": 2, "stats": 1, "observations": 1,
                                                                                   "replay": 1}),
                                                                  ("w3.response_ms.replay.n", 1), ("w3.cadence.collections", 1)).items()
                                           if k != "workers"}, "invalid"),
    ("a w2 with one answer", case("B", ("w2.requests", 1), ("w2.response_ms.n", 1), ("w2.collection_ms.n", 1),
                                  ("w2.publications", 0), ("w2.interval_s.n", 0)), "invalid"),
    ("no collection seen in the reconnect", case("B", ("reconnect.cadence.collections", 0)), "invalid"),
    ("w3 /api/stats p95 52 ms against 2", case("B", ("w3.response_ms.stats.p95", 52.0)), "violations"),
    ("HTTP errors", case("B", ("w1.errors", 3)), "violations"),
    ("missing ids", case("B", ("w3.cadence.missing_ids", 2)), "violations"),
    ("w2 /api/stats p95 regression", case("B", ("w2.response_ms.p95", 9.0)), "violations"),
    ("a reconnect round not done", case("B", ("reconnect.rounds", [{"seconds": 30, "cpu_s": 1, "done": False}] * 5)), "invalid"),
    ("fewer records than the rounds' backlog", case("B", ("reconnect.records", 600)), "invalid"),
    ("a memory sampler that stopped", case("B", ("rss_gone", [900.0, "KeyError"]), ("rss_trace", [[5, 1], [900, 1]])), "invalid"),
    ("a run that failed", {**case("B"), "failed": "RuntimeError: B not ready"}, "invalid"),
    ("a run from before worker.py", {k: v for k, v in case("B").items() if k not in ("workers", "rss_gone")}, "ok"),
]
SELF_TEST_M = [  # against S, with the concurrent and settle phases
    ("M, complete", case("M", concurrent=True), "ok"),
    ("M, a concurrent round not done", case("M", ("concurrent.rounds", [{"seconds": 30, "cpu_s": 1, "done": False,
                                                                        "workers": [{"error": None, "ended": True, "alive": False}] * 4}] * 3),
                                          concurrent=True), "invalid"),
    ("M, a concurrent poller that raised", case("M", ("concurrent.rounds", [{"seconds": 1, "cpu_s": 0.5, "done": True,
                                                                            "workers": [{"error": "KeyError: 'records'", "ended": True,
                                                                                         "alive": False}] * 4}] * 3),
                                               concurrent=True), "invalid"),
    ("M, too few concurrent records", case("M", ("concurrent.records", 600), concurrent=True), "invalid"),
    ("M, a concurrent loss", case("M", ("concurrent.losses", [[2050.0, "core", [{"from": 3, "to": 4}]]]), concurrent=True),
     "violations"),
    ("M, concurrent page p95 5 ms above S", case("M", ("concurrent.response_ms.replay", {"n": 300, "p50": 3, "p95": 11.0, "max": 20}),
                                                concurrent=True), "violations"),
    ("M, a thread left after settling", case("M", ("points.settled.threads", 6), concurrent=True), "violations"),
    ("M, a descriptor left after settling", case("M", ("points.settled.fds", 9), concurrent=True), "violations"),
    ("M, full-buffer PSS 30 % above S", case("M", ("points.filled.rollup_kib", {"Rss": 13000, "Pss": 10400}), concurrent=True),
     "violations"),
    ("M, concurrent peak PSS 20 % above S", case("M", ("concurrent.peak_pss_kib", 13200), concurrent=True), "ok"),
    ("M, settled PSS 30 % above S", case("M", ("points.settled.rollup_kib", {"Rss": 11000, "Pss": 10700}), concurrent=True),
     "violations"),
    ("no concurrent phase although set", {**case("M", concurrent=True), "concurrent": None}, "invalid"),
]


def self_test():
    a = [synthetic("A")]
    passed = 0
    for name, r, want in SELF_TEST:
        got, why, unverified = judge(copy.deepcopy(r), a)
        good = got == want and (unverified != []) == ("workers" not in r and "failed" not in r and got != "invalid" or
                                                     "workers" not in r and got == "invalid" and unverified != [])
        passed += good
        print(f"ab_report {'ok  ' if good else 'FAIL'} {name}: {got} {why[:2]} {unverified}", file=sys.stderr)
    s = [synthetic("S", True)]
    for name, r, want in SELF_TEST_M:
        got, why, unverified = judge(copy.deepcopy(r), s)
        good = got == want and not unverified
        passed += good
        print(f"ab_report {'ok  ' if good else 'FAIL'} {name}: {got} {why[:2]}", file=sys.stderr)
    total = len(SELF_TEST) + len(SELF_TEST_M)
    print(f"ab_report self-test: {passed}/{total} cases as expected", file=sys.stderr)
    return passed == total


if __name__ == "__main__":
    if not self_test():
        sys.exit("the self-test failed")
    for p in sys.argv[1:]:
        report(p)
