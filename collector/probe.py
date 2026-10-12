"""TCP connect time to configured targets, a low-frequency observation (runtime/slow.py). Active: it opens
a TCP connection to each target and closes it at once. Enabled by default, but idle without
targets listed in the config. This is the time to complete a TCP handshake (the SYN / SYN-ACK round trip
plus the target's accept), not an ICMP echo round trip. Contract: docs/reference/probe.md.
"""
import collections
import errno
import ipaddress
import select
import socket
import time

PROVIDER = "tcp_connect"
WINDOW = 10        # attempts per target that the failure share is over
MAX_TARGETS = 4


def parse_targets(text):
    """[(address, port)] from "192.0.2.1:443, [2001:db8::1]:22". Only IP literals: no name is ever
    resolved, so a lookup's time never mixes into the probe's. ValueError for anything else."""
    out = []
    for item in filter(None, (t.strip() for t in text.split(","))):
        host, sep, port = item.rpartition(":")
        if not sep or not port.isdigit() or not 0 < int(port) < 65536:
            raise ValueError(f"target {item!r}: expected address:port")
        bracketed = host.startswith("[") and host.endswith("]")
        if ":" in host and not bracketed:
            raise ValueError(f"target {item!r}: write an IPv6 address as [address]:port")
        addr = ipaddress.ip_address(host[1:-1] if bracketed else host)
        out.append((str(addr), int(port)))
    if len(out) > MAX_TARGETS:
        raise ValueError(f"at most {MAX_TARGETS} targets")
    if len(set(out)) != len(out):
        raise ValueError("a target is listed twice")
    return out


def connect_ms(address, port, timeout, clock=time.monotonic_ns):
    """(milliseconds to complete the connection, None) or (None, reason): refused, timeout, unreachable,
    error. One socket, closed before returning: nothing stays outstanding for a later answer."""
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    s = socket.socket(family, socket.SOCK_STREAM)
    try:
        s.setblocking(False)
        start = clock()
        rc = s.connect_ex((address, port))
        if rc not in (0, errno.EINPROGRESS):
            return None, _reason(rc)
        if rc:
            _, w, _ = select.select([], [s], [], timeout)
            if not w:
                return None, "timeout"
            rc = s.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            if rc:
                return None, _reason(rc)
        return round((clock() - start) / 1e6, 3), None
    finally:
        s.close()


def _reason(code):
    if code == errno.ECONNREFUSED:
        return "refused"
    if code in (errno.ENETUNREACH, errno.EHOSTUNREACH):
        return "unreachable"
    return "timeout" if code == errno.ETIMEDOUT else "error"


class Probe:
    """observe(group) for runtime/slow.py: one attempt per target per observation, one after another.
    Keeps the last WINDOW results per target for the failure share."""

    def __init__(self, targets, timeout=2.0, connect=connect_ms):
        self.targets, self.timeout, self._connect = targets, timeout, connect
        self._recent = {t: collections.deque(maxlen=WINDOW) for t in targets}

    def __call__(self, group):
        out = {"provider": PROVIDER, "timeout_ms": self.timeout * 1000, "targets": []}
        for address, port in self.targets:
            ms, reason = self._connect(address, port, self.timeout)
            recent = self._recent[address, port]
            recent.append(ms is not None)
            out["targets"].append(group.got({
                "address": address, "port": port, "connect_ms": ms, "reason": reason,
                "attempts": len(recent), "failures": recent.count(False),
                "failure_ratio": round(recent.count(False) / len(recent), 3)}))
        return out
