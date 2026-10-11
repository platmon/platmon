"""Power supplies, a low-frequency observation (collector/slow.py): what the kernel last knew about each
battery and each external supply, from /sys/class/power_supply. Passive: nothing is switched, charged,
calibrated or reset, and no identifying string (serial number) is read. Contract: docs/battery.md.

Read on its own slow cadence, not in the core cycle. On an ACPI host every attribute of a battery comes
from one firmware call behind the driver's 1 s cache (/sys/module/battery/parameters/cache_time): one
reading of the whole battery measured 101 ms elapsed, 0.6 ms of it CPU, and the core interval of 1 s sits
exactly on that cache, so the core cycle would pay the full 101 ms every collection. One call covers
every attribute, so a battery's attributes are read together. See docs/performance/budget.md.

Only attributes this collection found by listing are read: which ones a driver exposes differs by gauge
(charge_* or energy_*, cycle_count or not), and an attribute that is simply not there is not a problem
to report.
"""
from .sysfs import entries, reason_of

PROVIDER = "sysfs_power_supply"
ROOT = "/sys/class/power_supply"
# power_supply types that hold charge and report it the same way: a laptop or robot cell ("Battery") and
# a UPS. Everything else ("Mains", "USB" and its subtypes, "Wireless") is a source of external power and
# only says whether it is online.
STORES_CHARGE = ("Battery", "UPS")

# µAh and µWh families: a gauge counts one or the other, so a driver exposes one family. Reported as the
# kernel gives them, never converted into each other: that needs a voltage, which is its own momentary
# reading and would turn two readings into one number without saying so.
CHARGE = (("charge_now", "charge_now_uah"), ("charge_full", "charge_full_uah"),
          ("charge_full_design", "charge_full_design_uah"))
ENERGY = (("energy_now", "energy_now_uwh"), ("energy_full", "energy_full_uwh"),
          ("energy_full_design", "energy_full_design_uwh"))
# Signed: a discharging battery reports a negative current or power on some drivers and a positive one
# with a "Discharging" status on others, so the sign is passed through rather than interpreted.
SIGNED = (("current_now", "current_ua"), ("power_now", "power_uw"))
UNSIGNED = (("voltage_now", "voltage_uv"), ("cycle_count", "cycle_count"))
# Short driver enums (status: Charging / Discharging / Full / Not charging / Unknown, and so on). Kept as
# the kernel spells them: translating them would invent states the driver never reported.
TEXT = (("status", "status"), ("capacity_level", "capacity_level"),
        ("technology", "technology"), ("health", "health"))
MAX_TEXT = 32  # these are short enums; anything longer is not one of them
NUMBERS = [name for _, name in CHARGE + ENERGY + SIGNED + UNSIGNED] + ["percent"]


def text(value):
    """A driver enum, or ValueError. Empty or over-long is not one of them."""
    value = value.strip()
    if not value or len(value) > MAX_TEXT:
        raise ValueError("not an enum")
    return value


def flag(value):
    """present / online, as 0 or 1."""
    if value.strip() not in ("0", "1"):
        raise ValueError("not a flag")
    return value.strip() == "1"


def percent(value):
    value = int(value)
    if not 0 <= value <= 100:
        raise ValueError("out of range")
    return value


def unsigned(value):
    value = int(value)
    if value < 0:
        raise ValueError("negative")
    return value


class Battery:
    """observe(group) for collector/slow.py. The class directory is injectable for tests."""

    def __init__(self, root=ROOT):
        self._root = root

    def __call__(self, group):
        out = {"provider": PROVIDER, "batteries": [], "external": []}
        names = entries(self._root, group)
        for name in names:
            d = f"{self._root}/{name}"
            attrs = entries(d, group, listed=True)
            if not attrs:  # the supply went away between the two listings, or cannot be listed
                continue
            # read unconditionally: a supply with no type is noted rather than vanishing
            kind = group.read(f"{d}/type", f"{name}.type", parse=text, found=d)
            if kind is None:
                continue  # without the type there is no telling a battery from a charger
            if kind in STORES_CHARGE:
                out["batteries"].append(self._battery(group, d, attrs, name))
            else:
                out["external"].append(self._external(group, d, attrs, name, kind))
        if not names:
            group.note("power_supply", "not_detected")  # the class is there, no supply in it
        return out

    def _battery(self, group, d, attrs, name):
        item = {"name": name, "present": None, "percent": None, **{f: None for _, f in TEXT},
                **{f: None for f in NUMBERS if f != "percent"}}
        present = self._read(group, d, attrs, name, "present", flag)
        item["present"] = present
        if present is False:
            return group.got(item)  # an empty bay: the attributes below are not its reading
        item["percent"] = self._read(group, d, attrs, name, "capacity", percent)
        for attr, field in CHARGE + ENERGY:
            item[field] = self._read(group, d, attrs, name, attr, unsigned)
        for attr, field in UNSIGNED:
            item[field] = self._read(group, d, attrs, name, attr, unsigned)
        for attr, field in SIGNED:
            item[field] = self._read(group, d, attrs, name, attr, int)
        for attr, field in TEXT:
            item[field] = self._text(group, d, attrs, name, attr)
        return group.got(item)

    def _external(self, group, d, attrs, name, kind):
        return group.got({"name": name, "type": kind,
                          "online": self._read(group, d, attrs, name, "online", flag)})

    def _read(self, group, d, attrs, name, attr, parse):
        """The attribute, or None when this driver does not expose it (not read, nothing noted)."""
        if attr not in attrs:
            return None
        return group.read(f"{d}/{attr}", f"{name}.{attr}", parse=parse, found=d)

    def _text(self, group, d, attrs, name, attr):
        return self._read(group, d, attrs, name, attr, text)
