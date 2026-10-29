"""The A B B A results of service_ab.py, per host: each window's numbers for the first variant of the order
(the baseline: A, Python, in 3C; S, the system allocator, against M, mimalloc) and the other, the two runs of
each and their mean, the other against the baseline, and the rules README set for it (3C; System against
mimalloc: native/README.md as of b96aba8; the final comparison: README, "Final comparison (candidate 1)").

Each run is first checked for completeness (complete()): a run that did not do its load cannot pass, it
is invalid. The self-test runs first: synthetic runs the judgement must pass, refuse as invalid, or find
in violation.

With --pss-budget (the final comparison, README "Support scope"), the candidate's runs are judged against
an absolute PSS budget, each run on its own, instead of the 25 % rule against the baseline; a run that did
not hold or fetch the same data as the baseline's, or lacks the peak's readings, is invalid for it.

  python3 ab_report.py OUT.jsonl [OUT.jsonl ...]     # no file: the self-test only
  python3 ab_report.py --pss-budget FULL,PEAK,SETTLED OUT.jsonl     # MiB, for the host of OUT.jsonl"""
import copy
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # native/tools: walk.py, worker.py
import worker  # noqa: E402


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
            if "collected" in cc:
                if cc["collected"]["collections"] < cc["span_s"] - 4:
                    invalid.append(f"concurrent: {cc['collected']['collections']} collections over {cc['span_s']} s")
            else:  # runs of 2026-10-20: only the pollers' view, which stops at the ids they started for
                unverified.append("the collections during the rounds not recorded; the core sequence's advance over "
                                  "them and the after walk's losses stand for them")
                advance = read(lambda r: r["points"]["concurrent"]["replay"]["core"][1] -
                               r["points"]["reconnected"]["replay"]["core"][1], r)
                if advance is None or advance < cc["span_s"] - 4:
                    invalid.append(f"concurrent: the core sequence advanced {advance} over {cc['span_s']} s")
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


def slower(r, a_runs):
    """The windows whose response p95 is more than 1 ms above the baseline's mean."""
    out, base = [], a_runs[0]["variant"]
    for name, get in P95:
        a = mean([read(get, x) for x in a_runs])
        if a is not None and (read(get, r) is None or read(get, r) > a + 1):
            out.append(f"{name} {read(get, r)} ms, {base} {a:.3f} ms")
    return out


def rules(r, a_runs, final=False):
    """The 3C rules (native/README.md as of b96aba8) for B; also applied to A, so a broken run of either shows. A rule can only pass on a
    complete run (complete()). final (the final comparison, judge()): the absolute PSS budget instead of the
    25 % memory rule, the release CPU target, and response p95 above the baseline's reported as performance
    exceptions, not broken rules."""
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
        c = cc.get("collected", cc["cadence"])
        if cc["errors"] or cc["losses"] or cc["problems"] or c["missing_ids"] or c["gap_before"] or c["max_step_s"] > 1.5:
            out.append(f"concurrent: errors {cc['errors']} losses {cc['losses'][:3]} problems {cc['problems'][:3]} cadence {c}")
        before, settled = r["points"]["disconnected"], r["points"]["settled"]
        for k in ("threads", "fds"):
            if k in before and settled[k] > before[k]:
                out.append(f"settled: {settled[k]} {k}, {before[k]} before the reconnect")
    base = a_runs[0]["variant"] if a_runs else None
    if a_runs and r["variant"] != base and not final:  # the same window's p95, at most 1 ms above the baseline's mean
        out += slower(r, a_runs)
    if a_runs and r["variant"] != base and final:  # the release target: at most 2.5 % of one core in w1 and w2
        for w in ("w1", "w2"):
            if r[w]["cpu_pct"] > 2.5:
                out.append(f"{w}: {r[w]['cpu_pct']} % CPU, the release target 2.5 %")
    if a_runs and r["variant"] != base:
        if cc is not None and not final:  # memory: at most 25 % above the baseline's
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


def same_data(r, a_runs):
    """Why the run did not hold or fetch what the baseline's runs did (empty if it did): a budget is judged only
    on the same data. Replay records per stream and history points at most one reading's worth fewer (a stream
    can be one record short at the moment of reading, history one point per series); the same series; the
    concurrent backlog per stream at most one fewer."""
    out = []
    held = lambda x: {s: v[1] - v[0] + 1 for s, v in x["points"]["filled"].get("replay", {}).items()}
    least = {s: min(held(a).get(s, 0) for a in a_runs) for s in held(a_runs[0])}
    for s, n in least.items():
        if held(r).get(s, 0) < n - 1:
            out.append(f"full buffers: {held(r).get(s, 0)} {s} records, {n} in the baseline's runs")
    hist = [a["points"]["filled"].get("history") for a in a_runs]
    mine = r["points"]["filled"].get("history")
    if None in hist or mine is None:
        out.append("full buffers: no history count")
    elif mine["series"] != hist[0]["series"] or mine["points"] < min(h["points"] for h in hist) - mine["series"]:
        out.append(f"full buffers: history {mine}, the baseline's {hist[0]}")
    for s, n in a_runs[0]["concurrent"]["backlog"].items():
        if r["concurrent"]["backlog"].get(s, 0) < min(a["concurrent"]["backlog"].get(s, 0) for a in a_runs) - 1:
            out.append(f"concurrent: backlog {r['concurrent']['backlog']}, the baseline's {a_runs[0]['concurrent']['backlog']}")
            break
    return out


