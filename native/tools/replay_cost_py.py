"""The Python side of `platmon-native replay-cost`: the same stages for the same core publications, with the
Python service's runtime/replay.py and the server's encoding (frontends/server.py: json.dumps with
separators=(",", ":")). Per record, in µs, the median of 7 rounds of PAGES pages.

  python3 replay_cost_py.py FILE PYTHON_TREE [PAGES [OUT]]     # OUT gets the page itself"""
import json
import sys
import time


def main():
    path, tree = sys.argv[1], sys.argv[2]
    pages = int(sys.argv[3]) if len(sys.argv) > 3 else 20
    sys.path.insert(0, tree)
    from runtime.replay import Replay

    with open(path) as f:
        data = [json.loads(line) for line in f if line.strip()]
    n, now = len(data), (len(data) + 1) * 1_000_000_000
    r = Replay("cost", lambda: now, {"source": "monotonic", "suspend_aware": False}, retention=1e6, max_bytes=1 << 30)
    for i, d in enumerate(data):
        t = (i + 1) * 1_000_000_000
        r.clock = lambda t=t: t
        r.append("core", i + 1, t, t, d)
    r.clock = lambda: now

    def page():
        return json.dumps(r.view("core", None, 0, n), separators=(",", ":")).encode()

    got = len(json.loads(page())["records"])
    if got != n:
        sys.exit(f"one page holds {got} of the {n} records")
    if len(sys.argv) > 4:
        with open(sys.argv[4], "wb") as f:
            f.write(page())
    payloads = [json.dumps(d, separators=(",", ":"), allow_nan=False).encode() for d in data]

    def median(work):
        rounds = []
        for _ in range(7):
            t = time.perf_counter()
            for _ in range(pages):
                work()
            rounds.append((time.perf_counter() - t) * 1e6 / (pages * n))
        return sorted(rounds)[3]

    decode = median(lambda: [json.loads(p) for p in payloads])
    both = median(lambda: [json.dumps(json.loads(p), separators=(",", ":")) for p in payloads])
    whole = median(page)
    print(json.dumps({"records": n, "payload_bytes": sum(map(len, payloads)) // max(n, 1), "pages": pages,
                      "decode_us": round(decode, 3), "write_us": round(both - decode, 3), "page_us": round(whole, 3)}))


if __name__ == "__main__":
    main()
