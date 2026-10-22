"""A replay cursor walk checked page by page against docs/reference/replay.md, shared by live_http.py and
service_ab.py. A walk only passes when it got where it was going:

- every page: the same instance_id and stream as the first one; ids rising strictly and above the `after`
  asked for; ids and missing ranges disjoint and, together, exactly the ids from `after` + 1 to the page's
  `next_after` (the page explains every id it moves the cursor over)
- `has_more` true: `next_after` moved past `after`; `has_more` false: `next_after` is the page's `latest_id`
- done when `has_more` is false (or, with a target, when the cursor reached it); running out of pages or
  time is a failure, never a pass

  python3 walk.py      # the self-test: synthetic pages a correct walk must pass or must refuse"""
import sys


class Walk:
    def __init__(self, stream, instance=None, after=0, target=None):
        self.stream, self.instance, self.after, self.target = stream, instance, after, target
        self.ids, self.missing, self.pages, self.problems, self.done = [], [], 0, [], False

    def page(self, body):
        """Checks the answer to `?after=self.after` and moves the cursor; True when another page is needed."""
        p, after, self.pages = [], self.after, self.pages + 1
        if self.instance is None:
            self.instance = body["instance_id"]
        elif body["instance_id"] != self.instance:
            p.append(f"instance_id {body['instance_id']} after {self.instance}")
        if body["stream"] != self.stream:
            p.append(f"stream {body['stream']!r}, asked for {self.stream!r}")
        ids = [r["id"] for r in body["records"]]
        if any(b <= a for a, b in zip([after] + ids, ids)):
            p.append(f"ids {ids[:10]} not rising strictly after {after}")
        nxt, ranges = body["next_after"], [(m["from"], m["to"]) for m in body["missing_ranges"]]
        missing = [i for a, z in ranges for i in range(a, z + 1)]
        if any(a > z for a, z in ranges) or len(missing) != len(set(missing)) or set(missing) & set(ids):
            p.append(f"missing ranges {ranges} overlap each other or the ids")
        if sorted(set(ids) | set(missing)) != list(range(after + 1, nxt + 1)):
            p.append(f"ids and missing ranges do not explain {after + 1}..{nxt}")
        if body["has_more"] and nxt <= after:
            p.append(f"has_more with next_after {nxt} after {after}")
        if not body["has_more"] and nxt != body["latest_id"]:
            p.append(f"the last page's next_after {nxt} is not latest_id {body['latest_id']}")
        self.problems += [f"page {self.pages}: {x}" for x in p]
        self.ids += ids
        self.missing += ranges
        self.after = nxt
        self.done = not p and (not body["has_more"] or self.target is not None and nxt >= self.target)
        return not p and not self.done

    def result(self, why_not_done="the walk stopped"):
        if not self.done and not self.problems:
            self.problems.append(f"{why_not_done} at cursor {self.after} after {self.pages} pages")
        return {"stream": self.stream, "pages": self.pages, "records": len(self.ids), "missing": self.missing,
                "next_after": self.after, "instance": self.instance, "done": self.done, "problems": self.problems}


def walk(get, stream, after=0, target=None, pages=50):
    """`get(after, instance)` answers a page; at most `pages` of them."""
    w = Walk(stream, None, after, target)
    while w.pages < pages and w.page(get(w.after, w.instance)):
        pass
    return w.result(f"no end after {pages} pages")


def fake(pages, stream="core"):
    """A server answering from a list of (records, missing ranges, next_after, has_more, latest_id[, instance,
    stream]) for the pages in order; the last one is repeated."""
    n = [0]

    def get(after, instance):
        recs, missing, nxt, more, latest, *rest = pages[min(n[0], len(pages) - 1)]
        n[0] += 1
        return {"instance_id": rest[0] if rest else "a", "stream": rest[1] if len(rest) > 1 else stream,
                "records": [{"id": i} for i in recs], "missing_ranges": [{"from": a, "to": z} for a, z in missing],
                "next_after": nxt, "has_more": more, "latest_id": latest}
    return get


CASES = [  # name, pages, whether a correct walk passes[, a target]
    ("one page", [([1, 2, 3], [], 3, False, 3)], True),
    ("two pages", [([1, 2], [], 2, True, 4), ([3, 4], [], 4, False, 4)], True),
    ("expired start", [([3, 4], [(1, 2)], 4, False, 4)], True),
    ("everything expired", [([], [(1, 5)], 5, False, 5)], True),
    ("nothing yet", [([], [], 0, False, 0)], True),
    ("the target reached with more to come", [([1, 2], [], 2, True, 4)], True, 2),
    ("ids going back", [([2, 1], [], 2, False, 2)], False),
    ("an unexplained gap", [([1, 3], [], 3, False, 3)], False),
    ("a duplicate id", [([1, 1, 2], [], 2, False, 2)], False),
    ("no progress with has_more", [([], [], 0, True, 10)], False),
    ("a page without progress, then the rest", [([], [], 0, True, 2), ([1, 2], [], 2, False, 2)], False),
    ("more pages than the walk allows", [([i], [], i, True, 1000) for i in range(1, 61)], False),
    ("stopping short of latest", [([1], [], 1, False, 10)], False),
    ("another instance", [([1], [], 1, True, 2), ([2], [], 2, False, 2, "b")], False),
    ("an id also missing", [([1, 2], [(1, 1)], 2, False, 2)], False),
    ("another stream", [([1], [], 1, False, 1, "a", "storage")], False),
    ("the cursor past what was explained", [([1, 2], [], 4, False, 4)], False),
]


def self_test():
    wrong = [f"  {name}: {'failed' if passes else 'passed'}, must {'pass' if passes else 'fail'}: {r['problems']}"
             for name, pages, passes, *target in CASES
             if (not (r := walk(fake(pages), "core", 0, *target))["problems"]) != passes]
    print(f"replay walk self-test: {len(CASES) - len(wrong)}/{len(CASES)} cases as expected", file=sys.stderr)
    for w in wrong:
        print(w, file=sys.stderr)
    return not wrong


if __name__ == "__main__":
    sys.exit(0 if self_test() else 1)
