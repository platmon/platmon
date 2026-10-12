"""Wi-Fi signal, a low-frequency observation (runtime/slow.py): what the kernel last knew about each
wireless interface's link, from /proc/net/wireless. Passive: no scan, no reconnect, no setting changed;
SSID, BSSID and nearby access points are never read. Contract: docs/reference/wifi.md.

Format (net/wireless/wext-proc.c): "name: status link[.] level[.] noise[.] ...", where '.' marks a value
updated since the last read and level / noise are dBm (negative) when the driver reports dBm, otherwise a
value of unspecified unit; a noise of -256 is "not available".
"""
import errno
import re
import os
import socket

from .network import NETNS, namespace_id, valid_name
from .sysfs import reason_of

PROVIDER = "proc_net_wireless"
PATH = "/proc/net/wireless"
ROW = re.compile(r"\s*(\S+):\s+([0-9a-f]{4})\s+(-?\d+)(\.?)\s+(-?\d+)(\.?)\s+(-?\d+)(\.?)(?:\s+\d+){6}\s*")


def parse(text):
    """([interface], [(target, why)]). Rows that do not match the format are left out and reported."""
    lines = text.split("\n")
    if len(lines) < 2 or "Quality" not in lines[0] or "level" not in lines[1]:
        raise ValueError("unexpected header")
    out, bad = [], []
    for n, line in enumerate(lines[2:], 3):
        if not line.strip():
            continue
        m = ROW.fullmatch(line)
        if not m or not valid_name(m.group(1)):
            bad.append((f"line{n}", "invalid_data"))
            continue
        name, link, link_new, level, level_new, noise = m.group(1), int(m.group(3)), m.group(4), int(m.group(5)), \
            m.group(6), int(m.group(7))
        if not (0 <= link <= 255 and -256 <= level <= 255 and -256 <= noise <= 255):
            bad.append((name, "invalid_data"))
            continue
        # The dot describes signal freshness, not association or kernel carrier state.
        updated = bool(level_new)
        dbm = level < 0  # the kernel subtracts 256 for dBm values, so they come out negative
        out.append({"name": name, "connected": None, "connection_state": "unknown",
                    "signal_dbm": level if updated and dbm else None,
                    "signal_raw": level if updated and not dbm else None,  # a driver without dBm: unit unknown
                    "link_quality": link if updated and link_new else None,
                    "noise_dbm": noise if updated and noise < 0 and noise != -256 else None})
    return out, bad


class Wifi:
    """observe(group) for runtime/slow.py."""

    def __init__(self, read=None, netns=lambda: os.stat(NETNS), indexes=socket.if_nameindex, link=None):
        self._read = read or _read
        self._netns, self._indexes = netns, indexes
        self._link = link or link_connected

    def __call__(self, group):
        out = {"scope": {"kind": "process_network_namespace"}, "provider": PROVIDER, "interfaces": []}
        try:
            interfaces, bad = parse(self._read(PATH))
        except FileNotFoundError:
            group.note("wireless", "not_exposed")  # no wireless extensions / cfg80211 compat in this kernel
            return out
        except OSError as e:
            group.note("wireless", reason_of(e), f"{PATH}: {e.strerror}")
            return out
        except (UnicodeDecodeError, ValueError):
            group.note("wireless", "invalid_data", PATH)
            return out
        index = {}
        ns = None
        if interfaces:
            try:
                ns = namespace_id(self._netns())
                index = {name: number for number, name in self._indexes()}
            except OSError as e:
                group.note("identity", reason_of(e))
        out["scope"]["id"] = ns
        for item in interfaces:
            item["ifindex"] = index.get(item["name"])
            if item["ifindex"] is None:
                for key in ("signal_dbm", "signal_raw", "link_quality", "noise_dbm"):
                    item[key] = None
                continue
            try:
                connected = self._link(item["name"], item["ifindex"])
                item["connected"] = connected
                item["connection_state"] = "unknown" if connected is None else "connected" if connected else "disconnected"
            except FileNotFoundError:
                item["connection_state"] = "unsupported"
                group.note(item["name"], "not_exposed")
            except OSError as e:
                why = reason_of(e)
                item["connection_state"] = "permission_denied" if why == "permission_denied" else "unavailable"
                group.note(item["name"], why)
            except ValueError:
                item["connection_state"] = "unknown"
                group.note(item["name"], "invalid_data")
            if item["connected"] is not True:
                for key in ("signal_dbm", "signal_raw", "link_quality", "noise_dbm"):
                    item[key] = None
        for target, why in bad:
            group.note(target, why, PATH)
        out["interfaces"] = [group.got(i) for i in sorted(interfaces, key=lambda i: i["name"])]
        if not interfaces and not bad:
            group.note("wireless", "not_detected")  # the file is there, no wireless interface in it
        return out


def _read(path):
    with open(path, encoding="ascii") as f:
        return f.read()


def link_connected(name, ifindex, root="/sys/class/net", read=_read):
    """Kernel link carrier, not the wireless signal-update bit. Paths/reads are injectable.
    A mismatched index is not trusted (device replacement or incompatible sysfs scope)."""
    before = int(read(f"{root}/{name}/ifindex").strip())
    try:
        carrier = read(f"{root}/{name}/carrier").strip()
    except OSError as e:
        # Linux rejects carrier reads on administratively down interfaces.
        # Confirm IFF_UP is clear; unrelated failures must remain visible.
        if e.errno != errno.EINVAL or int(read(f"{root}/{name}/flags").strip(), 16) & 1:
            raise
        carrier = "0"
    after = int(read(f"{root}/{name}/ifindex").strip())
    if before != ifindex or after != ifindex:
        return None
    if carrier not in ("0", "1"):
        raise ValueError("invalid carrier")
    return carrier == "1"
