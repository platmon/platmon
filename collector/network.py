"""Passive network counters: per-interface totals of this process's network namespace from /proc/self/net/dev,
and rates over the elapsed time between two consecutive readings. Read once per collection by the service's
sampler thread; nothing is sent or probed. Contract: docs/reference/network.md.
"""
import hashlib
import json
import os
import socket
import time

from .sysfs import reason_of
from .counters import counter, read_file

PROVIDER = "proc_net_dev"
SCOPE = "process_network_namespace"
NET_DEV = "/proc/self/net/dev"
NETNS = "/proc/self/ns/net"
# /proc/net/dev header: the field names after "|", receive then transmit (Linux 2.6+)
HEADER = ("bytes packets errs drop fifo frame compressed multicast".split(),
          "bytes packets errs drop fifo colls carrier compressed".split())
COUNTERS = ("bytes", "packets", "errors", "dropped")  # per direction: positions 0-3 of its 8 fields
IFNAMSIZ = 16  # Linux: a name is at most 15 bytes


class InvalidFile(ValueError):
    """/proc/self/net/dev as a whole is not the expected format: no row of it can be trusted."""


def valid_name(name):
    """A Linux interface name (dev_valid_name): 1-15 bytes, not . or .., no /, : or whitespace; printable
    here, so a name never puts control characters into the API or the log."""
    return (0 < len(name.encode()) < IFNAMSIZ and name not in (".", "..") and name.isprintable()
            and not any(c in "/:" or c.isspace() for c in name))


def parse(text):
    """({name: (rx bytes, packets, errors, dropped, tx bytes, packets, errors, dropped)}, [(target, why)])
    from /proc/net/dev. A row that does not parse is left out and reported (target: its name if that is a
    valid one, else "line<N>"); a name on several rows is ambiguous, so none of them is used. Raises
    InvalidFile if the header is not the known one."""
    lines = text.split("\n")
    if len(lines) < 2 or [h.split() for h in lines[1].split("|")[1:]] != list(HEADER):
        raise InvalidFile("unexpected header")
    rows, bad, seen = {}, [], set()
    for n, line in enumerate(lines[2:], 3):
        if not line.strip():
            continue
        name, sep, rest = line.partition(":")
        name = name.strip()
        if not sep or not valid_name(name):
            bad.append((f"line{n}", "invalid_data"))
            continue
        if name in seen:  # also when one of the rows is malformed: which one is the interface is unclear
            rows.pop(name, None)
            bad.append((name, "invalid_data"))
            continue
        seen.add(name)
        fields = rest.split()
        try:
            if len(fields) != 16:
                raise ValueError(line)
            values = [counter(f) for f in fields]
        except ValueError:
            bad.append((name, "invalid_data"))
            continue
        rows[name] = (*values[0:4], *values[8:12])
    return rows, bad


def namespace_id(stat):
    """Opaque id of a network namespace from the device and inode its /proc/self/ns/net link resolves to."""
    text = json.dumps({"v": 1, "dev": stat.st_dev, "ino": stat.st_ino}, sort_keys=True, separators=(",", ":"))
    return "netns1:" + hashlib.sha256(text.encode()).hexdigest()


class NetworkCounters:
    """Kept by the service between collections, like common.CpuCounters: each sample() reads the counters
    once and compares every interface with its previous successful reading of the same namespace, index
    and name. The rate is the counter delta over the elapsed time between the two readings' starts."""

    def __init__(self, clock=time.monotonic_ns, max_gap=None, path=NET_DEV,
                 netns=lambda: os.stat(NETNS), indexes=socket.if_nameindex, read=read_file):
        """clock: elapsed nanoseconds (the service passes the Sampler's); max_gap: seconds, a longer window
        between readings gives no rate (None: no limit). netns, indexes and read are injectable for tests."""
        self.clock, self._path, self._netns, self._indexes, self._read = clock, path, netns, indexes, read
        self._max_gap = None if max_gap is None else round(max_gap * 1e9)
        self._last = None  # (elapsed ns the reading began, namespace id or None, {(ifindex, name): counters})
        self._ns = None    # ((st_dev, st_ino), id) of the last successful lookup: the id is not hashed again

    def sample(self, group):
        """(network output, read span or None). Read problems are noted in group (a metadata.Group); a broken
        file gives no interfaces and clears every baseline. An unexpected exception clears them too and is
        raised, for the caller's guard."""
        try:
            return self._sample(group)
        except Exception:
            self._last = None
            raise

    def _sample(self, group):
        ns = None
        try:
            st = self._netns()  # looked up on every reading; only the id of the same device/inode is reused
            key = (st.st_dev, st.st_ino)
            if self._ns is None or self._ns[0] != key:
                self._ns = (key, namespace_id(st))
            ns = self._ns[1]
        except OSError as e:
            group.note("netns", reason_of(e), f"{NETNS}: {e.strerror}")
        out = {"scope": {"kind": SCOPE, "id": ns}, "provider": PROVIDER, "interfaces": [], "read": None}
        start = self.clock()
        try:
            text = self._read(self._path).decode()
            span = (start, self.clock())
            rows, bad = parse(text)
        except FileNotFoundError:
            self._last = None
            group.note("net_dev", "not_exposed")
            return out, None
        except OSError as e:
            self._last = None
            group.note("net_dev", reason_of(e), f"{self._path}: {e.strerror}")
            return out, None
        except (UnicodeDecodeError, InvalidFile):
            self._last = None
            group.note("net_dev", "invalid_data", self._path)
            return out, None
        for target, why in bad:
            group.note(target, why, self._path)
        index = {}
        try:
            index = {name: i for i, name in self._indexes()}
        except OSError as e:
            group.note("ifindex", reason_of(e), f"if_nameindex: {e.strerror}")

        last, now = self._last, start
        window = None if last is None else now - last[0]
        kept = {}
        for name in sorted(rows):
            row, i = rows[name], index.get(name)
            rates = None
            if ns is None or i is None:
                reason = "identity_unavailable"
            elif last is not None and last[1] is not None and last[1] != ns:
                reason = "namespace_changed"
            elif last is None or (i, name) not in last[2]:
                reason = "warmup"
            elif window <= 0:
                reason = "invalid_interval"
            elif self._max_gap is not None and window > self._max_gap:
                reason = "gap"
            elif any(a < b for a, b in zip(row, last[2][i, name])):
                reason = "counter_regressed"  # not taken for a wrap or a reset to 0: no rate, a new baseline
            else:
                reason = None
                d = [a - b for a, b in zip(row, last[2][i, name])]
                rates = {"rx_bytes_per_s": _rate(d[0], window), "tx_bytes_per_s": _rate(d[4], window),
                         "rx_packets_per_s": _rate(d[1], window), "tx_packets_per_s": _rate(d[5], window)}
            if ns is not None and i is not None:
                kept[i, name] = row  # a valid reading is the next baseline, also when it gives no rate
            has_window = reason in (None, "gap", "counter_regressed")
            out["interfaces"].append(group.got({
                "name": name, "ifindex": i,
                "rx": dict(zip(COUNTERS, row[:4])), "tx": dict(zip(COUNTERS, row[4:])),
                "rates": rates, "window_ms": round(window / 1e6, 3) if has_window else None, "reason": reason}))
        self._last = (now, ns, kept)  # only what this reading saw: names that come and go do not pile up
        return out, span


def _rate(delta, window_ns):
    return round(delta * 1e9 / window_ns, 3)


def unavailable():
    """The output when sample() raised: no interfaces, nothing known about the scope."""
    return {"scope": {"kind": SCOPE, "id": None}, "provider": PROVIDER, "interfaces": [], "read": None}
