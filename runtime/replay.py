"""Bounded full-publication replay, independent of live freshness and numeric history."""
import collections
import json
import threading

MAX_RECORD_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_RECORDS = 4096
MAX_LIMIT = 100


class CursorError(ValueError):
    """A cursor belongs to another process or is ahead of this publication stream."""


class Replay:
    def __init__(self, instance_id, clock, clock_info, groups=(), retention=600.0, max_bytes=16 * 1024 * 1024):
        self.instance_id, self.clock, self.clock_info = instance_id, clock, dict(clock_info)
        self.retention, self.max_bytes = retention, max_bytes
        self._retention_ns = round(retention * 1e9)
        self._lock = threading.Lock()
        self._records = collections.OrderedDict()
        self._latest = dict.fromkeys(("core", *groups), 0)
        self._bytes = 0

    def append(self, stream, publication_id, started, completed, data, gap_before=False):
        # Freeze ownership and bound encoded payloads. Serialization never holds the reader lock.
        try:
            payload = json.dumps(data, separators=(",", ":"), allow_nan=False).encode()
        except (TypeError, ValueError):
            with self._lock:
                if stream in self._latest:
                    self._latest[stream] = max(self._latest[stream], publication_id)
            raise
        with self._lock:
            if stream not in self._latest or publication_id <= self._latest[stream]:
                return
            self._latest[stream] = publication_id
            self._prune(self.clock())
            if len(payload) > min(MAX_RECORD_BYTES, self.max_bytes):
                return  # latest still advances: clients see an explicit missing range
            self._records[stream, publication_id] = (started, completed, payload, gap_before)
            self._bytes += len(payload)
            while self._bytes > self.max_bytes or len(self._records) > MAX_RECORDS:
                self._remove(next(iter(self._records)))

    def _remove(self, key):
        self._bytes -= len(self._records.pop(key)[2])

    def _prune(self, now):
        # Publishers are independent: insertion order need not be timestamp order.
        cutoff = now - self._retention_ns
        for key, record in list(self._records.items()):
            if record[0] < cutoff:
                self._remove(key)

    def view(self, stream, instance_id=None, after=0, limit=100):
        if not isinstance(after, int) or isinstance(after, bool) or after < 0 or not 1 <= limit <= MAX_LIMIT:
            raise ValueError("invalid cursor or limit")
        with self._lock:
            if stream not in self._latest:
                raise KeyError(stream)
            now = self.clock()
            self._prune(now)
            latest = self._latest[stream]
            if instance_id is not None and instance_id != self.instance_id:
                raise CursorError("instance_changed")
            if after > latest:
                raise CursorError("cursor_ahead")
            available = [(pid, record) for (name, pid), record in self._records.items() if name == stream]
            oldest = available[0][0] if available else None
            selected, size = [], 4096  # reserve envelope and per-record timing/cursor overhead
            pending = [(pid, record) for pid, record in available if pid > after]
            for pid, record in pending:
                if len(selected) >= limit or size + len(record[2]) + 1024 > MAX_RESPONSE_BYTES:
                    break
                selected.append((pid, record))
                size += len(record[2]) + 1024
            more = len(selected) < len(pending)
            cursor = selected[-1][0] if more else latest
        missing, expected = [], after + 1
        records = []
        for pid, (started, completed, payload, gap) in selected:
            if pid > expected:
                missing.append({"from": expected, "to": pid - 1, "reason": "buffer_unavailable"})
            expected = pid + 1
            data = json.loads(payload)
            timing = data["sample"] if stream == "core" else data["observation"]
            elapsed_ms = round((now - completed) / 1e6, 3)
            for key in ("age_ms", "cycle_age_ms", "data_age_ms"):
                if key in timing:
                    timing[key] = round(timing[key] + elapsed_ms, 3)
            stale = timing["data_age_ms"] > (timing["stale_after_ms"] if stream == "core" else data["stale_after_ms"])
            timing["stale"] = stale
            if stream != "core":
                data["state"] = "stale" if stale else data["state"]
                data["collecting_for_ms"] = None
            records.append({"id": pid, "gap_before": gap, "data": data})
        if expected <= cursor:
            missing.append({"from": expected, "to": cursor, "reason": "buffer_unavailable"})
        return {"schema_version": 1, "instance_id": self.instance_id, "clock": dict(self.clock_info),
                "stream": stream, "data_schema_version": 1 if stream == "core" else 2,
                "historical": True, "retention_s": self.retention,
                "max_bytes": self.max_bytes, "max_records": MAX_RECORDS, "max_record_bytes": MAX_RECORD_BYTES,
                "max_response_bytes": MAX_RESPONSE_BYTES, "oldest_id": oldest, "latest_id": latest,
                "next_after": cursor, "has_more": more, "missing_ranges": missing, "records": records}
