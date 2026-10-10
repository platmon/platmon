"""Recent history: the last few minutes of selected numbers from the published core snapshots, in memory
only (no file, no database), for trend graphs. One point per published snapshot, appended by the Sampler's
writer thread; reading never adds points. Contract: docs/history.md.
"""
import collections
import hashlib
import json
import math
import threading

from .sysfs import pointer

MAX_SERIES = 64
MAX_POINTS_PER_SERIES = 3602
MAX_ID_LENGTH = 512
MAX_DROPPED_IDS = 256  # distinct dropped ids remembered; dropped_series stops growing there
# Which series keep a slot on a host with more identities than max_series. A reading no other series
# shows comes first; temperature is last because boards expose dozens of nearly identical channels (one
# per core, plus the package and its thermal zone twin), while every CPU core, interface and disk is a
# reading of its own. An id with none of these prefixes ranks with temperature.
PRIORITY = ("memory/", "cpu/", "gpu/", "npu/", "network/", "disk_io/", "observation/", "temperature/")


def priority(sid):
    """Where sid sorts when the series limit is reached; lower keeps its place."""
    for rank, prefix in enumerate(PRIORITY):
        if sid.startswith(prefix):
            return rank
    return PRIORITY.index("temperature/")


def points_of(stats, sensor_meta):
    """{series id: value or None} for one snapshot. Ids follow the identities the API already gives, so a
    replaced device, another namespace or a renamed sensor starts a new series instead of continuing one.
    A value without a rate (warmup, gap, counter went down) is None: a gap, never 0."""
    out = {}
    for c in stats.get("cpu") or []:
        out[f"cpu/{c['id']}/usage"] = c.get("usage")
    m = stats.get("memory") or {}
    if "used" in m:
        out["memory/used_bytes"] = m["used"]
    for kind in ("gpu", "npu"):  # one of each is reported, so the id needs nothing to tell them apart
        values = stats.get(kind)
        if isinstance(values, dict) and "usage" in values:
            out[f"{kind}/usage"] = values["usage"]
    meta = sensor_meta or {}
    for key, value in (stats.get("temperature") or {}).items():
        sid = (meta.get(pointer("temperature", key)) or {}).get("id")
        out[f"temperature/{sid or 'name:' + key}"] = value
    net = stats.get("network") or {}
    ns = (net.get("scope") or {}).get("id")
    for i in net.get("interfaces") or []:
        if ns is None or i.get("ifindex") is None:
            continue  # no identity to tie points of one interface together
        for k in ("rx_bytes_per_s", "tx_bytes_per_s"):
            out[f"network/{ns}/{i['ifindex']}/{i['name']}/{k}"] = (i.get("rates") or {}).get(k)
    for d in (stats.get("disk_io") or {}).get("disks") or []:
        for k in ("read_bytes_per_s", "write_bytes_per_s"):
            out[f"disk_io/{d['major']}:{d['minor']}/{d['name']}/{k}"] = (d.get("rates") or {}).get(k)
    return out


def observation_points(name, data):
    """Numbers from one independent observation; never read devices or recompute rates."""
    out = {}
    data = data or {}
    if name == "storage":
        for fs in data.get("filesystems") or []:
            identity = [fs.get(k) for k in ("major", "minor", "device", "fstype", "source")]
            token = hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()[:24]
            base = f"observation/storage/{fs['major']}:{fs['minor']}/{token}"
            for field in ("used_bytes", "available_bytes"):
                out[f"{base}/{field}"] = fs.get(field)
    elif name == "wifi":
        ns = (data.get("scope") or {}).get("id")
        for item in data.get("interfaces") or []:
            if ns is None or item.get("ifindex") is None:
                continue  # a name alone is not an interface identity
            sid = f"observation/wifi/{ns}/{item['ifindex']}/{item['name']}/signal_dbm"
            out[sid] = item.get("signal_dbm") if item.get("connected") else None
    elif name == "probe":
        for target in data.get("targets") or []:
            address = target["address"]
            host = f"[{address}]" if ":" in address else address
            out[f"observation/probe/{host}:{target['port']}/connect_ms"] = target.get("connect_ms")
    return out


