"""A thread whose end is part of the result. threading.Thread.join() waits for a thread but never re-raises
what it raised, so a load that died early would look like a quiet window. A Worker keeps what its target
raised (with where), what it returned, and whether it ended; finish() joins it and returns that state for
the result, and a caller judges a state with problems(). Shared by service_ab.py and live_http.py.

  python3 worker.py      # the self-test"""
import os
import sys
import threading
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))


class Worker(threading.Thread):
    def __init__(self, name, target, *args):
        super().__init__(name=name, daemon=True)
        self.target, self.args = target, args
        self.value = self.error = None
        self.ended = False

    def run(self):
        try:
            self.value = self.target(*self.args)
        except BaseException as e:  # everything, kept for the result: the thread ends either way
            frames = traceback.extract_tb(e.__traceback__)
            mine = [f for f in frames if os.path.abspath(f.filename).startswith(HERE + os.sep)]
            where = (mine or frames)[-1]  # the last line of these tools, not of the library it failed in
            self.error = f"{type(e).__name__}: {e}"[:200] + f" ({where.name}:{where.lineno})"
        finally:
            self.ended = True

    def finish(self, timeout=30):
        """Joins; {"error", "ended", "alive"} for the result. Alive means it did not stop when asked."""
        self.join(timeout)
        return {"error": self.error, "ended": self.ended, "alive": self.is_alive()}

    def result(self, timeout=30):
        """Joins and returns what the target returned; raises RuntimeError if it raised or is still running."""
        p = problems(self.name, self.finish(timeout))
        if p:
            raise RuntimeError("; ".join(p))
        return self.value


def problems(name, state):
    out = []
    if state.get("error"):
        out.append(f"{name} raised {state['error']}")
    if state.get("alive"):
        out.append(f"{name} still running after it was stopped")
    elif not state.get("ended"):
        out.append(f"{name} did not end")
    return out


def self_test():
    def ok():
        return 7

    def broken():
        return {}["sample"]

    cases = [("returns", ok, [], 7), ("raises", broken, ["raised KeyError"], None),
             ("never stops", lambda: threading.Event().wait(5), ["still running"], None)]
    passed = True
    for name, target, want, value in cases:
        w = Worker(name, target)
        w.start()
        got = problems(name, w.finish(0.5))
        good = len(got) == len(want) and all(x in g for x, g in zip(want, got)) and w.value == value
        if not want:
            try:
                good &= w.result() == value
            except RuntimeError:
                good = False
        else:
            try:
                w.result(0.1)
                good = False
            except RuntimeError:
                pass
        print(f"worker {'ok  ' if good else 'FAIL'} {name}: {got}", file=sys.stderr)
        passed &= good
    return passed


if __name__ == "__main__":
    sys.exit(0 if self_test() else 1)