def budget_check(r, a_runs, budget):
    """(invalid, violations) of the candidate's absolute PSS budget (KiB: full buffers, concurrent peak,
    settled), each run on its own. Judged only with the same data as the baseline's runs and with the peak's
    readings there (the one before round 1, at least 5 samples a second on average, one after each round)."""
    invalid = same_data(r, a_runs)
    cc = r["concurrent"]
    if "start_pss_kib" not in cc:
        invalid.append("concurrent: no reading before round 1")
    if cc["samples"] < 5 * cc["span_s"] or any("pss_kib" not in x for x in cc["rounds"]):
        invalid.append(f"concurrent: {cc['samples']} PSS samples over {cc['span_s']} s and the rounds' readings")
    if invalid:
        return invalid, []
    out = []
    for (name, get), limit in zip(MEMORY, budget):
        if read(get, r) > limit:
            out.append(f"{name} {read(get, r)} KiB, budget {limit} KiB")
    return [], out


def judge(r, a_runs, budget=None):
    """"ok", or what makes the run invalid (it cannot be judged), or the rules it broke; and what is unverified.
    budget (KiB, as MEMORY): the final comparison (rules(final=True)) with the candidate's absolute PSS budget."""
    invalid, unverified = complete(r)
    if invalid:
        return "invalid", invalid, unverified
    broken = rules(r, a_runs, final=budget is not None)
    if budget is not None and a_runs and r["variant"] != a_runs[0]["variant"] and r.get("concurrent"):
        invalid, over = budget_check(r, a_runs, budget)
        if invalid:
            return "invalid", invalid, unverified
        broken += over
    return ("violations", broken, unverified) if broken else ("ok", [], unverified)


def report(path, budget=None):
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
    if budget:
        print(f"  PSS budget (KiB): full buffers {budget[0]}, concurrent peak {budget[1]}, settled {budget[2]}, each run")
    if "concurrent" in r0:
        cc = r0["concurrent"]
        print(f"  concurrent backlog {cc['backlog']}, {cc['records']} records and {cc['bytes']} bytes over "
              f"{len(cc['rounds'])} rounds of {r0['phases']['concurrent_pollers']} pollers")
    verdicts = []
    for r in every:
        verdict, why, unverified = judge(r, a, budget)
        verdicts.append(verdict)
        print(f"  {r['variant']} {verdict}{': ' + '; '.join(why) if why else ''}")
        for u in unverified:
            print(f"    unverified: {u}")
        if budget and a and r["variant"] != a[0]["variant"] and verdict != "invalid":
            for x in slower(r, a):
                print(f"    performance exception: {x}")
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
    r["concurrent"] = {"backlog": {"core": 600}, "rounds": [{"seconds": 1.0, "cpu_s": 0.5, "done": True, "pss_kib": 10000,
                                                             "workers": [state] * 4}] * 3, "start_pss_kib": 9000,
                       "done": True, "span_s": 9.0, "records": 7300, "errors": 0, "losses": [], "problems": [],
                       "response_ms": {"stats": sp, "replay": sp}, "peak_rss_kib": 12000, "peak_pss_kib": 11000, "samples": 180,
                       "cadence": {"collections": 5, "missing_ids": 0, "gap_before": 0, "max_step_s": 1.01},
                       "collected": {"collections": 10, "missing_ids": 0, "gap_before": 0, "max_step_s": 1.01}}
    r["points"]["reconnected"] = {"rollup_kib": {"Rss": 10000, "Pss": 8000}, "replay": {"core": [1450, 2050]}}
    r["points"]["filled"] = {"rollup_kib": {"Rss": 10000, "Pss": 8000}, "replay": {"core": [601, 1200], "wifi": [1, 120]},
                             "history": {"series": 20, "points": 4700}}
    r["points"]["disconnected"] = {"rollup_kib": {"Rss": 10000, "Pss": 8000}, "threads": 5, "fds": 7}
    r["points"]["concurrent"] = {"rollup_kib": {"Rss": 11000, "Pss": 9000}, "threads": 5, "fds": 7,
                                 "replay": {"core": [1460, 2060]}}
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
    ("M, collection stopped during the rounds", case("M", ("concurrent.collected.collections", 2), concurrent=True), "invalid"),
    ("M, a 3 s collection step during the rounds", case("M", ("concurrent.collected.max_step_s", 3.0), concurrent=True),
     "violations"),
    ("M, slow rounds: the pollers saw 5 collections over 13 s", case("M", ("concurrent.span_s", 13.0),
                                                                    ("concurrent.collected.collections", 13), concurrent=True), "ok"),
]
BEFORE_COLLECTED = [  # runs of 2026-10-20, without "collected": judged by the sequence's advance, unverified
    ("M, before collected, the sequence advanced", "ok", ()),
    ("M, before collected, the sequence stood still", "invalid", (("points.concurrent.replay", {"core": [1450, 2050]}),)),
]


