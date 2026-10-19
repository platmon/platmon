"""The A B B A results of service_ab.py, per host: each window's numbers for A (Python) and B (Rust), the
two runs of each and their mean, B against A, and the rules README's 3C section set for B.

  python3 ab_report.py OUT.jsonl [OUT.jsonl ...]"""
import json
import sys


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
    ("reconnect peak VmRSS MiB", lambda r: r["reconnect"]["peak_rss_kib"] / 1024),
    ("full buffers RSS MiB", lambda r: r["points"]["filled"]["rollup_kib"]["Rss"] / 1024),
    ("full buffers PSS MiB", lambda r: r["points"]["filled"]["rollup_kib"]["Pss"] / 1024),
    ("end PSS MiB", lambda r: r["points"]["after"]["rollup_kib"]["Pss"] / 1024),
    ("w2 /api/stats p95 ms", lambda r: r["w2"]["response_ms"]["p95"]),
    ("w3 replay page p95 ms", lambda r: r["w3"]["response_ms"]["replay"]["p95"]),
    ("w2 collection p95 ms", lambda r: r["w2"]["collection_ms"]["p95"]),
]


def rules(r, a_runs):
    """README's 3C rules for B; also applied to A, so a broken run of either shows."""
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
    if r["variant"] == "B" and a_runs:
        for w, path in (("w1", ("response_ms", "p95")), ("w2", ("response_ms", "p95"))):
            a = mean([x[w][path[0]][path[1]] for x in a_runs])
            if a is not None and r[w][path[0]][path[1]] > a + 1:
                out.append(f"{w}: /api/stats p95 {r[w][path[0]][path[1]]} ms, A {a:.3f} ms")
        a = mean([x["w3"]["response_ms"]["replay"]["p95"] for x in a_runs])
        if a is not None and r["w3"]["response_ms"]["replay"]["p95"] > a + 1:
            out.append(f"w3: replay p95 {r['w3']['response_ms']['replay']['p95']} ms, A {a:.3f} ms")
    return out


def report(path):
    lines = [json.loads(line) for line in open(path)]
    setup = next(x["setup"] for x in lines if "setup" in x)
    runs = [x for x in lines if "variant" in x]
    print(f"## {setup['host']} ({setup['machine']}, {setup['cpus']} CPUs, python {setup['python']}, "
          f"governor {setup['governor']}) {path}")
    failed = [r for r in runs if "failed" in r]
    for r in failed:
        print(f"  run {r['variant']} failed: {r['failed']}")
    runs = [r for r in runs if "failed" not in r]
    a, b = [r for r in runs if r["variant"] == "A"], [r for r in runs if r["variant"] == "B"]
    print(f"  {'':30} {'A runs':>17} {'A':>8} {'B runs':>17} {'B':>8} {'B vs A':>8}")
    for name, get in ROWS:
        av, bv = [get(r) for r in a], [get(r) for r in b]
        ma, mb = mean(av), mean(bv)
        print(f"  {name:30} {' / '.join(f(x) for x in av):>17} {f(ma):>8} {' / '.join(f(x) for x in bv):>17} "
              f"{f(mb):>8} {f(change(ma, mb), 1) + ' %':>8}")
    r0 = (a or b)[0]
    rc = r0["reconnect"]
    print(f"  backlog {rc['backlog']}, {rc['records']} records and {rc['bytes']} bytes over {len(rc['rounds'])} rounds")
    for r in runs:
        problems = rules(r, a)
        print(f"  {r['variant']} rules: {'ok' if not problems else problems}")
    return runs


if __name__ == "__main__":
    for p in sys.argv[1:]:
        report(p)
