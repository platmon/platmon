"""Low-frequency observations: groups read on their own cadence (seconds to minutes) by one thread each,
apart from the core Sampler, and served by /api/observations with their own id, times, age and state.
A blocked read stalls only its own group (its age grows, collecting_for_ms says for how long); no new
thread is started for it. Contract: docs/reference/observations.md.
"""
import threading
import sys
import time

from collector import metadata

SCHEMA_VERSION = 2


class Slow:
    """One group: observe(group) returns its data (a new dict each time) and notes problems in group, a
    metadata.Group of the same name. Published observations are never changed afterwards."""

    def __init__(self, name, observe, interval, clock, wall=time.time):
        """interval: seconds between the starts of two observations; stale_after: 3 intervals.
        clock: the service's elapsed-ns clock (as the Sampler's)."""
        self.name, self._observe, self.interval = name, observe, interval
        self._interval_ns = round(interval * 1e9)
        self._stale_ns = 3 * self._interval_ns
        self._clock, self._wall = clock, wall
        self._lock = threading.Lock()
        self._last = None      # the published observation (see _run), replaced, never modified
        self._since = None     # elapsed ns the running observation started, None between them
        self._count = 0
        self._logged = {}
        self._stop = threading.Event()
        self._thread = None
        self.replay = None
        self.on_publish = None
        self._publish_failed = False

    def start(self):
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name=f"slow-{self.name}", daemon=True)
                self._thread.start()
        return self

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            started = self.observe_once()
            self._stop.wait(max(0.0, (started + self._interval_ns - self._clock()) / 1e9))  # no catch-up bursts

    def observe_once(self):
        """One observation, published as one update; returns the elapsed ns it started. Also for tests."""
        started_at, started = self._wall(), self._clock()
        with self._lock:
            self._since = started
        group, data = metadata.Group(self.name), None
        with metadata.guard(group):  # an unexpected exception is this group's internal_error, never the service's
            data = self._observe(group)
        completed = self._clock()
        collector = metadata.summarize({self.name: group}, self._logged)[self.name]
        with self._lock:
            self._count += 1
            self._last = {"id": self._count, "started": started, "completed": completed, "started_at": started_at,
                          "completed_at": self._wall(), "collector": collector, "data": data}
            self._since = None
            published = self._last
        if self.replay is not None:
            try:
                self.replay.append(self.name, published["id"], started, completed,
                                   self._entry(published, completed, None))
            except Exception as e:
                print(f"platmon: observation replay append failed: {e!r}", file=sys.stderr)
        if self.on_publish is not None:
            try:
                self.on_publish(self.name, self.interval, published)
            except Exception:
                if not self._publish_failed:
                    print("platmon: observation history append failed", file=sys.stderr)
                    self._publish_failed = True
        return started

    def view(self):
        """This group's /api/observations entry: a new dict whose data is the published observation's own
        (never changed after publishing, so it can be serialized outside the lock)."""
        with self._lock:
            now, last, since = self._clock(), self._last, self._since
        return self._entry(last, now, since)

    def _entry(self, last, now, since):
        """Format one publication with ages on the shared elapsed clock."""
        entry = {"interval_ms": ms(self._interval_ns), "stale_after_ms": ms(self._stale_ns),
                 "collecting_for_ms": None if since is None else ms(now - since), "observation": None,
                 "collector": None, "data": None}
        if last is None:
            entry["state"] = "starting" if since is None or now - since <= self._stale_ns else "stale"
            return entry
        stale = now - last["started"] > self._stale_ns
        entry.update(
            state="stale" if stale else last["collector"]["state"],
            observation={"id": last["id"], "started_at": last["started_at"], "completed_at": last["completed_at"],
                         "duration_ms": ms(last["completed"] - last["started"]), "age_ms": ms(now - last["completed"]),
                         "data_age_ms": ms(now - last["started"]), "stale": stale},
            collector=last["collector"], data=last["data"])
        return entry


def ms(ns):
    return round(ns / 1e6, 3)


class Observations:
    """The service's slow groups, for /api/observations and the /text view."""

    def __init__(self, groups, instance_id, clock_info):
        self.groups, self.instance_id, self.clock_info = groups, instance_id, clock_info

    def start(self):
        for g in self.groups:
            g.start()
        return self

    def view(self):
        return {"schema_version": SCHEMA_VERSION, "instance_id": self.instance_id, "clock": dict(self.clock_info),
                "groups": {g.name: g.view() for g in self.groups}}