BUDGET = [11000, 12000, 9000]  # KiB: full buffers, concurrent peak, settled
SELF_TEST_BUDGET = [  # against A, with the budget instead of the 25 % rule
    ("B within the budget, 30 % above A with full buffers", case("B", ("points.filled.rollup_kib", {"Rss": 13000, "Pss": 10400}),
                                                               concurrent=True), "ok"),
    ("B over the budget with full buffers", case("B", ("points.filled.rollup_kib", {"Rss": 13000, "Pss": 11500}), concurrent=True),
     "violations"),
    ("B's concurrent peak over the budget", case("B", ("concurrent.peak_pss_kib", 12500), concurrent=True), "violations"),
    ("B settled over the budget in this run", case("B", ("points.settled.rollup_kib", {"Rss": 11000, "Pss": 9100}), concurrent=True),
     "violations"),
    ("B holding 100 core records fewer", case("B", ("points.filled.replay", {"core": [701, 1200], "wifi": [1, 120]}), concurrent=True),
     "invalid"),
    ("B one Wi-Fi record short at the moment of reading", case("B", ("points.filled.replay", {"core": [601, 1200], "wifi": [2, 120]}),
                                                             concurrent=True), "ok"),
    ("B with a history series fewer", case("B", ("points.filled.history", {"series": 19, "points": 4465}), concurrent=True), "invalid"),
    ("B fetching a smaller concurrent backlog", case("B", ("concurrent.backlog", {"core": 300}), concurrent=True), "invalid"),
    ("B without the reading before round 1", {**case("B", concurrent=True),
                                              "concurrent": {k: v for k, v in case("B", concurrent=True)["concurrent"].items()
                                                             if k != "start_pss_kib"}}, "invalid"),
    ("B with 10 PSS samples over 9 s", case("B", ("concurrent.samples", 10), concurrent=True), "invalid"),
    ("A over the budget: the budget is the candidate's", case("A", ("concurrent.peak_pss_kib", 30000), concurrent=True), "ok"),
    ("B's concurrent page p95 5 ms above A: an exception, not a broken rule",
     case("B", ("concurrent.response_ms.replay", {"n": 300, "p50": 3, "p95": 11.0, "max": 20}), concurrent=True), "ok"),
    ("B over the release target in w2", case("B", ("w2.cpu_pct", 2.6), concurrent=True), "violations"),
    ("B with an HTTP error, under the budget", case("B", ("w1.errors", 1), concurrent=True), "violations"),
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
    for name, want, changes in BEFORE_COLLECTED:
        r = case("M", *changes, concurrent=True)
        del r["concurrent"]["collected"]
        got, why, unverified = judge(r, s)
        good = got == want and unverified != []
        passed += good
        print(f"ab_report {'ok  ' if good else 'FAIL'} {name}: {got} {why[:2]} {unverified}", file=sys.stderr)
    a_c = [synthetic("A", True)]
    for name, r, want in SELF_TEST_BUDGET:
        got, why, unverified = judge(copy.deepcopy(r), a_c, BUDGET)
        good = got == want and not unverified
        passed += good
        print(f"ab_report {'ok  ' if good else 'FAIL'} {name}: {got} {why[:2]}", file=sys.stderr)
    total = len(SELF_TEST) + len(SELF_TEST_M) + len(BEFORE_COLLECTED) + len(SELF_TEST_BUDGET)
    print(f"ab_report self-test: {passed}/{total} cases as expected", file=sys.stderr)
    return passed == total


if __name__ == "__main__":
    if not self_test():
        sys.exit("the self-test failed")
    args, budget = sys.argv[1:], None
    if args[:1] == ["--pss-budget"]:  # MiB: full buffers, concurrent peak, settled
        budget, args = [int(x) * 1024 for x in args[1].split(",")], args[2:]
    for p in args:
        report(p, budget)