class History:
    """Bounded independent timelines. Point id is a core sequence or a group's observation id.
    Only publication callbacks append; HTTP reads only copy. Missing values remain explicit gaps."""

    def __init__(self, retention, interval, max_series=MAX_SERIES):
        self.retention, self._retention_ns = retention, round(retention * 1e9)
        self._interval = interval
        self._maxlen = min(MAX_POINTS_PER_SERIES, int(retention / min(interval, 1.0)) + 2)
        self._max_series = min(max_series, MAX_SERIES)
        self._lock = threading.Lock()
        self._series = {}
        self._seen = {}       # last time an identity was present, not the time of its missing marker
        self._owners = {}     # series -> core, storage, wifi, probe
        self._periods = {"core": interval}
        self._latest = {}     # fixed publication domains -> (last id, last start ns)
        self._dropped = set()  # ids that did not fit, counted once each (bounded by MAX_DROPPED_IDS)
        self.dropped_series = 0

    def append(self, record):
        self._append("core", record["sequence"], record["started"], self._interval,
                     points_of(record["stats"], record.get("sensor_meta")), record.get("gap_before", False))

    def append_observation(self, name, interval, record):
        if name not in ("storage", "wifi", "probe"):
            return
        values = observation_points(name, record["data"])
        stale = record["completed"] - record["started"] > round(3 * interval * 1e9)
        if stale or record["collector"]["state"] in ("error", "unavailable"):
            values = {sid: None for sid in values}
        self._append(name, record["id"], record["started"], interval, values)

    def _append(self, owner, sequence, started, interval, values, gap_before=False):
        gap_ns = round(max(3 * interval, 5.0) * 1e9)
        with self._lock:
            last = self._latest.get(owner)
            if last is not None and (sequence <= last[0] or started < last[1]):
                return  # repeated or out-of-order publication cannot add a second point
            self._latest[owner] = (sequence, started)
            self._periods[owner] = interval
            cutoff = started - self._retention_ns
            for sid in [sid for sid, t in self._seen.items() if t < cutoff]:
                del self._series[sid], self._seen[sid], self._owners[sid]
            # Missing identities keep gaps, but those gaps do not keep a vanished identity alive.
            present = set(values)
            for sid in self._series:
                if self._owners[sid] == owner and sid not in values:
                    values[sid] = None
            for sid, value in self._fitting(owner, values).items():
                series = self._series.get(sid)
                if series is None:
                    series = self._series[sid] = collections.deque(maxlen=self._maxlen)
                    self._owners[sid] = owner
                if sid in present:
                    self._seen[sid] = started
                while series and series[0][1] < cutoff:
                    series.popleft()  # slow publishers must release expired points before reaching the count cap
                if series and (gap_before or started - series[-1][1] > gap_ns):
                    series.append((sequence, started - 1, None))  # explicit break after a stalled publisher
                number = value if isinstance(value, (int, float)) and not isinstance(value, bool) else None
                if number is not None and (abs(number) > 2**64 or not math.isfinite(number)):
                    number = None
                series.append((sequence, started, number))

    def _fitting(self, owner, values):
        """The part of one publication that fits in max_series, by PRIORITY, with every id left out
        counted once. Other publishers keep their slots; this publisher's own least important series
        release theirs, so a core that starts measuring its CPUs need not wait for a slot to expire.
        Called under the lock."""
        taken = sum(1 for sid in self._series if self._owners[sid] != owner)
        room = max(self._max_series - taken, 0)
        ids = [sid for sid in values if len(sid) <= MAX_ID_LENGTH]
        # Established series win ties, so two sensors of one rank never take turns dropping each other.
        keep = set(sorted(ids, key=lambda sid: (priority(sid), sid not in self._series))[:room])
        for sid in values:
            if sid in keep:
                continue
            if len(self._dropped) < MAX_DROPPED_IDS:
                self._dropped.add(sid)
            if sid in self._series:  # lost its slot to a more important series of this publisher
                del self._series[sid], self._seen[sid], self._owners[sid]
        self.dropped_series = len(self._dropped)
        return {sid: value for sid, value in values.items() if sid in keep}

    def intervals(self):
        """Per-series observation cadence, copied independently of point serialization."""
        with self._lock:
            return {sid: round(self._periods[owner] * 1000, 3) for sid, owner in self._owners.items()}

    def view(self, now, prefix="", seconds=None, points=300):
        seconds = self.retention if seconds is None else min(seconds, self.retention)
        since = now - round(seconds * 1e9)
        with self._lock:
            captured = {sid: list(d) for sid, d in self._series.items() if sid.startswith(prefix)}
            dropped = self.dropped_series
        out = {}
        for sid, samples in captured.items():
            ps = [p for p in samples if since <= p[1] <= now]
            if len(ps) > points:
                if points == 1:
                    ps = ps[-1:]
                else:
                    # Keep newest exactly. A bucket containing missing data remains a gap; do not
                    # decimate away nulls and draw a line across an outage.
                    older, reduced = ps[:-1], []
                    for i in range(points - 1):
                        bucket = older[i * len(older) // (points - 1):(i + 1) * len(older) // (points - 1)]
                        end = bucket[-1]
                        reduced.append((end[0], end[1], None if any(p[2] is None for p in bucket) else end[2]))
                    ps = reduced + ps[-1:]
            out[sid] = [[seq, round((now - t) / 1e6, 3), v] for seq, t, v in ps]
        return out, dropped
